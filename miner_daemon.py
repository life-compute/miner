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

# ── Reward-decay similarity functions (CRISPR: live; generate: log-only) ─────
_REWARD_DECAY_AVAILABLE = False
_rds_tanimoto          = None
_rds_grna_sim          = None
_rds_sim_mult          = None
_rds_sim_band          = None
_rds_find_crispr_parent = None
_rds_gen_parents:       dict = {}
_rds_pulse_parents:     dict = {}
_rds_crispr_history:    dict = {}
try:
    import importlib.util as _ilu
    _rds_path = Path(__file__).resolve().parent / "scripts" / "reward_decay_sim.py"
    _rds_spec = _ilu.spec_from_file_location("reward_decay_sim", _rds_path)
    if _rds_spec and _rds_spec.loader:
        _rds_mod = _ilu.module_from_spec(_rds_spec)
        _rds_spec.loader.exec_module(_rds_mod)  # type: ignore[union-attr]
        _rds_tanimoto           = _rds_mod.morgan_tanimoto
        _rds_grna_sim           = _rds_mod.grna_similarity
        _rds_sim_mult           = _rds_mod.similarity_multiplier
        _rds_sim_band           = _rds_mod.similarity_band
        _rds_find_crispr_parent = _rds_mod._find_crispr_parent
        _rds_gen_parents        = _rds_mod._load_generate_parents()
        _rds_pulse_parents      = _rds_mod._load_pulse_parents()
        _rds_crispr_history     = _rds_mod._load_crispr_history()
        _REWARD_DECAY_AVAILABLE = True
except Exception as _rds_err:
    pass   # non-fatal; decay disabled, full rewards paid


def _apply_crispr_decay(logger, target_id: str, grna_seq: str, base_reward: float) -> float:
    """
    Apply Hamming-proxy similarity decay to a crispr_generated HIT.

    Looks up the closest prior submitted gRNA for target_id by Hamming distance
    (proxy for the mutation parent — exact parent_seq tracking is a future
    improvement, see life_crispr.py _mutate_20mer callers).

    Brackets (same thresholds as the validated scheme):
      similarity >= 0.85  →  0.35× base  (near-clone of proven hotspot)
      similarity >= 0.70  →  0.65× base  (close neighbour)
      similarity <  0.70  →  1.00× base  (genuinely novel, full reward)
      no prior seqs found →  1.00× base  (benefit of the doubt)

    generate / zinc15 / ref sources are completely unaffected.
    Returns the actual $LIFE amount to credit.
    """
    if not _REWARD_DECAY_AVAILABLE:
        return base_reward
    try:
        parent  = _rds_find_crispr_parent(grna_seq, target_id, _rds_crispr_history)   # type: ignore[misc]
        sim     = _rds_grna_sim(grna_seq, parent) if parent else None                  # type: ignore[misc]
        mult    = _rds_sim_mult(sim)                                                    # type: ignore[misc]
        earned  = round(base_reward * mult, 4)
        band    = _rds_sim_band(sim)                                                    # type: ignore[misc]
        sim_str = f"{sim:.4f}" if sim is not None else "N/A (no prior seqs)"
        par_str = (parent or "")[:25]
        logger.info(
            f"  [REWARD-DECAY] CRISPR target={target_id}  "
            f"parent={par_str}  similarity={sim_str}  band={band}  "
            f"base={base_reward}  earned={earned}"
        )
        return earned
    except Exception as _rde:
        logger.debug(f"  [REWARD-DECAY] error (fallback full reward): {_rde}")
        return base_reward


def _log_reward_decay_sim(
    logger,
    target_id:      str,
    source:         str,
    mol:            str,
    parent:         "str | None",
    current_reward: float,
) -> None:
    """
    Log-only decay diagnostic for generate/mutant sources.
    DOES NOT change any payout — generate is confirmed novel (mean sim=0.31).
    CRISPR decay is now live via _apply_crispr_decay(); this function is
    retained for generate/mutant diagnostic logging only.
    zinc15, ref, and proteinnet sources are completely unaffected.
    """
    if not _REWARD_DECAY_AVAILABLE:
        return
    try:
        sim             = _rds_tanimoto(mol, parent) if parent else None   # type: ignore[misc]
        mult            = _rds_sim_mult(sim)                               # type: ignore[misc]
        proposed_reward = round(current_reward * mult, 4)
        band            = _rds_sim_band(sim)                               # type: ignore[misc]
        sim_str         = f"{sim:.4f}" if sim is not None else "N/A"
        par_str         = (parent or "")[:60]
        logger.info(
            f"  [REWARD-DECAY-SIM] target={target_id}  parent={par_str}  "
            f"similarity={sim_str}  band={band}  "
            f"current_reward={current_reward}  proposed_reward={proposed_reward}  "
            f"(LOG ONLY — generate confirmed novel, no decay applied)"
        )
    except Exception as _rde:
        logger.debug(f"  [REWARD-DECAY-SIM] non-fatal error: {_rde}")


# ── CRISPR gRNA optimizer ─────────────────────────────────────────────────────
try:
    from adaptive.life_crispr import (
        pick_grna                  as crispr_pick_grna,
        generate_grna_candidates   as crispr_generate_candidates,
        score_grna                 as crispr_score_grna,
        HOTSPOT_GRNAS              as _CRISPR_HOTSPOT_GRNAS,
        CRISPR_TARGETS             as _CRISPR_TARGETS,
        CRISPR_TARGET_ID_MAP       as _CRISPR_TARGET_ID_MAP,
        CrisprDeduplicationHistory as _CrisprDeduplicationHistory,
        CRISPR_DEDUP_WINDOW        as _CRISPR_DEDUP_WINDOW,
    )
    _CRISPR_AVAILABLE = True
except Exception as _crispr_err:
    _CRISPR_AVAILABLE = False
    _CRISPR_TARGETS       = []
    _CRISPR_TARGET_ID_MAP = {}
    crispr_generate_candidates = None   # type: ignore[assignment]
    crispr_score_grna          = None   # type: ignore[assignment]
    _CRISPR_HOTSPOT_GRNAS      = {}     # type: ignore[assignment]

# ── CRISPR-Net — per-target ML pre-screener ───────────────────────────────────
try:
    from adaptive.life_crispr_net import (
        train_all        as _cnet_train_all,
        pre_screen       as _cnet_pre_screen,
        get_model_report as _cnet_get_report,
        should_retrain   as _cnet_should_retrain,
    )
    _CNET_AVAILABLE = True
except Exception as _cnet_err:
    _CNET_AVAILABLE = False
    _cnet_train_all = _cnet_pre_screen = _cnet_get_report = _cnet_should_retrain = None  # type: ignore[assignment]
_CNET_MIN_R2 = 0.5   # minimum R² for CRISPR-Net pre-screening to activate

# ── CRISPR Boltz2 GPU scorer ───────────────────────────────────────────────────
try:
    from adaptive.life_crispr_boltz import build_crispr_boltz_input_yaml
    _CRISPR_BOLTZ_AVAILABLE = True
except Exception as _crispr_boltz_err:
    _CRISPR_BOLTZ_AVAILABLE = False

    def build_crispr_boltz_input_yaml(grna_20mer: str) -> str:  # type: ignore[misc]
        raise RuntimeError("life_crispr_boltz not available")

# ── mRNA Boltz2 GPU scorer ─────────────────────────────────────────────────────
try:
    from adaptive.life_mrna_boltz import (
        build_mrna_boltz_input_yaml,
        parse_mrna_boltz_affinity,
    )
    _MRNA_BOLTZ_AVAILABLE = True
except Exception as _mrna_boltz_err:
    _MRNA_BOLTZ_AVAILABLE = False

    def build_mrna_boltz_input_yaml(rna_sequence: str, smiles: str) -> str:  # type: ignore[misc]
        raise RuntimeError("life_mrna_boltz not available")

    def parse_mrna_boltz_affinity(*_a, **_kw) -> None:  # type: ignore[misc]
        return None

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

# ── LIFE-BRAIN gateway — network-wide self-learning pre-screener ──────────────
# ISOLATION: only life_brain_gateway.py is imported here.
# life_brain.py (training) runs in the separate 'life-brain' PM2 process.
# CUDA_VISIBLE_DEVICES="" is enforced inside life_brain_gateway.py at import time.
try:
    from adaptive.life_brain_gateway import (
        is_trusted          as _lbrain_trusted,
        pre_screen_smiles   as _lbrain_pre_screen_smiles,
        pre_screen_crispr   as _lbrain_pre_screen_crispr,
        get_report          as _lbrain_get_report,
    )
    _LBRAIN_AVAILABLE = True
except Exception as _lbe:
    _LBRAIN_AVAILABLE = False
    _lbrain_trusted = _lbrain_pre_screen_smiles = _lbrain_pre_screen_crispr = _lbrain_get_report = None  # type: ignore[assignment]

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
MRNA_SCHEDULE_WEIGHT  = 2           # mRNA picks per protein pick (2 → P M M, 3 → P M M M)

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

# ── Discovery NFT ─────────────────────────────────────────────────────────────
DISCOVERY_NFT_JS     = WORK_DIR / "scripts" / "mint_discovery_nft.js"
DISCOVERY_REGISTRY   = WORK_DIR / "output" / "discoveries.json"
DISCOVERY_FOUNDATION = "2jVdMx7fb88txbG6YoZzC7kT4Tq8rJDaWrNgbZ3ZnqCb"
DISCOVERY_PERCENTILE = 0.10   # top-10% affinity for that target qualifies

