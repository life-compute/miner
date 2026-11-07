"""
coordinator/loop.py — coordinator_cycle() for COORDINATOR_MODE (C21b).

Replaces the molecule-pick → run_boltz2_scoring → submit_on_chain path
inside gpu_worker() when COORDINATOR_MODE=True.

Calling convention:
    coordinator_cycle(wlog, client, shared_stats, gpu_idx)

Internal steps
--------------
1. GET /work → Lease; on None (no work / network) log and return.
2. Modality guard: CRISPR leases are declined cleanly (log + return).
3. Protein: verify pinned receptor/MSA hashes; fetch alignment if stale.
4. Walk ShardManager.iter_molecules() with a persisted cursor so a restart
   resumes where it left off within the same lease.
5. Score each molecule:
   - protein  → score_batch_with_profile (applies locked scorer profile)
   - mRNA     → run_boltz2_mrna_scoring  (profile lock gap noted below)
6. Convert raw scorer output → kcal/mol contract score:
   - protein  → _affinity_pred_value_to_dg(raw_score)
   - mRNA     → raw kcal value (already ΔG)
7. Enforce 2 048-char candidate limit (hard-drop if exceeded).
8. Derive molecule_hash = sha256(candidate)[:32] (32 hex chars).
9. enqueue_result() → durable JSONL queue + background drain thread.
10. Write to life_boltz_scores.jsonl with source="coordinator".

CRISPR
------
The API contract defines a CRISPR scorer_profile but does not define a
numeric `score` field or `hit_threshold` for CRISPR leases.  The miner
has no safe way to produce a dimensionally-correct contract score.
Action: log DECLINED_CRISPR and return without posting.
Worker change needed: add a `modality_allowlist` field (e.g. ["protein","mrna"])
to COORD_CONFIG in config.ts so the Worker never issues CRISPR work to miners
that have not opted in; currently the Worker will re-issue the same shard
after lease expiry, causing repeated declines.

molecule_hash definition
------------------------
SHA-256 of the UTF-8 candidate string, first 32 hex chars.
This matches the lease_id format (32-char hex) and matches the example in
API_CONTRACT.md ("mol_9f8e7d6c…" — we drop the "mol_" prefix as the contract
only requires a unique string, not a specific prefix).  The coordinator
de-duplicates by (molecule_hash, target); SHA-256 collision probability is
negligible for any real library.

mRNA profile lock gap
---------------------
run_boltz2_mrna_scoring does not accept a scorer_profile argument; it uses
hardcoded Boltz2 settings.  A profile-aware mRNA variant analogous to
score_batch_with_profile does not yet exist.  This is noted in the report
as a remaining gap; the miner uses the existing scorer so mRNA results
are accepted by the coordinator (score + candidate are present and typed
correctly) but the exact boltz flags may differ from the lease profile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from coordinator.client import CoordinatorClient, Lease

from coordinator.hashes import PinnedHashError, verify_protein_target  # noqa: E402
from coordinator.shards import ShardManager  # noqa: E402 — module-level so patch() can find it

# ── Failure classification (C26) ────────────────────────────────────────────
#
# Per-molecule failures: a single molecule that cannot be scored due to a
# property of that specific molecule (bad SMILES, unparseable conformer, mol
# too large, 3D-embedding failure, RDKit ValueError).  These do NOT count as
# systemic failures; the miner skips the molecule and reports it in the
# skip-reason summary.
#
# Systemic failures: the scorer process crashed, timed out, ran OOM, or an
# import failed.  These count toward MAX_SCORER_FAILURES and the rolling 95%
# window check.  After 5 consecutive systemic failures OR >95% systemic in
# the last FAIL_RATE_WINDOW molecules, the lease is abandoned.

class PerMoleculeError(Exception):
    """Raised inside _score_protein/_score_mrna when the failure is specific
    to this molecule (bad SMILES, conformer, too large, etc.) and not systemic.
    The caller catches this and counts it as a skipped molecule."""


class GpuOomError(Exception):
    """Raised when a scoring subprocess exits due to a GPU out-of-memory error.

    C48: triggers an immediate lease abandon + unfit report to the coordinator
    so the target is not reissued to this GPU for 7 days.
    """
    def __init__(self, reason: str = ""):
        super().__init__(f"GPU OOM: {reason}")
        self.reason = reason

# Keywords in scorer stderr/stdout that indicate a per-molecule problem.
# Any line matching one of these patterns is classified as PerMoleculeError.
#
# Boltz-specific patterns (C30):
#   "Failed to process"  — Boltz prints this when it cannot featurize/parse a
#       ligand SMILES.  The structure stage is skipped, no .npz is written, and
#       the affinity stage raises FileNotFoundError (which propagates through the
#       DataLoader worker).  The runner then sees an empty metrics dict and raises
#       RescoreFailed.  The parent catches rc=1 and re-checks this pattern.
#   "Skipping. Error:"   — the message Boltz appends after the path above.
#   "GetAtoms"           — RDKit's NoneType.GetAtoms when SMILES is unparseable.
_PER_MOL_PATTERNS = [
    "Failed to compute 3D conformer",
    "3d conformer",
    "Cannot sanitize",
    "SMILES Parse Error",
    "invalid smiles",
    "unparseable smiles",
    "molecule too large",
    "mol too large",
    "num_atoms",
    "AtomValenceException",
    "SmilesParseError",
    "Kekulize",
    "BadMolecule",
    "rdkit",
    "ValueError:",
    # Boltz-level per-input skip patterns (C30)
    "Failed to process",
    "Skipping. Error:",
    "GetAtoms",
]

def _is_per_molecule_error(stderr: str, stdout: str) -> tuple[bool, str]:
    """Return (True, reason_line) if the error text matches a per-molecule pattern.

    reason_line is the first matching line from the combined output, up to 300
    characters — enough for a SKIP_MOL log entry and test assertions.
    """
    combined = stderr + stdout
    combined_lower = combined.lower()
    for pat in _PER_MOL_PATTERNS:
        if pat.lower() in combined_lower:
            # Return the actual matching line for a human-readable log message.
            for line in combined.splitlines():
                if pat.lower() in line.lower():
                    return True, line.strip()[:300]
            return True, pat
    return False, ""


# C48: GPU OOM patterns — these trigger an unfit report and immediate lease abandon.
# OOM is systemic (not per-molecule) but has a special cause: GPU memory too small.
_GPU_OOM_PATTERNS = [
    "CUDA out of memory",
    "OutOfMemoryError",
    "cuda out of memory",
    "out of memory",
    "GPU memory",
    "RuntimeError: CUDA",
    "torch.cuda.OutOfMemoryError",
]


def _is_gpu_oom(stderr: str, stdout: str) -> tuple[bool, str]:
    """Return (True, reason) when stderr/stdout indicates a GPU OOM failure.

    Distinct from generic systemic failures: these trigger an unfit report
    (POST /leases/{id}/unfit) so the target is not reissued to this GPU.
    """
    combined = stderr + stdout
    for pat in _GPU_OOM_PATTERNS:
        if pat.lower() in combined.lower():
            for line in combined.splitlines():
                if pat.lower() in line.lower():
                    return True, line.strip()[:200]
            return True, pat
    return False, ""

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
_WORK_DIR   = Path(__file__).resolve().parents[1]
CURSOR_PATH = _WORK_DIR / "data" / "coordinator_cursor.json"
BOLTZ_JSONL = _WORK_DIR / "output" / "life_boltz_scores.jsonl"

# ── Boltz interpreter (Fix C25) ────────────────────────────────────────────
# PM2 runs this file with `interpreter: python3` (= /usr/bin/python3) which does
# not have boltz installed.  Boltz 2.2.1 lives in the nova_subnet venv.
# All Boltz calls are dispatched as subprocesses under NOVA_VENV — same pattern
# used by miner_daemon.run_boltz2_scoring — so the PM2 interpreter never needs
# to import boltz directly.
NOVA_DIR: Path  = Path("/mnt/minos-drive/nova_subnet")
NOVA_VENV: Path = NOVA_DIR / ".venv" / "bin" / "python3"

# Consecutive-scorer-failure threshold before the lease is abandoned (Fix C25/C26).
# Only SYSTEMIC failures count (GPU unavailable, import error, scorer crash/timeout,
# OOM).  Per-molecule failures (bad SMILES, unparseable conformer, mol too large)
# are counted as skipped_mol and do NOT count toward this limit.
MAX_SCORER_FAILURES: int = int(os.environ.get("COORD_MAX_SCORER_FAILURES", "5"))

# Rolling window for the 95%-failure rate check.
# If >95% of the last FAIL_RATE_WINDOW molecules are systemic failures, abandon lease.
FAIL_RATE_WINDOW: int = 200

# ── Reference-scoring subprocess helper ───────────────────────────────────
#
# Replaces the old _BOLTZ_HELPER which called nova_pulse_scorer._score_batch_impl
# and omitted the –6.0 offset from the conversion.
#
# The new helper imports coordinator/reference_scoring/runner.py, which vendors
# the validator's exact code (rescore.protein_yaml, boltz_flags_for, seed_for,
# protein_dg with the –6.0 offset) and calls boltz.main.predict() directly.
#
# Accepts a JSON blob on argv[1]; prints ONE JSON line to stdout on success:
#     {"raw": <float>, "score": <float>, "smiles": <str>}
# Exits with code 2 on per-molecule errors, code 1 on systemic errors.
#
# NOVA_DIR / NOVA_VENV are still used for the interpreter (it has boltz 2.2.1 +
# torch) and for the sys.path entry that covers boltz itself.  The old
# nova_pulse_scorer import is gone.
_BOLTZ_HELPER = """\
import sys, json
sys.path.insert(0, "{nova_dir}")

