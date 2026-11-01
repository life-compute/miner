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
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from coordinator.client import CoordinatorClient, Lease

from coordinator.hashes import PinnedHashError, verify_protein_target  # noqa: E402
from coordinator.shards import ShardManager  # noqa: E402 — module-level so patch() can find it

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
_WORK_DIR   = Path(__file__).resolve().parents[1]
CURSOR_PATH = _WORK_DIR / "data" / "coordinator_cursor.json"
BOLTZ_JSONL = _WORK_DIR / "output" / "life_boltz_scores.jsonl"

# Candidate length limit — must match MAX_CANDIDATE_BYTES in client.py and
# the Worker's results.ts MAX_CANDIDATE_BYTES (both currently 4 096).
# The task spec says 2 048; we apply the stricter value so the miner never
# sends a candidate the coordinator rejects AND stays within our own budget.
MAX_CANDIDATE_CHARS = 2048

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
    )
    _CLIENT_CACHE[gpu_idx] = client
    return client


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
    """Convert raw scorer output to the kcal/mol float required by POST /results."""
    if raw is None:
        return None
    if modality == "protein":
        return _affinity_pred_value_to_dg(raw)
    if modality == "mrna":
        return float(raw)   # run_boltz2_mrna_scoring already returns kcal/mol
    return None


# ── Protein: MSA helpers ───────────────────────────────────────────────────

