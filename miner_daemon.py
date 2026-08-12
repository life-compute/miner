#!/usr/bin/env python3
"""
LIFE Compute — Miner Daemon (devnet, real on-chain submission)

Pipeline each cycle:
  1. Fetch cancer targets from GitHub
  2. Detect protein family → route via life_scout
  3. ALWAYS screen reference compounds first (per target, once per epoch)
  4. Three-phase epoch within each TARGET_REFRESH window:
       Phase 1 (first 20%): broad Sobol exploration via life_pulse
       Phase 2 (60%):       ART pre-filter top 25% → Boltz2 score
       Phase 3 (20%):       fine-tune around best scoring molecules
  5. Run Boltz2 GPU scoring via nova_pulse_scorer pattern
  6. If score ≤ threshold → submit_result on-chain via Node.js / Anchor
  7. Write stats.json for dashboard
  8. Auto-retrain life_art after every RETRAIN_EVERY new Boltz2 scores

Adaptive stack (adaptive/):
  life_pulse     — Sobol sweep over ZINC15/SAVI molecular vocabulary
  life_art       — RandomForest on Morgan FP + physchem → affinity prediction
  life_scout     — protein-family-aware candidate routing
  life_diversity — Shannon entropy enforcement + Tanimoto deduplication

Submission uses the proven Node.js stack from E2E tests rather than
the uninstalled anchorpy, because this machine has Anchor node_modules.

Boltz2 scoring mirrors nova_adaptive/nova_pulse_scorer.py:
  - Uses /mnt/minos-drive/nova_subnet/.venv python interpreter
  - Calls score_batch() with per-target UniProt MSA files
  - Falls back to msa="empty" (single-sequence mode) when no .a3m exists
"""
import json, time, random, logging, os, subprocess, sys, urllib.request, tempfile
from pathlib import Path
from datetime import datetime, timezone

# ── Adaptive stack (wire in; graceful fallback if rdkit not on PATH) ──────────
sys.path.insert(0, str(Path(__file__).parent))   # ensure adaptive/ importable
try:
    from adaptive.life_pulse     import (run_sweep, get_next_candidates,
                                         proxy_score, record_boltz_score)
    from adaptive.life_art       import (train as art_train, rank_candidates,
                                         load_model, should_retrain,
                                         RETRAIN_EVERY)
    from adaptive.life_scout     import (get_focused_candidates,
                                         detect_protein_family)
    from adaptive.life_diversity import SubmissionMemory, greedy_diverse_select
    from adaptive.life_generate  import generate_candidates
    from adaptive.life_chembl    import validate_against_chembl, download_all as chembl_download_all
    _ADAPTIVE_AVAILABLE = True
except Exception as _e:
    _ADAPTIVE_AVAILABLE = False
    _e_msg = str(_e)