# The reference_scoring package lives in the life-compute-miner tree.
import pathlib
_MINER_DIR = pathlib.Path("{miner_dir}")
sys.path.insert(0, str(_MINER_DIR))

args = json.loads(sys.argv[1])
smiles              = args["smiles"]
msa_path            = args["msa_path"]
scorer_profile_dict = args.get("scorer_profile", {{}})
default_seed        = args.get("default_seed", 68)
num_subsampled_msa  = args.get("num_subsampled_msa", 1024)
timeout_s           = args.get("timeout_s", 1200.0)

from coordinator.reference_scoring.runner import run_protein_reference, RescoreFailed

try:
    result = run_protein_reference(
        smiles=smiles,
        msa_path=msa_path,
        scorer_profile_dict=scorer_profile_dict,
        default_seed=default_seed,
        num_subsampled_msa=num_subsampled_msa,
        timeout_s=timeout_s,
    )
    print(json.dumps(result))
    sys.exit(0)
except RescoreFailed as exc:
    sys.stderr.write(f"SYSTEMIC: {{exc}}\\n")
    sys.exit(1)
except (ValueError, RuntimeError) as exc:
    # Per-molecule patterns — see loop._PER_MOL_PATTERNS
    msg = str(exc)
    sys.stderr.write(msg + "\\n")
    sys.exit(2)
except Exception as exc:
    sys.stderr.write(f"SYSTEMIC: {{exc}}\\n")
    sys.exit(1)