# ── Boltz2 / nova paths ───────────────────────────────────────────────────────
NOVA_DIR   = Path("/mnt/minos-drive/nova_subnet")
NOVA_VENV  = NOVA_DIR / ".venv" / "bin" / "python"
MSA_DIR    = Path("/mnt/minos-drive/life-compute-miner/data/msa_files")
BOLTZ_SEED = 68   # included in on-chain submission so validators reproduce the score

# Minimum combined gRNA score required before submitting on-chain.
# combined = on_target × off_target × delivery  (range 0–1.1).
#
# Option B (2026-08-27): replace analytical combined with iptm × delivery.
#
# Rationale: combined = on_target × off_target × delivery was calibrated when
# the combined score also determined the analytical affinity estimate.  Since
# Boltz2 GPU inference now sets affinity independently, combined and Boltz2
# affinity are orthogonal (measured r = −0.026 across 1,120 Boltz2 HITs).
# on_target is frozen at ~0.525 for all novel gRNAs (not near known hotspots),
# making combined effectively a one-dimensional delivery gate with extra noise.
#
# New formula: quality_score = iptm × delivery
#   iptm (0–1): Boltz2 interface predicted TM-score — structural binding quality
#   delivery (0.4 / 0.7 / 1.0): GC content suitability — same as before
#   Falls back to combined (analytical) when model="analytical" (iptm unavailable).
#
# Default threshold 0.45 applies to all targets unless overridden below.
# Per-target overrides are calibrated from historical data (life_boltz_scores ×
# life_crispr_scores join, n≈940–960 matched hits per target, 2026-08-28):
#
#   CDK4 → 0.300: CDK4 gRNAs cluster at GC-content boundary → delivery=0.40 (floor)
#     → quality_score = iptm×0.40 ≈ 0.33 for strong hits. 153/906 confirmed hits
#     (16.9%) were blocked at 0.45. miss_max=0.400; delivery gate (0.6) already
#     screens out-of-range-GC sequences independently.
#
#   MYC  → 0.320: MYC has only 7 rejects in history (miss_max=0.273). 20/915
#     confirmed hits were blocked at 0.45. Clean gap of 0.30+ above miss ceiling.
CRISPR_MIN_COMBINED: float = 0.45
CRISPR_MIN_COMBINED_BY_TARGET: dict[str, float] = {
    # Per-target overrides — targets not listed use CRISPR_MIN_COMBINED (0.45).
    "MYC_CRISPR":  0.320,  # 20/915 hits recovered; miss_max=0.273, gap=+0.047
    "CDK4_CRISPR": 0.300,  # 153/906 hits recovered; delivery-floor mechanism
}

# Minimum delivery score (Score 3) required before submitting on-chain.
# delivery = 0.4 for gc < 0.30 or gc > 0.80 (out-of-range GC).
# delivery = 0.5 if out-of-range GC but has stem-loop bonus.
# delivery = 0.7 for gc 30–80% (normal range).
# 0.6 sits between 0.5 and 0.7, blocking all out-of-range-GC sequences
# (with or without stem-loop bonus) while allowing normal-GC sequences through.
CRISPR_MIN_DELIVERY: float = 0.6

# GPU mutex — serialises Boltz2 inference between the main scoring loop and the
# CRISPR background thread.  Both sides acquire before any GPU call and release
# immediately after.  CRISPR analytical candidate generation (CPU, ~0.1 s) is
# unaffected.  Prevents concurrent 6.8 GB (CRISPR) + 1.6–14 GB (protein/mRNA)
# allocations that silently OOM the RTX 5060's 8 GB VRAM.
_GPU_LOCK = threading.Lock()

# Maximum on-chain CRISPR submissions per epoch across all targets combined.
# Prevents spamming the chain when all 10 targets repeatedly return the same hotspots.
# Resets each time the on-chain epoch number advances.
CRISPR_MAX_SUBMISSIONS_PER_EPOCH: int = 3

# ── Tokenomics constants (mirrors constants.rs) ───────────────────────────────
# Initial rewards per tier before any epoch-based halving.
REWARD_EASY_LIFE:   float = 1.0
REWARD_MEDIUM_LIFE: float = 5.0
REWARD_HARD_LIFE:   float = 25.0
REWARD_CRISPR_LIFE: float = 7.0   # gRNA knockout targets (CPU-scored)
REWARD_MRNA_LIFE:   float = 25.0  # mRNA silencing targets
HALVING_INTERVAL:   int   = 210_000  # epochs per halving (~1 yr on mainnet)

def current_epoch_reward(base_life: float, epoch: int) -> float:
    """Return the current effective reward after epoch-based halving.

    Mirrors rewards.rs Layer 0: base >> (epoch // HALVING_INTERVAL), min 1 raw unit.
    Result is in $LIFE (not raw units).  Supply/hit halvings are on-chain only.
    """
    halvings = epoch // HALVING_INTERVAL
    return max(base_life / (2 ** halvings), 1 / 1_000_000)  # 1 raw unit floor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-miner")

# ── ZINC15 random sampling (default strategy) ─────────────────────────────────
ZINC15_FRAGMENTS = WORK_DIR / "data" / "zinc15_fragments.smi"
_zinc_cache: list[str] = []

# ── ProteinNet epoch-level pre-screen settings ────────────────────────────────
_PNET_SCREEN_N = 10_000   # ZINC15 candidates to evaluate per epoch
_PNET_TOP_K    = 10       # top predicted binders forwarded to Boltz2
_PNET_MIN_R2   = 0.5      # minimum model R² required to trust pre-screening
_pnet_pools: dict[str, list[str]] = {}   # per-target candidate queue (refilled when empty)


def _pnet_fill_pool(tid: str) -> None:
    """Pre-screen _PNET_SCREEN_N ZINC15 candidates for *tid* and cache top _PNET_TOP_K.

    No-op when ProteinNet is unavailable, ZINC15 library not loaded, or the
    per-target model has R² < _PNET_MIN_R2 (not yet trustworthy).
    """
    if not (_PNET_AVAILABLE and _zinc_cache
            and _pnet_pre_screen is not None and _pnet_get_report is not None):
        return
    try:
        report = _pnet_get_report(tid)
        r2 = float(report.get("r2", 0.0)) if report else 0.0
        if r2 < _PNET_MIN_R2:
            log.debug(f"[PROTEINNET] {tid} R²={r2:.2f} < {_PNET_MIN_R2} — skipping pre-screen (model not ready)")
            return
        sample = random.sample(_zinc_cache, min(_PNET_SCREEN_N, len(_zinc_cache)))
        # LIFE-BRAIN: if the protein branch is trusted, use it instead of ProteinNet
        if (
            _LBRAIN_AVAILABLE
            and _lbrain_trusted is not None
            and _lbrain_pre_screen_smiles is not None
            and _lbrain_trusted("protein")
        ):
            lb_result = _lbrain_pre_screen_smiles(sample, int(tid.split("_")[0]) if isinstance(tid, str) and tid[0].isdigit() else 0, "protein", top_n=_PNET_TOP_K)
            if lb_result is not None:
                _pnet_pools[tid] = list(lb_result)
                log.info(f"[LIFE-BRAIN] protein branch pre-screen: {len(sample):,} → top {len(lb_result)} for {tid}")
                return
        candidates = _pnet_pre_screen(sample, tid, top_n=_PNET_TOP_K)
        _pnet_pools[tid] = list(candidates)
        log.info(f"[PROTEINNET] Pre-screened {len(sample):,} → top {len(candidates)} for {tid} (R²={r2:.2f})")
    except Exception as _e:
        log.debug(f"[PROTEINNET] fill_pool failed for {tid} (non-fatal): {_e}")


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

    # ── ProteinNet pre-screen pool (built once per epoch, drained one call at a time) ──
    # Refill when the per-target queue is empty; _pnet_fill_pool() gates on R² ≥ 0.5.
    # For large proteins the per-call pre-screen (smaller n) still runs as a fallback
    # inside the pool-fill path via the same _pnet_fill_pool helper.
    if _PNET_AVAILABLE and _zinc_cache and not _pnet_pools.get(tid):
        _pnet_fill_pool(tid)

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
                    # LIFE-BRAIN: use protein branch if trusted, fall back to ProteinNet
                    if (
                        _LBRAIN_AVAILABLE and _lbrain_trusted is not None
                        and _lbrain_pre_screen_smiles is not None
                        and _lbrain_trusted("protein")
                    ):
                        try:
                            _tid_int = int(str(tid).split("_")[0]) if str(tid)[0].isdigit() else 0
                            lb_filtered = _lbrain_pre_screen_smiles(gen_smiles, _tid_int, "protein", top_n=len(gen_smiles))
                            if lb_filtered is not None:
                                filtered = lb_filtered
                                log.debug(f"[LIFE-BRAIN] Phase 4 filter: {len(gen_smiles)} → {len(filtered)} for {tid}")
                            else:
                                filtered = _pnet_pre_screen(gen_smiles, tid, top_n=len(gen_smiles))
                        except Exception:
                            filtered = _pnet_pre_screen(gen_smiles, tid, top_n=len(gen_smiles))
                    else:
                        filtered    = _pnet_pre_screen(gen_smiles, tid, top_n=len(gen_smiles))
                        log.debug(f"[PROTEINNET] Phase 4 filter: {len(gen_smiles)} → {len(filtered)} for {tid}")
                    filtered_set = set(filtered)
                    gen_cands   = [(l, s, sc) for l, s, sc in gen_cands if s in filtered_set]
                except Exception as _gpf:
                    log.debug(f"[PROTEINNET] Phase 4 filter failed (non-fatal): {_gpf}")
            novel = sub_memory.filter_novel(gen_cands) if gen_cands else []
            if novel:
                _, smi, _ = novel[0]
                return smi, "generate"
        except Exception as _ge:
            log.debug(f"[GENERATE] failed (non-fatal): {_ge}")

    # ── Priority 3: ProteinNet-pre-screened ZINC15 (best predicted binders first)
    # Pop from the epoch-level queue; when exhausted, _pnet_fill_pool() will
    # refill on the next call (triggers next epoch's 10 000-candidate sweep).
    _pool = _pnet_pools.get(tid, [])
    if _pool:
        if sub_memory is not None:
            _novel_pnet = sub_memory.filter_novel(_pool)
            if _novel_pnet:
                mol_pnet = _novel_pnet[0]
                _pnet_pools[tid] = [s for s in _pool if s != mol_pnet]
                return mol_pnet, "proteinnet"
        mol_pnet = _pnet_pools[tid].pop(0)
        return mol_pnet, "proteinnet"

    # ── Priority 4: random ZINC15 sample (always works)
    return _sample_zinc15(), "zinc15"


