#!/usr/bin/env python3
"""
LIFE Compute — Miner Daemon (devnet, real on-chain submission)

Pipeline each cycle:
  1. Fetch cancer targets from GitHub
  2. Screen reference compounds once per target (every REF_RESCREEN_INTERVAL)
  3. Pick a molecule — default: random ZINC15 fragment (~1.7M drug-like molecules)
     Override _pick_molecule() with your own strategy to earn more $LIFE
  4. Run Boltz2 GPU scoring
  5. If affinity ≤ threshold → submit_result on-chain via Node.js / Anchor
  6. Write stats.json for dashboard

Provided tools in adaptive/ (wire in as you see fit):
  life_generate  — generative AI: BRICS recombination, scaffold hopping, guided mutation
  life_chembl    — ChEMBL actives as high-quality seeds + novelty cross-reference
  life_diversity — Shannon entropy enforcement + Tanimoto deduplication

Boltz2 scoring uses /mnt/minos-drive/nova_subnet/.venv and the proven
nova_pulse_scorer.score_batch() pattern; falls back to single-sequence mode
when no MSA is available.
"""
import json, time, random, logging, os, subprocess, sys, urllib.request, tempfile
import threading, multiprocessing
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))   # ensure adaptive/ importable

# ── Optional tools (fail-open; wire in your own strategy) ────────────────────
try:
    from adaptive.life_diversity import SubmissionMemory, greedy_diverse_select
    from adaptive.life_generate  import generate_candidates
    from adaptive.life_chembl    import validate_against_chembl, download_all as chembl_download_all
    _TOOLS_AVAILABLE = True
except Exception as _e:
    _TOOLS_AVAILABLE = False
    _tools_err = str(_e)

# ── LIFE PULSE — continuous Sobol molecular sweep ─────────────────────────────
try:
    from adaptive.life_pulse import (
        run_sweep       as pulse_run_sweep,
        get_next_candidates as pulse_get_candidates,
        record_boltz_score  as pulse_record_boltz,
        PULSE_JSONL,
    )
    _PULSE_AVAILABLE = True
except Exception as _pe:
    _PULSE_AVAILABLE = False
    _pulse_err = str(_pe)

# ── Auto MSA downloader ───────────────────────────────────────────────────────
try:
    from adaptive.auto_msa import (
        ensure_msa               as _auto_ensure_msa,
        start_background_prefetch as _auto_msa_prefetch,
        prefetch_status           as _auto_msa_status,
    )
    _AUTO_MSA_AVAILABLE = True
except Exception as _ame:
    _AUTO_MSA_AVAILABLE = False
    _auto_msa_err = str(_ame)

# ── ProteinNet — per-protein ML pre-screener ──────────────────────────────────
try:
    from adaptive.life_proteinnet import (
        train_all       as _pnet_train_all,
        pre_screen      as _pnet_pre_screen,
        get_model_report as _pnet_get_report,
    )
    _PNET_AVAILABLE = True
except Exception as _pne:
    _PNET_AVAILABLE = False
    _pnet_pre_screen = _pnet_train_all = _pnet_get_report = None  # type: ignore[assignment]

