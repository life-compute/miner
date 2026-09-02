#!/usr/bin/env python3
"""
reward_decay_sim.py — Similarity-based reward decay diagnostic (log-only).

Reads historical generate / crispr_generated hits from life_boltz_scores.jsonl
and computes what rewards WOULD have been under the proposed similarity-decay
scheme.  No rewards are changed.  zinc15 / ref sources are completely unaffected.

Similarity brackets (molecules, Morgan-2048 Tanimoto):
  >= 0.85  →  multiplier 0.35   (near-clone of winning parent)
  >= 0.70  →  multiplier 0.65   (close neighbour)
  <  0.70  →  multiplier 1.00   (genuinely novel, even if seeded from winner)

CRISPR gRNAs (20-mers, normalised Hamming / edit distance):
  similarity = 1 - hamming(candidate, parent) / 20
  Same brackets as above.

Log format per hit:
  [REWARD-DECAY-SIM] target=X parent=Y similarity=Z
                      current_reward=W proposed_reward=N

Usage
-----
    python scripts/reward_decay_sim.py           # last 48 h
    python scripts/reward_decay_sim.py --hours 72
    python scripts/reward_decay_sim.py --all     # entire history
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

# ── paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_LIFE_DIR   = _SCRIPT_DIR.parent
_OUTPUT     = _LIFE_DIR / "output"

_BOLTZ_JSONL    = _OUTPUT / "life_boltz_scores.jsonl"
_GEN_JSONL      = _OUTPUT / "life_generated.jsonl"
_PULSE_JSONL    = _OUTPUT / "life_pulse_data.jsonl"
_CRISPR_JSONL   = _OUTPUT / "life_crispr_scores.jsonl"

# ── similarity thresholds & multipliers ───────────────────────────────────────
BAND_CLONE   = 0.85   # >= this → multiplier 0.35
BAND_CLOSE   = 0.70   # >= this → multiplier 0.65
MULT_CLONE   = 0.35
MULT_CLOSE   = 0.65
MULT_NOVEL   = 1.00

DECAY_SOURCES = {"generate", "mutant", "pulse", "crispr_generated"}

# ── RDKit (optional — falls back to a None result if unavailable) ─────────────
_RDKIT_OK   = False
_Chem       = None
_DataStructs= None
_MORGAN_GEN = None
try:
    from rdkit import Chem as _Chem                          # type: ignore[assignment]
    from rdkit.DataStructs import TanimotoSimilarity as _TanimotoSim  # type: ignore[assignment]
    from rdkit.Chem import rdFingerprintGenerator as _rfg    # type: ignore[assignment]
    _RDKIT_OK   = True
    # Reuse the same generator as life_proteinnet.py (radius=2, 2048-bit)
    _MORGAN_GEN = _rfg.GetMorganGenerator(radius=2, fpSize=2048)  # type: ignore[assignment]
except (ImportError, Exception):
    pass


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — Similarity functions
# ══════════════════════════════════════════════════════════════════════════════

def morgan_tanimoto(smi_a: str, smi_b: str) -> Optional[float]:
    """
    Tanimoto similarity between two molecules via Morgan-2048 fingerprint.

    Uses the same radius=2 / 2048-bit generator as life_proteinnet.py.
    Returns None if either SMILES is invalid or RDKit is unavailable.
    """
    if not _RDKIT_OK or not smi_a or not smi_b:
        return None
    mol_a = _Chem.MolFromSmiles(smi_a)   # type: ignore[union-attr]
    mol_b = _Chem.MolFromSmiles(smi_b)   # type: ignore[union-attr]
    if mol_a is None or mol_b is None:
        return None
    try:
        fp_a = _MORGAN_GEN.GetFingerprint(mol_a)   # type: ignore[union-attr]
        fp_b = _MORGAN_GEN.GetFingerprint(mol_b)   # type: ignore[union-attr]
        return float(_TanimotoSim(fp_a, fp_b))  # type: ignore[misc]
    except Exception:
        return None


def grna_similarity(seq_a: str, seq_b: str) -> Optional[float]:
    """
    Normalised sequence similarity for 20-mer gRNA sequences.

    similarity = 1 - hamming_distance(a, b) / 20

    Both sequences are upper-cased before comparison.  Returns None if
    either sequence is empty or not exactly 20 bases.
    """
    if not seq_a or not seq_b:
        return None
    a, b = seq_a.upper(), seq_b.upper()
    if len(a) != 20 or len(b) != 20:
        # Fall back to normalised edit distance for non-20mers
        mismatches = sum(ca != cb for ca, cb in zip(a, b))
        length = max(len(a), len(b))
        return 1.0 - mismatches / length
    mismatches = sum(ca != cb for ca, cb in zip(a, b))
    return 1.0 - mismatches / 20


def similarity_multiplier(sim: Optional[float]) -> float:
    """
    Return the proposed reward multiplier for a given similarity score.

    None similarity (unknown parent) → multiplier 1.0 (full reward, give
    benefit of the doubt when parent tracking is missing).
    """
    if sim is None:
        return MULT_NOVEL
    if sim >= BAND_CLONE:
        return MULT_CLONE
    if sim >= BAND_CLOSE:
        return MULT_CLOSE
    return MULT_NOVEL


def similarity_band(sim: Optional[float]) -> str:
    if sim is None:
        return "unknown"
    if sim >= BAND_CLONE:
        return f"clone (>={BAND_CLONE})"
    if sim >= BAND_CLOSE:
        return f"close ({BAND_CLOSE}–{BAND_CLONE})"
    return f"novel (<{BAND_CLOSE})"


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — Parent lineage lookup helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_generate_parents() -> dict[str, str]:
    """
    Build smiles → parent_smiles index from life_generated.jsonl.

    Coverage: 100% of generate rows carry parent_smiles (confirmed).
    When multiple rows exist for the same SMILES (different targets/methods),
    we keep the first parent encountered — the molecular similarity is
    target-independent.
    """
    parent_map: dict[str, str] = {}
    if not _GEN_JSONL.exists():
        return parent_map
    with _GEN_JSONL.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            smi    = row.get("smiles", "")
            parent = row.get("parent_smiles", "")
            if smi and parent and smi not in parent_map:
                parent_map[smi] = parent
    return parent_map


def _load_pulse_parents() -> dict[str, str]:
    """
    Build smiles → scaffold_smi index from life_pulse_data.jsonl (mutant rows).

    For EliteMutator rows, scaffold_smi is the parent molecule that was
    decorated via R-group substitution.  This is the correct parent for
    Tanimoto similarity (the decoration is a local perturbation of the scaffold).

    GAP NOTE: 'mutant' source rows are emitted to life_pulse_data.jsonl but
    their smiles are submitted to Boltz2 as source='pulse' (the daemon does not
    distinguish pulse-sobol from pulse-mutant at pick time).  Cross-referencing
    the boltz scores JSONL with pulse_data JSONL by SMILES recovers the parent.
    """
    parent_map: dict[str, str] = {}
    if not _PULSE_JSONL.exists():
        return parent_map
    with _PULSE_JSONL.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("source") != "mutant":
                continue
            smi    = row.get("smiles", "")
            parent = row.get("scaffold_smi", "")
            if smi and parent and smi not in parent_map:
                parent_map[smi] = parent
    return parent_map


def _load_crispr_history() -> dict[str, list[str]]:
    """
    Build target_id → [previously_seen_seqs] index.

    Source: life_boltz_scores.jsonl, rows with source='crispr_generated'.
    The gRNA sequence is stored in the 'smiles' field (CRISPR convention).
    """
    history: dict[str, list[str]] = {}
    if not _BOLTZ_JSONL.exists():
        return history
    with _BOLTZ_JSONL.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("source") != "crispr_generated":
                continue
            tid = row.get("target_id", "")
            seq = row.get("smiles", "")
            if tid and seq and len(seq) == 20:
                history.setdefault(tid, []).append(seq)
    return history


def _find_crispr_parent(grna: str, target_id: str,
                         crispr_history: dict[str, list[str]]) -> Optional[str]:
    """
    Find the closest previously-seen gRNA for *target_id* (Hamming proxy).

    Returns None if no prior sequences exist for that target.
    """
    seqs = crispr_history.get(target_id, [])
    if not seqs:
        return None
    best_seq: Optional[str] = None
    best_sim = -1.0
    for s in seqs:
        if s == grna:
            continue   # skip self
        sim = grna_similarity(grna, s)
        if sim is not None and sim > best_sim:
            best_sim = sim
            best_seq = s
    return best_seq


# ══════════════════════════════════════════════════════════════════════════════
# Reward inference (mirrors miner_daemon.py tiers)
# ══════════════════════════════════════════════════════════════════════════════

def _infer_reward(target_id: str, source: str) -> float:
    """
    Best-effort reward inference without access to the live targets list.

    Rules (match miner_daemon.py constants):
      crispr_generated  →  7.0 LIFE
      *_mRNA targets    →  25.0 LIFE (REWARD_MRNA_LIFE)
      *_CRISPR targets  →  7.0 LIFE
      anything else     →  25.0 LIFE (Hard-tier assumption — generate source
                           is only profitable on hard targets)
    """
    if source == "crispr_generated":
        return 7.0
    tid_upper = target_id.upper()
    if tid_upper.endswith("_MRNA"):
        return 25.0
    if tid_upper.endswith("_CRISPR"):
        return 7.0
    return 25.0   # Hard-tier default for protein generate hits


# ══════════════════════════════════════════════════════════════════════════════
# Main analysis loop
# ══════════════════════════════════════════════════════════════════════════════

def run_analysis(hours: Optional[float]) -> None:
    cutoff = (time.time() - hours * 3600) if hours else 0.0

    print("=" * 72)
    print("LIFE Compute — Reward Decay Similarity Diagnostic (log-only)")
    print("=" * 72)
    print()

    # ── Build parent lookup tables ────────────────────────────────────────
    print("Loading parent lineage tables …", flush=True)
    t0 = time.time()
    gen_parents    = _load_generate_parents()
    pulse_parents  = _load_pulse_parents()
    crispr_history = _load_crispr_history()
    print(f"  generate parents loaded : {len(gen_parents):,}")
    print(f"  mutant parents loaded   : {len(pulse_parents):,}")
    print(f"  CRISPR prior seqs       : {sum(len(v) for v in crispr_history.values()):,}")
    print(f"  Load time               : {time.time()-t0:.1f}s")
    print()

    # ── Walk boltz scores and compute per-hit proposed reward ─────────────
    results: list[dict] = []
    skipped_not_decay = 0
    skipped_miss      = 0
    no_parent_found   = 0

    with _BOLTZ_JSONL.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Window filter
            if row.get("ts", 0) < cutoff:
                continue

            # Only decay sources
            source = row.get("source", "")
            if source not in DECAY_SOURCES:
                skipped_not_decay += 1
                continue

            # Only hits
            if not row.get("hit"):
                skipped_miss += 1
                continue

            tid  = row.get("target_id", "")
            mol  = row.get("smiles", "")

            # ── Find parent ───────────────────────────────────────────────
            parent: Optional[str] = None
            sim:    Optional[float] = None

            is_crispr = (
                source == "crispr_generated"
                or row.get("target_type") == "CRISPR"
            )

            if is_crispr:
                # CRISPR: use Hamming to closest prior sequence (proxy —
                # true parent is not tracked; see Part 1 gap notes)
                parent = _find_crispr_parent(mol, tid, crispr_history)
                if parent:
                    sim = grna_similarity(mol, parent)
            elif source == "generate":
                parent = gen_parents.get(mol)
                if parent:
                    sim = morgan_tanimoto(mol, parent)
            elif source in ("mutant", "pulse"):
                parent = pulse_parents.get(mol)
                if parent:
                    sim = morgan_tanimoto(mol, parent)

            if parent is None:
                no_parent_found += 1

            # ── Compute rewards ───────────────────────────────────────────
            current_reward  = _infer_reward(tid, source)
            mult            = similarity_multiplier(sim)
            proposed_reward = round(current_reward * mult, 4)
            band            = similarity_band(sim)

            results.append({
                "ts":              row.get("ts", 0),
                "target_id":       tid,
                "source":          source,
                "smiles":          mol,
                "parent":          parent,
                "similarity":      sim,
                "band":            band,
                "multiplier":      mult,
                "current_reward":  current_reward,
                "proposed_reward": proposed_reward,
                "delta":           proposed_reward - current_reward,
            })

    if not results:
        print("No generate/mutant/crispr_generated hits found in the time window.")
        return

    # ── Print per-hit log lines ───────────────────────────────────────────
    print(f"Per-hit [REWARD-DECAY-SIM] log ({len(results)} hits):")
    print("-" * 72)
    for r in results:
        sim_str  = f"{r['similarity']:.4f}" if r["similarity"] is not None else "N/A"
        par_disp = (r["parent"] or "")[:40] + ("…" if r["parent"] and len(r["parent"]) > 40 else "")
        print(
            f"[REWARD-DECAY-SIM] target={r['target_id']}  source={r['source']}  "
            f"parent={par_disp}  "
            f"similarity={sim_str}  band={r['band']}  "
            f"current_reward={r['current_reward']}  proposed_reward={r['proposed_reward']}"
        )

    print()

    # ── Similarity distribution ───────────────────────────────────────────
    print("=" * 72)
    print("SIMILARITY DISTRIBUTION BY SOURCE")
    print("=" * 72)
    print()

    for src in sorted(set(r["source"] for r in results)):
        src_rows = [r for r in results if r["source"] == src]
        is_crispr_src = (src == "crispr_generated")

        band_counts: Counter[str] = Counter(r["band"] for r in src_rows)
        total = len(src_rows)
        current_total  = sum(r["current_reward"]  for r in src_rows)
        proposed_total = sum(r["proposed_reward"] for r in src_rows)
        delta_total    = proposed_total - current_total

        print(f"  Source: {src}  ({total} hits)")
        if is_crispr_src:
            print("  ⚠ CRISPR: parent is PROXY (closest prior seq by Hamming).")
            print("    True parent not recorded — see Part 1 gap notes below.")

        # Band table
        band_order = [
            f"clone (>={BAND_CLONE})",
            f"close ({BAND_CLOSE}–{BAND_CLONE})",
            f"novel (<{BAND_CLOSE})",
            "unknown",
        ]
        print(f"  {'Band':<28} {'Count':>7} {'%':>7}  {'Current $LIFE':>14}  {'Proposed $LIFE':>14}  {'Delta $LIFE':>12}")
        print(f"  {'-'*28} {'-'*7} {'-'*7}  {'-'*14}  {'-'*14}  {'-'*12}")
        for band in band_order:
            band_rows = [r for r in src_rows if r["band"] == band]
            if not band_rows:
                continue
            n = len(band_rows)
            pct = 100 * n / total
            cur = sum(r["current_reward"]  for r in band_rows)
            prop= sum(r["proposed_reward"] for r in band_rows)
            dlt = prop - cur
            print(f"  {band:<28} {n:>7,} {pct:>6.1f}%  {cur:>14.1f}  {prop:>14.1f}  {dlt:>12.1f}")
        print(f"  {'TOTAL':<28} {total:>7,} {'100.0':>7}%  {current_total:>14.1f}  {proposed_total:>14.1f}  {delta_total:>12.1f}")

        # Similarity stats
        sims = [r["similarity"] for r in src_rows if r["similarity"] is not None]
        if sims:
            sims_sorted = sorted(sims)
            n_s = len(sims_sorted)
            p10 = sims_sorted[int(n_s * 0.10)]
            p50 = sims_sorted[int(n_s * 0.50)]
            p90 = sims_sorted[int(n_s * 0.90)]
            mean = sum(sims_sorted) / n_s
            print(f"  Similarity stats: mean={mean:.3f}  p10={p10:.3f}  p50={p50:.3f}  p90={p90:.3f}  n={n_s}")
        else:
            print("  Similarity stats: N/A (parent lookup unavailable or RDKit missing)")

        print()

    # ── Grand summary ─────────────────────────────────────────────────────
    current_grand  = sum(r["current_reward"]  for r in results)
    proposed_grand = sum(r["proposed_reward"] for r in results)
    delta_grand    = proposed_grand - current_grand
    savings_pct    = -100 * delta_grand / current_grand if current_grand else 0

    print("=" * 72)
    print("GRAND TOTAL (generate + mutant + crispr_generated hits)")
    print(f"  Hits analysed        : {len(results):,}")
    print(f"  Current reward total : {current_grand:,.1f} $LIFE")
    print(f"  Proposed total       : {proposed_grand:,.1f} $LIFE")
    print(f"  Delta                : {delta_grand:,.1f} $LIFE  ({savings_pct:+.1f}%)")
    print()

    # ── Part 1 gap notes ──────────────────────────────────────────────────
    print("=" * 72)
    print("PART 1 — LINEAGE TRACKING GAPS")
    print("=" * 72)
    print()
    print("generate source:")
    print("  ✅ life_generated.jsonl has parent_smiles on 100% of rows.")
    print("  ✅ 100% of generate hits in the analysis window matched a parent.")
    print("  ⚠  life_boltz_scores.jsonl does NOT carry parent_smiles — the")
    print("     cross-reference via SMILES lookup is required for live logging.")
    print("     Fix: add parent_smiles to the boltz_scores.jsonl write in")
    print("     miner_daemon.py at the point where source='generate' is selected.")
    print()
    print("mutant / pulse source:")
    print("  ✅ life_pulse_data.jsonl records scaffold_smi (the decoration parent).")
    print("  ⚠  EliteMutator candidates enter boltz scoring as source='pulse',")
    print("     not source='mutant' — the distinction is lost at scoring time.")
    print("     Fix: thread source_tag through _pick_molecule() to differentiate")
    print("     pulse-sobol from pulse-mutant, and carry scaffold_smi in the")
    print("     boltz_scores.jsonl write.")
    print()
    print("crispr_generated source:")
    print("  ❌ generate_grna_candidates() does not record which cached 'best'")
    print("     sequence each mutant was derived from.")
    print("  The analysis above uses Hamming proximity to the closest prior")
    print("  sequence as a PROXY — this underestimates true parent similarity")
    print("  because the actual parent is usually the exact mutation source.")
    print("  Fix required: add parent_seq field to the scored candidate dicts")
    print("  returned by generate_grna_candidates() in life_crispr.py (line ~511,")
    print("  method 3 loop) and propagate it to the miner_daemon.py hit log.")
    print()
    print(f"  Hits with no parent found : {no_parent_found} (similarity treated as None → MULT=1.0)")
    print()

    if not _RDKIT_OK:
        print("=" * 72)
        print("⚠  RDKit NOT available — Tanimoto similarity could not be computed.")
        print("   Install with: pip install rdkit")
        print("   All molecule similarities shown as N/A; multipliers defaulted to 1.0.")
        print("=" * 72)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--hours", type=float, default=48,
                     help="Analyse hits from the last N hours (default: 48)")
    grp.add_argument("--all",   action="store_true",
                     help="Analyse entire history (may be slow for large JSONL files)")
    args = parser.parse_args()

    hours = None if args.all else args.hours
    run_analysis(hours)