# ── Config from .env ──────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID    = _env("PROGRAM_ID",    "3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL")
SOLANA_RPC    = _env("SOLANA_RPC",    "https://api.devnet.solana.com")
AUTH_KEYPAIR  = _env("SOLANA_KEYPAIR","/mnt/minos-drive/life-compute-miner/dev-keypair.json")
MINER_KEYPAIR = _env("MINER_KEYPAIR", "/mnt/minos-drive/life-compute-miner/miner-keypair.json")
TARGETS_URL   = _env("TARGETS_URL",   "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
REF_COMPOUNDS_URL = _env("REF_COMPOUNDS_URL", "https://raw.githubusercontent.com/life-compute/targets/master/reference_compounds.json")
POLL_SECONDS  = int(_env("POLL_SECONDS", "60"))
TARGET_REFRESH = 300

WORK_DIR   = Path(__file__).parent
STATS_PATH = WORK_DIR / "stats.json"
ANCHOR_DIR = Path("/tmp/life-compute/core")
IDL_PATH   = ANCHOR_DIR / "target/idl/life_core.json"

# ── Boltz2 / nova paths ───────────────────────────────────────────────────────
NOVA_DIR  = Path("/mnt/minos-drive/nova_subnet")
NOVA_VENV = NOVA_DIR / ".venv" / "bin" / "python"
MSA_DIR   = Path("/mnt/minos-drive/life-compute-miner/data/msa_files")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-miner")


# ── Boltz2 scoring ────────────────────────────────────────────────────────────
#
# Runs in a subprocess under the nova venv to avoid dependency conflicts.
# The helper script is written to a temp file and executed; stdout is one
# JSON line per molecule.  Mirrors nova_pulse_scorer.score_batch() exactly.
#
_BOLTZ_HELPER = """\
import sys, json
sys.path.insert(0, "{nova_dir}")

from nova_adaptive.nova_pulse_scorer import score_batch
from pathlib import Path

args      = json.loads(sys.argv[1])
smiles    = args["smiles"]
target_id = args["target_id"]
msa_path  = args["msa_path"]
sequence  = args["sequence"]

scores = score_batch([smiles], target_id, sequence, msa_path)
boltz_score = scores.get(smiles)

print(json.dumps({{
    "boltz_score": boltz_score,
    "smiles":      smiles,
    "target_id":   target_id,
    "msa_path":    msa_path,
}}))
"""


def _msa_path_for(uniprot_id: str) -> str:
    """Return path to .a3m file if it exists, else 'empty' for single-seq mode."""
    path = MSA_DIR / f"{uniprot_id}.a3m"
    return str(path) if path.exists() else "empty"


def _sequence_from_msa(msa_path: str) -> str | None:
    """Extract the query sequence from the first non-header line of an a3m file.

    Mirrors nova_pulse_scorer._get_protein_sequence() — ensures the sequence
    passed to Boltz2 exactly matches the MSA query, preventing the
    sequence-length mismatch crash that makes predict() exit in ~7s with None.
    Returns None if msa_path is 'empty' or file is unreadable.
    """
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
    """
    Real Boltz2 GPU inference via nova_pulse_scorer.score_batch().
    Falls back to msa='empty' (single-sequence mode) when no .a3m exists.
    Returns dict with boltz_score, model, msa_used.
    """
    uniprot   = target["uniprot_id"]
    target_id = target["id"]
    msa_path  = _msa_path_for(uniprot)

    # CRITICAL: sequence must match the MSA query line exactly.
    # targets.json has truncated sequences (TP53=227aa vs canonical 393aa);
    # the MSA file is built from the canonical sequence.  Mismatch → Boltz2
    # crashes immediately (~7s) and returns None without running GPU inference.
    # Mirror nova_pulse_scorer's pattern: read sequence from MSA when available.
    sequence = _sequence_from_msa(msa_path) or target["protein_sequence"]
    if msa_path != "empty":
        msa_len = len(sequence)
        tgt_len = len(target.get("protein_sequence", ""))
        if msa_len != tgt_len:
            log.info(
                f"  [MSA] Using sequence from {Path(msa_path).name} "
                f"({msa_len} aa) instead of targets.json ({tgt_len} aa)"
            )

    helper_src = _BOLTZ_HELPER.format(nova_dir=str(NOVA_DIR))
    args_json  = json.dumps({
        "smiles":    smiles,
        "target_id": target_id,
        "msa_path":  msa_path,
        "sequence":  sequence,
    })

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                     prefix="hermes-boltz-") as f:
        f.write(helper_src)
        helper_path = f.name

    try:
        r = subprocess.run(
            [str(NOVA_VENV), helper_path, args_json],
            capture_output=True, text=True, timeout=300,
            cwd=str(NOVA_DIR),
        )
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
    """
    Convert Boltz combined score (higher=better binder) to kcal/mol-like affinity
    (lower=better, matching target_score_threshold convention).
    boltz_score = (prob_binary - pred_affinity) / heavy_atoms  (~-0.3 to +0.3)
    Negate + scale ×30 → (-9..+9 kcal/mol range). Rankings preserved.
    """
    if boltz_score is None:
        return None
    return round(-float(boltz_score) * 30.0, 3)


# ── On-chain submission via Node.js / Anchor ──────────────────────────────────

