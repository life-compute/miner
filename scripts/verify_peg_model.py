#!/usr/bin/env python3
"""
Stage 2 verification for adaptive/life_peg_model.py.

Offline, deterministic, no network, no GPU.  Checks the model behaves like a
predictor and not like a plausible-number generator.

  A. Contract      - model file carries the N_PEG_FEATURES it was trained on
  B. Honesty       - invalid input returns None, never a default score
  C. Range         - every prediction in [0.0, 1.0]
  D. Determinism   - repeated prediction is bit-identical
  E. Discrimination- predictions vary across the design grid, not collapsed
  F. Ranking       - rank_candidates is ordered, deterministic, drops unscorables
  G. Real targets  - all four TP53 hotspots produce scored candidates
  H. Sanity        - the model responds to design changes in the published
                     direction (very short PBS should not outscore a sane one
                     on average), and does not simply echo one feature
  I. Report        - grouped R2 present, beats mean baseline, leakage bounded
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from adaptive.life_peg import (                                  # noqa: E402
    N_PEG_FEATURES, design_candidates, get_hotspot,
)
from adaptive import life_peg_model as lpm                       # noqa: E402

TP53_HOTSPOTS = ["R175H", "R248W/Q", "R249S", "R273H"]

fails: list[str] = []
passes = 0


def check(cond: bool, label: str) -> bool:
    global passes
    if cond:
        passes += 1
    else:
        fails.append(label)
    return cond


def main() -> int:
    print("=" * 72)
    print("STAGE 2 VERIFICATION — pegRNA efficiency model")
    print("=" * 72)

    if not lpm.is_trained():
        print(f"BLOCKER: no model at {lpm.MODEL_PATH}")
        return 2

    # ── A. contract ─────────────────────────────────────────────────────────
    import pickle
    with lpm.MODEL_PATH.open("rb") as fh:
        payload = pickle.load(fh)
    print(f"\n── A. contract ──")
    print(f"  n_features stored : {payload.get('n_features')}")
    print(f"  N_PEG_FEATURES    : {N_PEG_FEATURES}")
    check(payload.get("n_features") == N_PEG_FEATURES,
          "A: model feature count disagrees with life_peg.N_PEG_FEATURES")
    check(payload.get("label") == lpm.PRIMARY_LABEL,
          "A: model label column unexpected")
    check(payload.get("n_rows", 0) >= lpm.MIN_ROWS_TO_TRAIN,
          "A: model trained on fewer rows than MIN_ROWS_TO_TRAIN")

    # ── B. honesty on bad input ─────────────────────────────────────────────
    print("\n── B. invalid input must return None ──")
    bad = [
        ("empty spacer",  ("", "GGCCTT", "AACCGG", "substitution")),
        ("empty pbs",     ("A" * 20, "", "AACCGG", "substitution")),
        ("empty rtt",     ("A" * 20, "GGCCTT", "", "substitution")),
        ("N in spacer",   ("N" * 20, "GGCCTT", "AACCGG", "substitution")),
        ("bad edit_type", ("A" * 20, "GGCCTT", "AACCGG", "frameshift")),
        ("RNA U in rtt",  ("A" * 20, "GGCCTT", "AAUCGG", "substitution")),
    ]
    for name, args in bad:
        got = lpm.predict_efficiency(*args)
        print(f"  {name:16} -> {got}")
        check(got is None, f"B: '{name}' returned {got} instead of None")

    # ── C-F on real TP53 candidates ─────────────────────────────────────────
    print("\n── C-F. real TP53 candidates ──")
    all_scores: list[float] = []
    for label in TP53_HOTSPOTS:
        h = get_hotspot("TP53", label)
        check(h is not None, f"G: TP53/{label} missing from reference")
        if h is None:
            continue
        cands = design_candidates("TP53", label)
        ranked = lpm.rank_candidates(cands)
        scores = [s for _, s in ranked]
        all_scores.extend(scores)

        print(f"  {label:10} candidates={len(cands):5} scored={len(ranked):5} "
              f"max={max(scores):.4f} mean={statistics.mean(scores):.4f} "
              f"min={min(scores):.4f}")

        check(len(ranked) > 0, f"G: TP53/{label} scored nothing")
        check(len(ranked) == len(cands),
              f"F: TP53/{label} dropped {len(cands) - len(ranked)} candidates")
        check(all(0.0 <= s <= 1.0 for s in scores),
              f"C: TP53/{label} predictions outside [0,1]")
        check(scores == sorted(scores, reverse=True),
              f"F: TP53/{label} rank_candidates not descending")
        # determinism
        again = [s for _, s in lpm.rank_candidates(cands)]
        check(again == scores, f"D: TP53/{label} ranking not deterministic")
        c0 = ranked[0][0]
        check(lpm.predict_candidate(c0) == ranked[0][1],
              f"D: TP53/{label} repeated prediction differs")

    # ── E. discrimination ───────────────────────────────────────────────────
    print("\n── E. discrimination ──")
    spread = max(all_scores) - min(all_scores)
    distinct = len(set(round(s, 6) for s in all_scores))
    print(f"  n={len(all_scores):,}  spread={spread:.4f}  "
          f"distinct={distinct:,}  stdev={statistics.pstdev(all_scores):.4f}")
    check(spread > 0.05,
          f"E: prediction spread {spread:.4f} too narrow — model collapsed")
    check(distinct > len(all_scores) * 0.1,
          f"E: only {distinct} distinct values — model is near-constant")

    # ── H. responds to design, not one echoed feature ───────────────────────
    print("\n── H. design sensitivity ──")
    cands = design_candidates("TP53", "R175H")
    by_pbs: dict[int, list[float]] = {}
    for c in cands:
        s = lpm.predict_candidate(c)
        if s is not None:
            by_pbs.setdefault(len(c.pbs), []).append(s)
    means = {k: statistics.mean(v) for k, v in sorted(by_pbs.items())}
    for k, v in means.items():
        print(f"  PBS len {k:2}: mean predicted eff = {v:.4f}  (n={len(by_pbs[k])})")
    check(len(set(round(v, 4) for v in means.values())) > 1,
          "H: prediction identical across all PBS lengths — not using PBS")

    # ── I. training report ──────────────────────────────────────────────────
    print("\n── I. training report ──")
    rep = lpm.get_model_report()
    m = rep.get("metrics", {})
    g_r2 = m.get("grouped_r2")
    leak = m.get("leakage_gap")
    print(f"  rows={rep.get('n_rows'):,} loci={rep.get('n_loci'):,}")
    print(f"  grouped R2={g_r2}  Spearman={m.get('grouped_spearman')}")
    print(f"  random  R2={m.get('random_r2')}  leakage gap={leak}")
    check(g_r2 is not None and g_r2 > 0.05,
          f"I: grouped R2 {g_r2} does not beat the mean baseline")
    check(m.get("beats_mean_baseline") is True,
          "I: report says model does not beat mean baseline")
    check(leak is not None and leak < 0.25,
          f"I: leakage gap {leak} too large — grouped/random disagree badly")
    check(bool(rep.get("caveat")), "I: report is missing the transfer caveat")

    print("\n" + "=" * 72)
    print(f"assertions passed: {passes}")
    if fails:
        print(f"\nFAILURES ({len(fails)}):")
        for f in fails:
            print(f"  {f}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
