#!/usr/bin/env python3
"""
train_peg_model.py — Stage 2 trainer for the pegRNA efficiency predictor.

Reconstructs pegRNA segments from the vendored PRIDICT2 library, featurises
them with the Stage 1 extractor, and fits a GradientBoostingRegressor against
REAL measured HEK293T editing efficiencies.

The leakage question, and why grouping matters
----------------------------------------------
The library contains many pegRNA designs per target locus (`grp_id`).  A
random train/test split puts designs from the SAME locus on both sides, so the
model can memorise "locus X edits well" and score a high R2 without having
learned anything transferable about pegRNA DESIGN.  That is the same confound
that faked an R2 of 0.57 in the ART/chromosome work.

This script therefore reports BOTH:
  * GroupKFold by grp_id  <- the honest number, held-out loci
  * plain KFold           <- shown only to expose the size of the leak

Deployment uses the grouped number.  If grouped R2 is near zero the model is
not learning design rules and should not be trusted, regardless of what the
random split claims.

Usage:
    python scripts/train_peg_model.py
    python scripts/train_peg_model.py --label K562averageedited_clamped
    python scripts/train_peg_model.py --limit 4000      # fast smoke run
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from adaptive.life_peg import (                              # noqa: E402
    N_PEG_FEATURES, PEG_FEATURE_NAMES, featurize_pegrna, revcomp,
)
from adaptive import life_peg_model as lpm                   # noqa: E402

CSV_PATH = REPO / "data" / "peg_training" / "pridict2_23k.csv"

# PRIDICT Correction_Type -> our edit_type vocabulary
EDIT_TYPE_MAP = {
    "Replacement": "substitution",
    "Insertion": "insertion",
    "Deletion": "deletion",
}


def reconstruct(row: dict) -> dict | None:
    """
    Rebuild (spacer, pbs, rtt) plus geometry from one library row.

    Coordinates in the CSV are [start, end) into the 99nt wide target windows.
    The PBS anneals 5' of the nick and the RT template copies the MUTATED
    strand, so both are stored reverse-complemented - same convention as
    life_peg.design_candidates().
    """
    try:
        w = row["wide_initial_target"]
        m = row["wide_mutated_target"]
        ps = ast.literal_eval(row["protospacerlocation_only_initial"])
        pb = ast.literal_eval(row["PBSlocation"])
        rt = ast.literal_eval(row["RT_initial_location"])
    except Exception:
        return None

    spacer = w[ps[0]:ps[1]]
    pbs = revcomp(w[pb[0]:pb[1]])
    rtt = revcomp(m[rt[0]:rt[1]])
    if not (spacer and pbs and rtt):
        return None

    edit_type = EDIT_TYPE_MAP.get(row["Correction_Type"])
    if edit_type is None:
        return None

    try:
        corr_len = int(float(row["Correction_Length"]))
        edit_pos = int(float(row["Editing_Position"]))
    except (TypeError, ValueError):
        return None

    # Nick sits at the 3' end of the protospacer; the RT template starts there.
    nick_to_edit = edit_pos
    edit_to_rtt_end = len(rtt) - edit_pos - corr_len

    return {
        "spacer": spacer, "pbs": pbs, "rtt": rtt, "edit_type": edit_type,
        "nick_to_edit": nick_to_edit,
        "correction_length": corr_len,
        "edit_to_rtt_end": edit_to_rtt_end,
        "grp_id": row["grp_id"],
        "seq_id": row["seq_id"],
    }


def build_dataset(rows: list[dict], label_col: str):
    """Featurise every usable row.  Returns (X, y, groups, n_skipped)."""
    X, y, groups = [], [], []
    skipped = 0
    for row in rows:
        rec = reconstruct(row)
        if rec is None:
            skipped += 1
            continue
        raw = row.get(label_col)
        if raw in (None, "", "nan"):
            skipped += 1
            continue
        try:
            label = float(raw)
        except (TypeError, ValueError):
            skipped += 1
            continue

        feats = featurize_pegrna(
            rec["spacer"], rec["pbs"], rec["rtt"], rec["edit_type"],
            nick_to_edit=rec["nick_to_edit"],
            correction_length=rec["correction_length"],
            edit_to_rtt_end=rec["edit_to_rtt_end"],
        )
        if feats is None:
            skipped += 1
            continue

        X.append(feats)
        y.append(label)
        groups.append(rec["grp_id"])
    return X, y, groups, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default=lpm.PRIMARY_LABEL)
    ap.add_argument("--limit", type=int, default=0,
                    help="use only the first N rows (smoke test)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    if not CSV_PATH.exists():
        print(f"BLOCKER: {CSV_PATH} missing — run "
              f"scripts/fetch_peg_training_data.py first")
        return 2

    import numpy as np
    from scipy.stats import spearmanr
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import GroupKFold, KFold

    rows = list(csv.DictReader(CSV_PATH.open()))
    if args.limit:
        rows = rows[:args.limit]
    print("=" * 72)
    print("STAGE 2 — pegRNA efficiency model")
    print("=" * 72)
    print(f"library rows : {len(rows):,}")
    print(f"label        : {args.label}")

    t0 = time.time()
    X, y, groups, skipped = build_dataset(rows, args.label)
    print(f"featurised   : {len(X):,}  (skipped {skipped:,})  "
          f"[{time.time() - t0:.1f}s]")
    print(f"features     : {N_PEG_FEATURES}")
    print(f"distinct loci: {len(set(groups)):,}")

    if len(X) < lpm.MIN_ROWS_TO_TRAIN:
        print(f"BLOCKER: {len(X)} rows < MIN_ROWS_TO_TRAIN "
              f"({lpm.MIN_ROWS_TO_TRAIN})")
        return 1

    Xa = np.asarray(X, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    ga = np.asarray(groups)
    print(f"label range  : {ya.min():.4f} .. {ya.max():.4f}  "
          f"mean={ya.mean():.4f} median={np.median(ya):.4f}")

    def new_gbr() -> GradientBoostingRegressor:
        """Construct the regressor from the shared, house-standard params."""
        return GradientBoostingRegressor(**lpm.GBR_PARAMS)   # type: ignore[arg-type]

    def cv_eval(splitter, split_args, name):
        """Out-of-fold R2 + Spearman for one CV scheme."""
        oof = np.full(len(ya), np.nan)
        for tr, te in splitter.split(*split_args):
            mdl = new_gbr()
            mdl.fit(Xa[tr], ya[tr])
            oof[te] = mdl.predict(Xa[te])
        ss_res = float(((ya - oof) ** 2).sum())
        ss_tot = float(((ya - ya.mean()) ** 2).sum())
        r2 = 1.0 - ss_res / ss_tot
        rho = float(spearmanr(ya, oof)[0])
        print(f"  {name:24} R2={r2:+.4f}  Spearman={rho:+.4f}")
        return {"r2": round(r2, 4), "spearman": round(rho, 4), "oof": oof}

    print("\n── cross-validation ──")
    grouped = cv_eval(GroupKFold(n_splits=5), (Xa, ya, ga),
                      "GroupKFold (honest)")
    random_cv = cv_eval(KFold(n_splits=5, shuffle=True, random_state=42),
                        (Xa, ya), "KFold (leaky, ref only)")
    leak = random_cv["r2"] - grouped["r2"]
    print(f"  {'leakage gap':24} {leak:+.4f}"
          f"{'   <-- random split inflated by locus memorisation' if leak > 0.05 else ''}")

    # Baselines: a model must beat predicting the mean.
    print("\n── baselines ──")
    mean_r2 = 0.0
    print(f"  {'predict-the-mean':24} R2={mean_r2:+.4f}")
    beats = grouped["r2"] > 0.05
    print(f"  {'model beats mean?':24} {'YES' if beats else 'NO'}")

    print("\n── fitting final model on all data ──")
    model = new_gbr()
    model.fit(Xa, ya)
    imp = sorted(zip(PEG_FEATURE_NAMES, list(model.feature_importances_)),
                 key=lambda t: -float(t[1]))
    print("  top 12 features by importance:")
    for name, val in imp[:12]:
        print(f"    {val:6.4f}  {name}")

    metrics = {
        "grouped_r2": grouped["r2"], "grouped_spearman": grouped["spearman"],
        "random_r2": random_cv["r2"], "random_spearman": random_cv["spearman"],
        "leakage_gap": round(leak, 4),
        "beats_mean_baseline": beats,
    }
    report = {
        "label": args.label,
        "n_rows": len(X),
        "n_loci": len(set(groups)),
        "n_features": N_PEG_FEATURES,
        "skipped": skipped,
        "metrics": metrics,
        "top_features": [{"name": n, "importance": round(float(v), 5)}
                         for n, v in imp[:20]],
        "gbr_params": lpm.GBR_PARAMS,
        "source": "PRIDICT2 data_23k_v1.csv (measured HEK293T/K562)",
        "trained_at": time.time(),
        "caveat": ("Trained on PRIDICT2 HEK293T/K562 library loci. Transfers "
                   "pegRNA DESIGN rules (PBS/RTT length, GC, Tm, edit "
                   "position); does not model our targets' chromatin context."),
    }

    if args.no_save:
        print("\n--no-save: model not written")
    else:
        lpm.save_model(model, n_rows=len(X), metrics=metrics)
        lpm.REPORT_PATH.write_text(json.dumps(report, indent=1))
        print(f"\nsaved {lpm.MODEL_PATH}")
        print(f"saved {lpm.REPORT_PATH}")

    print("\n" + "=" * 72)
    print(f"VERDICT: grouped R2={grouped['r2']:+.4f} "
          f"Spearman={grouped['spearman']:+.4f} — "
          f"{'usable' if beats else 'NOT usable, do not deploy'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