# ── Config ────────────────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID    = _env("PROGRAM_ID",    "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf")
SOLANA_RPC    = _env("SOLANA_RPC",    "https://api.devnet.solana.com")
AUTH_KEYPAIR  = _env("SOLANA_KEYPAIR","/mnt/minos-drive/life-compute-miner/dev-keypair.json")
MINER_KEYPAIR = _env("MINER_KEYPAIR", "/mnt/minos-drive/life-compute-miner/miner-keypair.json")
TARGETS_URL   = _env("TARGETS_URL",   "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
REF_COMPOUNDS_URL = _env("REF_COMPOUNDS_URL", "https://raw.githubusercontent.com/life-compute/targets/master/reference_compounds.json")
POLL_SECONDS  = int(_env("POLL_SECONDS", "60"))
TARGET_REFRESH       = 300      # seconds between target list refreshes
REF_RESCREEN_INTERVAL = 4 * 3600.0  # screen each ref compound at most once per 4 h

# ── Multi-GPU configuration ───────────────────────────────────────────────────
def _detect_gpu_count() -> int:
    """Return number of NVIDIA GPUs visible via nvidia-smi, or 1 on failure."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            timeout=5, text=True, stderr=subprocess.DEVNULL)
        n = len([l for l in out.strip().splitlines() if l.strip()])
        return max(1, n)
    except Exception:
        return 1

_GPU_COUNT_ENV = _env("GPU_COUNT", "1").strip().lower()
if _GPU_COUNT_ENV == "auto":
    GPU_COUNT = _detect_gpu_count()
else:
    try:
        GPU_COUNT = max(1, int(_GPU_COUNT_ENV))
    except ValueError:
        GPU_COUNT = 1

MULTI_GPU = GPU_COUNT > 1
MIN_SOL_LAMPORTS_MULTI   = 100_000_000  # 0.1 SOL for multi-GPU registration

WORK_DIR   = Path(__file__).parent
STATS_PATH = WORK_DIR / "stats.json"
ANCHOR_DIR = Path("/tmp/life-compute/core")
IDL_PATH   = ANCHOR_DIR / "target/idl/life_core.json"

# ── Boltz2 / nova paths ───────────────────────────────────────────────────────
NOVA_DIR   = Path("/mnt/minos-drive/nova_subnet")
NOVA_VENV  = NOVA_DIR / ".venv" / "bin" / "python"
MSA_DIR    = Path("/mnt/minos-drive/life-compute-miner/data/msa_files")
BOLTZ_SEED = 68   # included in on-chain submission so validators reproduce the score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-miner")

# ── ZINC15 random sampling (default strategy) ─────────────────────────────────
ZINC15_FRAGMENTS = WORK_DIR / "data" / "zinc15_fragments.smi"
_zinc_cache: list[str] = []

def _sample_zinc15() -> str:
    """Random SMILES from ZINC15 fragment library (~1.7M drug-like molecules)."""
    global _zinc_cache
    if not _zinc_cache:
        if ZINC15_FRAGMENTS.exists():
            with ZINC15_FRAGMENTS.open() as f:
                _zinc_cache = [ln.split()[0] for ln in f if ln.strip()]
    if not _zinc_cache:
        raise RuntimeError("ZINC15 fragment library not found; run data/download_zinc15_fragments.py")
    return random.choice(_zinc_cache)


def _pick_molecule(target: dict, sub_memory, best_smiles: list[str]) -> tuple[str, str]:
    """
    Return (smiles, source_label) for the next molecule to score.

    Default: random ZINC15 fragment.  Replace this function — or add logic
    around it — with your own search strategy.  The adaptive/ tools are there
    to help: life_generate, life_chembl, life_diversity.

    Parameters
    ----------
    target      : target dict from fetch_targets()
    sub_memory  : SubmissionMemory instance (or None if tools unavailable)
    best_smiles : recent high-scoring SMILES from this session

    Returns
    -------
    (smiles, label)  — label is logged and stored for diagnostics
    """
    tid = target.get("id", "").upper()
    uid = target.get("uniprot_id", tid)
    seq = target.get("protein_sequence", "")
    is_large_protein = len(seq) > 800   # APC=2843aa, BRCA1=1863aa, SMAD4=552aa

    # ── ProteinNet pre-screen pool (built once, shared across priorities) ──────
    # For large proteins: 5000 candidates → top 20 to avoid wasting Boltz2 time.
    # For normal proteins: 2000 candidates → top 100.
    _pnet_pool: list[str] = []
    if _PNET_AVAILABLE and _zinc_cache and _pnet_pre_screen is not None:
        try:
            if is_large_protein:
                _n_screen, _top_k = 5000, 20
            else:
                _n_screen, _top_k = 2000, 100
            _sample = random.sample(_zinc_cache, min(_n_screen, len(_zinc_cache)))
            _pnet_pool = _pnet_pre_screen(_sample, tid, top_n=_top_k)
            _mode_str  = "Large protein mode — " if is_large_protein else ""
            log.info(f"[PROTEINNET] {_mode_str}{len(_sample)} screened → top {len(_pnet_pool)} for {tid}")
        except Exception as _pne_err:
            log.debug(f"[PROTEINNET] pre_screen failed (non-fatal): {_pne_err}")

    # ── Priority 1: LIFE PULSE top candidates (Sobol sweep + EliteMutator)
    if _PULSE_AVAILABLE:
        try:
            # Map target ID to protein family for focused sampling
            family = None
            if tid in ("EGFR", "HER2", "KRAS", "BRAF", "VEGFR2", "FGFR1", "RET",
                       "ABL1", "CDK4", "FLT3", "JAK2", "NTRK1"):
                family = "kinase"
            elif tid in ("BCL2", "MDM2", "PDL1", "STAT3", "MYC"):
                family = "cytokine"
            elif tid in ("PARP1", "HDAC1", "HDAC2"):
                family = "protease"
            elif tid in ("AR", "ESR1"):
                family = "nuclear_receptor"

            pulse_cands = pulse_get_candidates(n=20, family_filter=family)
            if pulse_cands and sub_memory is not None:
                novel = sub_memory.filter_novel([r["smiles"] for r in pulse_cands])
                if novel:
                    return novel[0], "pulse"
            elif pulse_cands:
                return pulse_cands[0]["smiles"], "pulse"
        except Exception as _pulse_pick_err:
            log.debug(f"[PULSE] candidate pick failed (non-fatal): {_pulse_pick_err}")

    # ── Priority 2: Generative AI (when real Boltz2 scores available)
    # Wrapped in a 60-second timeout to prevent BRICSBuild / RDKit C-extension
    # calls from hanging the main loop indefinitely (APC/SMAD4 observed >6h).
    # CRITICAL: do NOT use `with ThreadPoolExecutor` here — the context manager
    # calls shutdown(wait=True) on exit, which blocks until the C thread dies
    # (never) even after TimeoutError is caught. Use shutdown(wait=False) instead.
    _GENERATE_TIMEOUT = 60
    if _TOOLS_AVAILABLE and best_smiles and sub_memory is not None:
        try:
            _gen_pool   = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            _gen_future = _gen_pool.submit(generate_candidates, target, None, 50)
            try:
                gen_cands = _gen_future.result(timeout=_GENERATE_TIMEOUT)
            except concurrent.futures.TimeoutError:
                log.warning(
                    f"[GENERATE] Phase 4 timed out after {_GENERATE_TIMEOUT}s "
                    f"for target={tid} — skipping, continuing to zinc15"
                )
                gen_cands = []
            finally:
                _gen_pool.shutdown(wait=False, cancel_futures=True)  # never block on hung C thread
            # ProteinNet filter: only forward generated candidates above model threshold
            if gen_cands and _PNET_AVAILABLE and _pnet_pre_screen is not None:
                try:
                    gen_smiles  = [smi for _, smi, _ in gen_cands]
                    filtered    = _pnet_pre_screen(gen_smiles, tid, top_n=len(gen_smiles))
                    filtered_set = set(filtered)
                    gen_cands   = [(l, s, sc) for l, s, sc in gen_cands if s in filtered_set]
                    log.debug(f"[PROTEINNET] Phase 4 filter: {len(gen_smiles)} → {len(gen_cands)} for {tid}")
                except Exception as _gpf:
                    log.debug(f"[PROTEINNET] Phase 4 filter failed (non-fatal): {_gpf}")
            novel = sub_memory.filter_novel(gen_cands) if gen_cands else []
            if novel:
                _, smi, _ = novel[0]
                return smi, "generate"
        except Exception as _ge:
            log.debug(f"[GENERATE] failed (non-fatal): {_ge}")

    # ── Priority 3: ProteinNet-pre-screened ZINC15 (best predicted binders first)
    if _pnet_pool:
        if sub_memory is not None:
            _novel_pnet = sub_memory.filter_novel(_pnet_pool)
            if _novel_pnet:
                return _novel_pnet[0], "proteinnet"
        return _pnet_pool[0], "proteinnet"

    # ── Priority 4: random ZINC15 sample (always works)
    return _sample_zinc15(), "zinc15"


# ── Solana RPC helpers ────────────────────────────────────────────────────────
_DISC_TARGET = bytes([140, 246, 247, 200, 198, 220,  24, 250])
_DISC_MINER  = bytes([232, 196,  79, 139, 222, 213, 161,  99])
# ResultSubmission discriminator: sha256("account:ResultSubmission")[:8]
_DISC_RESULT = bytes([0xd6, 0x73, 0xa5, 0x67, 0x43, 0xd3, 0x2f, 0x58])
_NETWORK_STATS_CACHE: dict = {}
_NETWORK_STATS_TTL   = 120

# NetworkConfig PDA — seeds: [b"network_config"], program DzcQHhTP…
# Derived once at module load; change if PROGRAM_ID changes.
_NETWORK_CONFIG_PDA = "3cp9veeRTsqnXWSJYw2jqhRVeeKcaEkp4Pb2md9GJXPi"

# Slots per 24 h (400 ms/slot × 216 000 = 86 400 s).
_SLOTS_PER_DAY = 216_000

_B58_ALPHA = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def _b58enc(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(_B58_ALPHA[r])
    out.extend(_B58_ALPHA[0] for b in data if b == 0)
    return bytes(reversed(out)).decode()

def _rpc(method: str, params: list) -> object:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode()
    try:
        req = urllib.request.Request(
            SOLANA_RPC, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("result")
    except Exception as e:
        log.debug(f"[RPC] {method}: {e}")
        return None

def _get_accounts(disc: bytes) -> list[bytes]:
    import base64 as _b64
    result = _rpc("getProgramAccounts", [
        PROGRAM_ID,
        {"encoding": "base64",
         "filters": [{"memcmp": {"offset": 0, "bytes": _b58enc(disc)}}]},
    ])
    if not isinstance(result, list):
        return []
    out = []
    for item in result:
        try:
            raw = _b64.b64decode(item["account"]["data"][0])
            if raw[:8] == disc:
                out.append(raw)
        except Exception:
            pass
    return out

def _get_network_config_miners_registered() -> int | None:
    """Read total_miners_registered from the NetworkConfig PDA.

    This is the authoritative on-chain counter incremented by register_miner.
    It never decreases and is unaffected by test/ghost MinerAccount PDAs
    created outside of the program's register_miner instruction.

    NetworkConfig byte layout (Anchor-serialised):
      0–7   discriminator
      8–39  authority Pubkey
      40–71 life_mint Pubkey
      72–79 supply_cap u64
      80–87 total_minted u64
      88–95 current_epoch u64
      96–103 epoch_start_slot i64
      104–111 epoch_duration_slots u64
      112   validators_required u8
      113–116 validation_tolerance f32
      117–276 validators [Pubkey; 5]  (5 × 32 = 160 bytes)
      277   validator_count u8
      278   bump u8
      279   mint_authority_bump u8
      280–287 total_miners_registered u64   ← we read this
      288–295 total_validators_registered u64
    """
    import base64 as _b64, struct as _struct
    try:
        res = _rpc("getAccountInfo", [_NETWORK_CONFIG_PDA, {"encoding": "base64"}])
        if not isinstance(res, dict) or res.get("value") is None:
            log.debug("[NETWORK] NetworkConfig PDA not found")
            return None
        raw = _b64.b64decode(res["value"]["data"][0])
        if len(raw) < 288:
            log.debug(f"[NETWORK] NetworkConfig too short: {len(raw)} bytes")
            return None
        return _struct.unpack_from("<Q", raw, 280)[0]
    except Exception as e:
        log.debug(f"[NETWORK] _get_network_config_miners_registered failed: {e}")
        return None

def _count_active_miners() -> int | None:
    """Count distinct miners who submitted a result in the last 24 h.

    Reads ResultSubmission PDAs and filters by submitted_slot >= current_slot - 216_000.
    Returns the count of unique miner pubkeys, or None on RPC failure.

    ResultSubmission byte layout (Anchor-serialised, target_id: u16):
      0–7    discriminator
      8–39   miner Pubkey
      40–41  target_id u16 (2-byte LE)
      42–49  epoch u64
      50–561 smiles [u8; 512]
      562–563 smiles_len u16
      564–567 claimed_affinity f32
      568–575 submitted_slot i64   ← we filter on this
    """
    import base64 as _b64, struct as _struct
    try:
        current_slot = _rpc("getSlot", [])
        if not isinstance(current_slot, int):
            return None
        cutoff_slot = current_slot - _SLOTS_PER_DAY
        result_accounts = _get_accounts(_DISC_RESULT)
        active: set[bytes] = set()
        for raw in result_accounts:
            if len(raw) < 576:
                continue
            submitted_slot = _struct.unpack_from("<q", raw, 568)[0]
            if submitted_slot >= cutoff_slot:
                active.add(raw[8:40])   # miner pubkey bytes
        return len(active)
    except Exception as e:
        log.debug(f"[NETWORK] _count_active_miners failed: {e}")
        return None

def fetch_network_stats() -> dict:
    """Return global network stats for the dashboard.

    total_miners is sourced from total_miners_registered in the NetworkConfig PDA —
    the authoritative on-chain counter incremented by register_miner.  This avoids
    counting test/ghost MinerAccount PDAs created during development.

    molecules_screened and targets_solved are still aggregated from individual
    on-chain accounts.
    """
    global _NETWORK_STATS_CACHE
    if _NETWORK_STATS_CACHE.get("_ts", 0) + _NETWORK_STATS_TTL > time.time():
        return _NETWORK_STATS_CACHE
    result: dict = {"total_miners": None, "molecules_screened": None, "targets_solved": None}
    try:
        # ── Miner count: read from NetworkConfig, not from PDA enumeration ──
        registered = _get_network_config_miners_registered()
        if registered is not None:
            result["total_miners"] = registered
        else:
            # Fallback: count active submitters from the last 24 h
            result["total_miners"] = _count_active_miners()

        # ── molecules_screened: sum from MinerAccount PDAs ──
        miner_accounts = _get_accounts(_DISC_MINER)
        result["molecules_screened"] = sum(
            int.from_bytes(raw[48:56], "little")
            for raw in miner_accounts if len(raw) >= 56
        )

        # ── targets_solved: sum from TargetAccount PDAs ──
        target_accounts = _get_accounts(_DISC_TARGET)
        result["targets_solved"] = sum(
            int.from_bytes(raw[65:73], "little")
            for raw in target_accounts if len(raw) >= 73
        )
    except Exception as e:
        log.debug(f"[NETWORK] fetch_network_stats failed: {e}")
    result["_ts"] = time.time()
    _NETWORK_STATS_CACHE = result
    return result


# ── Epoch management ─────────────────────────────────────────────────────────

_ADVANCE_EPOCH_JS = ANCHOR_DIR / "life_advance_epoch.js"
_EPOCH_ADVANCE_COOLDOWN = 60.0   # seconds — skip re-check after a successful advance
_last_advance_attempt   = 0.0


def _read_epoch_state() -> dict | None:
    """Decode epoch fields from the NetworkConfig PDA.

    Returns a dict with keys:
        current_epoch       int
        epoch_start_slot    int
        epoch_duration_slots int
    or None on any RPC / parse failure.

    NetworkConfig byte layout (Anchor-serialised):
      0–7   discriminator
      8–39  authority Pubkey
      40–71 life_mint Pubkey
      72–79 supply_cap u64
      80–87 total_minted u64
      88–95 current_epoch u64
      96–103 epoch_start_slot i64
      104–111 epoch_duration_slots u64
    """
    import base64 as _b64, struct as _struct
    try:
        res = _rpc("getAccountInfo", [_NETWORK_CONFIG_PDA, {"encoding": "base64"}])
        if not isinstance(res, dict) or res.get("value") is None:
            log.debug("[EPOCH] NetworkConfig PDA not found")
            return None
        raw = _b64.b64decode(res["value"]["data"][0])
        if len(raw) < 112:
            log.debug(f"[EPOCH] NetworkConfig too short: {len(raw)} bytes")
            return None
        current_epoch        = _struct.unpack_from("<Q", raw,  88)[0]
        epoch_start_slot     = _struct.unpack_from("<q", raw,  96)[0]
        epoch_duration_slots = _struct.unpack_from("<Q", raw, 104)[0]
        return {
            "current_epoch":        current_epoch,
            "epoch_start_slot":     epoch_start_slot,
            "epoch_duration_slots": epoch_duration_slots,
        }
    except Exception as e:
        log.debug(f"[EPOCH] _read_epoch_state failed: {e}")
        return None


def _maybe_advance_epoch() -> None:
    """Advance the on-chain epoch if it has expired.

    Checks current_slot > epoch_start_slot + epoch_duration_slots on every
    call.  When the epoch has expired this miner calls life_advance_epoch.js
    which is permissionless — the first miner to call it advances the epoch and
    all miners benefit.

    Includes a Python-level 429 retry loop (3 attempts, 10 s backoff) on top of
    the JS script's own retry logic so transient rate-limits don't silence the
    attempt entirely.

    Safe to call every cycle — no-op when epoch is still live.
    """
    global _last_advance_attempt

    epoch_state = _read_epoch_state()
    if epoch_state is None:
        log.debug("[EPOCH] Could not read epoch state — skipping advance check")
        return

    current_slot = _rpc("getSlot", [])
    if not isinstance(current_slot, int):
        log.debug("[EPOCH] Could not fetch current slot — skipping advance check")
        return

    epoch_start    = epoch_state["epoch_start_slot"]
    epoch_duration = epoch_state["epoch_duration_slots"]
    current_epoch  = epoch_state["current_epoch"]
    elapsed        = current_slot - epoch_start

    log.debug(
        f"[EPOCH] slot={current_slot}  epoch={current_epoch}  "
        f"elapsed={elapsed}/{epoch_duration}  "
        f"expired={'YES' if elapsed >= epoch_duration else 'NO'}"
    )

    if elapsed < epoch_duration:
        return   # epoch still running — nothing to do

    now = time.time()
    if now - _last_advance_attempt < _EPOCH_ADVANCE_COOLDOWN:
        log.debug("[EPOCH] Advance cooldown active — skipping duplicate attempt")
        return

    log.info(
        f"[EPOCH] Advancing epoch {current_epoch} → {current_epoch + 1}  "
        f"(slot {current_slot}, elapsed {elapsed}/{epoch_duration})"
    )
    _last_advance_attempt = now

    MAX_RETRIES = 3
    RETRY_DELAY = 10.0   # seconds

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ["node", str(_ADVANCE_EPOCH_JS)],
                capture_output=True, text=True, timeout=120,
                cwd=str(ANCHOR_DIR),
            )
            # Parse the JSON result line from the JS script
            resp = None
            for line in reversed(result.stdout.strip().splitlines()):
                try:
                    resp = json.loads(line)
                    break
                except Exception:
                    continue

            if result.returncode == 0 and resp and resp.get("status") == "success":
                log.info(
                    f"[EPOCH] ✔ advance_epoch confirmed — "
                    f"old={resp.get('old_epoch')}  new={resp.get('new_epoch')}  "
                    f"tx={resp.get('tx','?')}"
                )
                return

            # Check for 429 in stdout/stderr so we can retry
            combined = (result.stdout + result.stderr).lower()
            is_rate_limited = "429" in combined or "too many requests" in combined
            is_not_ready    = "epoch not ready" in combined

            if is_not_ready:
                # Another miner already advanced — race is over
                log.info("[EPOCH] Epoch already advanced by another miner (race won by peer)")
                return

            if is_rate_limited and attempt < MAX_RETRIES:
                log.warning(
                    f"[EPOCH] 429 rate-limit on attempt {attempt}/{MAX_RETRIES} — "
                    f"retrying in {RETRY_DELAY:.0f}s"
                )
                time.sleep(RETRY_DELAY)
                RETRY_DELAY *= 2   # exponential backoff
                continue

            log.warning(
                f"[EPOCH] advance_epoch attempt {attempt}/{MAX_RETRIES} failed "
                f"(rc={result.returncode})"
            )
            if result.stderr.strip():
                log.warning(f"[EPOCH] stderr: {result.stderr.strip()[-400:]}")
            if result.stdout.strip():
                log.debug(f"[EPOCH] stdout: {result.stdout.strip()[-400:]}")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                RETRY_DELAY *= 2

        except subprocess.TimeoutExpired:
            log.warning(f"[EPOCH] advance_epoch timed out (attempt {attempt}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        except Exception as e:
            log.warning(f"[EPOCH] advance_epoch exception (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)


# ── Boltz2 scoring ────────────────────────────────────────────────────────────
_BOLTZ_HELPER = """\
import sys, json
sys.path.insert(0, "{nova_dir}")

from nova_adaptive.nova_pulse_scorer import score_batch

args      = json.loads(sys.argv[1])
scores    = score_batch([args["smiles"]], args["target_id"],
                        args["sequence"], args["msa_path"])
boltz_score = scores.get(args["smiles"])

print(json.dumps({{
    "boltz_score": boltz_score,
    "smiles":      args["smiles"],
    "target_id":   args["target_id"],
    "msa_path":    args["msa_path"],
    "seed":        args.get("seed", 68),
}}))
"""

def _msa_path_for(uniprot_id: str, gene_name: str = "", download: bool = False) -> str:
    """Return path to local .a3m or 'empty'.

    If download=True and auto_msa is available, downloads the MSA synchronously
    before returning (used for the current round's target when file is missing).
    Pass download=False (default) for the fast cached-only check used in logging.
    """
    path = MSA_DIR / f"{uniprot_id}.a3m"
    if path.exists() and path.stat().st_size > 1024:
        return str(path)
    if download and _AUTO_MSA_AVAILABLE:
        return _auto_ensure_msa(uniprot_id, gene_name)  # type: ignore[possibly-unbound]
    return "empty"

def _sequence_from_msa(msa_path: str) -> str | None:
    if msa_path == "empty":
        return None
    try:
        with open(msa_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith(">"):
                    return line
    except OSError as e:
        log.warning(f"Could not read MSA {msa_path}: {e}")
    return None

def run_boltz2_scoring(smiles: str, target: dict) -> dict:
    uniprot   = target["uniprot_id"]
    target_id = target["id"]
    msa_path  = _msa_path_for(uniprot)
    sequence  = _sequence_from_msa(msa_path) or target["protein_sequence"]
    if msa_path != "empty":
        msa_len, tgt_len = len(sequence), len(target.get("protein_sequence", ""))
        if msa_len != tgt_len:
            log.info(f"  [MSA] Using sequence from {Path(msa_path).name} "
                     f"({msa_len} aa) instead of targets.json ({tgt_len} aa)")

    helper_src = _BOLTZ_HELPER.format(nova_dir=str(NOVA_DIR))
    args_json  = json.dumps({"smiles": smiles, "target_id": target_id,
                              "msa_path": msa_path, "sequence": sequence,
                              "seed": BOLTZ_SEED})
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                     prefix="life-boltz-") as f:
        f.write(helper_src)
        helper_path = f.name
    try:
        r = subprocess.run([str(NOVA_VENV), helper_path, args_json],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(NOVA_DIR))
    finally:
        os.unlink(helper_path)

    if r.returncode != 0:
        log.warning(f"  Boltz2 stderr: {r.stderr[-400:]}")
        return {"boltz_score": None, "model": "boltz2-gpu",
                "msa_used": msa_path, "error": r.stderr[-200:]}

    for line in reversed(r.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            result["model"]    = "boltz2-gpu"
            result["msa_used"] = msa_path
            return result
        except Exception:
            continue

    log.warning(f"  Boltz2 stdout unparseable: {r.stdout[-200:]}")
    return {"boltz_score": None, "model": "boltz2-gpu", "msa_used": msa_path}

def _boltz_score_to_affinity(boltz_score) -> float | None:
    """Higher boltz_score = better binder → negate+scale to kcal/mol-like."""
    if boltz_score is None:
        return None
    return round(-float(boltz_score) * 30.0, 3)


# ── On-chain submission ───────────────────────────────────────────────────────
def submit_on_chain(target_id_num: int, smiles: str, affinity: float,
                    boltz_seed: int = BOLTZ_SEED) -> dict | None:
    args = {
        "rpc": SOLANA_RPC, "authKeypair": AUTH_KEYPAIR,
        "minerKeypair": MINER_KEYPAIR, "idlPath": str(IDL_PATH),
        "programId": PROGRAM_ID, "targetIdNum": target_id_num,
        "smiles": smiles, "affinity": affinity, "boltzSeed": boltz_seed,
    }
    try:
        result = subprocess.run(
            ["node", str(ANCHOR_DIR / "life_submit.js"), json.dumps(args)],
            capture_output=True, text=True, timeout=120, cwd=str(ANCHOR_DIR))
        if result.returncode != 0:
            log.error(f"submit node FAILED (rc={result.returncode})")
            if result.stderr.strip(): log.error(f"stderr:\n{result.stderr.strip()}")
            if result.stdout.strip(): log.error(f"stdout:\n{result.stdout.strip()}")
            return None
        parsed_resp = None
        for line in reversed(result.stdout.strip().splitlines()):
            try:
                parsed_resp = json.loads(line)
                break
            except Exception:
                continue
        if result.stderr.strip():
            level = (log.info if (parsed_resp and parsed_resp.get("status") == "already_submitted")
                     else log.debug)
            for diag_line in result.stderr.strip().splitlines():
                level(f"[node] {diag_line}")
        if parsed_resp is not None:
            return parsed_resp
        log.warning(f"submit stdout (no JSON found): {result.stdout.strip()[:500]}")
        return None
    except subprocess.TimeoutExpired:
        log.error("submit timed out after 120s")
        return None
    except Exception as e:
        log.error(f"submit exception: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def fetch_targets() -> list:
    try:
        with urllib.request.urlopen(TARGETS_URL, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"fetch_targets failed: {e}")
        return []

def fetch_reference_compounds() -> dict[str, str]:
    try:
        with urllib.request.urlopen(REF_COMPOUNDS_URL, timeout=15) as r:
            data = json.loads(r.read())
        return {c["target_id"]: c["smiles"] for c in data.get("compounds", [])}
    except Exception as e:
        log.warning(f"fetch_reference_compounds failed: {e}")
        return {}

def write_stats(stats: dict):
    STATS_PATH.write_text(json.dumps(stats, indent=2))


# ── Registration + balance gate ───────────────────────────────────────────────
FREE_MINER_THRESHOLD = 20       # miners below this count register for free
MIN_SOL_LAMPORTS     = 33_000_000  # 0.033 SOL


def _get_sol_balance_lamports(wallet: str) -> int:
    """Return wallet SOL balance in lamports via RPC, or 0 on failure."""
    result = _rpc("getBalance", [wallet])
    if isinstance(result, dict):
        return int(result.get("value", 0))
    return 0


def _is_miner_registered() -> bool:
    """Return True if a MinerAccount PDA exists for MINER_KEYPAIR's pubkey."""
    # Derive pubkey from keypair file
    try:
        kp_data = json.loads(Path(MINER_KEYPAIR).read_bytes())
        # Anchor / Solana keypair: 64-byte array [privkey(32) | pubkey(32)]
        if isinstance(kp_data, list) and len(kp_data) == 64:
            pubkey_bytes = bytes(kp_data[32:])
            pubkey_b58   = _b58enc(pubkey_bytes)
        else:
            return False  # unrecognised format — assume not registered
    except Exception:
        return False

    # Check if any MinerAccount has this pubkey as owner
    import base64 as _b64
    result = _rpc("getProgramAccounts", [
        PROGRAM_ID,
        {"encoding": "base64",
         "filters": [{"memcmp": {"offset": 0, "bytes": _b58enc(_DISC_MINER)}}]},
    ])
    if not isinstance(result, list):
        log.debug("[REGISTER] getProgramAccounts failed — assuming not registered")
        return False

    for item in result:
        try:
            raw = _b64.b64decode(item["account"]["data"][0])
            # MinerAccount layout (Anchor): disc(8) + owner Pubkey(32) + …
            if len(raw) >= 40 and raw[:8] == _DISC_MINER:
                acct_owner = _b58enc(raw[8:40])
                if acct_owner == pubkey_b58:
                    return True
        except Exception:
            continue
    return False


def _check_registration_and_balance() -> None:
    """
    Gate: ensure the miner is registered (or can register) before mining begins.

    Flow:
      1. Already registered on-chain → proceed immediately.
      2. Not registered + total_miners < FREE_MINER_THRESHOLD → free slot, proceed.
      3. Not registered + balance >= 0.033 SOL → paid registration, proceed.
      4. Not registered + balance < 0.033 SOL → print error and sys.exit(1).

    Registration itself is handled by the Anchor JS client (submit_result.js /
    register_miner instruction) on first submission — this function only gates
    the balance requirement so the miner fails fast with a clear message instead
    of running for hours then failing on-chain.
    """
    log.info("[REGISTER] Checking on-chain registration status...")
    try:
        if _is_miner_registered():
            log.info("[REGISTER] ✓ Miner already registered on-chain — starting")
            return

        log.info("[REGISTER] Not yet registered — checking eligibility...")
        total = _get_network_config_miners_registered()
        if total is None:
            log.warning("[REGISTER] Could not read on-chain miner count (RPC issue) — proceeding anyway")
            return

        log.info(f"[REGISTER] Total miners registered: {total}")

        if total < FREE_MINER_THRESHOLD:
            log.info(f"[REGISTER] ✓ Free registration slot available ({total} < {FREE_MINER_THRESHOLD}) — starting")
            return

        # Paid registration required — check balance
        wallet = _env("SOLANA_WALLET", "")
        if not wallet:
            # Derive wallet from keypair file as fallback
            try:
                kp_data = json.loads(Path(MINER_KEYPAIR).read_bytes())
                if isinstance(kp_data, list) and len(kp_data) == 64:
                    wallet = _b58enc(bytes(kp_data[32:]))
            except Exception:
                pass

        if not wallet:
            log.warning("[REGISTER] Cannot determine wallet address — skipping balance check")
            return

        # Multi-GPU miners pay a higher fee (0.1 SOL vs 0.033 SOL)
        required_lamports = MIN_SOL_LAMPORTS_MULTI if MULTI_GPU else MIN_SOL_LAMPORTS
        required_str      = "0.1 SOL (~$15) — multi-GPU" if MULTI_GPU else "0.033 SOL (~$5)"

        balance     = _get_sol_balance_lamports(wallet)
        balance_sol = balance / 1e9
        log.info(f"[REGISTER] Wallet {wallet[:8]}… balance: {balance_sol:.4f} SOL "
                 f"(required: {required_str}, GPUs: {GPU_COUNT})")

        if balance >= required_lamports:
            log.info(f"[REGISTER] ✓ Balance sufficient — registration will proceed on first submission")
            return

        # Insufficient balance — hard exit with clear user-facing message
        print()
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║         INSUFFICIENT SOL to register as a miner             ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Wallet:   {wallet:<51}║")
        print(f"║  Balance:  {balance_sol:.4f} SOL{' ' * 46}║")
        print(f"║  Required: {required_str:<51}║")
        print(f"║  GPUs:     {GPU_COUNT:<51}║")
        print(f"║  Fund at:  https://phantom.app{' ' * 31}║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        sys.exit(1)

    except SystemExit:
        raise
    except Exception as e:
        log.warning(f"[REGISTER] Registration check failed ({e}) — proceeding anyway")


# ── GPU worker process ────────────────────────────────────────────────────────
def gpu_worker(gpu_idx: int, gpu_count: int, shared_stats: dict) -> None:
    """
    Single-GPU mining loop.  Runs as a separate process (multiprocessing.Process).

    - Sets CUDA_VISIBLE_DEVICES to isolate this worker to one physical GPU.
    - Picks targets via round-robin with an offset of gpu_idx so workers
      spread across different cancer targets simultaneously.
    - Writes per-GPU rows to life_boltz_scores.jsonl (tagged gpu=N).
    - Accumulates molecules_screened and life_earned into shared_stats.
    """
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_idx)

    # Per-worker logger tags every line with [GPU:N]
    _fmt = logging.Formatter(
        f"%(asctime)s  %(levelname)-8s  [GPU:{gpu_idx}]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    _h = logging.StreamHandler()
    _h.setFormatter(_fmt)
    wlog = logging.getLogger(f"life-miner-gpu{gpu_idx}")
    wlog.handlers = [_h]
    wlog.setLevel(logging.INFO)
    wlog.propagate = False

    wlog.info(f"Worker started — GPU {gpu_idx}/{gpu_count-1}, CUDA_VISIBLE_DEVICES={gpu_idx}")

    # Worker-local state
    targets:           list             = []
    last_refresh:      float            = 0.0
    ref_compounds:     dict[str, str]   = {}
    ref_scores:        dict[str, float] = {}
    ref_last_screened: dict[str, float] = {}
    best_boltz_smiles: list[str]        = []
    sub_memory = SubmissionMemory() if _TOOLS_AVAILABLE else None  # type: ignore[possibly-unbound]

    TARGET_ID_MAP = {
        "TP53":   0, "BRCA1":  1, "EGFR":   2, "HER2":   3, "KRAS":   4,
        "BCL2":   5, "CDK4":   6, "VEGFR2": 7, "PDL1":   8, "MDM2":   9,
        "BRAF":  10, "PTEN":  11, "MYC":   12, "STAT3": 13, "PIK3CA": 14,
        "MTOR":  15, "FGFR1": 16, "RET":   17, "AR":    18, "NTRK1":  19,
        "IDH1":  20, "FLT3":  21, "SMAD4": 22, "APC":   23, "PARP1":  24,
        "JAK2":  25, "ESR1":  26, "HDAC1": 27, "HDAC2": 28, "ABL1":   29,
    }

    # Offset target index so GPUs work different targets simultaneously
    target_idx = gpu_idx

    while True:
        now = time.time()

        if now - last_refresh > TARGET_REFRESH or not targets:
            targets = fetch_targets()
            if not targets:
                wlog.warning("No targets — retrying in 30s")
                time.sleep(30)
                continue
            ref_compounds = fetch_reference_compounds()
            ref_scores.clear()
            ref_last_screened.clear()
            last_refresh = now

        _maybe_advance_epoch()

        target = targets[target_idx % len(targets)]
        target_idx += gpu_count  # skip by gpu_count so workers stay spread
        tid    = target["id"]
        thresh = target.get("target_score_threshold", -7.0)
        uid    = target["uniprot_id"]

        # Reference compound screening (once per 4h per target per worker)
        is_ref = False
        ref_smiles = ref_compounds.get(tid)
        if ref_smiles and (now - ref_last_screened.get(tid, 0)) > REF_RESCREEN_INTERVAL:
            mol, source = ref_smiles, "reference"
            is_ref = True
            ref_last_screened[tid] = now
        else:
            try:
                mol, source = _pick_molecule(target, sub_memory, best_boltz_smiles)
            except Exception as _pe:
                wlog.warning(f"_pick_molecule failed: {_pe}")
                time.sleep(5)
                continue

        wlog.info(f"Target: {tid} | Mol: {mol[:60]}  [{source}]")
        wlog.info("Running Boltz2 GPU scoring...")

        t0      = time.time()
        result  = run_boltz2_scoring(mol, target)
        elapsed = time.time() - t0

        boltz_score     = result.get("boltz_score")
        boltz_seed_used = result.get("seed", BOLTZ_SEED)
        affinity        = _boltz_score_to_affinity(boltz_score)

        if is_ref and affinity is not None:
            ref_scores[tid] = affinity

        eff_thresh = ref_scores[tid] + 0.5 if tid in ref_scores else thresh
        hit        = affinity is not None and affinity <= eff_thresh
        score_str  = f"{affinity:.3f}" if affinity is not None else "None"

        wlog.info(f"  score={score_str}  {'✔ HIT' if hit else '✘ miss'}  {elapsed:.1f}s")

        # Write to shared JSONL feed (file-level append is atomic on Linux)
        _boltz_jsonl = WORK_DIR / "output" / "life_boltz_scores.jsonl"
        try:
            _boltz_jsonl.parent.mkdir(exist_ok=True)
            with _boltz_jsonl.open("a") as _fh:
                _fh.write(json.dumps({
                    "ts": time.time(), "target_id": tid, "smiles": mol,
                    "boltz_score": boltz_score, "affinity": affinity,
                    "hit": hit, "source": source, "gpu": gpu_idx,
                }) + "\n")
        except Exception as _je:
            wlog.debug(f"JSONL write failed: {_je}")

        # Update per-GPU shared stats key
        life_delta = 0.0
        if hit and affinity is not None and TARGET_ID_MAP.get(tid) is not None:
            resp = submit_on_chain(TARGET_ID_MAP[tid], mol, affinity, boltz_seed_used)
            if resp and resp.get("status") == "submitted":
                tx_sig = resp.get("signature", "")
                tier_reward = {1: 1.0, 2: 5.0, 3: 25.0}.get(target.get("difficulty_tier", 1), 1.0)
                life_delta = tier_reward
                wlog.info(f"  ✔ tx: {tx_sig}")

        # Accumulate into shared manager dict
        try:
            key_mols  = f"gpu{gpu_idx}_molecules"
            key_life  = f"gpu{gpu_idx}_life"
            key_target = f"gpu{gpu_idx}_target"
            key_score  = f"gpu{gpu_idx}_score"
            key_power  = f"gpu{gpu_idx}_power"
            shared_stats[key_mols]   = shared_stats.get(key_mols, 0) + 1
            shared_stats[key_life]   = shared_stats.get(key_life, 0.0) + life_delta
            shared_stats[key_target] = tid
            shared_stats[key_score]  = affinity
            # Read own GPU power
            try:
                _pw = subprocess.check_output(
                    ["nvidia-smi", f"--id={gpu_idx}", "--query-gpu=power.draw",
                     "--format=csv,noheader,nounits"], timeout=3, text=True).strip()
                shared_stats[key_power] = float(_pw) if _pw else None
            except Exception:
                pass
        except Exception:
            pass  # manager proxy may fail transiently — non-fatal

        wlog.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("\033[92m    L I F E  C O M P U T E  \033[0m")
    print()

    if not ANCHOR_DIR.exists() or not IDL_PATH.exists():
        log.error(f"Anchor dir missing: {ANCHOR_DIR}")
        sys.exit(1)
    if not NOVA_VENV.exists():
        log.error(f"Nova venv missing: {NOVA_VENV}")
        sys.exit(1)
    if not _TOOLS_AVAILABLE:
        log.warning(f"Optional tools unavailable ({_tools_err}); using ZINC15-only mode")

    # ── On-chain registration gate ────────────────────────────────────────────
    _check_registration_and_balance()

    # ── GPU startup banner ────────────────────────────────────────────────────
    gpu_names = []
    try:
        _gn = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            timeout=5, text=True, stderr=subprocess.DEVNULL).strip().splitlines()
        gpu_names = [g.strip() for g in _gn]
    except Exception:
        pass
    if MULTI_GPU:
        log.info(f"MULTI-GPU MODE: spawning {GPU_COUNT} workers")
        for i, gname in enumerate(gpu_names[:GPU_COUNT]):
            log.info(f"  GPU {i}: {gname}")
    else:
        log.info(f"SINGLE-GPU MODE: GPU 0 = {gpu_names[0] if gpu_names else 'unknown'}")

    # ── Multi-GPU worker dispatch ─────────────────────────────────────────────
    if MULTI_GPU:
        mgr = multiprocessing.Manager()
        shared = mgr.dict()
        workers = []
        for gpu_idx in range(GPU_COUNT):
            p = multiprocessing.Process(
                target=gpu_worker,
                args=(gpu_idx, GPU_COUNT, shared),
                name=f"life-gpu-{gpu_idx}",
                daemon=True,
            )
            p.start()
            workers.append(p)
            log.info(f"  Started worker PID {p.pid} → GPU {gpu_idx}")

        # Supervisor loop: aggregate stats and write stats.json every 30s
        stats = {
            "alive": True, "current_target": "", "gpu_count": GPU_COUNT,
            "molecules_screened": 0, "life_earned": 0.0,
            "targets_contributed": [], "transactions": [],
            "tools": {"available": _TOOLS_AVAILABLE},
            "global": {"total_miners": None, "molecules_screened": None, "targets_solved": None},
            "started_at": datetime.now(timezone.utc).isoformat(), "last_updated": "",
            "gpu_workers": [],
        }
        write_stats(stats)

        while True:
            time.sleep(30)
            # Restart any dead worker
            for i, p in enumerate(workers):
                if not p.is_alive():
                    log.warning(f"[SUPERVISOR] GPU {i} worker died — restarting")
                    new_p = multiprocessing.Process(
                        target=gpu_worker,
                        args=(i, GPU_COUNT, shared),
                        name=f"life-gpu-{i}",
                        daemon=True,
                    )
                    new_p.start()
                    workers[i] = new_p

            # Aggregate per-GPU stats
            total_mols = sum(shared.get(f"gpu{i}_molecules", 0) for i in range(GPU_COUNT))
            total_life = sum(shared.get(f"gpu{i}_life", 0.0)    for i in range(GPU_COUNT))
            gpu_workers_info = []
            for i, gname in enumerate(gpu_names[:GPU_COUNT]):
                gpu_workers_info.append({
                    "gpu":       i,
                    "name":      gname,
                    "target":    shared.get(f"gpu{i}_target"),
                    "last_score": shared.get(f"gpu{i}_score"),
                    "power_w":   shared.get(f"gpu{i}_power"),
                    "molecules": shared.get(f"gpu{i}_molecules", 0),
                    "life":      shared.get(f"gpu{i}_life", 0.0),
                })

            stats.update({
                "alive":              True,
                "gpu_count":          GPU_COUNT,
                "molecules_screened": total_mols,
                "life_earned":        total_life,
                "gpu_workers":        gpu_workers_info,
                "last_updated":       datetime.now(timezone.utc).isoformat(),
                "global":             fetch_network_stats(),
            })
            write_stats(stats)
        return  # supervisor loop is infinite; workers run as daemons

    # ── Single-GPU path (GPU_COUNT == 1) ─────────────────────────────────────
    targets            : list             = []
    last_refresh       : float            = 0.0
    molecules_done     : int              = 0
    life_earned        : float            = 0.0
    txs                : list             = []
    ref_compounds      : dict[str, str]   = {}
    ref_scores         : dict[str, float] = {}
    ref_last_screened  : dict[str, float] = {}
    best_boltz_smiles  : list[str]        = []   # recent high-scorers for generative seeding
    sub_memory = SubmissionMemory() if _TOOLS_AVAILABLE else None

    # ChEMBL actives — background pre-download (cache-first, non-fatal)
    if _TOOLS_AVAILABLE:
        def _bg_chembl():
            try:
                with urllib.request.urlopen(TARGETS_URL, timeout=15) as _r:
                    _tgts = json.loads(_r.read())
                uids = [t["uniprot_id"] for t in _tgts]
                log.info(f"[ChEMBL] Pre-downloading actives for {len(uids)} targets ...")
                chembl_download_all(uids)
                log.info("[ChEMBL] Pre-download complete")
            except Exception as _ce:
                log.warning(f"[ChEMBL] Background download failed (non-fatal): {_ce}")
        threading.Thread(target=_bg_chembl, daemon=True, name="chembl-prefetch").start()

    # ── LIFE PULSE background sweep ────────────────────────────────────────────
    if _PULSE_AVAILABLE:
        def _pulse_loop():
            """Run continuous Sobol sweep in a background daemon thread.

            Runs in batches of 50 molecules.  Sleeps 2 s between batches to
            yield the GIL to the main scoring loop.  Never raises — all
            exceptions are caught and logged so a PULSE crash never kills the
            miner process.
            """
            log.info("[PULSE] Background sweep thread started")
            while True:
                try:
                    pulse_run_sweep(
                        max_configs=50,
                        verbose=False,
                        use_mutants=True,
                        tanimoto_threshold=0.85,
                    )
                except Exception as _be:
                    log.warning(f"[PULSE] sweep error (non-fatal): {_be}")
                time.sleep(2)
        threading.Thread(target=_pulse_loop, daemon=True, name="pulse-sweep").start()
    else:
        log.warning(f"[PULSE] life_pulse unavailable — pulse sweep disabled ({_pulse_err})")

    # ── ProteinNet background retrain loop ────────────────────────────────────
    if _PNET_AVAILABLE and _pnet_train_all is not None:
        def _pnet_retrain_loop():
            # Build target→uniprot map from targets.json on first call
            _t_map: dict[str, str] = {}
            while True:
                try:
                    tgts = fetch_targets()
                    _t_map = {t["id"]: t["uniprot_id"] for t in tgts if "id" in t and "uniprot_id" in t}
                    _pnet_train_all(_t_map)
                except Exception as _rte:
                    log.debug(f"[PROTEINNET] retrain error (non-fatal): {_rte}")
                time.sleep(300)   # check every 5 min; train_all() is a no-op when up to date
        threading.Thread(target=_pnet_retrain_loop, daemon=True, name="proteinnet-retrain").start()
        log.info("[PROTEINNET] Background retrain thread started (every 5 min)")

    stats = {
        "alive": True,
        "current_target": "",
        "molecules_screened": 0, "life_earned": 0.0,
        "targets_contributed": [], "transactions": [],
        "tools": {"available": _TOOLS_AVAILABLE},
        "global": {"total_miners": None, "molecules_screened": None, "targets_solved": None},
        "started_at": datetime.now(timezone.utc).isoformat(), "last_updated": "",
    }
    write_stats(stats)

    # On-chain target ID map — index matches the on-chain TargetAccount target_id field.
    # Targets 0-9 are registered on devnet (MAX_TARGETS=10).
    # Targets 10-29 are in targets.json but pending on-chain registration (MAX_TARGETS
    # must be raised to ≥30 and register_target called for each before hits can be
    # submitted).  All 30 are screened every cycle; submission is gated at line ~477.
    TARGET_ID_MAP = {
        "TP53":   0, "BRCA1":  1, "EGFR":   2, "HER2":   3, "KRAS":   4,
        "BCL2":   5, "CDK4":   6, "VEGFR2": 7, "PDL1":   8, "MDM2":   9,
        # ── pending on-chain registration (MAX_TARGETS ≥ 30 required) ──────────
        "BRAF":  10, "PTEN":  11, "MYC":   12, "STAT3": 13, "PIK3CA": 14,
        "MTOR":  15, "FGFR1": 16, "RET":   17, "AR":    18, "NTRK1":  19,
        "IDH1":  20, "FLT3":  21, "SMAD4": 22, "APC":   23, "PARP1":  24,
        "JAK2":  25, "ESR1":  26, "HDAC1": 27, "HDAC2": 28, "ABL1":   29,
    }

    # Round-robin index — rotates through all fetched targets regardless of
    # on-chain registration.  Submission is still gated by TARGET_ID_MAP below.
    # To switch to random: replace the two lines below with
    #   target = random.choice(targets or [{}])
    target_idx = 0

    while True:
        now = time.time()

        # ── Target / epoch refresh ────────────────────────────────────────────
        if now - last_refresh > TARGET_REFRESH or not targets:
            log.info(f"Fetching targets from {TARGETS_URL}...")
            targets = fetch_targets()
            if not targets:
                log.warning("No targets — retrying in 30s")
                time.sleep(30)
                continue
            ref_compounds = fetch_reference_compounds()
            log.info(f"Reference compounds loaded: {len(ref_compounds)} ({', '.join(ref_compounds)})")
            ref_scores.clear()
            ref_last_screened.clear()  # reset timer so ref is scored on next iteration
            for t in targets:
                uid  = t["uniprot_id"]
                flag = "✓ MSA" if _msa_path_for(uid) != "empty" else "✗ no MSA (single-seq)"
                log.info(f"  {t['id']:8s} {uid}  {flag}")
            # Launch background MSA prefetch for all targets (skips cached files)
            if _AUTO_MSA_AVAILABLE:
                _auto_msa_prefetch(targets)  # type: ignore[possibly-unbound]
            last_refresh = now

        # ── Epoch advance check ───────────────────────────────────────────────
        # Permissionless: first miner to detect an expired epoch advances it;
        # all other miners benefit automatically.  No-op when epoch is live.
        _maybe_advance_epoch()

        # Round-robin over all fetched targets; submission eligibility is separate
        target = targets[target_idx % len(targets)]
        target_idx += 1
        tid    = target["id"]
        thresh = target.get("target_score_threshold", -7.0)
        uid    = target["uniprot_id"]
        msa    = _msa_path_for(uid, gene_name=target.get("gene_name", tid), download=True)

        # ── Molecule selection ────────────────────────────────────────────────
        # Priority 1: reference compound (at most once per REF_RESCREEN_INTERVAL)
        ref_smi = ref_compounds.get(tid) if ref_compounds else None
        ref_due = (now - ref_last_screened.get(tid, 0.0)) >= REF_RESCREEN_INTERVAL
        if ref_smi and ref_due:
            mol, source = ref_smi, "ref"
            ref_last_screened[tid] = now
            log.info(f"[REF] Screening reference compound for {tid}: {mol[:50]}")
        else:
            # Priority 2: your strategy (_pick_molecule) — default is ZINC15 random
            try:
                mol, source = _pick_molecule(target, sub_memory, best_boltz_smiles)
            except Exception as _pe:
                log.warning(f"[PICK] molecule selection failed ({_pe}) — ZINC15 fallback")
                mol, source = _sample_zinc15(), "zinc15-fallback"

        is_ref = (source == "ref")
        log.info(f"Target: {tid} ({uid}) | tier {target.get('difficulty_tier','?')} "
                 f"| threshold {thresh} | MSA: {'local' if msa != 'empty' else 'empty'}")
        log.info(f"Molecule: {mol[:80]}  [{source}]")
        log.info("Running Boltz2 GPU scoring...")

        t0      = time.time()
        result  = run_boltz2_scoring(mol, target)
        elapsed = time.time() - t0

        boltz_score     = result.get("boltz_score")
        boltz_seed_used = result.get("seed", BOLTZ_SEED)
        affinity        = _boltz_score_to_affinity(boltz_score)

        if is_ref and affinity is not None:
            ref_scores[tid] = affinity
            log.info(f"  [REF-SCORE] {tid} reference affinity: {affinity:.3f} kcal/mol "
                     f"→ effective threshold: {affinity + 0.5:.3f} kcal/mol")

        eff_thresh = ref_scores[tid] + 0.5 if tid in ref_scores else thresh
        hit        = affinity is not None and affinity <= eff_thresh
        score_str  = f"{affinity:.3f} kcal/mol" if affinity is not None else "None (scoring failed)"

        log.info(f"  Boltz score: {boltz_score}  → affinity: {score_str}  "
                 f"({'✔ HIT' if hit else '✘ miss'})  thresh={eff_thresh:.3f}  {elapsed:.1f}s  "
                 f"msa={result.get('msa_used', '?')}")

        # ── Append to live scoring feed JSONL ────────────────────────────────
        _boltz_jsonl = WORK_DIR / "output" / "life_boltz_scores.jsonl"
        try:
            _boltz_jsonl.parent.mkdir(exist_ok=True)
            with _boltz_jsonl.open("a") as _fh:
                _fh.write(json.dumps({
                    "ts":          time.time(),
                    "target_id":   tid,
                    "smiles":      mol,
                    "boltz_score": boltz_score,
                    "affinity":    affinity,
                    "hit":         hit,
                    "source":      source,
                }) + "\n")
        except Exception as _je:
            log.debug(f"JSONL write failed: {_je}")

        # Track best molecules for generative seeding; record in diversity memory
        if boltz_score is not None:
            best_boltz_smiles.append(mol)
            best_boltz_smiles = best_boltz_smiles[-50:]
            if sub_memory is not None:
                sub_memory.mark_submitted(mol, boltz_score)
            # Write confirmed Boltz2 score back to PULSE so it can learn ──────
            if _PULSE_AVAILABLE and source == "pulse":
                try:
                    pulse_record_boltz(mol, boltz_score, tid)
                except Exception as _pbe:
                    log.debug(f"[PULSE] record_boltz failed (non-fatal): {_pbe}")

        # ── On-chain submission ───────────────────────────────────────────────
        tx_sig = None
        if hit and affinity is not None and tid in TARGET_ID_MAP:
            log.info(f"  HIT — submitting to devnet program {PROGRAM_ID}...")
            # ChEMBL novelty cross-reference (non-fatal)
            chembl_result: dict = {}
            if _TOOLS_AVAILABLE:
                try:
                    chembl_result = validate_against_chembl(mol, uid)
                    log.info(f"  [ChEMBL] novel={chembl_result.get('is_novel')}  "
                             f"sim={chembl_result.get('similarity', 0):.2f}  "
                             f"vs {str(chembl_result.get('closest_smiles','n/a'))[:50]}")
                except Exception as _ce:
                    log.debug(f"  [ChEMBL] cross-reference failed: {_ce}")

            resp = submit_on_chain(TARGET_ID_MAP[tid], mol, affinity, boltz_seed=boltz_seed_used)
            if resp and resp.get("tx"):
                tx_sig = resp["tx"]
                # Tier-based reward tracking (mirrors Rust DifficultyTier::base_reward_raw):
                #   tier 1 (easy)   =   1 LIFE
                #   tier 2 (medium) =   5 LIFE
                #   tier 3 (hard)   =  25 LIFE
                #   unknown         =   1 LIFE (conservative fallback)
                _tier = target.get("difficulty_tier", 1)
                _tier_reward = {1: 1.0, 2: 5.0, 3: 25.0}.get(_tier, 1.0)
                life_earned += _tier_reward
                log.info(f"  ✔ tx: {tx_sig}")
                log.info(f"  Explorer: https://explorer.solana.com/tx/{tx_sig}?cluster=devnet")
                txs.append({"tx": tx_sig, "target": tid, "score": affinity,
                             "boltz_score": boltz_score,
                             "chembl_novel": chembl_result.get("is_novel"),
                             "chembl_sim":   chembl_result.get("similarity"),
                             "ts": datetime.now(timezone.utc).isoformat()})
                # ── Update results database ───────────────────────────────────
                try:
                    import importlib.util as _ilu
                    _upd_path = Path("/tmp/life-compute/targets/scripts/update_results_db.py")
                    if _upd_path.exists():
                        _spec = _ilu.spec_from_file_location("update_results_db", _upd_path)
                        if _spec and _spec.loader:
                            _upd = _ilu.module_from_spec(_spec)
                            _spec.loader.exec_module(_upd)  # type: ignore[union-attr]
                            _hit = _upd.add_hit(
                                smiles=mol, score=float(affinity),
                                target_id=tid, uniprot_id=uid,
                                miner_wallet=AUTH_KEYPAIR,
                                epoch=int(time.time() // 86400),
                                tx=tx_sig, life_earned=int(life_earned * 1_000_000),
                            )
                            _upd.update_hits(_hit)
                            _upd.rebuild_leaderboard()
                            _upd.rebuild_daily_report()
                            _upd.update_network_stats(_hit)
                            _upd.git_push(_hit)
                            log.info(f"  [RESULTS DB] updated and pushed")
                except Exception as _re:
                    log.debug(f"  [RESULTS DB] update failed (non-fatal): {_re}")
            elif resp and resp.get("status") == "already_submitted":
                log.info("  Already submitted this epoch — waiting for next epoch")
            else:
                log.warning("  Submission failed (see above)")
        elif hit:
            log.info(f"  HIT but {tid} not yet registered on-chain — skipping submission")

        molecules_done += 1
        stats.update({
            "alive": True,
            "current_target": tid,
            "molecules_screened": molecules_done,
            "life_earned": life_earned,
            "targets_contributed": list({t["target"] for t in txs}),
            "transactions": txs[-20:],
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "global": fetch_network_stats(),
        })
        write_stats(stats)
        log.info(f"Screened: {molecules_done} | $LIFE: {life_earned:.1f} | txs: {len(txs)}")
        log.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
