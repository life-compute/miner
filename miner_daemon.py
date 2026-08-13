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
import threading
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

# ── Config ────────────────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID    = _env("PROGRAM_ID",    "DzcQHhTPuiqxCxZurDbEAaV1U2JBFXWy6JG1LE6WsKvJ")
SOLANA_RPC    = _env("SOLANA_RPC",    "https://api.devnet.solana.com")
AUTH_KEYPAIR  = _env("SOLANA_KEYPAIR","/mnt/minos-drive/life-compute-miner/dev-keypair.json")
MINER_KEYPAIR = _env("MINER_KEYPAIR", "/mnt/minos-drive/life-compute-miner/miner-keypair.json")
TARGETS_URL   = _env("TARGETS_URL",   "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
REF_COMPOUNDS_URL = _env("REF_COMPOUNDS_URL", "https://raw.githubusercontent.com/life-compute/targets/master/reference_compounds.json")
POLL_SECONDS  = int(_env("POLL_SECONDS", "60"))
TARGET_REFRESH       = 300      # seconds between target list refreshes
REF_RESCREEN_INTERVAL = 4 * 3600.0  # screen each ref compound at most once per 4 h

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
    # ── Generative AI (wire in when you have real Boltz2 scores to learn from)
    if _TOOLS_AVAILABLE and best_smiles and sub_memory is not None:
        try:
            gen_cands = generate_candidates(target, art_model=None, n_total=50)
            novel = sub_memory.filter_novel(gen_cands) if gen_cands else []
            if novel:
                _, smi, _ = novel[0]
                return smi, "generate"
        except Exception as _ge:
            log.debug(f"[GENERATE] failed (non-fatal): {_ge}")

    # ── Default: random ZINC15 sample
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

    ResultSubmission byte layout (Anchor-serialised):
      0–7    discriminator
      8–39   miner Pubkey
      40     target_id u8
      41–48  epoch u64
      49–560 smiles [u8; 512]
      561–562 smiles_len u16
      563–566 claimed_affinity f32
      567–574 submitted_slot i64   ← we filter on this
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
            if len(raw) < 575:
                continue
            submitted_slot = _struct.unpack_from("<q", raw, 567)[0]
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

def _msa_path_for(uniprot_id: str) -> str:
    path = MSA_DIR / f"{uniprot_id}.a3m"
    return str(path) if path.exists() else "empty"

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
            last_refresh = now

        # Round-robin over all fetched targets; submission eligibility is separate
        target = targets[target_idx % len(targets)]
        target_idx += 1
        tid    = target["id"]
        thresh = target.get("target_score_threshold", -7.0)
        uid    = target["uniprot_id"]
        msa    = _msa_path_for(uid)

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
                life_earned += 1.0
                log.info(f"  ✔ tx: {tx_sig}")
                log.info(f"  Explorer: https://explorer.solana.com/tx/{tx_sig}?cluster=devnet")
                txs.append({"tx": tx_sig, "target": tid, "score": affinity,
                             "boltz_score": boltz_score,
                             "chembl_novel": chembl_result.get("is_novel"),
                             "chembl_sim":   chembl_result.get("similarity"),
                             "ts": datetime.now(timezone.utc).isoformat()})
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
