"""
life_peg_model.py — Stage 2 miner-side pegRNA efficiency predictor.

Predicts prime-editing efficiency (0.0-1.0) from the Stage 1 feature vector.
Sequence-feature model, NOT a structural Boltz2 complex - matching the
approach real published tools (PRIDICT, DeepPrime) actually use.

STAGE 2 SCOPE: prediction only, log-only.  No on-chain submission, no reward
logic, no LIFE-BRAIN wiring, no validator surface.  Nothing here imports from
or mutates production code.

Training labels
---------------
Real measured HEK293T / K562 editing efficiencies from the published PRIDICT2
library (22,956 pegRNAs).  We have no wet-lab data of our own, and inventing a
heuristic target would produce a confident number traceable to nothing, so the
model is trained on real experimental readouts or it is not trained at all.

Honesty contract
----------------
`predict_efficiency` returns None - never a fabricated number - when the model
file is absent or the inputs do not featurise.  A caller that wants a score
must handle None.  This is deliberate: a silently-defaulted 0.5 is exactly the
class of bug that produces an unexplainable claimed value downstream.

Determinism
-----------
Fixed hyperparameters, fixed random_state, no RNG at predict time.  The same
(spacer, pbs, rtt, edit_type) always yields the same float, which Stage 3 will
require when the validator recomputes independently.
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Optional

from adaptive.life_peg import (
    N_PEG_FEATURES, PegCandidate, featurize_candidate, featurize_pegrna,
)

REPO = Path(__file__).resolve().parents[1]
MODEL_DIR = REPO / "output"
MODEL_PATH = MODEL_DIR / "peg_model.pkl"
REPORT_PATH = MODEL_DIR / "peg_model_report.json"

# Label column used for the primary head.  HEK293T has by far the better
# spread (median 0.102 vs K562's 0.008, which is mostly zeros).
PRIMARY_LABEL = "HEKaverageedited_clamped"

# Hyperparameters mirror adaptive/life_crispr_net.py so the peg branch matches
# the house convention rather than introducing a second style.
GBR_PARAMS: dict[str, object] = {
    "n_estimators": 200,
    "max_depth": 4,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "random_state": 42,
}

MIN_ROWS_TO_TRAIN = 150   # matches the other LIFE-BRAIN branches

_model = None
_model_missing_logged = False


# ══════════════════════════════════════════════════════════════════════════════
# Model load / save
# ══════════════════════════════════════════════════════════════════════════════

def _load_model():
    """Load the trained model once.  Returns None when absent (not an error)."""
    global _model, _model_missing_logged
    if _model is not None:
        return _model
    if not MODEL_PATH.exists():
        if not _model_missing_logged:
            _model_missing_logged = True
        return None
    with MODEL_PATH.open("rb") as fh:
        payload = pickle.load(fh)
    # Refuse a model built against a different feature contract.
    if payload.get("n_features") != N_PEG_FEATURES:
        raise ValueError(
            f"peg_model.pkl was trained on {payload.get('n_features')} features "
            f"but life_peg.py now exposes {N_PEG_FEATURES}. Retrain: "
            f"python scripts/train_peg_model.py"
        )
    _model = payload["model"]
    return _model


def save_model(model, *, n_rows: int, metrics: dict) -> None:
    """Persist the model together with the feature-count contract."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    with MODEL_PATH.open("wb") as fh:
        pickle.dump({
            "model": model,
            "n_features": N_PEG_FEATURES,
            "label": PRIMARY_LABEL,
            "n_rows": n_rows,
            "metrics": metrics,
        }, fh)


def get_model_report() -> dict:
    """Training report, or {} if the model has never been trained."""
    if not REPORT_PATH.exists():
        return {}
    return json.loads(REPORT_PATH.read_text())


def is_trained() -> bool:
    return MODEL_PATH.exists()


# ══════════════════════════════════════════════════════════════════════════════
# Prediction
# ══════════════════════════════════════════════════════════════════════════════

def _predict_vector(feats: list[float]) -> Optional[float]:
    """Run the model on one feature vector.  None when no model is available."""
    model = _load_model()
    if model is None:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    raw = float(model.predict(np.array([feats], dtype=np.float64))[0])
    # Efficiency is a fraction; the regressor is unconstrained, so clamp.
    return max(0.0, min(1.0, raw))


def predict_efficiency(spacer: str, pbs: str, rtt: str, edit_type: str,
                       **context) -> Optional[float]:
    """
    Predicted prime-editing efficiency in [0, 1], or None.

    None means "no prediction available" - either the model has not been
    trained or the inputs did not featurise.  It is never a stand-in for a
    real score; callers must handle it explicitly.

    `context` accepts the same optional keywords as
    adaptive.life_peg.featurize_pegrna (gene, genomic_edit_pos, nick_to_edit,
    correction_length, edit_to_rtt_end, window, edit_offset, pam_offset,
    strand).
    """
    feats = featurize_pegrna(spacer, pbs, rtt, edit_type, **context)
    if feats is None:
        return None
    return _predict_vector(feats)


def predict_candidate(cand: PegCandidate) -> Optional[float]:
    """predict_efficiency() with all context supplied from a PegCandidate."""
    feats = featurize_candidate(cand)
    if feats is None:
        return None
    return _predict_vector(feats)


def rank_candidates(cands: list[PegCandidate],
                    top: Optional[int] = None) -> list[tuple[PegCandidate, float]]:
    """
    Score and rank candidates best-first.

    Candidates that fail to score are dropped rather than defaulted, so an
    empty result means "nothing could be scored", not "everything scored 0".
    Ties break on (PBS length, RTT length) to keep the order deterministic.
    """
    scored = [(c, s) for c in cands
              if (s := predict_candidate(c)) is not None]
    scored.sort(key=lambda t: (-t[1], len(t[0].pbs), len(t[0].rtt)))
    return scored[:top] if top else scored