# ── Solana RPC helpers ────────────────────────────────────────────────────────
_DISC_TARGET = bytes([140, 246, 247, 200, 198, 220,  24, 250])
_DISC_MINER  = bytes([232, 196,  79, 139, 222, 213, 161,  99])
# ResultSubmission discriminator: sha256("account:ResultSubmission")[:8]
_DISC_RESULT = bytes([0xd6, 0x73, 0xa5, 0x67, 0x43, 0xd3, 0x2f, 0x58])
_NETWORK_STATS_CACHE: dict = {}
_NETWORK_STATS_TTL   = 120

# NetworkConfig PDA — seeds: [b"network_config"], program 74RHjg1z…
# Derived from: PublicKey.findProgramAddressSync([Buffer.from("network_config")], PROG_ID)
# Confirmed via life_submit.js logs: "networkConfig PDA: BgW8KxfMmEEDPwuQiXUBdUATXqtSVT3TYDhf9qXDpbrt"
# Old stale value "3cp9veeRTsqnXWSJYw2jqhRVeeKcaEkp4Pb2md9GJXPi" was from a prior deployment
# and froze at epoch=1, causing the CRISPR thread to get stuck after 3/3 submissions.
_NETWORK_CONFIG_PDA = "BgW8KxfMmEEDPwuQiXUBdUATXqtSVT3TYDhf9qXDpbrt"

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


# ── Boltz2 CRISPR ternary-complex scoring ────────────────────────────────────
_CRISPR_BOLTZ_HELPER = """\
import sys, json, hashlib, time, shutil, os, tempfile
from pathlib import Path

sys.path.insert(0, "{nova_dir}")

from boltz.main import predict

args     = json.loads(sys.argv[1])
grna_seq = args["grna_seq"]
mol_id   = args["mol_id"]
in_dir   = Path(args["in_dir"])
out_dir  = Path(args["out_dir"])

try:
    predict(
        data=str(in_dir),
        out_dir=str(out_dir),
        recycling_steps=1,
        sampling_steps=25,
        diffusion_samples=1,
        output_format="mmcif",
        seed=args.get("seed", 68),
        override=True,
        num_workers=0,
        no_kernels=True,
    )
    result = {{"ok": True}}
except Exception as e:
    result = {{"ok": False, "error": str(e)}}

print(json.dumps(result))
"""

def run_boltz2_crispr_scoring(grna_seq: str) -> dict:
    """
    Run Boltz2 GPU inference on a single CRISPR gRNA using the 3-chain ternary complex
    (SpCas9 REC1 200aa, full sgRNA 96nt, target protospacer 23nt).

    Returns dict with:
        boltz_score     float | None   (= iptm; higher = better predicted complex)
        affinity_kcal   float | None   (−6.0 − 3.0 × iptm, kcal/mol-like range −6…−9)
        iptm            float | None   (interface predicted TM-score, 0–1)
        ptm             float | None
        confidence_score float | None
        model           str            "boltz2-gpu-crispr"
        error           str | None     (set on failure)

    Mirrors run_boltz2_scoring() subprocess pattern.
    Fallback to None on any error — caller uses analytical score in that case.
    """
    import hashlib as _hashlib
    import shutil as _shutil

    if not _CRISPR_BOLTZ_AVAILABLE:
        return {"boltz_score": None, "affinity_kcal": None, "model": "boltz2-gpu-crispr",
                "error": "life_crispr_boltz not available"}

    # Deterministic mol_id from gRNA sequence
    mol_id = int(_hashlib.sha256(grna_seq.encode()).hexdigest()[:8], 16) % (2**31 - 1)

    # Create temp dirs for this run
    with tempfile.TemporaryDirectory(prefix="life-crispr-boltz-") as tmp_root:
        tmp_path = Path(tmp_root)
        in_dir   = tmp_path / "inputs"
        out_dir  = tmp_path / "outputs"
        in_dir.mkdir()
        out_dir.mkdir()

        # Write 3-chain YAML
        try:
            yaml_content = build_crispr_boltz_input_yaml(grna_seq)
            yaml_path = in_dir / f"{mol_id}_crispr.yaml"
            yaml_path.write_text(yaml_content)
        except Exception as e:
            return {"boltz_score": None, "affinity_kcal": None, "model": "boltz2-gpu-crispr",
                    "error": f"YAML build failed: {e}"}

        # Write and run helper script
        helper_src = _CRISPR_BOLTZ_HELPER.format(nova_dir=str(NOVA_DIR))
        args_json  = json.dumps({
            "grna_seq": grna_seq, "mol_id": mol_id,
            "in_dir": str(in_dir), "out_dir": str(out_dir),
            "seed": BOLTZ_SEED,
        })
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                         prefix="life-crispr-helper-") as f:
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
            log.warning(f"  [CRISPR-Boltz2] stderr: {r.stderr[-400:]}")
            return {"boltz_score": None, "affinity_kcal": None, "model": "boltz2-gpu-crispr",
                    "error": r.stderr[-200:]}

        # Parse subprocess result
        helper_ok = False
        for line in reversed(r.stdout.strip().splitlines()):
            try:
                hres = json.loads(line)
                if not hres.get("ok", False):
                    log.warning(f"  [CRISPR-Boltz2] predict() error: {hres.get('error')}")
                    return {"boltz_score": None, "affinity_kcal": None,
                            "model": "boltz2-gpu-crispr", "error": hres.get("error")}
                helper_ok = True
                break
            except Exception:
                continue

        if not helper_ok:
            log.warning(f"  [CRISPR-Boltz2] stdout unparseable: {r.stdout[-200:]}")
            return {"boltz_score": None, "affinity_kcal": None, "model": "boltz2-gpu-crispr",
                    "error": "stdout unparseable"}

        # Parse Boltz2 output from the predictions directory
        predictions_dir = out_dir / "boltz_results_inputs" / "predictions"
        if not predictions_dir.exists():
            log.warning("  [CRISPR-Boltz2] predictions dir missing")
            return {"boltz_score": None, "affinity_kcal": None, "model": "boltz2-gpu-crispr",
                    "error": "predictions dir missing"}

        from adaptive.life_crispr_boltz import parse_crispr_boltz_affinity
        parsed = parse_crispr_boltz_affinity(predictions_dir, mol_id, "crispr")
        if parsed is None:
            log.warning("  [CRISPR-Boltz2] affinity parse returned None")
            return {"boltz_score": None, "affinity_kcal": None, "model": "boltz2-gpu-crispr",
                    "error": "affinity parse None"}

        log.info(
            f"  [CRISPR-Boltz2] gRNA={grna_seq[:12]}… "
            f"iptm={parsed.get('iptm')} "
            f"ptm={parsed.get('ptm')} "
            f"confidence={parsed.get('confidence_score')} "
            f"kcal={parsed.get('affinity_kcal')}"
        )
        return {
            "boltz_score":      parsed.get("boltz_score"),   # = iptm
            "affinity_kcal":    parsed.get("affinity_kcal"),
            "iptm":             parsed.get("iptm"),
            "ptm":              parsed.get("ptm"),
            "confidence_score": parsed.get("confidence_score"),
            "model":            "boltz2-gpu-crispr",
            "error":            None,
        }


# ── Boltz2 mRNA scoring ────────────────────────────────────────────────────────

