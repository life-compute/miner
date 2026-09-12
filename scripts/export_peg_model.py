"""
export_peg_model.py — export the Stage 2 GBR to dependency-free JSON.

WHY THIS EXISTS
---------------
Stage 3 requires the validator to recompute the miner's score independently.
Stage 1 was built pure-stdlib on purpose (ViennaRNA was rejected so that a
C-extension build difference could never desynchronise the two sides).  Stage 2
then reintroduced exactly that risk by persisting a pickled sklearn estimator:
reproducing a prediction meant matching the sklearn AND numpy build.

A GradientBoostingRegressor with squared_error loss is not a black box.  It is:

    prediction = init_constant + learning_rate * sum_over_trees(tree_value(x))

Every tree is a plain threshold decision tree.  All of that serialises to JSON
and evaluates in pure Python, so the validator needs no sklearn, no numpy, and
no pickle deserialisation of untrusted bytes.

This script writes peg_model.json and proves it reproduces the pickle exactly.

OUTPUT
------
output/peg_model.json  — self-contained, human-inspectable, git-diffable.

Floats are written via repr(), which round-trips exactly in Python (PEP 3141 /
float.__repr__ shortest-repr guarantee).  json.dump uses repr for floats, so
the loaded values are bit-identical, not approximately equal.
"""
from __future__ import annotations

import json
import pickle
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from adaptive.life_peg import N_PEG_FEATURES, PEG_FEATURE_NAMES  # noqa: E402

MODEL_PKL = REPO / "output" / "peg_model.pkl"
MODEL_JSON = REPO / "output" / "peg_model.json"

# sklearn marks leaves with left_child == -1 (TREE_LEAF).
_LEAF = -1


def export_tree(tree) -> dict:
    """Flatten a sklearn Tree into parallel arrays of plain Python types."""
    t = tree.tree_
    return {
        "children_left": [int(v) for v in t.children_left],
        "children_right": [int(v) for v in t.children_right],
        "feature": [int(v) for v in t.feature],
        "threshold": [float(v) for v in t.threshold],
        # value[node][0][0] — single output, single target.
        "value": [float(t.value[i][0][0]) for i in range(t.node_count)],
    }


def to_float32(v: float) -> float:
    """
    Round a Python float to float32 precision, returned as a float64.

    CRITICAL — this is not cosmetic.  sklearn's tree module casts X to
    ``np.float32`` (``sklearn.tree._tree.DTYPE``) before traversal, while
    thresholds stay float64.  Comparing a float64 input against a float64
    threshold therefore takes a DIFFERENT BRANCH than sklearn does whenever the
    two straddle a float32 rounding boundary.

    Observed on the very first real candidate: edit_to_rtt_end = 1/6 =
    0.16666666666666666 is <= threshold 0.1666666679084301 in float64 (go
    left), but as float32 it becomes 0.1666666716337204 which is > threshold
    (go right).  That single flipped branch moved the prediction by 0.095.

    struct round-trips through an IEEE-754 single, which is exactly what numpy
    does, so this keeps the evaluator stdlib-only.
    """
    return struct.unpack("f", struct.pack("f", v))[0]


def eval_tree(tree: dict, x: list) -> float:
    """
    Pure-Python traversal. Mirrors sklearn: go left when x[f] <= threshold,
    comparing at float32 precision (see to_float32).
    """
    node = 0
    while tree["children_left"][node] != _LEAF:
        f = tree["feature"][node]
        if to_float32(x[f]) <= tree["threshold"][node]:
            node = tree["children_left"][node]
        else:
            node = tree["children_right"][node]
    return tree["value"][node]


def predict_json(spec: dict, x: list) -> float:
    """Reference implementation the validator can copy verbatim. No deps."""
    if len(x) != spec["n_features"]:
        raise ValueError(
            f"expected {spec['n_features']} features, got {len(x)}"
        )
    total = spec["init_constant"]
    lr = spec["learning_rate"]
    for tree in spec["trees"]:
        total += lr * eval_tree(tree, x)
    lo, hi = spec["output_clamp"]
    return max(lo, min(hi, total))


def main() -> int:
    if not MODEL_PKL.exists():
        print(f"BLOCKER: no model at {MODEL_PKL}")
        return 1

    with MODEL_PKL.open("rb") as fh:
        payload = pickle.load(fh)

    model = payload["model"]
    if payload.get("n_features") != N_PEG_FEATURES:
        print("BLOCKER: pickle feature count disagrees with life_peg.py")
        return 1
    if model.loss != "squared_error":
        print(f"BLOCKER: loss={model.loss!r}; exporter assumes squared_error")
        return 1

    import numpy as np

    init_constant = float(model._raw_predict_init(np.zeros((1, N_PEG_FEATURES)))
                          .ravel()[0])

    spec = {
        "format": "life-peg-gbr-json",
        "format_version": 1,
        "model_type": "GradientBoostingRegressor",
        "loss": model.loss,
        "n_features": int(N_PEG_FEATURES),
        "feature_order": list(PEG_FEATURE_NAMES),
        "normalization": None,
        "normalization_note": (
            "No scaling of any kind is applied. Gradient-boosted decision trees "
            "are invariant to monotonic per-feature rescaling, so no scaler was "
            "fitted and none is required. Feed raw values from "
            "adaptive.life_peg.featurize_* in feature_order."
        ),
        "formula": (
            "y_raw = init_constant + learning_rate * sum(tree_value(x) for tree "
            "in trees); y = clamp(y_raw, 0.0, 1.0). Each tree: descend from node "
            "0, take children_left when float32(x[feature[n]]) <= threshold[n] "
            "else children_right, stop when children_left[n] == -1, emit "
            "value[n]."
        ),
        "float32_compare_note": (
            "MANDATORY: cast the feature value to IEEE-754 single precision "
            "before comparing against threshold. sklearn casts X to float32 "
            "(sklearn.tree._tree.DTYPE) while thresholds remain float64; "
            "comparing in float64 takes a different branch near rounding "
            "boundaries and silently changes the prediction. In Python: "
            "struct.unpack('f', struct.pack('f', v))[0]."
        ),
        "learning_rate": float(model.learning_rate),
        "init_constant": init_constant,
        "output_clamp": [0.0, 1.0],
        "label": payload.get("label"),
        "n_rows_trained": payload.get("n_rows"),
        "metrics": payload.get("metrics"),
        "trees": [export_tree(model.estimators_[i, 0])
                  for i in range(model.estimators_.shape[0])],
    }

    MODEL_JSON.write_text(json.dumps(spec, indent=1))
    size = MODEL_JSON.stat().st_size
    n_nodes = sum(len(t["value"]) for t in spec["trees"])
    print(f"wrote {MODEL_JSON}")
    print(f"  trees={len(spec['trees'])}  nodes={n_nodes}  bytes={size:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