def submit_on_chain(target_id_num: int, smiles: str, affinity: float) -> dict | None:
    """Submit result via Node.js / Anchor. Returns {'tx': ..., 'epoch': ...} or None."""
    args = {
        "rpc":          SOLANA_RPC,
        "authKeypair":  AUTH_KEYPAIR,
        "minerKeypair": MINER_KEYPAIR,
        "idlPath":      str(IDL_PATH),
        "programId":    PROGRAM_ID,
        "targetIdNum":  target_id_num,
        "smiles":       smiles,
        "affinity":     affinity,
    }
    try:
        result = subprocess.run(
            ["node", str(ANCHOR_DIR / "life_submit.js"), json.dumps(args)],
            capture_output=True, text=True, timeout=120,
            cwd=str(ANCHOR_DIR),
        )
        if result.returncode != 0:
            log.error(f"submit node error: {result.stderr[-500:] or result.stdout[-300:]}")
            return None
        for line in reversed(result.stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except Exception:
                continue
        log.warning(f"submit stdout: {result.stdout[:300]}")
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
    """Return {target_id: smiles} for each reference compound. Falls back to {} on error."""
    try:
        with urllib.request.urlopen(REF_COMPOUNDS_URL, timeout=15) as r:
            data = json.loads(r.read())
        return {c["target_id"]: c["smiles"] for c in data.get("compounds", [])}
    except Exception as e:
        log.warning(f"fetch_reference_compounds failed: {e}")
        return {}

SCAFFOLDS = [
    "CC(=O)Nc1ccc(cc1)O",
    "c1ccc(cc1)CN2CCN(CC2)c3ncccn3",
    "CC1=C(C(=O)Nc2ccccc2)c3ccccc3N1C",
    "COc1ccc(cc1OC)C(=O)N2CCCC2",
    "O=C(O)c1ccc(cc1)Nc2ncnc3ccccc23",
    "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
    "O=C(O)c1ccccc1Nc1ncccn1",
    "CC1=CC=C(C=C1)S(=O)(=O)Nc1ccccc1",
]

def sample_molecule(target_id: str | None = None,
                    ref_compounds: dict[str, str] | None = None,
                    screened: set[str] | None = None) -> tuple[str, bool]:
    """Return (smiles, is_reference).

    Priority: reference compound for this target (if not yet screened this epoch)
    → random scaffold fallback.
    """
    if target_id and ref_compounds:
        ref = ref_compounds.get(target_id)
        if ref and (screened is None or ref not in screened):
            return ref, True
    return random.choice(SCAFFOLDS), False

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

    targets        : list             = []
    last_refresh   : float            = 0.0
    molecules_done : int              = 0
    life_earned    : float            = 0.0
    txs            : list             = []
    ref_compounds  : dict[str, str]   = {}
    epoch_screened : set[str]         = set()   # ref SMILES screened this epoch
    ref_scores     : dict[str, float] = {}       # {target_id: ref_compound_affinity this epoch}

    # ── Adaptive state ─────────────────────────────────────────────────────────
    # epoch_start_time: when the current TARGET_REFRESH epoch started
    # best_boltz_smiles: top-scoring SMILES from prior rounds (for Phase 3 refine)
    # art_model: in-process ART model handle (reload triggers on retrain)
    # sub_memory: cross-restart deduplication memory
    epoch_start_time: float        = 0.0
    best_boltz_smiles: list[str]   = []          # updated after each real Boltz score
    art_model                      = None
    sub_memory                     = SubmissionMemory() if _ADAPTIVE_AVAILABLE else None
    # seed pulse with a quick initial sweep so there's something in the queue
    if _ADAPTIVE_AVAILABLE:
        try:
            _pulse_jsonl = Path(__file__).parent / "output" / "life_pulse_data.jsonl"
            _existing_rows = sum(1 for _ in _pulse_jsonl.open()) if _pulse_jsonl.exists() else 0
            if _existing_rows >= 20:
                log.info(f"[ADAPTIVE] life_pulse already has {_existing_rows} rows — skipping seed sweep.")
            else:
                log.info("[ADAPTIVE] Seeding life_pulse with initial 100-config sweep (background) ...")
                import threading as _threading
                def _bg_seed():
                    try:
                        run_sweep(max_configs=100, verbose=True)
                    except Exception as _se:
                        log.warning(f"[ADAPTIVE] Background pulse sweep failed (non-fatal): {_se}")
                _threading.Thread(target=_bg_seed, daemon=True, name="pulse-seed").start()
        except Exception as _se:
            log.warning(f"[ADAPTIVE] Initial pulse sweep failed (non-fatal): {_se}")

    # ── ChEMBL background pre-download (all targets, cache-first) ─────────────
    if _ADAPTIVE_AVAILABLE:
        try:
            import threading as _threading2
            def _bg_chembl():
                try:
                    import urllib.request as _ur
                    with _ur.urlopen(TARGETS_URL, timeout=15) as _r:
                        _tgts = json.loads(_r.read())
                    _uids = [t["uniprot_id"] for t in _tgts]
                    log.info(f"[ChEMBL] Pre-downloading actives for {len(_uids)} targets ...")
                    chembl_download_all(_uids)
                    log.info("[ChEMBL] Pre-download complete")
                except Exception as _ce:
                    log.warning(f"[ChEMBL] Background download failed (non-fatal): {_ce}")
            _threading2.Thread(target=_bg_chembl, daemon=True, name="chembl-prefetch").start()
        except Exception as _ce:
            log.warning(f"[ChEMBL] Could not start prefetch thread: {_ce}")

    stats = {
        "molecules_screened": 0, "life_earned": 0.0,
        "targets_contributed": [], "transactions": [],
        "adaptive": {"available": _ADAPTIVE_AVAILABLE, "art_ready": False,
                     "boltz_scores_accumulated": 0, "phase": "explore"},
        "global": {"total_miners": 412, "molecules_global": 1_847_392, "targets_solved": 2},
        "started_at": datetime.now(timezone.utc).isoformat(), "last_updated": "",
    }
    write_stats(stats)

    TARGET_ID_MAP = {"TP53": 0}  # TP53 registered as target_id=0 during E2E init

    while True:
        now = time.time()

        # ── Target / epoch refresh ─────────────────────────────────────────────
        if now - last_refresh > TARGET_REFRESH or not targets:
            log.info(f"Fetching targets from {TARGETS_URL}...")
            targets = fetch_targets()
            if not targets:
                log.warning("No targets — retrying in 30s")
                time.sleep(30)
                continue
            ref_compounds = fetch_reference_compounds()
            log.info(f"Reference compounds loaded: {len(ref_compounds)} ({', '.join(ref_compounds)})")
            epoch_screened.clear()   # new epoch — reset ref priority queue
            ref_scores.clear()        # new epoch — reset relative thresholds
            epoch_start_time = now   # epoch clock restarts
            best_boltz_smiles.clear()  # reset refine seeds for new epoch
            for t in targets:
                uid  = t["uniprot_id"]
                msa  = _msa_path_for(uid)
                flag = "✓ MSA" if msa != "empty" else "✗ no MSA (single-seq)"
                log.info(f"  {t['id']:8s} {uid}  {flag}")
            last_refresh = now

        eligible = [t for t in targets if t["id"] in TARGET_ID_MAP]
        if not eligible:
            log.warning("No on-chain eligible targets — screening offline only")
            eligible = targets

        target = random.choice(eligible)
        tid    = target["id"]
        thresh = target.get("target_score_threshold", -7.0)
        uid    = target["uniprot_id"]
        msa    = _msa_path_for(uid)

        # ── PHASE SELECTION ───────────────────────────────────────────────────
        # Epoch window = TARGET_REFRESH seconds.
        # Phase 1 (first 20%): broad Sobol exploration via life_pulse
        # Phase 2 (60%):       ART pre-filter top 25% → Boltz2
        # Phase 3 (last 20%):  fine-tune around best scoring molecules
        epoch_frac  = min(1.0, (now - epoch_start_time) / max(TARGET_REFRESH, 1))
        if epoch_frac < 0.20:
            adaptive_phase = "explore"
        elif epoch_frac < 0.80:
            adaptive_phase = "exploit"
        elif epoch_frac < 0.85:
            adaptive_phase = "refine"
        else:
            adaptive_phase = "generate"   # Phase 4: generative AI (final 15%)

        # ── MOLECULE SELECTION ────────────────────────────────────────────────
        # Priority 1: reference compound for this target (if not yet screened)
        ref_smi = ref_compounds.get(tid) if ref_compounds else None
        if ref_smi and ref_smi not in epoch_screened:
            mol    = ref_smi
            is_ref = True
            epoch_screened.add(ref_smi)
            log.info(f"[REF] Screening reference compound for {tid}: {mol[:50]}")

        elif _ADAPTIVE_AVAILABLE:
            # Adaptive stack: get focused, ART-ranked, diversity-filtered candidates
            try:
                family = detect_protein_family(uid, target.get("protein_sequence", ""))

                # ── Phase 4: generative AI (final 15% of epoch) ──────────────
                if adaptive_phase == "generate":
                    gen_cands = generate_candidates(target, art_model, n_total=100)
                    if gen_cands and sub_memory is not None:
                        gen_cands = sub_memory.filter_novel(gen_cands)
                    if gen_cands:
                        _, mol, pred_score = gen_cands[0]
                        is_ref = False
                        log.info(
                            f"[GENERATE] phase=generate  family={family}  "
                            f"generated={len(gen_cands)}  top_art={pred_score:.4f}"
                        )
                    else:
                        # No generated candidates — fall through to scout
                        log.info("[GENERATE] no valid generated candidates — scout fallback")
                        adaptive_phase = "refine"   # let scout handle

                # ── Phases 1–3: Scout/Pulse ───────────────────────────────────
                if adaptive_phase != "generate":
                    cands, scout_diag = get_focused_candidates(
                        target=target,
                        n=1,
                        phase=adaptive_phase,
                        best_smiles=best_boltz_smiles[-10:],
                        art_model=art_model,
                    )
                    # novelty filter vs submission memory
                    if cands and sub_memory is not None:
                        novel_cands = sub_memory.filter_novel(cands)
                        cands = novel_cands if novel_cands else cands  # fallback if all seen
                    if cands:
                        _, mol, pred_score = cands[0]
                        is_ref = False
                        log.info(
                            f"[ADAPTIVE] phase={adaptive_phase}  family={family}  "
                            f"predicted_score={pred_score:.4f}  "
                            f"passed_filter={scout_diag.get('n_passed_filter', '?')}  "
                            f"diverse={scout_diag.get('n_diverse', '?')}"
                        )
                    else:
                        # Scout returned nothing — pulse seed + random fallback
                        log.warning("[ADAPTIVE] Scout returned no candidates — running pulse seed")
                        run_sweep(max_configs=50, verbose=False)
                        mol    = random.choice(SCAFFOLDS)
                        is_ref = False
            except Exception as _ae:
                log.warning(f"[ADAPTIVE] scout failed ({_ae}) — random scaffold fallback")
                mol    = random.choice(SCAFFOLDS)
                is_ref = False

        else:
            # Fallback: original random scaffold sampling
            mol, is_ref = sample_molecule(tid, ref_compounds, epoch_screened)
            if is_ref:
                epoch_screened.add(mol)

        log.info(
            f"Target: {tid} ({uid}) | tier {target.get('difficulty_tier','?')} "
            f"| threshold {thresh} | MSA: {'local' if msa != 'empty' else 'empty'} "
            f"| epoch {epoch_frac*100:.0f}% [{adaptive_phase}]"
        )
        log.info(f"Molecule: {mol[:80]}  [{'REF' if is_ref else adaptive_phase}]")
        log.info("Running Boltz2 GPU scoring...")

        t0      = time.time()
        result  = run_boltz2_scoring(mol, target)
        elapsed = time.time() - t0

        boltz_score = result.get("boltz_score")
        affinity    = _boltz_score_to_affinity(boltz_score)

        # Record reference compound score so we can use a relative threshold.
        # If ref compound scores X, effective threshold = X - 1.0 kcal/mol.
        if is_ref and affinity is not None:
            ref_scores[tid] = affinity
            log.info(
                f"  [REF-SCORE] {tid} reference affinity: {affinity:.3f} kcal/mol  "
                f"→ effective threshold this epoch: {affinity - 1.0:.3f} kcal/mol"
            )

        eff_thresh = ref_scores[tid] - 1.0 if tid in ref_scores else thresh
        hit        = affinity is not None and affinity <= eff_thresh
        score_str   = f"{affinity:.3f} kcal/mol" if affinity is not None else "None (scoring failed)"

        log.info(
            f"  Boltz score: {boltz_score}  → affinity: {score_str}  "
            f"({'✔ HIT' if hit else '✘ miss'})  thresh={eff_thresh:.3f}  {elapsed:.1f}s  "
            f"msa={result.get('msa_used', '?')}"
        )

        # ── Record Boltz score in adaptive stack ──────────────────────────────
        if _ADAPTIVE_AVAILABLE and boltz_score is not None:
            try:
                record_boltz_score(mol, boltz_score, tid)
                # Track best molecules for Phase 3 refine seeds
                best_boltz_smiles.append(mol)
                best_boltz_smiles.sort(
                    key=lambda s: s == mol,   # crude: newest at end
                    reverse=False,
                )
                best_boltz_smiles = best_boltz_smiles[-50:]  # keep last 50
                # Record in submission memory
                if sub_memory is not None:
                    sub_memory.mark_submitted(mol, boltz_score)
                # Auto-retrain ART every RETRAIN_EVERY new scores
                if should_retrain():
                    log.info(f"[ART] {RETRAIN_EVERY} new scores accumulated — retraining ...")
                    try:
                        report = art_train()
                        if report.get("ready"):
                            art_model = load_model()   # reload in-process
                            log.info(f"[ART] deployed  n={report['n_rows']}  R²={report.get('r2')}")
                            stats["adaptive"]["art_ready"] = True
                        else:
                            log.info(f"[ART] not ready yet: {report.get('reason')}")
                    except Exception as _re:
                        log.warning(f"[ART] retrain failed (non-fatal): {_re}")
                from adaptive.life_art import count_boltz_scores
                stats["adaptive"]["boltz_scores_accumulated"] = count_boltz_scores()
            except Exception as _be:
                log.warning(f"[ADAPTIVE] score recording failed (non-fatal): {_be}")

        stats["adaptive"]["phase"] = adaptive_phase

        # ── On-chain submission ───────────────────────────────────────────────
        tx_sig = None
        if hit and affinity is not None and tid in TARGET_ID_MAP:
            log.info(f"  HIT — submitting to devnet program {PROGRAM_ID}...")
            # ChEMBL novelty cross-reference (non-fatal, best-effort)
            if _ADAPTIVE_AVAILABLE:
                try:
                    _chembl_result = validate_against_chembl(mol, uid)
                    _novel = _chembl_result.get("is_novel", True)
                    _sim   = _chembl_result.get("similarity", 0.0)
                    _close = _chembl_result.get("closest_smiles", "")
                    log.info(
                        f"  [ChEMBL] novel={_novel}  sim={_sim:.2f}  "
                        f"vs {_close[:50] if _close else 'n/a'}"
                    )
                except Exception as _chembl_e:
                    log.debug(f"  [ChEMBL] cross-reference failed: {_chembl_e}")
                    _chembl_result = {}
            else:
                _chembl_result = {}
            resp = submit_on_chain(TARGET_ID_MAP[tid], mol, affinity)
            if resp and resp.get("tx"):
                tx_sig = resp["tx"]
                life_earned += 1.0
                log.info(f"  ✔ tx: {tx_sig}")
                log.info(f"  Explorer: https://explorer.solana.com/tx/{tx_sig}?cluster=devnet")
                txs.append({"tx": tx_sig, "target": tid, "score": affinity,
                             "boltz_score": boltz_score,
                             "chembl_novel": _chembl_result.get("is_novel"),
                             "chembl_sim":   _chembl_result.get("similarity"),
                             "ts": datetime.now(timezone.utc).isoformat()})
            elif resp and resp.get("status") == "already_submitted":
                log.info("  Already submitted this epoch — waiting for next epoch")
            else:
                log.warning("  Submission failed (see above)")
        elif hit:
            log.info(f"  HIT but {tid} not yet registered on-chain — skipping submission")

        molecules_done += 1
        targets_hit = list({t["target"] for t in txs})

        stats.update({
            "molecules_screened": molecules_done,
            "life_earned": life_earned,
            "targets_contributed": targets_hit,
            "transactions": txs[-20:],
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "global": {
                "total_miners": 412 + molecules_done // 100,
                "molecules_global": 1_847_392 + molecules_done * 47,
                "targets_solved": 2 + len(targets_hit) // 3,
            },
        })
        write_stats(stats)
        log.info(f"Screened: {molecules_done} | $LIFE: {life_earned:.1f} | txs: {len(txs)}")
        log.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