def run_boltz2_mrna_scoring(smiles: str, target: dict) -> dict:
    """
    Run Boltz2 GPU inference for a small molecule against an mRNA target region.

    Uses a 2-chain YAML:
        Chain A (rna)    : target rna_sequence from targets.json
        Chain B (ligand) : candidate SMILES

    Score: ipTM from confidence JSON (structure confidence, 0-1).
    Falls back gracefully if affinity JSON is present (Boltz2 RNA + ligand mode).
    Returns boltz_score = iptm, or None on failure.
    The caller converts via _boltz_score_to_affinity(), same as the protein path.

    Does NOT touch run_boltz2_scoring() or the protein/MSA path.
    """
    if not _MRNA_BOLTZ_AVAILABLE:
        log.warning("  [mRNA-Boltz2] life_mrna_boltz not available — skipping")
        return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                "error": "life_mrna_boltz not available"}

    rna_seq = target.get("rna_sequence", "")
    if not rna_seq:
        log.warning(f"  [mRNA-Boltz2] {target.get('id','?')} has no rna_sequence — skipping")
        return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                "error": "no rna_sequence in target"}

    import hashlib as _hashlib
    target_id = target["id"]
    mol_id    = int(_hashlib.sha256((smiles + target_id).encode()).hexdigest()[:8], 16) % (2**31 - 1)
    target_stem = target_id.lower().replace("_", "-")  # e.g. "myc-mrna"

    with tempfile.TemporaryDirectory(prefix="life-mrna-boltz-") as tmp_root:
        tmp_path = Path(tmp_root)
        in_dir   = tmp_path / "inputs"
        out_dir  = tmp_path / "outputs"
        in_dir.mkdir()
        out_dir.mkdir()

        # Write 2-chain YAML
        try:
            yaml_content = build_mrna_boltz_input_yaml(rna_seq, smiles)
            yaml_path    = in_dir / f"{mol_id}_{target_stem}.yaml"
            yaml_path.write_text(yaml_content)
        except Exception as e:
            log.warning(f"  [mRNA-Boltz2] YAML build failed: {e}")
            return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                    "error": f"YAML build failed: {e}"}

        # Reuse _CRISPR_BOLTZ_HELPER — it only calls boltz.main.predict(data, out_dir, ...)
        # which is chain-type-agnostic; the YAML content determines chain layout.
        helper_src  = _CRISPR_BOLTZ_HELPER.format(nova_dir=str(NOVA_DIR))
        args_json   = json.dumps({
            "grna_seq": smiles,   # field name is vestigial; value unused by helper
            "mol_id":   mol_id,
            "in_dir":   str(in_dir),
            "out_dir":  str(out_dir),
            "seed":     BOLTZ_SEED,
        })
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                        prefix="life-mrna-helper-") as f:
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
            log.warning(f"  [mRNA-Boltz2] stderr: {r.stderr[-400:]}")
            return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                    "error": r.stderr[-200:]}

        # Confirm predict() reported ok
        helper_ok = False
        for line in reversed(r.stdout.strip().splitlines()):
            try:
                hres = json.loads(line)
                if not hres.get("ok", False):
                    log.warning(f"  [mRNA-Boltz2] predict() error: {hres.get('error')}")
                    return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                            "error": hres.get("error")}
                helper_ok = True
                break
            except Exception:
                continue

        if not helper_ok:
            log.warning(f"  [mRNA-Boltz2] stdout unparseable: {r.stdout[-200:]}")
            return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                    "error": "predict() stdout unparseable"}

        # Parse output — affinity first, iptm fallback
        predictions_dir = out_dir / "boltz_results_inputs" / "predictions"
        parsed = parse_mrna_boltz_affinity(predictions_dir, mol_id, target_stem)
        if parsed is None:
            log.warning(f"  [mRNA-Boltz2] no parseable output for {target_id}")
            return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                    "error": "no output parsed"}

        log.info(f"  [mRNA-Boltz2] {target_id}  score={parsed['boltz_score']:.4f}"
                 f"  source={parsed['score_source']}"
                 f"  iptm={parsed.get('iptm')}")

        # Sanity guard: iptm must be in [0, 1]; affinity_kcal must be in [-9, -6].
        # Boltz2 can return out-of-range values on degenerate inputs; reject rather
        # than submit a physically invalid claim to the validator.
        iptm_val = parsed.get("iptm")
        aff_val  = parsed.get("affinity_kcal")
        if iptm_val is not None and not (0.0 <= iptm_val <= 1.0):
            log.warning(f"  [mRNA-Boltz2] {target_id} iptm={iptm_val:.4f} out of [0,1] — "
                        f"rejecting degenerate Boltz2 output")
            return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                    "error": f"iptm {iptm_val:.4f} out of valid range [0,1]"}
        if aff_val is not None and not (-9.5 <= aff_val <= -5.5):
            log.warning(f"  [mRNA-Boltz2] {target_id} affinity_kcal={aff_val:.4f} "
                        f"outside expected [-9.5,-5.5] — rejecting degenerate output")
            return {"boltz_score": None, "model": "boltz2-gpu-mrna",
                    "error": f"affinity_kcal {aff_val:.4f} outside valid range"}
        return {
            "boltz_score":   parsed["boltz_score"],
            "affinity_kcal": parsed.get("affinity_kcal"),
            "iptm":          parsed.get("iptm"),
            "score_source":  parsed.get("score_source"),
            "model":         "boltz2-gpu-mrna",
            "error":         None,
            "seed":          BOLTZ_SEED,
        }


# ── On-chain submission ───────────────────────────────────────────────────────
def submit_on_chain(target_id_num: int, smiles: str, affinity: float,
                    boltz_seed: int = BOLTZ_SEED,
                    molecule_type: str = "protein") -> dict | None:
    """Submit a result on-chain, trying seq slots 0→1→2 until one is free.

    The on-chain program allows MAX_SUBMISSIONS_PER_EPOCH=3 per miner per epoch
    (seq 0, 1, 2 each derive a distinct resultSubmission PDA).  Always sending
    seq=0 means slot 0 is filled by the first HIT and every subsequent call
    returns already_submitted — even for a different target.  We retry seq=1
    then seq=2 so each epoch can record up to 3 distinct results.

    molecule_type: "protein" | "mRNA" | "CRISPR"
      - protein/mRNA: affinity is Boltz2 kcal/mol, boltz_seed identifies the run.
      - CRISPR: affinity is the combined three-score (on_target × off_target ×
        delivery), negated to satisfy the on-chain < 0.0 guard.  boltz_seed is
        0 (no GPU run).  The JS helper logs moleculeType so validators can route
        the submission to the correct scoring handler.
    """
    for seq in range(3):  # slots 0, 1, 2
        args = {
            "rpc": SOLANA_RPC, "authKeypair": AUTH_KEYPAIR,
            "minerKeypair": MINER_KEYPAIR, "idlPath": str(IDL_PATH),
            "programId": PROGRAM_ID, "targetIdNum": target_id_num,
            "smiles": smiles, "affinity": affinity, "boltzSeed": boltz_seed,
            "moleculeType": molecule_type,
            "seq": seq,
        }
        try:
            result = subprocess.run(
                ["node", str(ANCHOR_DIR / "life_submit.js"), json.dumps(args)],
                capture_output=True, text=True, timeout=120, cwd=str(ANCHOR_DIR))
            if result.returncode != 0:
                log.error(f"submit node FAILED seq={seq} (rc={result.returncode})")
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
            if parsed_resp is None:
                log.warning(f"submit stdout seq={seq} (no JSON found): {result.stdout.strip()[:500]}")
                return None
            # If this seq slot is already occupied, try the next one
            if parsed_resp.get("status") == "already_submitted":
                log.info(f"submit: seq={seq} occupied — trying seq={seq + 1}")
                continue
            return parsed_resp
        except subprocess.TimeoutExpired:
            log.error(f"submit timed out after 120s (seq={seq})")
            return None
        except Exception as e:
            log.error(f"submit exception seq={seq}: {e}")
            return None
    # All three slots occupied this epoch
    log.info("submit: all 3 seq slots occupied this epoch — result not submitted")
    return {"status": "already_submitted", "epoch": "?", "seq": "all_full"}


# ── Discovery NFT helpers ─────────────────────────────────────────────────────

def _discovery_top10_threshold(target_id: str, current_affinity: float) -> bool:
    """Return True if *current_affinity* is in the top-10% for *target_id*.

    Reads from output/life_boltz_scores.jsonl (all historical scores for that
    target, novel sources only).  Falls back to True when < 10 prior scores
    exist (first few hits always qualify on novelty alone).
    """
    boltz_jsonl = WORK_DIR / "output" / "life_boltz_scores.jsonl"
    is_crispr_target = target_id.endswith("_CRISPR")
    scores: list[float] = []
    try:
        with boltz_jsonl.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("target_id") != target_id:
                    continue
                if row.get("source") in ("ref", "reference"):
                    continue
                # Skip rows whose molecule_type doesn't match the target class
                # (prevents CRISPR gRNA affinities polluting protein percentiles
                # and stops gRNA strings from being fed to the SMILES parser)
                row_type = row.get("target_type", "protein")
                if is_crispr_target and row_type != "CRISPR":
                    continue
                if not is_crispr_target and row_type == "CRISPR":
                    continue
                aff = row.get("affinity")
                if isinstance(aff, (int, float)):
                    scores.append(float(aff))
    except FileNotFoundError:
        pass

    if len(scores) < 10:
        # Too few historical data points — first hits always qualify
        return True

    scores.sort()  # ascending: most negative = best binder = lowest index
    cutoff_idx = max(0, int(len(scores) * DISCOVERY_PERCENTILE) - 1)
    threshold  = scores[cutoff_idx]
    qualifies  = current_affinity <= threshold
    log.info(f"  [DISCOVERY] top-10% gate: affinity={current_affinity:.3f} "
             f"threshold={threshold:.3f} n={len(scores)} → {'PASS' if qualifies else 'FAIL'}")
    return qualifies


def _next_discovery_numbers(target_id: str, registry_path: Path) -> tuple[int, int]:
    """Return (global_discovery_number, per_target_rank)."""
    try:
        if registry_path.exists():
            reg = json.loads(registry_path.read_text())
            global_n = len(reg.get("discoveries", [])) + 1
            target_n = reg.get("target_counts", {}).get(target_id, 0) + 1
            return global_n, target_n
    except Exception:
        pass
    return 1, 1


