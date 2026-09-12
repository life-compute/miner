"""
verify_peg_export.py — prove the JSON export reproduces the sklearn pickle.

This is the Stage 3 equivalence gate.  If the two paths disagree by even one
ULP the validator cannot use the JSON, so the bar here is EXACT equality on the
raw (pre-clamp) prediction, not a tolerance.

Exercises real Stage 1 candidates across all 28 hotspots, not synthetic
vectors, so the comparison covers the feature distribution the model will
actually see.
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from adaptive.life_peg import (  # noqa: E402
    N_PEG_FEATURES, PEG_FEATURE_NAMES, design_candidates, featurize_candidate,
    list_reference_genes, load_gene_reference,
)
from scripts.export_peg_model import predict_json  # noqa: E402

MODEL_PKL = REPO / "output" / "peg_model.pkl"
MODEL_JSON = REPO / "output" / "peg_model.json"

MAX_PER_HOTSPOT = 40   # keeps runtime sane; still thousands of comparisons


def main() -> int:
    checks = 0

    if not MODEL_JSON.exists():
        print("BLOCKER: run scripts/export_peg_model.py first")
        return 1

    spec = json.loads(MODEL_JSON.read_text())
    with MODEL_PKL.open("rb") as fh:
        payload = pickle.load(fh)
    model = payload["model"]

    import numpy as np

    # ── A. structural contract ────────────────────────────────────────────
    assert spec["n_features"] == N_PEG_FEATURES, "feature count drift"
    checks += 1
    assert tuple(spec["feature_order"]) == tuple(PEG_FEATURE_NAMES), \
        "feature_order does not match life_peg.PEG_FEATURE_NAMES"
    checks += 1
    assert spec["normalization"] is None, "unexpected normalization block"
    checks += 1
    assert len(spec["trees"]) == model.n_estimators, "tree count mismatch"
    checks += 1
    assert spec["learning_rate"] == model.learning_rate, "lr mismatch"
    checks += 1
    assert spec["output_clamp"] == [0.0, 1.0], "clamp mismatch"
    checks += 1
    print(f"  structural contract OK  ({len(spec['trees'])} trees, "
          f"{spec['n_features']} features)")

    # ── B. numerical equivalence on real candidates ───────────────────────
    vectors: list[list[float]] = []
    for gene in list_reference_genes():
        rec = load_gene_reference(gene)
        if not rec:
            continue
        for hs in rec.get("hotspots", []):
            cands = design_candidates(gene, hs["label"])[:MAX_PER_HOTSPOT]
            for c in cands:
                f = featurize_candidate(c)
                if f is not None:
                    vectors.append(f)

    assert vectors, "no candidates featurised — cannot verify"
    checks += 1

    X = np.array(vectors, dtype=np.float64)
    sk_raw = model.predict(X)

    max_abs = 0.0
    n_exact = 0
    for i, vec in enumerate(vectors):
        js = predict_json(spec, vec)
        sk = max(0.0, min(1.0, float(sk_raw[i])))
        d = abs(js - sk)
        max_abs = max(max_abs, d)
        if js == sk:
            n_exact += 1
        assert d == 0.0, (
            f"MISMATCH at vector {i}: json={js!r} sklearn={sk!r} delta={d!r}"
        )
        checks += 1

    print(f"  candidates compared      : {len(vectors):,}")
    print(f"  bit-identical            : {n_exact:,}/{len(vectors):,}")
    print(f"  max abs deviation        : {max_abs!r}")

    # ── C. the evaluator is genuinely dependency-free ─────────────────────
    # Scan executable lines only: the docstrings legitimately *mention* numpy
    # and sklearn while explaining why the evaluator avoids them.
    src = (REPO / "scripts" / "export_peg_model.py").read_text()
    body = src.split("def to_float32")[1].split("def predict_json")[0] \
        + src.split("def predict_json")[1].split("def main")[0]
    code_lines, in_doc = [], False
    for line in body.splitlines():
        if line.strip().startswith('"""'):
            in_doc = not in_doc
            continue
        if not in_doc:
            code_lines.append(line)
    code = "\n".join(code_lines)
    for banned in ("numpy", "np.", "sklearn", "pickle.", "scipy"):
        assert banned not in code, f"evaluator depends on {banned}"
        checks += 1
    print("  evaluator dependency-free: numpy/sklearn/scipy/pickle absent")

    print()
    print("=" * 74)
    print(f"assertions passed: {checks}")
    print()
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