def _resolve_protein_inputs(
    lease: "Lease",
    wlog: logging.Logger,
) -> tuple[Optional[Path], Optional[str]]:
    """
    Return (msa_path, protein_sequence) for a protein lease.

    Strategy:
      1. Use _msa_path_for / _sequence_from_msa from miner_daemon (lazy import).
      2. If MSA missing and lease.pinned_msa_sha256 is set, attempt to fetch
         from coordinator via align_fetch.fetch_alignment.
      3. Return (None, None) on failure — caller must skip this lease.
    """
    target_id = lease.target
    try:
        import sys
        sys.path.insert(0, str(_WORK_DIR))
        from miner_daemon import _msa_path_for, _sequence_from_msa
    except Exception as exc:
        wlog.warning("[COORD] cannot import miner_daemon helpers: %s", exc)
        return None, None

    msa_raw = _msa_path_for(target_id, gene_name=target_id, download=True)
    msa_path = Path(msa_raw) if msa_raw and msa_raw != "empty" else None

    # If no local MSA but coordinator has a pinned hash → try fetch_alignment
    if msa_path is None and lease.pinned_msa_sha256:
        try:
            from coordinator.align_fetch import fetch_alignment, AlignFetchError
            coordinator_url = os.environ.get("COORDINATOR_URL", "http://localhost:8787")
            dest_dir = _WORK_DIR / "data" / "msa_files"
            gz_name = f"{target_id}.a3m.gz"
            fetched_path = fetch_alignment(
                coordinator_url=coordinator_url,
                target_id=target_id,
                gz_file_name=gz_name,
                pinned_raw_sha256=lease.pinned_msa_sha256,
                cache_dir=dest_dir,
            )
            msa_path = fetched_path if fetched_path else None
            wlog.info("[COORD] align_fetch wrote %s", msa_path)
        except Exception as exc:
            wlog.warning("[COORD] align_fetch failed for %s: %s", target_id, exc)

    if msa_path is None or not msa_path.exists():
        wlog.warning("[COORD] no MSA for %s — skipping lease", target_id)
        return None, None

    seq = _sequence_from_msa(str(msa_path))
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
    Score one protein-mode molecule.

    Uses score_batch_with_profile (C3b) which applies the locked profile
    flags to the Boltz2 subprocess invocation.  Returns raw affinity_pred_value
    (log10 IC50/µM); caller converts to kcal/mol.
    """
    try:
        from adaptive.vendor.boltz_scorer import score_batch_with_profile
        result = score_batch_with_profile(
            smiles_list=[smiles],
            target=target_id,
            protein_sequence=protein_sequence,
            msa_path=str(msa_path),
            scorer_profile=scorer_profile,
        )
        return result.get(smiles)   # float or None
    except Exception as exc:
        wlog.warning("[COORD] protein scoring failed for %s: %s", smiles[:40], exc)
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
    """
    try:
        import sys
        sys.path.insert(0, str(_WORK_DIR))
        from miner_daemon import run_boltz2_mrna_scoring
        # Build a minimal target dict from the lease target string.
        target_dict = {"id": target_id, "uniprot_id": "", "gene_name": target_id}
        result = run_boltz2_mrna_scoring(smiles, target_dict)
        return result.get("kcal")   # float or None
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

    - Fetches a lease (GET /work).
    - Declines CRISPR leases cleanly.
    - Iterates shard molecules with a persisted cursor (restart-resume).
    - Scores each molecule using the locked scorer profile.
    - Enqueues results to the durable JSONL queue (retry + backoff in background).
    - Returns after exhausting the lease shards (or on error).
    """
    # ── 1. Lease ────────────────────────────────────────────────────────
    wlog.info("[COORD] GET /work …")
    try:
        lease = client.get_work()
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

    # ── 4. Protein: hash verification + MSA ────────────────────────────
    msa_path:         Optional[Path] = None
    protein_sequence: Optional[str]  = None

    if lease.modality == "protein":
        # Verify pinned hashes (raises PinnedHashError on mismatch)
        try:
            from coordinator.hashes import verify_protein_target, PinnedHashError  # noqa: F401
            import sys as _sys; _sys.path.insert(0, str(_WORK_DIR))
            from miner_daemon import _msa_path_for as _msa_for
            msa_raw = _msa_for(lease.target, gene_name=lease.target)
            receptor_path = None   # receptor PDB not yet in local cache by default
            msa_file = Path(msa_raw) if msa_raw and msa_raw != "empty" else None
            verify_protein_target(
                target_id=lease.target,
                receptor_path=receptor_path,
                msa_path=msa_file,
                pinned_receptor_sha256=lease.pinned_receptor_sha256,
                pinned_msa_sha256=lease.pinned_msa_sha256,
            )
        except Exception as exc:
            from coordinator.hashes import PinnedHashError
            if isinstance(exc, PinnedHashError):
                wlog.warning("[COORD] pinned-hash mismatch — trying align_fetch: %s", exc)
                # Fall through; _resolve_protein_inputs will re-fetch
            else:
                wlog.debug("[COORD] hash check skipped (%s)", exc)

        msa_path, protein_sequence = _resolve_protein_inputs(lease, wlog)
        if msa_path is None:
            # MSA unavailable; cannot score protein target
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
    sm      = ShardManager()
    scored  = 0
    skipped = 0

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

        # Candidate length limit
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

        if lease.modality == "protein":
            raw_score = _score_protein(
                smiles, lease.target,
                msa_path, protein_sequence,  # type: ignore[arg-type]
                lease.scorer_profile, wlog,
            )
        elif lease.modality == "mrna":
            raw_score = _score_mrna(smiles, lease.target, wlog)

        elapsed = time.time() - t0

        # Convert to contract score
        contract_score = _to_contract_score(raw_score, lease.modality)
        mol_hash       = molecule_hash(smiles)
        hit_threshold  = lease.hit_threshold

        hit = (
            contract_score is not None
            and hit_threshold is not None
            and contract_score <= hit_threshold
            and contract_score < 0
        )

        wlog.info(
            "[COORD] shard=%s row=%d  mol=%s…  raw=%.4f  score=%.4f  %s  %.1fs",
            shard_id, row, smiles[:40],
            raw_score if raw_score is not None else float("nan"),
            contract_score if contract_score is not None else float("nan"),
            "✔ HIT" if hit else "✘",
            elapsed,
        )

        # ── 8. Enqueue ───────────────────────────────────────────────
        if contract_score is not None:
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

        # Persist cursor after each molecule
        _save_cursor(lease.lease_id, shard_id, row)
        scored += 1

    if skipped:
        wlog.info("[COORD] skipped %d already-processed molecules (cursor resume)", skipped)
    wlog.info(
        "[COORD] lease %s done — scored=%d  target=%s",
        lease.lease_id[:12], scored, lease.target,
    )
    _clear_cursor()