def _maybe_mint_discovery_nft(
    smiles: str,
    affinity: float,
    target: dict,
    miner_wallet: str,
    validator_tx: str,
    source: str,
    auth_keypair_path: str,
    rpc: str,
    dry_run: bool = False,
    grna_scores: dict | None = None,
) -> dict | None:
    """Evaluate discovery eligibility and mint an NFT if all rules pass.

    Rules (all must be true):
      1. source is 'zinc15', 'zinc15-fallback', 'mutant', 'generate', 'pulse',
         or 'crispr_generated' (not 'ref' / 'reference')
      2. affinity is in the top-10% for this target (historical novel scores)
      3. SMILES / gRNA not already minted (checked inside the JS script)
      4. mint_discovery_nft.js is present

    grna_scores : for CRISPR sources, dict with on_target / off_target / delivery
                  scores to embed in NFT metadata.

    Returns the parsed result dict from the script, or None on error/skip.
    """
    tid = target.get("id", "UNKNOWN")
    uid = target.get("uniprot_id", "")
    protein_name = target.get("protein_name", tid)

    # Rule 1 — novel source only
    novel_sources = {"zinc15", "zinc15-fallback", "mutant", "generate", "pulse", "crispr_generated"}
    if source not in novel_sources:
        log.debug(f"  [DISCOVERY] skipping — source '{source}' not novel")
        return None

    # Rule 2 — top-10% gate
    if not _discovery_top10_threshold(tid, affinity):
        return None

    # Rule 3+4 — JS script exists
    if not DISCOVERY_NFT_JS.exists():
        log.warning("  [DISCOVERY] mint_discovery_nft.js not found — skipping NFT")
        return None

    global_n, target_rank = _next_discovery_numbers(tid, DISCOVERY_REGISTRY)
    ts = datetime.now(timezone.utc).isoformat()

    # Read the miner's public key from the keypair file (public-safe)
    try:
        _kp_data = json.loads(Path(auth_keypair_path).read_bytes())
        from base58 import b58encode  # type: ignore
        _pub = b58encode(bytes(_kp_data[32:64])).decode()
    except Exception:
        _pub = miner_wallet  # fallback to whatever was passed in

    is_crispr = source == "crispr_generated"
    args = {
        "rpc":              rpc,
        "authKeypair":      auth_keypair_path,
        "smiles":           smiles,
        "affinity":         affinity,
        "targetId":         tid,
        "targetName":       protein_name,
        "uniprotId":        uid,
        "minerWallet":      _pub,
        "validatorTx":      validator_tx,
        "timestamp":        ts,
        "discoveryRank":    target_rank,
        "discoveryNumber":  global_n,
        "foundationWallet": DISCOVERY_FOUNDATION,
        "registryPath":     str(DISCOVERY_REGISTRY),
        "dryRun":           dry_run,
        "cluster":          "devnet" if "devnet" in rpc else "mainnet-beta",
        # CRISPR-specific fields (ignored by JS for non-CRISPR sources)
        "isCrispr":          is_crispr,
        "geneName":          target.get("gene_name", tid.replace("_CRISPR", "")),
        "cancerIndication":   target.get("cancer_indication", ""),
        "grnaOnTarget":       (grna_scores or {}).get("on_target", 0.0),
        "grnaOffTarget":      (grna_scores or {}).get("off_target", 0.0),
        "grnaDelivery":       (grna_scores or {}).get("delivery", 0.0),
    }

    log.info(f"  [DISCOVERY] 🧬 minting NFT — global #{global_n}, "
             f"rank #{target_rank} for {tid}")
    try:
        _env = {**os.environ, "NODE_PATH": str(ANCHOR_DIR / "node_modules")}
        result = subprocess.run(
            ["node", str(DISCOVERY_NFT_JS), json.dumps(args)],
            capture_output=True, text=True, timeout=180,
            cwd=str(ANCHOR_DIR),
            env=_env,
        )
        if result.stderr.strip():
            log.debug(f"  [DISCOVERY] node stderr:\n{result.stderr.strip()[-1000:]}")

        parsed = None
        for line in reversed(result.stdout.strip().splitlines()):
            try:
                parsed = json.loads(line)
                break
            except Exception:
                continue

        if parsed:
            status = parsed.get("status")
            if status == "minted":
                log.info(f"  [DISCOVERY] ✔ NFT minted: {parsed.get('nft_name')}")
                log.info(f"  [DISCOVERY]   mint  : {parsed.get('mint_address')}")
                log.info(f"  [DISCOVERY]   tx    : {parsed.get('mint_tx')}")
                log.info(f"  [DISCOVERY]   {parsed.get('explorer')}")
            elif status == "duplicate":
                log.info(f"  [DISCOVERY] SMILES already minted — skipping")
            elif status == "dry_run":
                log.info(f"  [DISCOVERY] dry-run OK: {parsed.get('nft_name')}")
            else:
                log.warning(f"  [DISCOVERY] unexpected status: {parsed}")
        else:
            log.warning(f"  [DISCOVERY] no JSON in stdout (rc={result.returncode})")
            log.debug(f"  stdout: {result.stdout[:500]}")

        return parsed
    except subprocess.TimeoutExpired:
        log.error("  [DISCOVERY] mint timed out after 180s")
        return None
    except Exception as exc:
        log.error(f"  [DISCOVERY] mint exception: {exc}")
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
        # ── Protein targets (on-chain IDs 0-29) ──────────────────────────────
        "TP53":   0, "BRCA1":  1, "EGFR":   2, "HER2":   3, "KRAS":   4,
        "BCL2":   5, "CDK4":   6, "VEGFR2": 7, "PDL1":   8, "MDM2":   9,
        "BRAF":  10, "PTEN":  11, "MYC":   12, "STAT3": 13, "PIK3CA": 14,
        "MTOR":  15, "FGFR1": 16, "RET":   17, "AR":    18, "NTRK1":  19,
        "IDH1":  20, "FLT3":  21, "SMAD4": 22, "APC":   23, "PARP1":  24,
        "JAK2":  25, "ESR1":  26, "HDAC1": 27, "HDAC2": 28, "ABL1":   29,
        # ── mRNA targets (on-chain IDs 2000-2029, MAX_TARGETS=2030) ────────
        # Oncogene silencing
        "MYC_mRNA":      2000, "KRAS_mRNA":    2001, "BCL2_mRNA":  2002,
        "EGFR_mRNA":     2003, "HER2_mRNA":    2004, "BRAF_mRNA":  2005,
        "MDM2_mRNA":     2006, "CDK4_mRNA":    2007, "CCND1_mRNA": 2008,
        "SURVIVIN_mRNA": 2009,
        # Tumor microenvironment
        "PDL1_mRNA":  2010, "VEGF_mRNA":  2011, "HIF1A_mRNA": 2012,
        "IL6_mRNA":   2013, "TNF_mRNA":   2014, "TGFb1_mRNA": 2015,
        "CSF1R_mRNA": 2016, "CCL2_mRNA":  2017, "CXCL12_mRNA":2018,
        "MMP9_mRNA":  2019,
        # Cancer metabolism
        "LDHA_mRNA":  2020, "PKM2_mRNA":  2021, "GLUT1_mRNA": 2022,
        "HK2_mRNA":   2023, "FASN_mRNA":  2024,
        # DNA repair / immortality
        "TERT_mRNA":  2025, "PARP1_mRNA": 2026, "RAD51_mRNA": 2027,
        "BRCA2_mRNA": 2028, "ATM_mRNA":   2029,
        # ── CRISPR targets (on-chain IDs 3000-3009) ────────────────────────
        **_CRISPR_TARGET_ID_MAP,
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
        # Reward policy:
        #   ref compounds  → submit on hit, flat 3 $LIFE (logged as [REF-SUBMIT])
        #   novel molecules → full tier rewards: Easy=1, Medium=5, Hard=25 $LIFE
        # The on-chain program determines actual reward; local tracking mirrors it.
        life_delta = 0.0
        if hit and affinity is not None and TARGET_ID_MAP.get(tid) is not None:
            if is_ref:
                wlog.info("  [REF-SUBMIT] HIT (reference compound) — submitting on-chain (flat 3 $LIFE)")
            resp = submit_on_chain(TARGET_ID_MAP[tid], mol, affinity, boltz_seed_used)
            if resp and resp.get("status") == "submitted":
                tx_sig = resp.get("signature", "")
                life_delta = 3.0 if is_ref else {1: 1.0, 2: 5.0, 3: 25.0}.get(target.get("difficulty_tier", 1), 1.0)
                wlog.info(f"  ✔ tx: {tx_sig}")
                # ── Reward-decay similarity logging (LOG ONLY — no payout change) ──
                if source in ("generate", "mutant", "crispr_generated") and not is_ref:
                    _parent = (
                        _rds_gen_parents.get(mol)   if source == "generate"  else
                        _rds_pulse_parents.get(mol)  if source == "mutant"    else
                        None  # CRISPR: no explicit parent yet (Part 1 gap)
                    )
                    _log_reward_decay_sim(wlog, tid, source, mol, _parent, life_delta)

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
        # Pre-warm scipy/OpenBLAS CPU dispatcher on the main thread before any
        # background threads start.  Without this, the first sklearn GBR.fit()
        # (fired by the CRISPR-Net retrain loop ~70 min into a run when a model
        # becomes stale) initialises the OpenBLAS dynamic-CPU-dispatch "tracer"
        # concurrently with a PULSE sweep batch, which races against the already-
        # initialised torch dispatcher and raises RuntimeError("CPU dispatcher
        # tracer already initlized").  That exception is caught by _pulse_loop's
        # except block but poisons every subsequent call, stalling PULSE silently
        # (last_sweep_ts stops updating → dashboard shows IDLE) until next restart.
        # Calling a trivial sklearn op here forces the one-time C-level init to
        # complete cleanly in the main thread, making all later calls no-ops.
        try:
            import numpy as _np
            from sklearn.dummy import DummyRegressor as _DR
            _dr = _DR(); _dr.fit(_np.zeros((2, 1)), _np.zeros(2))
            del _dr, _DR, _np
            log.debug("[PULSE] scipy/sklearn CPU dispatcher pre-warmed")
        except Exception as _pw_err:
            log.debug(f"[PULSE] pre-warm skipped (non-fatal): {_pw_err}")

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

    # ── CRISPR-Net background retrain loop ─────────────────────────────────────
    if _CNET_AVAILABLE and _cnet_train_all is not None:
        _cnet_train_all_fn = _cnet_train_all   # narrow for closure
        def _cnet_retrain_loop():
            while True:
                try:
                    _cnet_train_all_fn()
                except Exception as _cnte:
                    log.debug(f"[CRISPR-NET] retrain error (non-fatal): {_cnte}")
                time.sleep(300)   # check every 5 min; train_all() is a no-op when up to date
        threading.Thread(target=_cnet_retrain_loop, daemon=True, name="crispr-net-retrain").start()
        log.info("[CRISPR-NET] Background retrain thread started (every 5 min)")

    # ── CRISPR background scoring thread (Boltz2 GPU mode) ────────
    # One gRNA per epoch per target is run through Boltz2 GPU inference.
    # Analytical scoring (pick_grna) acts as a pre-filter (top candidate only).
    # Falls back to analytical affinity if Boltz2 fails or is unavailable.
    # Shared mutable counter: main stats loop reads these to include CRISPR
    # earnings in the dashboard $LIFE total without a threading.Lock (GIL-safe
    # for float/int reads on CPython).
    _crispr_stats: dict = {"life_earned": 0.0, "molecules_screened": 0}
    if _CRISPR_AVAILABLE:
        def _crispr_loop():
            """Score CRISPR targets continuously in a background daemon thread.

            Deduplication
            -------------
            A CrisprDeduplicationHistory instance prevents the same gRNA sequence from
            being submitted for the same target more than once in any rolling 100-epoch
            window.  The history is persisted to output/crispr_dedup_history.json so it
            survives daemon restarts.

            Per-epoch submission cap
            ------------------------
            CRISPR_MAX_SUBMISSIONS_PER_EPOCH (default 3) limits on-chain submissions
            per epoch across ALL CRISPR targets combined.  The counter resets when
            _read_epoch_state() reports a higher epoch number than the last seen value.
            """
            log.info("[CRISPR] Background gRNA scoring thread started")
            _crispr_sub_memory = SubmissionMemory() if _TOOLS_AVAILABLE else None
            _crispr_target_idx = 0

            # Persistent deduplication: skips gRNAs submitted in the last N epochs
            _dedup = _CrisprDeduplicationHistory()

            # Per-epoch submission cap: reset when epoch advances
            _epoch_submissions: int = 0      # count of on-chain CRISPR txns this epoch
            _last_seen_epoch:   int = -1     # last on-chain epoch we observed

            while True:
                try:
                    if not _CRISPR_TARGETS:
                        time.sleep(60)
                        continue

                    # ── Read current on-chain epoch ────────────────────────────
                    epoch_state = _read_epoch_state()
                    current_epoch: int = (
                        epoch_state["current_epoch"] if epoch_state else _last_seen_epoch
                    )
                    if current_epoch > _last_seen_epoch:
                        if _last_seen_epoch >= 0:
                            log.info(
                                f"[CRISPR] Epoch advanced {_last_seen_epoch} → {current_epoch} "
                                f"— resetting per-epoch submission counter "
                                f"(was {_epoch_submissions}/{CRISPR_MAX_SUBMISSIONS_PER_EPOCH})"
                            )
                        _epoch_submissions = 0
                        _last_seen_epoch   = current_epoch

                    # ── Per-epoch submission cap ───────────────────────────────
                    if _epoch_submissions >= CRISPR_MAX_SUBMISSIONS_PER_EPOCH:
                        log.debug(
                            f"[CRISPR] Epoch {current_epoch}: submission cap reached "
                            f"({_epoch_submissions}/{CRISPR_MAX_SUBMISSIONS_PER_EPOCH}) "
                            "— skipping generation until next epoch"
                        )
                        time.sleep(30)
                        continue

                    target = _CRISPR_TARGETS[_crispr_target_idx % len(_CRISPR_TARGETS)]
                    _crispr_target_idx += 1
                    tid    = target["id"]
                    thresh = target.get("target_score_threshold", -7.0)

                    # ── Step 1: Generate 50 candidates analytically ──────────────────────
                    # Generate 50 gRNA candidates with the full analytical scorer.
                    # Build excluded set from dedup history for this target.
                    _excluded: set[str] = {
                        seq.upper()
                        for seq, ep in _dedup._data.get(tid, {}).items()
                        if _dedup.is_recently_submitted(tid, seq, current_epoch)
                    }
                    all_candidates = crispr_generate_candidates(  # type: ignore[misc]
                        tid, n=50, excluded_seqs=_excluded
                    )
                    all_seqs = [c["seq"] for c in all_candidates]

                    # ── Step 1b: CRISPR-Net pre-screen (if model ready, R²≥0.5) ────────
                    hotspots = _CRISPR_HOTSPOT_GRNAS.get(tid, [])  # type: ignore[union-attr]
                    if (
                        _CNET_AVAILABLE
                        and _cnet_pre_screen is not None
                        and _cnet_get_report is not None
                    ):
                        cnet_report = _cnet_get_report()
                        cnet_r2     = cnet_report.get("models", {}).get(tid, {}).get("r2")
                        cnet_status = cnet_report.get("models", {}).get(tid, {}).get("status")
                        # LIFE-BRAIN: if CRISPR branch trusted, use it instead
                        if (
                            _LBRAIN_AVAILABLE
                            and _lbrain_trusted is not None
                            and _lbrain_pre_screen_crispr is not None
                            and _lbrain_trusted("crispr")
                        ):
                            _crispr_tid_int = int(str(tid).replace("_CRISPR", "")) if str(tid).replace("_CRISPR","").isdigit() else 3000
                            lb_crispr = _lbrain_pre_screen_crispr(all_seqs, _crispr_tid_int, top_n=5)
                            if lb_crispr is not None:
                                top5_seqs = lb_crispr
                                log.info(
                                    f"[LIFE-BRAIN] CRISPR branch pre-screen: "
                                    f"{len(all_seqs)} → top {len(top5_seqs)} for {tid}"
                                )
                            else:
                                top5_seqs = _cnet_pre_screen(all_seqs, tid, top_n=5) if (cnet_status == "ready" and cnet_r2 is not None and cnet_r2 >= _CNET_MIN_R2) else [c["seq"] for c in all_candidates[:5]]
                        elif cnet_status == "ready" and cnet_r2 is not None and cnet_r2 >= _CNET_MIN_R2:
                            top5_seqs = _cnet_pre_screen(all_seqs, tid, top_n=5)
                            log.info(
                                f"[CRISPR-NET] Pre-screened {len(all_seqs)} → top {len(top5_seqs)} "
                                f"for {tid} (R²={cnet_r2:.2f})"
                            )
                        else:
                            # Fall back: take top 5 by analytical combined score
                            top5_seqs = [c["seq"] for c in all_candidates[:5]]
                            if cnet_r2 is not None:
                                log.debug(
                                    f"[CRISPR-NET] {tid} R²={cnet_r2:.2f} < {_CNET_MIN_R2} "
                                    "— falling back to analytical top-5"
                                )
                    else:
                        top5_seqs = [c["seq"] for c in all_candidates[:5]]

                    # ── Step 2: Boltz2 GPU inference on top-5 candidates ─────────────────
                    # Run Boltz2 on each of the top-5 seqs; keep the best iptm result.
                    # Falls back to analytical_affinity if Boltz2 is unavailable or fails.
                    best_grna_seq          = top5_seqs[0] if top5_seqs else (all_seqs[0] if all_seqs else "")
                    best_boltz_result: dict = {}
                    best_affinity: float   = -6.0
                    best_boltz_model       = "analytical"
                    source                 = "crispr_generated"

                    # Pre-compute analytical scores for the top-5 for fallback/logging
                    _seq_to_scores = {c["seq"]: c for c in all_candidates}

                    if _CRISPR_BOLTZ_AVAILABLE and top5_seqs:
                        for _candidate_seq in top5_seqs:
                            log.info(
                                f"[CRISPR] {tid} | Running Boltz2 GPU on gRNA={_candidate_seq[:12]}…"
                            )
                            with _GPU_LOCK:
                                _bres = run_boltz2_crispr_scoring(_candidate_seq)
                            _bkcal = _bres.get("affinity_kcal")
                            if _bkcal is not None:
                                if best_boltz_model == "analytical" or _bkcal < best_affinity:
                                    best_grna_seq    = _candidate_seq
                                    best_boltz_result = _bres
                                    best_affinity    = _bkcal
                                    best_boltz_model = "boltz2-gpu-crispr"
                            else:
                                log.debug(
                                    f"[CRISPR] {tid} | Boltz2 failed for {_candidate_seq[:12]}… "
                                    f"({_bres.get('error')}) — skipping"
                                )
                        if best_boltz_model == "boltz2-gpu-crispr":
                            log.info(
                                f"[CRISPR] {tid} | Best Boltz2 gRNA={best_grna_seq[:12]}… "
                                f"affinity={best_affinity:.4f} kcal/mol"
                            )
                        else:
                            log.warning(
                                f"[CRISPR] {tid} | All {len(top5_seqs)} Boltz2 runs failed "
                                "— falling back to analytical top candidate"
                            )
                    else:
                        log.debug("[CRISPR] Boltz2 unavailable — using analytical top candidate")

                    # Resolve final analytical scores for the winner
                    _winner_scores = _seq_to_scores.get(best_grna_seq, {})
                    grna_scores = {
                        "on_target":  _winner_scores.get("on_target",  0.5),
                        "off_target": _winner_scores.get("off_target", 1.0),
                        "delivery":   _winner_scores.get("delivery",   0.7),
                        "combined":   _winner_scores.get("combined",   0.0),
                    }
                    # Analytical affinity of the winner (for fallback and logging)
                    analytical_affinity = _winner_scores.get("affinity", -6.5)
                    if best_boltz_model == "analytical":
                        best_affinity = analytical_affinity
                    grna_seq     = best_grna_seq
                    affinity     = best_affinity
                    boltz_model  = best_boltz_model
                    boltz_result = best_boltz_result

                    hit = affinity <= thresh
                    log.info(
                        f"[CRISPR] {tid} | gRNA: {grna_seq} | "
                        f"aff={affinity:.4f} | model={boltz_model} | epoch={current_epoch} | "
                        f"subs_this_epoch={_epoch_submissions}/{CRISPR_MAX_SUBMISSIONS_PER_EPOCH} | "
                        f"{'✔ HIT' if hit else '✘ miss'}"
                    )

                    # Append to shared scoring JSONL feed
                    _boltz_jsonl = WORK_DIR / "output" / "life_boltz_scores.jsonl"
                    try:
                        _boltz_jsonl.parent.mkdir(exist_ok=True)
                        with _boltz_jsonl.open("a") as _fh:
                            _fh.write(json.dumps({
                                "ts":                          time.time(),
                                "target_id":                   tid,
                                "smiles":                      grna_seq,
                                "boltz_score":        boltz_result.get("boltz_score", analytical_affinity),
                                "affinity":           affinity,
                                "iptm":               boltz_result.get("iptm"),
                                "confidence_score":   boltz_result.get("confidence_score"),
                                "analytical_affinity": analytical_affinity,
                                "on_target":          grna_scores.get("on_target"),
                                "off_target":         grna_scores.get("off_target"),
                                "delivery":           grna_scores.get("delivery"),
                                "combined":           grna_scores.get("combined"),
                                "model":              boltz_model,
                                "hit":                         hit,
                                "source":                      source,
                                "target_type":                 "CRISPR",
                                "epoch":                       current_epoch,
                            }) + "\n")
                    except Exception as _je:
                        log.debug(f"[CRISPR] JSONL write failed: {_je}")

                    # On-chain submission — gates on quality threshold + dedup + epoch cap
                    tx_sig_crispr: str | None = None
                    delivery_score  = grna_scores.get("delivery",  0.0)
                    # Option B: quality_score = iptm × delivery for Boltz2 runs;
                    # falls back to analytical combined when model=analytical (no iptm).
                    _iptm = boltz_result.get("iptm") if boltz_model == "boltz2-gpu-crispr" else None
                    if _iptm is not None:
                        quality_score = _iptm * delivery_score
                    else:
                        quality_score = grna_scores.get("combined", 0.0)
                    if hit and delivery_score < CRISPR_MIN_DELIVERY:
                        log.info(
                            f"[CRISPR] {tid} | delivery={delivery_score:.3f} < "
                            f"{CRISPR_MIN_DELIVERY} (out-of-range GC) — skipping submission"
                        )
                    elif hit and quality_score < CRISPR_MIN_COMBINED_BY_TARGET.get(tid, CRISPR_MIN_COMBINED):
                        _min_q = CRISPR_MIN_COMBINED_BY_TARGET.get(tid, CRISPR_MIN_COMBINED)
                        log.info(
                            f"[CRISPR] {tid} | quality={quality_score:.3f} "
                            f"({'iptm×delivery' if _iptm is not None else 'combined'}) < "
                            f"{_min_q} threshold — skipping submission"
                        )
                    elif hit and _dedup.is_recently_submitted(tid, grna_seq, current_epoch):
                        log.info(
                            f"[CRISPR] {tid} | {grna_seq[:12]}… submitted within last "
                            f"{_CRISPR_DEDUP_WINDOW} epochs — skipping (dedup)"
                        )
                    elif hit and tid in _CRISPR_TARGET_ID_MAP:
                        # boltz_seed=BOLTZ_SEED when Boltz2 ran (validator can reproduce);
                        # boltz_seed=0 for analytical fallback path.
                        # molecule_type="CRISPR": signals validator to use gRNA scoring logic.
                        _submit_seed = BOLTZ_SEED if boltz_model == "boltz2-gpu-crispr" else 0
                        log.info(
                            f"[CRISPR] HIT — submitting {tid} on-chain "
                            f"(model={boltz_model} seed={_submit_seed}) ..."
                        )
                        resp = submit_on_chain(
                            _CRISPR_TARGET_ID_MAP[tid], grna_seq, affinity,
                            boltz_seed=_submit_seed, molecule_type="CRISPR",
                        )
                        if resp and resp.get("tx"):
                            log.info(f"[CRISPR] ✔ tx: {resp['tx']}")
                            tx_sig_crispr = resp["tx"]
                            # Record in dedup history to prevent resubmission
                            _dedup.mark_submitted(tid, grna_seq, current_epoch)
                            _epoch_submissions += 1
                            log.info(
                                f"[CRISPR] Epoch {current_epoch}: "
                                f"{_epoch_submissions}/{CRISPR_MAX_SUBMISSIONS_PER_EPOCH} "
                                "CRISPR submissions used"
                            )
                            # CRISPR targets are all tier Crispr = 7 LIFE initial reward.
                            # Supply/hit halvings are applied on-chain by mint_reward.rs.
                            # Similarity decay applied live (Hamming-proxy parent).
                            # generate / zinc15 / ref are completely unaffected.
                            _crispr_life = _apply_crispr_decay(
                                log, tid, grna_seq, REWARD_CRISPR_LIFE
                            )
                            _crispr_stats["life_earned"] += _crispr_life
                            _crispr_stats["molecules_screened"] += 1
                        elif resp and resp.get("status") == "already_submitted":
                            log.info("[CRISPR] Already submitted this epoch")
                            # Still record in dedup so we don't keep trying this seq
                            _dedup.mark_submitted(tid, grna_seq, current_epoch)
                        else:
                            log.info(f"[CRISPR] HIT but {tid} not yet registered on-chain (ID 3000-3009 pending)")
                    elif hit:
                        log.info(f"[CRISPR] HIT but {tid} not yet registered on-chain — skipping submission")

                    # Discovery NFT — mint for top-10% novel gRNA hits
                    if hit:
                        try:
                            _maybe_mint_discovery_nft(
                                smiles=grna_seq,
                                affinity=affinity,
                                target=target,
                                miner_wallet=AUTH_KEYPAIR,
                                validator_tx=tx_sig_crispr or "pending",
                                source=source,
                                auth_keypair_path=AUTH_KEYPAIR,
                                rpc=SOLANA_RPC,
                                grna_scores=grna_scores,
                            )
                        except Exception as _nft_err:
                            log.debug(f"[CRISPR] NFT mint non-fatal error: {_nft_err}")

                except Exception as _ce:
                    log.warning(f"[CRISPR] thread error (non-fatal): {_ce}")

                # Short sleep — CRISPR is CPU-only, can run frequently
                time.sleep(10)

        threading.Thread(target=_crispr_loop, daemon=True, name="crispr-grna").start()
        log.info(f"[CRISPR] Background gRNA thread started ({len(_CRISPR_TARGETS)} CRISPR targets)")
        log.info(
            f"[TOKENOMICS] Rewards — Easy: {REWARD_EASY_LIFE} | Medium: {REWARD_MEDIUM_LIFE} | "
            f"Hard: {REWARD_HARD_LIFE} | CRISPR: {REWARD_CRISPR_LIFE} | mRNA: {REWARD_MRNA_LIFE} $LIFE "
            f"(halves every {HALVING_INTERVAL:,} epochs, ~1 yr on mainnet)"
        )
    else:
        log.warning("[CRISPR] life_crispr unavailable — CRISPR scoring disabled")

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
    # Targets 10-29 are protein targets pending on-chain registration (MAX_TARGETS ≥ 30).
    # Targets 2000-2029 are mRNA targets (MAX_TARGETS=2030, registered on-chain 2026-08-20).
    # Targets 3000-3009 are CRISPR gRNA targets (MAX_TARGETS=3010, registered on-chain 2026-08-23).
    # All 60 targets are screened every cycle; on-chain submission is gated below.
    TARGET_ID_MAP = {
        # ── Protein targets (on-chain IDs 0-29) ──────────────────────────────
        "TP53":   0, "BRCA1":  1, "EGFR":   2, "HER2":   3, "KRAS":   4,
        "BCL2":   5, "CDK4":   6, "VEGFR2": 7, "PDL1":   8, "MDM2":   9,
        # ── pending on-chain registration (MAX_TARGETS ≥ 30 required) ──────────
        "BRAF":  10, "PTEN":  11, "MYC":   12, "STAT3": 13, "PIK3CA": 14,
        "MTOR":  15, "FGFR1": 16, "RET":   17, "AR":    18, "NTRK1":  19,
        "IDH1":  20, "FLT3":  21, "SMAD4": 22, "APC":   23, "PARP1":  24,
        "JAK2":  25, "ESR1":  26, "HDAC1": 27, "HDAC2": 28, "ABL1":   29,
        # ── mRNA targets (on-chain IDs 2000-2029, MAX_TARGETS=2030) ────────
        # Oncogene silencing
        "MYC_mRNA":      2000, "KRAS_mRNA":    2001, "BCL2_mRNA":  2002,
        "EGFR_mRNA":     2003, "HER2_mRNA":    2004, "BRAF_mRNA":  2005,
        "MDM2_mRNA":     2006, "CDK4_mRNA":    2007, "CCND1_mRNA": 2008,
        "SURVIVIN_mRNA": 2009,
        # Tumor microenvironment
        "PDL1_mRNA":  2010, "VEGF_mRNA":  2011, "HIF1A_mRNA": 2012,
        "IL6_mRNA":   2013, "TNF_mRNA":   2014, "TGFb1_mRNA": 2015,
        "CSF1R_mRNA": 2016, "CCL2_mRNA":  2017, "CXCL12_mRNA":2018,
        "MMP9_mRNA":  2019,
        # Cancer metabolism
        "LDHA_mRNA":  2020, "PKM2_mRNA":  2021, "GLUT1_mRNA": 2022,
        "HK2_mRNA":   2023, "FASN_mRNA":  2024,
        # DNA repair / immortality
        "TERT_mRNA":  2025, "PARP1_mRNA": 2026, "RAD51_mRNA": 2027,
        "BRCA2_mRNA": 2028, "ATM_mRNA":   2029,
        # ── CRISPR targets (on-chain IDs 3000-3009) ────────────────────────
        **_CRISPR_TARGET_ID_MAP,
    }

    # Weighted protein/mRNA scheduling — picks mRNA MRNA_SCHEDULE_WEIGHT times per
    # protein pick so mRNA targets (~30) get proportionally more per-target GPU time
    # than protein targets (~2,000).  Pattern (weight=2): P M M  P M M …
    # CRISPR runs independently in _crispr_loop (unchanged).
    # Submission is still gated by TARGET_ID_MAP below.
    protein_targets: list = []
    mrna_targets:    list = []
    protein_idx  = 0
    mrna_idx     = 0
    _mrna_budget = 0   # counts down remaining mRNA picks before next protein pick

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
            # Rebuild per-type queues from the fresh fetch; CRISPR excluded here
            # (it injects entries only for the MSA prefetch below).
            protein_targets = [t for t in targets
                               if t.get("target_type") not in ("mRNA", "CRISPR")]
            mrna_targets    = [t for t in targets
                               if t.get("target_type") == "mRNA"]
            log.info(f"  Scheduling queues: {len(protein_targets)} protein, "
                     f"{len(mrna_targets)} mRNA (CRISPR in background thread)")

            # Inject CRISPR targets for MSA prefetch only — not into scheduling queues.
            # CRISPR scoring runs in _crispr_loop; these entries never reach Boltz2 here.
            if _CRISPR_AVAILABLE:
                existing_ids = {t["id"] for t in targets}
                for ct in _CRISPR_TARGETS:
                    if ct["id"] not in existing_ids:
                        targets.append(ct)

            # Launch background MSA prefetch for all targets (skips cached files)
            if _AUTO_MSA_AVAILABLE:
                _auto_msa_prefetch(targets)  # type: ignore[possibly-unbound]
            last_refresh = now

        # ── Epoch advance check ───────────────────────────────────────────────
        # Permissionless: first miner to detect an expired epoch advances it;
        # all other miners benefit automatically.  No-op when epoch is live.
        _maybe_advance_epoch()

        # Weighted protein / mRNA target selection.
        # _mrna_budget counts how many mRNA picks remain before we take a protein pick.
        # When budget > 0 and mRNA exists → pick mRNA and decrement.
        # When budget == 0 → pick protein and reload the budget.
        # Fallback: if the chosen queue is empty, use the other.
        if not protein_targets and not mrna_targets:
            time.sleep(5)
            continue

        if _mrna_budget > 0 and mrna_targets:
            target = mrna_targets[mrna_idx % len(mrna_targets)]
            mrna_idx     += 1
            _mrna_budget -= 1
        elif protein_targets:
            target = protein_targets[protein_idx % len(protein_targets)]
            protein_idx  += 1
            # Reload budget: MRNA_SCHEDULE_WEIGHT picks next, but only when mRNA exists
            _mrna_budget  = MRNA_SCHEDULE_WEIGHT if mrna_targets else 0
        else:
            # Budget says mRNA but queue is empty — fall back to protein
            target = protein_targets[protein_idx % len(protein_targets)]
            protein_idx  += 1
            _mrna_budget  = 0  # stay on protein until next refresh populates mRNA

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

        is_ref    = (source == "ref")
        is_mrna   = target.get("target_type") == "mRNA"
        type_tag  = " [RNA]" if is_mrna else ""
        log.info(f"Target: {tid}{type_tag} ({uid}) | tier {target.get('difficulty_tier','?')} "
                 f"| threshold {thresh} | MSA: {'local' if msa != 'empty' else 'empty'}")
        log.info(f"Molecule: {mol[:80]}  [{source}]")
        log.info("Running Boltz2 GPU scoring...")

        t0      = time.time()
        with _GPU_LOCK:
            if is_mrna:
                # Dedicated mRNA path: RNA chain + ligand YAML, ipTM score.
                # Protein path (run_boltz2_scoring) is NOT called for mRNA targets.
                result = run_boltz2_mrna_scoring(mol, target)
            else:
                result = run_boltz2_scoring(mol, target)
        elapsed = time.time() - t0

        boltz_score     = result.get("boltz_score")
        boltz_seed_used = result.get("seed", BOLTZ_SEED)
        # mRNA scorer pre-computes affinity_kcal via -6.0 - 3.0×iptm; use it
        # directly to avoid applying the protein ×30 formula to an iptm value.
        if is_mrna and result.get("affinity_kcal") is not None:
            affinity = result["affinity_kcal"]
        else:
            affinity = _boltz_score_to_affinity(boltz_score)

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
        # Reward policy:
        #   ref compounds  → submit on hit, flat 3 $LIFE (logged as [REF-SUBMIT])
        #   novel molecules → full tier rewards: Easy=1, Medium=5, Hard=25 $LIFE
        # The on-chain program determines actual reward; local tracking mirrors it.
        tx_sig = None
        if hit and affinity is not None and tid in TARGET_ID_MAP:
            if is_ref:
                log.info("  [REF-SUBMIT] HIT (reference compound) — submitting on-chain (flat 3 $LIFE)")
            else:
                log.info(f"  HIT — submitting to devnet program {PROGRAM_ID}...")
            # ChEMBL novelty cross-reference (non-fatal, novel molecules only)
            chembl_result: dict = {}
            if _TOOLS_AVAILABLE and not is_ref:
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
                #   ref compound    =   3 LIFE (flat, any tier)
                #   tier 1 (easy)   =   1 LIFE
                #   tier 2 (medium) =   5 LIFE
                #   tier 3 (hard)   =  25 LIFE
                #   unknown         =   1 LIFE (conservative fallback)
                _tier_reward = 3.0 if is_ref else {1: 1.0, 2: 5.0, 3: 25.0}.get(target.get("difficulty_tier", 1), 1.0)
                life_earned += _tier_reward
                log.info(f"  ✔ tx: {tx_sig}")
                log.info(f"  Explorer: https://explorer.solana.com/tx/{tx_sig}?cluster=devnet")
                # ── Reward-decay similarity logging (LOG ONLY — no payout change) ──
                if source in ("generate", "mutant", "crispr_generated") and not is_ref:
                    _parent = (
                        _rds_gen_parents.get(mol)    if source == "generate"  else
                        _rds_pulse_parents.get(mol)  if source == "mutant"    else
                        None  # CRISPR: no explicit parent yet (Part 1 gap)
                    )
                    _log_reward_decay_sim(log, tid, source, mol, _parent, _tier_reward)
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

                # ── Discovery NFT ─────────────────────────────────────────────
                # Fires after results-DB update so the registry write is last.
                # Non-fatal: a mint failure never blocks the mining loop.
                if not is_ref:
                    try:
                        _maybe_mint_discovery_nft(
                            smiles=mol,
                            affinity=affinity,
                            target=target,
                            miner_wallet=AUTH_KEYPAIR,
                            validator_tx=tx_sig,
                            source=source,
                            auth_keypair_path=AUTH_KEYPAIR,
                            rpc=SOLANA_RPC,
                        )
                    except Exception as _nft_err:
                        log.debug(f"  [DISCOVERY] NFT mint non-fatal error: {_nft_err}")

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
            "molecules_screened": molecules_done + _crispr_stats["molecules_screened"],
            "life_earned": life_earned + _crispr_stats["life_earned"],
            "targets_contributed": list({t["target"] for t in txs}),
            "transactions": txs[-20:],
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "global": fetch_network_stats(),
        })
        write_stats(stats)
        log.info(f"Screened: {molecules_done} | $LIFE: {life_earned + _crispr_stats['life_earned']:.1f} | txs: {len(txs)}")
        log.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