"""


def startup_boltz_check() -> None:
    """
    Fail fast if Boltz 2.2.1 or the GPU is unavailable.

    Checks:
      1. NOVA_VENV interpreter exists.
      2. boltz.__version__ == "2.2.1" — stops if it is not 2.2.1 (ref scoring
         was validated against exactly this version; a different version must
         be explicitly reviewed before deployment).
      3. torch.cuda.is_available() — must be True.

    Called once before any lease is taken.  Raises RuntimeError with a single
    clear error line if the check fails — the caller (gpu_worker / main) must
    exit rather than take a lease that will fail every molecule.
    """
    if not NOVA_VENV.exists():
        raise RuntimeError(
            f"[COORD] STARTUP_CHECK FAILED: nova venv missing at {NOVA_VENV}  "
            "— cannot score molecules without Boltz 2.2.1"
        )
    check_src = (
        "import sys\n"
        f"sys.path.insert(0, '{NOVA_DIR}')\n"
        "import boltz; import torch\n"
        "ver = boltz.__version__\n"
        "if ver != '2.2.1':\n"
        "    print(f'BOLTZ_VERSION_MISMATCH: {ver!r} != 2.2.1', flush=True)\n"
        "    sys.exit(2)\n"
        "gpu_ok = torch.cuda.is_available()\n"
        "print('BOLTZ_OK', ver, 'GPU', gpu_ok)\n"
        "sys.exit(0 if gpu_ok else 1)\n"
    )
    try:
        r = subprocess.run(
            [str(NOVA_VENV), "-c", check_src],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        raise RuntimeError(
            f"[COORD] STARTUP_CHECK FAILED: could not exec nova venv: {exc}"
        ) from exc
    if r.returncode == 2:
        # Version mismatch — hard stop
        raise RuntimeError(
            f"[COORD] STARTUP_CHECK FAILED: {(r.stdout + r.stderr).strip()}"
        )
    if r.returncode != 0:
        details = (r.stdout + r.stderr)[-300:]
        raise RuntimeError(
            f"[COORD] STARTUP_CHECK FAILED: Boltz or GPU unavailable — {details}"
        )
    log.info("[COORD] startup_boltz_check: %s", r.stdout.strip())

# Candidate length limit — must match MAX_CANDIDATE_BYTES in client.py and
# the Worker's results.ts MAX_CANDIDATE_BYTES (both currently 4 096).
# The task spec says 2 048; we apply the stricter value so the miner never
# sends a candidate the coordinator rejects AND stays within our own budget.
MAX_CANDIDATE_CHARS = 2048

# ── Back-off state (C24) ───────────────────────────────────────────────────
# When every molecule in a lease fails due to shard/alignment errors the miner
# must not ask for another lease immediately (each failed lease locks 4 shards
# for 4 hours).  Exponential back-off: first wait BACKOFF_BASE_S, doubling each
# consecutive failure up to BACKOFF_MAX_S.  Reset to zero on any scored result.
BACKOFF_BASE_S:  float = float(os.environ.get("COORD_BACKOFF_BASE_S",  "60"))
BACKOFF_MAX_S:   float = float(os.environ.get("COORD_BACKOFF_MAX_S",   "600"))
BACKOFF_MAX_CONSECUTIVE: int = int(os.environ.get("COORD_BACKOFF_MAX_FAILS", "10"))

# Module-level back-off state — shared across all calls in the same process.
# Reset with _reset_backoff() in tests.
_backoff_state: dict = {
    "consecutive_failures": 0,
    "next_allowed_at": 0.0,   # epoch seconds
}


def _reset_backoff() -> None:
    """Reset back-off state (used in tests and on process start)."""
    _backoff_state["consecutive_failures"] = 0
    _backoff_state["next_allowed_at"] = 0.0


def _record_failure(wlog: logging.Logger) -> None:
    """
    Record one consecutive shard/alignment failure and schedule a back-off sleep.
    Logs a single clear error line each time.
    """
    n = _backoff_state["consecutive_failures"] + 1
    _backoff_state["consecutive_failures"] = n
    wait = min(BACKOFF_BASE_S * (2 ** (n - 1)), BACKOFF_MAX_S)
    _backoff_state["next_allowed_at"] = time.time() + wait
    wlog.error(
        "[COORD] SHARD_FAIL: 0 molecules scored (consecutive=%d, back-off=%.0fs)",
        n, wait,
    )


def _record_success() -> None:
    """Reset back-off after any lease that produced at least one scored molecule."""
    _backoff_state["consecutive_failures"] = 0
    _backoff_state["next_allowed_at"] = 0.0


def _backoff_exceeded(wlog: logging.Logger) -> bool:
    """Return True and log when the max consecutive-failure limit is reached."""
    if _backoff_state["consecutive_failures"] >= BACKOFF_MAX_CONSECUTIVE:
        wlog.error(
            "[COORD] SHARD_FAIL: %d consecutive failures — stopping lease requests "
            "(COORD_BACKOFF_MAX_FAILS=%d). Fix shards or alignment before restarting.",
            _backoff_state["consecutive_failures"], BACKOFF_MAX_CONSECUTIVE,
        )
        return True
    return False


def _wait_for_backoff(wlog: logging.Logger) -> None:
    """Block until the back-off window has elapsed (if any)."""
    remaining = _backoff_state["next_allowed_at"] - time.time()
    if remaining > 0:
        wlog.info("[COORD] back-off: waiting %.0fs before next lease request", remaining)
        time.sleep(remaining)


# ── Client cache (one per gpu_idx) ────────────────────────────────────────
_CLIENT_CACHE: dict[int, "CoordinatorClient"] = {}


def _get_or_create_client(gpu_idx: int = 0) -> "CoordinatorClient":
    """
    Lazy-init a CoordinatorClient; result is cached per gpu_idx.

    Reads COORDINATOR_URL, COORDINATOR_FALLBACK_URL, MINER_KEYPAIR,
    COORDINATOR_WORKER_NAME from the environment.  Falls back to a
    throwaway key if the keypair file is missing (tests / demo mode only).
    """
    if gpu_idx in _CLIENT_CACHE:
        return _CLIENT_CACHE[gpu_idx]

    from coordinator.client import CoordinatorClient
    from coordinator.auth import MinerKeypair

    coordinator_url = os.environ.get("COORDINATOR_URL", "http://localhost:8787")
    fallback_url    = os.environ.get("COORDINATOR_FALLBACK_URL") or None
    keypair_path    = os.environ.get(
        "MINER_KEYPAIR",
        "/mnt/minos-drive/life-compute-miner/miner-keypair.json",
    )
    worker_name = os.environ.get("COORDINATOR_WORKER_NAME") or None

    try:
        keypair = MinerKeypair.from_solana_json(keypair_path)
    except Exception as exc:
        log.warning("[COORD] keypair load failed (%s) — using throwaway key", exc)
        keypair = MinerKeypair.generate()

    client = CoordinatorClient(
        keypair=keypair,
        coordinator_url=coordinator_url,
        fallback_url=fallback_url,
        worker_name=worker_name,
        gpu_memory_mb=_detect_gpu_memory_mb_for_client(gpu_idx),
        gpu_model=_detect_gpu_model_for_client(gpu_idx),
        miner_version="2.0",
    )
    # C28: drop queued results whose leases have already expired before starting drain
    client.drop_stale_queued_results()
    _CLIENT_CACHE[gpu_idx] = client
    return client


def _detect_gpu_memory_mb_for_client(gpu_idx: int) -> int:
    """Detect GPU memory for the given GPU index (subprocess-safe)."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            if gpu_idx < len(lines):
                return int(lines[gpu_idx])
    except Exception:
        pass
    return 0


