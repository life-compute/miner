"""
life_diversity.py — Shannon entropy enforcement and Tanimoto deduplication.

Analogous to nova_diversity.py but adapted for Life Compute:
  • SubmissionMemory: persists submitted SMILES fingerprints across restarts
    so the miner never re-submits a similar molecule even after a restart.
  • is_novel(smiles, threshold): fast Tanimoto check vs submission memory.
  • batch_shannon_entropy(smiles_list): bits/bit on Morgan FP bit distribution.
  • greedy_diverse_select(ranked, n, threshold): same greedy algorithm as Nova.
  • enforce_entropy_min(smiles_list, min_entropy): warns and filters if batch
    entropy is too low (hard-copy of a prior submission set).

Design
------
Morgan fingerprint: radius=2, 2048 bits (same as nova_diversity for consistency;
wider than life_art's 512-bit FP to improve Tanimoto discrimination).

Submission memory is stored as:
  output/life_submitted_memory.jsonl
  Each row: {"smiles": ..., "fp_hex": ..., "boltz_score": ..., "ts": ...}

The fp_hex is stored so memory can be reloaded without re-computing RDKit.
Ring buffer: last MAX_MEMORY_SIZE entries only (evict oldest on overflow).
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────
SIM_THRESHOLD   = 0.65     # Tanimoto ≥ this → too similar (reject)
FP_RADIUS       = 2
FP_NBITS        = 2048
MIN_ENTROPY     = 0.50     # bits/bit — warn if batch entropy below this
MAX_MEMORY_SIZE = 5000     # ring-buffer cap for submission memory

LIFE_DIR      = Path(__file__).resolve().parents[1]
OUTPUT_DIR    = LIFE_DIR / "output"
MEMORY_JSONL  = OUTPUT_DIR / "life_submitted_memory.jsonl"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Fingerprint helpers ────────────────────────────────────────────────────────

def morgan_fingerprint(smiles: str) -> Optional[object]:
    """Return a Morgan ExplicitBitVect or None if unparseable."""
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return AllChem.GetMorganFingerprintAsBitVect(mol, FP_RADIUS, nBits=FP_NBITS)
    except Exception:
        return None


def tanimoto_similarity(fp1, fp2) -> float:
    """Tanimoto coefficient between two RDKit fingerprints."""
    try:
        from rdkit import DataStructs
        return DataStructs.TanimotoSimilarity(fp1, fp2)
    except Exception:
        return 0.0


def _fp_to_hex(fp) -> str:
    """Serialize an RDKit ExplicitBitVect to hex string for JSONL storage."""
    try:
        return fp.ToBitString()  # '010110...' — deterministic, no rdkit import needed
    except Exception:
        return ""


def _fp_from_bitstring(bs: str):
    """Reconstruct fingerprint from bitstring."""
    try:
        from rdkit import DataStructs
        fp = DataStructs.ExplicitBitVect(len(bs))
        for i, ch in enumerate(bs):
            if ch == "1":
                fp.SetBit(i)
        return fp
    except Exception:
        return None


# ── Shannon entropy ────────────────────────────────────────────────────────────

def batch_shannon_entropy(smiles_list: list[str]) -> float:
    """
    Per-bit Shannon entropy of the Morgan FP bit distribution across the batch.
    Each bit position treated as Bernoulli; entropy averaged over FP_NBITS.

    Returns 0.0 on empty / all-invalid input.
    """
    if not smiles_list:
        return 0.0
    fps = [morgan_fingerprint(s) for s in smiles_list]
    fps = [fp for fp in fps if fp is not None]
    if not fps:
        return 0.0
    n         = len(fps)
    bit_counts = [0] * FP_NBITS
    for fp in fps:
        for bit in fp.GetOnBits():
            bit_counts[bit] += 1
    entropy = 0.0
    for cnt in bit_counts:
        if cnt == 0 or cnt == n:
            continue
        p1 = cnt / n
        p0 = 1.0 - p1
        entropy -= p1 * math.log2(p1) + p0 * math.log2(p0)
    return entropy / FP_NBITS


# ── Greedy diverse selection ───────────────────────────────────────────────────

def greedy_diverse_select(
    ranked: list[tuple[str, str, float]],
    n: int = 50,
    sim_threshold: float = SIM_THRESHOLD,
) -> list[tuple[str, str, float]]:
    """
    Select up to n diverse candidates from ranked list (best score first).

    A candidate is accepted iff its Tanimoto similarity to every already-
    accepted candidate is strictly below sim_threshold.  Walk from best to
    worst score; stop when n accepted or list exhausted.

    Parameters
    ----------
    ranked        : [(label, smiles, score), ...] sorted descending by score.
    n             : maximum candidates to return.
    sim_threshold : Tanimoto acceptance ceiling.

    Returns
    -------
    List of (label, smiles, score), length ≤ n, descending score order.
    """
    selected:     list[tuple[str, str, float]] = []
    selected_fps: list                          = []

    for label, smiles, score in ranked:
        fp = morgan_fingerprint(smiles)
        if fp is None:
            continue
        too_similar = any(
            tanimoto_similarity(fp, sfp) >= sim_threshold
            for sfp in selected_fps
        )
        if too_similar:
            continue
        selected.append((label, smiles, score))
        selected_fps.append(fp)
        if len(selected) >= n:
            break

    return selected


# ── Novelty check (vs submission memory) ──────────────────────────────────────

def is_novel(smiles: str, threshold: float = SIM_THRESHOLD) -> bool:
    """
    Return True if smiles is sufficiently different from all previously
    submitted molecules stored in SubmissionMemory (the default singleton).

    Convenience wrapper around SubmissionMemory.is_novel().
    """
    return _GLOBAL_MEMORY.is_novel(smiles, threshold)


def enforce_entropy_min(
    smiles_list: list[str],
    min_entropy: float = MIN_ENTROPY,
) -> tuple[list[str], float]:
    """
    Compute batch entropy; log a warning if below min_entropy.
    Returns (smiles_list, entropy) — does NOT filter (entropy gate is advisory).
    """
    ent = batch_shannon_entropy(smiles_list)
    if ent < min_entropy and len(smiles_list) > 1:
        print(f"[DIVERSITY] WARN: batch entropy={ent:.3f} < {min_entropy} — "
              f"batch may be too homogeneous ({len(smiles_list)} molecules)")
    return smiles_list, ent


# ── SubmissionMemory ──────────────────────────────────────────────────────────

class SubmissionMemory:
    """
    Persistent ring buffer of submitted molecule fingerprints.

    Loaded from life_submitted_memory.jsonl at construction; updated on
    each mark_submitted() call.  Thread-safety: single-process only (no locks).
    """

    def __init__(self, path: Path = MEMORY_JSONL, max_size: int = MAX_MEMORY_SIZE):
        self._path    = path
        self._max     = max_size
        self._entries: list[dict]  = []   # [{smiles, fp_bs, boltz_score, ts}, ...]
        self._fps:     list        = []   # parallel list of decoded FPs
        self._smiles:  set[str]    = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text().splitlines():
            try:
                row = json.loads(line)
                smi = row.get("smiles", "")
                bs  = row.get("fp_bs", "")
                if not smi or smi in self._smiles:
                    continue
                fp = _fp_from_bitstring(bs) if bs else morgan_fingerprint(smi)
                if fp is None:
                    continue
                self._entries.append(row)
                self._fps.append(fp)
                self._smiles.add(smi)
            except Exception:
                pass
        # Truncate to ring-buffer size (keep most recent)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]
            self._fps     = self._fps[-self._max:]
            self._smiles  = {e["smiles"] for e in self._entries}

    def is_novel(self, smiles: str, threshold: float = SIM_THRESHOLD) -> bool:
        """Return True if smiles is not too similar to any submitted molecule."""
        if smiles in self._smiles:
            return False
        fp = morgan_fingerprint(smiles)
        if fp is None:
            return False
        return all(tanimoto_similarity(fp, sfp) < threshold for sfp in self._fps)

    def mark_submitted(
        self,
        smiles: str,
        boltz_score: Optional[float] = None,
    ) -> None:
        """Record a submission; persist to JSONL."""
        if smiles in self._smiles:
            return
        fp = morgan_fingerprint(smiles)
        if fp is None:
            return
        fp_bs = _fp_to_hex(fp)
        row   = {
            "smiles":      smiles,
            "fp_bs":       fp_bs,
            "boltz_score": boltz_score,
            "ts":          time.time(),
        }
        with self._path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        self._entries.append(row)
        self._fps.append(fp)
        self._smiles.add(smiles)
        # Ring-buffer overflow: evict oldest
        if len(self._entries) > self._max:
            self._entries.pop(0)
            self._fps.pop(0)
            self._smiles = {e["smiles"] for e in self._entries}

    def filter_novel(
        self,
        candidates: list[tuple[str, str, float]],
        threshold: float = SIM_THRESHOLD,
    ) -> list[tuple[str, str, float]]:
        """
        Return only candidates that are novel vs the submission memory.
        Inputs are (label, smiles, score) tuples.
        """
        out  = []
        seen_fps: list = []
        for label, smi, score in candidates:
            if not self.is_novel(smi, threshold):
                continue
            fp = morgan_fingerprint(smi)
            if fp is None:
                continue
            # Also deduplicate within the output batch itself
            if any(tanimoto_similarity(fp, sfp) >= threshold for sfp in seen_fps):
                continue
            out.append((label, smi, score))
            seen_fps.append(fp)
        return out

    def __len__(self) -> int:
        return len(self._entries)

    def entropy(self) -> float:
        """Shannon entropy of the current memory FP distribution."""
        return batch_shannon_entropy([e["smiles"] for e in self._entries])


# Module-level singleton — miner_daemon imports and reuses this across cycles
_GLOBAL_MEMORY = SubmissionMemory()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Life Diversity tools")
    p.add_argument("--status",  action="store_true")
    p.add_argument("--entropy", action="store_true",
                   help="Print entropy of current submission memory")
    p.add_argument("--test-smiles", nargs="*",
                   help="Test novelty for given SMILES")
    args = p.parse_args()

    mem = SubmissionMemory()
    if args.status:
        print(f"Submission memory: {len(mem)} entries")
        print(f"JSONL: {MEMORY_JSONL}")
    if args.entropy:
        ent = mem.entropy()
        print(f"Memory entropy: {ent:.4f} bits/bit  (min={MIN_ENTROPY})")
    if args.test_smiles:
        for smi in args.test_smiles:
            novel = mem.is_novel(smi)
            fp    = morgan_fingerprint(smi)
            print(f"  {'NOVEL' if novel else 'TOO SIMILAR'}  {smi}")