def _detect_gpu_model_for_client(gpu_idx: int) -> Optional[str]:
    """Detect GPU model name for the given GPU index (subprocess-safe)."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            if gpu_idx < len(lines):
                return lines[gpu_idx][:64]  # cap length
    except Exception:
        pass
    return None


# ── Persisted cursor (restart-resume within a lease) ──────────────────────

def _load_cursor() -> dict:
    """Return {lease_id, shard_id, last_row} or {} if absent/corrupt."""
    try:
        if CURSOR_PATH.exists():
            return json.loads(CURSOR_PATH.read_text())
    except Exception as exc:
        log.debug("[COORD] cursor load failed: %s", exc)
    return {}


def _save_cursor(lease_id: str, shard_id: int, last_row: int) -> None:
    """Persist progress so a restart skips already-posted molecules."""
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        CURSOR_PATH.write_text(json.dumps({
            "lease_id": lease_id,
            "shard_id": shard_id,
            "last_row": last_row,
        }))
    except Exception as exc:
        log.debug("[COORD] cursor save failed: %s", exc)


def _clear_cursor() -> None:
    try:
        CURSOR_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ── molecule_hash ──────────────────────────────────────────────────────────

def molecule_hash(candidate: str) -> str:
    """
    Unique molecule identifier for POST /results.

    Definition: SHA-256(UTF-8 candidate)[:32] — 32 lowercase hex chars.
    Matches lease_id format.  The coordinator de-duplicates on
    (molecule_hash, target); collisions are negligible for real libraries.
    """
    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:32]


# ── Score conversion ───────────────────────────────────────────────────────

def _affinity_pred_value_to_dg(v: Optional[float]) -> Optional[float]:
    """
    Convert log10(IC50/µM) to ΔG (kcal/mol).

    Inline copy of miner_daemon._affinity_pred_value_to_dg to avoid a
    circular import at module load time.  Must stay in sync with that function.
    """
    if v is None:
        return None
    # ΔG = RT × ln(10) × log10(IC50/µM) where RT = 0.593 kcal/mol at 298 K
    return 0.593 * math.log(10) * float(v)


def _to_contract_score(
    raw: Optional[float],
    modality: str,
) -> Optional[float]:
    """Convert scorer output to the kcal/mol float required by POST /results.

    For protein: _score_protein() now returns the already-converted kcal/mol
    score from reference_scoring.runner (protein_dg with the –6.0 offset), so
    this function passes it through without further conversion.

    For mRNA: run_boltz2_mrna_scoring() already returns kcal/mol.
    """
    if raw is None:
        return None
    if modality == "protein":
        return float(raw)   # already kcal/mol from protein_dg(); pass through
    if modality == "mrna":
        return float(raw)   # run_boltz2_mrna_scoring already returns kcal/mol
    return None


# ── Protein: MSA helpers ───────────────────────────────────────────────────

class ManifestLookupError(Exception):
    """Raised when a target is not in the alignment manifest or on fetch failure."""


def _manifest_lookup(coordinator_url: str, target_name: str) -> tuple[str, str]:
    """
    Fetch GET /align/manifest.json and return (gz_file_name, pinned_raw_sha256)
    for the requested target.

    The manifest maps target name → UniProt accession → gz_file_name, so this
    is the correct lookup path (NOT building '<target_name>.a3m.gz' locally).

    Raises ManifestLookupError when:
      - the manifest cannot be fetched (network / HTTP error)
      - the target name is not listed in the manifest

    The 'target' field in each manifest entry is the human-readable name
    (e.g. 'CDK4') that matches the lease's target string.
    The gz_file_name is the UniProt-accession-based filename (e.g. 'P11802.a3m.gz').
    The pinned_msa_sha256 field is the raw (decompressed) SHA-256 used to verify
    the cached .a3m file.
    """
    from coordinator.align_fetch import _fetch_manifest, AlignFetchError
    try:
        manifest = _fetch_manifest(coordinator_url)
    except AlignFetchError as exc:
        raise ManifestLookupError(f"cannot fetch manifest: {exc}") from exc

    files = manifest.get("files")
    if not isinstance(files, list):
        raise ManifestLookupError("manifest 'files' key missing or not a list")

    for entry in files:
        if isinstance(entry, dict) and entry.get("target") == target_name:
            gz_file_name = entry.get("gz_file_name", "")
            # Prefer pinned_msa_sha256 (which equals raw_sha256 for all current entries);
            # fall back to raw_sha256 or decompressed_sha256 for forward-compat.
            pinned_sha = (
                entry.get("pinned_msa_sha256")
                or entry.get("raw_sha256")
                or entry.get("decompressed_sha256")
                or ""
            )
            if not gz_file_name or not pinned_sha:
                raise ManifestLookupError(
                    f"manifest entry for {target_name!r} is missing gz_file_name "
                    f"or pinned_msa_sha256 — entry={entry}"
                )
            return gz_file_name, pinned_sha

    raise ManifestLookupError(
        f"DECLINED_TARGET_NOT_IN_MANIFEST: target {target_name!r} is not listed in "
        f"/align/manifest.json — lease declined"
    )


def _resolve_protein_inputs(
    lease: "Lease",
    wlog: logging.Logger,
) -> tuple[Optional[Path], Optional[str]]:
    """
    Return (msa_path, protein_sequence) for a protein lease.

    Strategy:
      1. Look up the target name in GET /align/manifest.json to get the correct
         gz_file_name (UniProt-accession-based, e.g. 'P11802.a3m.gz') and the
         pinned raw SHA-256.  Decline (return None, None + log) when the target
         is not in the manifest — this is the authoritative name-to-file mapping.
      2. Call align_fetch.fetch_alignment() with the manifest-derived gz_file_name
         and pinned_raw_sha256.  fetch_alignment() checks a local cache first
         (no download if the file is valid and hash matches).
      3. Return (None, None) on any failure — caller skips the lease.

    NOTE: the old path (_msa_path_for from miner_daemon) relied on a local
    ticker table (e.g. "CDK4" → some local path) and built the gz filename as
    "<target_name>.a3m.gz" — which is wrong because the coordinator stores files
    by UniProt accession ("P11802.a3m.gz"), not by target name.  The manifest is
    the single source of truth for this mapping.
    """
    target_id = lease.target
    coordinator_url = os.environ.get("COORDINATOR_URL", "http://localhost:8787")

    # ── Step 1: Manifest lookup — decline if target not listed ────────────
    try:
        gz_file_name, pinned_raw_sha256 = _manifest_lookup(coordinator_url, target_id)
    except ManifestLookupError as exc:
        wlog.warning(
            "[COORD] %s — skipping lease %s",
            exc, lease.lease_id[:12],
        )
        return None, None

    wlog.info(
        "[COORD] manifest resolved %s → %s (sha256=%s…)",
        target_id, gz_file_name, pinned_raw_sha256[:16],
    )

    # ── Step 2: Fetch (or verify cache) via align_fetch ───────────────────
    dest_dir = _WORK_DIR / "data" / "align_cache"
    try:
        from coordinator.align_fetch import fetch_alignment, AlignFetchError
        msa_path: Optional[Path] = fetch_alignment(
            coordinator_url=coordinator_url,
            target_id=target_id,
            gz_file_name=gz_file_name,
            pinned_raw_sha256=pinned_raw_sha256,
            cache_dir=dest_dir,
        )
        wlog.info("[COORD] align_fetch wrote/verified %s", msa_path)
    except Exception as exc:
        wlog.warning(
            "[COORD] align_fetch failed for %s (%s): %s",
            target_id, gz_file_name, exc,
        )
        return None, None

    if msa_path is None or not msa_path.exists():
        wlog.warning("[COORD] no MSA for %s — skipping lease", target_id)
        return None, None

    # ── Step 3: Read protein sequence from the decompressed .a3m ─────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(_WORK_DIR))
        from miner_daemon import _sequence_from_msa
        seq = _sequence_from_msa(str(msa_path))
    except Exception as exc:
        wlog.warning("[COORD] _sequence_from_msa failed for %s: %s", msa_path, exc)
        return None, None

    if not seq:
        wlog.warning("[COORD] empty sequence from MSA %s — skipping lease", msa_path)
        return None, None

    return msa_path, seq


# ── Scoring dispatch ───────────────────────────────────────────────────────

def _score_protein(
    smiles: str,
    target_id: str,
    msa_path: Path,
    protein_sequence: str,
    scorer_profile: dict,
    wlog: logging.Logger,
) -> Optional[float]:
    """
    Score one protein-mode molecule using the reference scoring path.

    Dispatches to NOVA_VENV (boltz 2.2.1) via a subprocess so this module
    never tries to import boltz under PM2's /usr/bin/python3.

    The subprocess runs coordinator/reference_scoring/runner.py which vendors
    the validator's exact conversion:  protein_dg(affinity_pred_value) =
    round(1.364247 * (affinity_pred_value - 6.0), 3).

    Returns the kcal/mol score directly (already converted); the caller passes
    it straight to _to_contract_score with modality="protein" which no longer
    applies an additional conversion.

    protein_sequence is accepted for interface compatibility but is NOT passed
    to the subprocess: the reference runner reads the sequence from the a3m
    (sequence_from_msa) exactly as the validator does.
    """
    helper_src = _BOLTZ_HELPER.format(
        nova_dir=str(NOVA_DIR),
        miner_dir=str(_WORK_DIR),
    )
    args_json  = json.dumps({
        "smiles":              smiles,
        "msa_path":            str(msa_path),
        "scorer_profile":      scorer_profile,
        "default_seed":        68,
        "num_subsampled_msa":  1024,
        "timeout_s":           300.0,
    })
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                         prefix="coord-boltz-") as f:
            f.write(helper_src)
            helper_path = f.name
        try:
            r = subprocess.run(
                [str(NOVA_VENV), helper_path, args_json],
                capture_output=True, text=True, timeout=300,
                cwd=str(NOVA_DIR),
            )
        finally:
            try:
                os.unlink(helper_path)
            except Exception:
                pass
    except Exception as exc:
        # C26: classify exception — timeout and process errors are systemic
        wlog.warning("[COORD] protein scoring subprocess error %s: %s", smiles[:40], exc)
        return None

    if r.returncode != 0:
        # Exit code 2 = per-molecule error (helper exited via PerMoleculeError /
        # ValueError branch); exit code 1 = systemic.
        if r.returncode == 2:
            reason = (r.stderr + r.stdout).strip()[:300] or "per-molecule error (rc=2)"
            raise PerMoleculeError(
                f"Per-molecule error for {smiles[:40]!r}: {reason}"
            )
        # C48: check for GPU OOM before generic per-molecule patterns
        is_oom, oom_reason = _is_gpu_oom(r.stderr, r.stdout)
        if is_oom:
            raise GpuOomError(oom_reason)
        # rc=1 or other — also check text patterns (C26/C30):
        # Boltz may print "Failed to process <input>. Skipping." then exit
        # non-zero via the runner's RescoreFailed path; reclassify those as
        # PerMoleculeError so they do NOT count toward the systemic abandon limit.
        is_mol_err, reason = _is_per_molecule_error(r.stderr, r.stdout)
        if is_mol_err:
            raise PerMoleculeError(
                f"Per-molecule error for {smiles[:40]!r}: {reason}"
            )
        wlog.warning("[COORD] protein scoring failed %s (rc=%d): %s",
                     smiles[:40], r.returncode, r.stderr[-300:])
        return None

    for line in reversed(r.stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
            # "score" is the already-converted kcal/mol value from protein_dg().
            # Return it as-is; _to_contract_score passes it through for "protein".
            score = parsed.get("score")
            if score is not None:
                return float(score)
        except Exception:
            continue

    wlog.warning("[COORD] protein scoring: no parseable output for %s", smiles[:40])
    return None


def _score_mrna(
    smiles: str,
    target_id: str,
    wlog: logging.Logger,
) -> Optional[float]:
    """
    Score one mRNA-mode molecule.

    Uses run_boltz2_mrna_scoring from miner_daemon.  Profile lock is a known
    gap (no mRNA equivalent of score_batch_with_profile exists yet); the
    coordinator scorer_profile is not applied to mRNA calls.

    Raises PerMoleculeError on per-molecule failures (ValueError, bad SMILES).
    Returns None on systemic failures (import error, process crash).
    """
    try:
        import sys
        sys.path.insert(0, str(_WORK_DIR))
        from miner_daemon import run_boltz2_mrna_scoring
        # Build a minimal target dict from the lease target string.
        target_dict = {"id": target_id, "uniprot_id": "", "gene_name": target_id}
        result = run_boltz2_mrna_scoring(smiles, target_dict)
        return result.get("kcal")   # float or None
    except ValueError as exc:
        # C26: per-molecule — bad SMILES, unparseable molecule, etc.
        raise PerMoleculeError(f"mRNA per-molecule error for {smiles[:40]!r}: {exc}") from exc
    except Exception as exc:
        wlog.warning("[COORD] mRNA scoring failed for %s: %s", smiles[:40], exc)
        return None


# ── JSONL audit log ────────────────────────────────────────────────────────

def _write_jsonl(row: dict) -> None:
    try:
        BOLTZ_JSONL.parent.mkdir(exist_ok=True)
        with BOLTZ_JSONL.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


# ── Main entry point ───────────────────────────────────────────────────────

def coordinator_cycle(
    wlog: logging.Logger,
    client: "CoordinatorClient",
    shared_stats: dict,
    gpu_idx: int = 0,
) -> None:
    """
    One coordinator work cycle.  Called from gpu_worker() when COORDINATOR_MODE=True.

    - Checks back-off: if previous leases all scored 0 due to shard/alignment
      errors, waits for the back-off window and stops after BACKOFF_MAX_CONSECUTIVE
      consecutive failures.
    - Fetches a lease (GET /work).
    - Declines CRISPR leases cleanly.
    - Iterates shard molecules with a persisted cursor (restart-resume).
    - Scores each molecule using the locked scorer profile.
    - Enqueues results to the durable JSONL queue (retry + backoff in background).
    - Records success (scored > 0) or failure (scored == 0 due to shard errors)
      to drive exponential back-off on the next call.
    - Returns after exhausting the lease shards (or on error).
    """
    # ── 0. Back-off guard (C24) ─────────────────────────────────────────
    if _backoff_exceeded(wlog):
        return
    _wait_for_backoff(wlog)

    from coordinator.client import NoFeasibleTargetError as _NoFeasibleTargetError

    # ── 1. Lease ────────────────────────────────────────────────────────
    wlog.info("[COORD] GET /work …")
    try:
        lease = client.get_work()
    except _NoFeasibleTargetError as nft:
        # C48: no feasible target for this GPU — wait and retry later
        wait = max(nft.retry_after, 60)
        wlog.info(
            "[COORD] NO_FEASIBLE_TARGET: all targets unfit for this GPU "
            "(gpu_memory_mb=%d) — waiting %ds before retry",
            client.gpu_memory_mb, wait,
        )
        time.sleep(wait)
        return
    except Exception as exc:
        wlog.warning("[COORD] get_work() raised: %s", exc)
        return

    if lease is None:
        wlog.info("[COORD] no work available — sleeping")
        return

    wlog.info(
        "[COORD] lease=%s  target=%s  modality=%s  shards=%d  expires_in=%.0fs",
        lease.lease_id[:12],
        lease.target,
        lease.modality,
        len(lease.shards),
        (lease.expires_at - time.time() * 1000) / 1000,
    )

    # ── 2. CRISPR guard ─────────────────────────────────────────────────
    if lease.modality == "crispr":
        wlog.warning(
            "[COORD] DECLINED_CRISPR: lease=%s target=%s — CRISPR modality is not"
            " supported (no dimensionally-correct contract score is defined)."
            " Worker fix needed: add modality_allowlist=[\"protein\",\"mrna\"] to"
            " COORD_CONFIG in config.ts so CRISPR shards are not issued to miners"
            " that have not explicitly opted in.",
            lease.lease_id[:12], lease.target,
        )
        return

    # ── 3. Lease-expiry pre-check (skip if already expired) ─────────────
    now_ms = time.time() * 1000
    if now_ms >= lease.expires_at:
        wlog.warning("[COORD] lease %s already expired — dropping", lease.lease_id[:12])
        _clear_cursor()
        return

    # ── 4. Protein: resolve alignment through manifest + align_fetch ────
    msa_path:         Optional[Path] = None
    protein_sequence: Optional[str]  = None

    if lease.modality == "protein":
        # _resolve_protein_inputs fetches /align/manifest.json to map the
        # target name to the correct gz_file_name (UniProt-accession-based),
        # then calls align_fetch.fetch_alignment() to verify/download the .a3m.
        # It declines (returns None, None) with a log line if the target is
        # not in the manifest.
        msa_path, protein_sequence = _resolve_protein_inputs(lease, wlog)
        if msa_path is None:
            # MSA unavailable or target not in manifest; cannot score protein target
            return

    # ── 5. Cursor (restart-resume) ────────────────────────────────────
    cursor        = _load_cursor()
    resume_shard  = cursor.get("shard_id", -1)
    resume_row    = cursor.get("last_row", -1)
    same_lease    = cursor.get("lease_id") == lease.lease_id

    if not same_lease:
        # New lease → start fresh
        resume_shard = -1
        resume_row   = -1

    wlog.info(
        "[COORD] cursor: same_lease=%s  resume_shard=%s  resume_row=%s",
        same_lease, resume_shard, resume_row,
    )

    # ── 6. Shard iteration ───────────────────────────────────────────
    sm             = ShardManager()
    scored         = 0     # molecules with a finite contract_score that were enqueued
    failed         = 0     # SYSTEMIC failures (count toward abandon threshold)
    skipped        = 0     # cursor-resume skips
    skipped_mol    = 0     # per-molecule failures (bad SMILES, conformer, too large)
    consec_fail    = 0     # consecutive SYSTEMIC failures (reset on any success)
    fail_history: list[bool] = []    # rolling window for 95% rate check (True=systemic)
    skip_reasons: dict[str, int] = {}  # reason → count for summary line

    for smiles, shard_id, row in sm.iter_molecules(lease.shards):
        # Lease-expiry mid-run check
        if time.time() * 1000 >= lease.expires_at:
            wlog.warning(
                "[COORD] lease %s expired mid-shard — stopping iteration",
                lease.lease_id[:12],
            )
            break

        # Resume: skip rows already processed in a previous run
        if same_lease and shard_id == resume_shard and row <= resume_row:
            skipped += 1
            continue

        # Candidate length limit — advance cursor but do NOT count as scored/failed
        if len(smiles) > MAX_CANDIDATE_CHARS:
            wlog.warning(
                "[COORD] candidate too long (%d chars > %d) — dropping mol shard=%s row=%d",
                len(smiles), MAX_CANDIDATE_CHARS, shard_id, row,
            )
            _save_cursor(lease.lease_id, shard_id, row)
            continue

        # ── 7. Score ─────────────────────────────────────────────────
        t0 = time.time()
        raw_score: Optional[float] = None
        per_mol_skip = False

        try:
            if lease.modality == "protein":
                raw_score = _score_protein(
                    smiles, lease.target,
                    msa_path, protein_sequence,  # type: ignore[arg-type]
                    lease.scorer_profile, wlog,
                )
            elif lease.modality == "mrna":
                raw_score = _score_mrna(smiles, lease.target, wlog)
        except GpuOomError as oom_err:
            # C48: systemic GPU OOM → abandon lease immediately + post unfit report
            wlog.error(
                "[COORD] GPU_OOM: systemic OOM on target=%s lease=%s shard=%s row=%d "
                "— abandoning lease and posting unfit report (gpu_memory_mb=%d)",
                lease.target, lease.lease_id[:12], shard_id, row,
                client.gpu_memory_mb,
            )
            # Post the unfit report (with retry); failures are logged but not fatal
            client.post_unfit(
                lease_id=lease.lease_id,
                target=lease.target,
                gpu_memory_mb=client.gpu_memory_mb,
                gpu_model=client.gpu_model,
                molecules_attempted=scored + skipped_mol + 1,
                error=oom_err.reason,
            )
            # Abandon: stop processing this lease
            break
        except PerMoleculeError as per_err:
            # C26/C30: per-molecule failure — skip, log reason, do NOT count as systemic
            reason_str = str(per_err)
            # Carry the first 300 chars of the Boltz reason in the log line.
            short_reason = reason_str[:300]
            skip_reasons[short_reason[:60]] = skip_reasons.get(short_reason[:60], 0) + 1
            skipped_mol += 1
            per_mol_skip = True
            wlog.info(
                "[COORD] SKIP_MOL shard=%s row=%d  mol=%s…  reason=%s",
                shard_id, row, smiles[:40], short_reason,
            )
            # Advance cursor so we don't retry this molecule on restart
            _save_cursor(lease.lease_id, shard_id, row)

        if per_mol_skip:
            # C30: rolling 95%-skip check (per-molecule skips, not systemic failures).
            # If the vast majority of molecules in the last FAIL_RATE_WINDOW are
            # unscoreable, the shard is unusable and the lease should be abandoned
            # rather than burning the full time budget.
            fail_history.append(False)   # append False (not a systemic failure)
            if len(fail_history) > FAIL_RATE_WINDOW:
                fail_history.pop(0)
            # Compute skip rate over the window using skip_in_window counter.
            # We reuse fail_history length for the denominator but track skips separately.
            # Simple approach: if skipped_mol / (skipped_mol + scored) > 0.95 after
            # at least FAIL_RATE_WINDOW total molecules, abandon.
            total_attempted = skipped_mol + scored + failed
            if total_attempted >= FAIL_RATE_WINDOW:
                skip_rate = skipped_mol / total_attempted
                if skip_rate > 0.95:
                    wlog.error(
                        "[COORD] LEASE_ABANDONED: >95%% of last %d molecules were per-molecule "
                        "skips (rate=%.2f, scored=%d, skipped=%d, systemic=%d) on lease %s — "
                        "shard molecules are systematically unscoreable",
                        total_attempted, skip_rate, scored, skipped_mol, failed,
                        lease.lease_id[:12],
                    )
                    break
            continue

        elapsed = time.time() - t0

        # Convert to contract score
        contract_score = _to_contract_score(raw_score, lease.modality)

        # ── NaN / non-finite guard (Fix C25/C26) ─────────────────────
        # A None or non-finite contract_score means a SYSTEMIC failure.
        # A None or non-finite contract_score must never be enqueued,
        # posted, or counted as scored.  The cursor must NOT advance past
        # this molecule until a finite score is produced.
        if contract_score is None or not math.isfinite(contract_score):
            failed      += 1
            consec_fail += 1
            fail_history.append(True)
            if len(fail_history) > FAIL_RATE_WINDOW:
                fail_history.pop(0)
            wlog.warning(
                "[COORD] SCORE_FAIL shard=%s row=%d  mol=%s…  raw=%s  contract=%s  %.1fs"
                "  (consec=%d/%d)",
                shard_id, row, smiles[:40],
                raw_score, contract_score,
                elapsed, consec_fail, MAX_SCORER_FAILURES,
            )
            _write_jsonl({
                "ts": time.time(), "source": "coordinator",
                "lease_id": lease.lease_id, "target_id": lease.target,
                "modality": lease.modality, "shard_id": shard_id, "shard_row": row,
                "smiles": smiles, "raw_score": raw_score, "score": None,
                "molecule_hash": molecule_hash(smiles), "hit": False,
                "elapsed": round(elapsed, 2), "gpu": gpu_idx,
            })
            if consec_fail >= MAX_SCORER_FAILURES:
                wlog.error(
                    "[COORD] LEASE_ABANDONED: %d consecutive SYSTEMIC scorer failures on lease %s"
                    " — abandoning (consec_fail=%d >= MAX_SCORER_FAILURES=%d)",
                    consec_fail, lease.lease_id[:12],
                    consec_fail, MAX_SCORER_FAILURES,
                )
                break
            # C26: rolling 95% rate check
            if len(fail_history) >= FAIL_RATE_WINDOW:
                systemic_rate = sum(fail_history) / len(fail_history)
                if systemic_rate > 0.95:
                    wlog.error(
                        "[COORD] LEASE_ABANDONED: >95%% systemic failure rate in last %d mols"
                        " (rate=%.2f) on lease %s",
                        FAIL_RATE_WINDOW, systemic_rate, lease.lease_id[:12],
                    )
                    break
            # Do NOT save cursor — do not advance past a molecule that failed scoring
            continue

        # Score is finite: reset consecutive-failure counter
        consec_fail = 0
        fail_history.append(False)

        mol_hash      = molecule_hash(smiles)
        hit_threshold = lease.hit_threshold

        hit = (
            hit_threshold is not None
            and contract_score <= hit_threshold
            and contract_score < 0
        )

        wlog.info(
            "[COORD] shard=%s row=%d  mol=%s…  raw=%.4f  score=%.4f  %s  %.1fs",
            shard_id, row, smiles[:40],
            raw_score if raw_score is not None else float("nan"),
            contract_score,
            "✔ HIT" if hit else "✘",
            elapsed,
        )

        # ── 8. Enqueue ───────────────────────────────────────────────
        try:
            from miner_daemon import _detect_gpu_model
            gpu_model: Optional[str] = _detect_gpu_model(gpu_idx)[:128]
        except Exception:
            gpu_model = None

        client.enqueue_result(
            lease_id=lease.lease_id,
            molecule_hash=mol_hash,
            score=contract_score,
            candidate=smiles,
            shard_id=shard_id,
            shard_row=row,
            miner_gpu_model=gpu_model,
            expires_at=lease.expires_at,  # C28: used for startup stale-drop
            target=lease.target,          # C47b: required by Worker log-v2 audit trail
        )

        # ── 9. Audit JSONL ───────────────────────────────────────────
        _write_jsonl({
            "ts":           time.time(),
            "source":       "coordinator",
            "lease_id":     lease.lease_id,
            "target_id":    lease.target,
            "modality":     lease.modality,
            "shard_id":     shard_id,
            "shard_row":    row,
            "smiles":       smiles,
            "raw_score":    raw_score,
            "score":        contract_score,
            "molecule_hash": mol_hash,
            "hit":          hit,
            "elapsed":      round(elapsed, 2),
            "gpu":          gpu_idx,
        })

        # ── 10. Accumulate shared stats ──────────────────────────────
        try:
            shared_stats["molecules_screened"] = (
                shared_stats.get("molecules_screened", 0) + 1
            )
        except Exception:
            pass

        # Persist cursor only after a successful score
        _save_cursor(lease.lease_id, shard_id, row)
        scored += 1

    if skipped:
        wlog.info("[COORD] skipped %d already-processed molecules (cursor resume)", skipped)
    # C26: extended summary — scored, skipped_mol (per-molecule), failed (systemic), skipped (cursor)
    top_reasons = sorted(skip_reasons.items(), key=lambda x: -x[1])[:3]
    reasons_str = "; ".join(f"{r} ×{n}" for r, n in top_reasons) if top_reasons else "none"
    wlog.info(
        "[COORD] lease %s done — scored=%d  skipped_mol=%d(%s)  failed_systemic=%d  cursor_skip=%d  target=%s",
        lease.lease_id[:12], scored, skipped_mol, reasons_str, failed, skipped, lease.target,
    )
    _clear_cursor()

    # ── Back-off accounting (C24) ─────────────────────────────────────
    if scored > 0:
        _record_success()
    else:
        # scored == 0: all shards failed (format error, download fail, alignment
        # error, or empty shards).  Record as a failure and schedule back-off
        # so we don't hammer the coordinator with immediate re-requests that each
        # lock 4 more shards for 4 hours.
        _record_failure(wlog)
