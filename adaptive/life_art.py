"""
life_art.py — Adaptive Ranking Trainer for Life Compute molecule candidates.

Analogous to nova_art.py but predicts Boltz2 binding affinity from
Morgan fingerprints (512-bit, radius=2) + 13 physicochemical descriptors.

Key differences from Nova ART:
  • Features: Morgan FP (512 bits) + 13 phys-chem = 525 total features.
    Fingerprints encode local structural environment — essential for
    binding affinity prediction, not just drug-likeness.
  • Label source: output/life_boltz_scores.jsonl (populated by miner_daemon).
  • Training data: submission_archive rows joined to Boltz scores.
  • Gate: n_rows ≥ 50 AND 5-fold CV R² ≥ 0.25.
  • Auto-retrain threshold: 50 new real scores (configurable).

Readiness gate
--------------
  n ≥ 50              — minimum rows for meaningful RF fit
  5-fold CV R² ≥ 0.25  — better than naive mean prediction
  Falls back to proxy scorer when gate not met.

Priority in rank_candidates():
  1. Real boltz_score from life_boltz_scores.jsonl (ground truth)
  2. ART model prediction (when gate passes)
  3. Proxy score fallback (from life_pulse.py)
"""

from __future__ import annotations

import json
import pickle
import time
from pathlib import Path
from typing import Optional

import numpy as np

# ── Paths ──────────────────────────────────────────────────────────────────────
LIFE_DIR      = Path(__file__).resolve().parents[1]
OUTPUT_DIR    = LIFE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PULSE_JSONL   = OUTPUT_DIR / "life_pulse_data.jsonl"
BOLTZ_SCORES  = OUTPUT_DIR / "life_boltz_scores.jsonl"
MODEL_PATH    = OUTPUT_DIR / "life_art_model.pkl"
MODEL_BAK     = OUTPUT_DIR / "life_art_model.pkl.bak"
REPORT_PATH   = OUTPUT_DIR / "life_art_report.json"

# ── Training gates ─────────────────────────────────────────────────────────────
MIN_ROWS       = 50
MIN_CV_R2      = 0.25
RETRAIN_EVERY  = 50    # auto-retrain after this many new real scores accumulated

# ── Feature spec ──────────────────────────────────────────────────────────────
FP_RADIUS = 2
FP_NBITS  = 512   # compact — enough for within-family discrimination; fast RF

PHYSCHEM_FEATURES = [
    "heavy_atoms",          # mol.GetNumHeavyAtoms() / 55.0
    "rotatable_bonds",      # NumRotatableBonds / 10.0
    "mol_weight",           # MolWt / 500.0
    "logp",                 # (MolLogP + 5) / 15.0
    "n_rings",              # RingCount / 6.0
    "n_hba",                # NumHAcceptors / 10.0
    "n_hbd",                # NumHDonors / 5.0
    "tpsa_norm",            # TPSA / 200.0
    "n_hetero",             # heteroatom count / 15.0
    "fsp3",                 # FractionCSP3  [0,1]
    "qed",                  # qed  [0,1]
    "n_arom_rings",         # CalcNumAromaticRings / 6.0
    "has_banned",           # 1.0 if banned atom present else 0.0
]
# Total features = FP_NBITS + len(PHYSCHEM_FEATURES) = 525

BANNED_ATOMS = {"Se", "Na", "Fe", "Zn", "B", "Si", "P"}


# ── Feature extraction ────────────────────────────────────────────────────────

def extract_features(smiles: str) -> Optional[np.ndarray]:
    """
    Return a 525-d float32 feature vector: [Morgan FP (512) | physchem (13)].
    Returns None if SMILES is invalid or RDKit raises.
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem, Descriptors, rdMolDescriptors
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        # Reject wildcard/attachment atoms (*) — Boltz2 cannot score them
        if any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()):
            return None
        # Morgan fingerprint (512 bits, radius=2)
        fp = AllChem.GetMorganFingerprintAsBitVect(mol, FP_RADIUS, nBits=FP_NBITS)
        fp_arr = np.array(fp, dtype=np.float32)  # shape (512,)
        # Physical-chemistry features
        sym      = {a.GetSymbol() for a in mol.GetAtoms()}
        n_hetero = sum(1 for a in mol.GetAtoms() if a.GetAtomicNum() not in (6, 1))
        fsp3     = rdMolDescriptors.CalcFractionCSP3(mol)
        qed_val  = Descriptors.qed(mol)
        n_arom   = rdMolDescriptors.CalcNumAromaticRings(mol)
        pc = np.array([
            mol.GetNumHeavyAtoms() / 55.0,
            Descriptors.NumRotatableBonds(mol) / 10.0,
            Descriptors.MolWt(mol) / 500.0,
            (Descriptors.MolLogP(mol) + 5.0) / 15.0,
            Descriptors.RingCount(mol) / 6.0,
            Descriptors.NumHAcceptors(mol) / 10.0,
            Descriptors.NumHDonors(mol) / 5.0,
            Descriptors.TPSA(mol) / 200.0,
            n_hetero / 15.0,
            fsp3,
            qed_val,
            n_arom / 6.0,
            1.0 if sym & BANNED_ATOMS else 0.0,
        ], dtype=np.float32)
        return np.concatenate([fp_arr, pc])  # shape (525,)
    except Exception:
        return None


# ── Load Boltz scores ─────────────────────────────────────────────────────────

def _load_boltz_scores() -> dict[str, float]:
    """Return {smiles: boltz_score} from life_boltz_scores.jsonl (latest wins)."""
    scores: dict[str, float] = {}
    if BOLTZ_SCORES.exists():
        for line in BOLTZ_SCORES.read_text().splitlines():
            try:
                r = json.loads(line)
                smi = r.get("smiles", "")
                val = r.get("boltz_score")
                if smi and val is not None:
                    scores[smi] = float(val)
            except Exception:
                pass
    return scores


def count_boltz_scores() -> int:
    """Return number of unique SMILES with real Boltz2 scores."""
    return len(_load_boltz_scores())


# ── Training data assembly ────────────────────────────────────────────────────

def load_training_data() -> tuple[np.ndarray, np.ndarray]:
    """
    Load PULSE rows and join to Boltz scores.

    Only uses rows that have a real Boltz2 label (proxy rows are skipped —
    proxy dilutes signal; the proxy score is only useful before any real
    scores exist).

    Deduplicates by canonical SMILES (last row wins on duplicates from PULSE).
    """
    boltz_lookup = _load_boltz_scores()
    if not boltz_lookup:
        print("[ART] no Boltz scores yet — cannot train")
        return np.empty((0, FP_NBITS + len(PHYSCHEM_FEATURES))), np.empty(0)

    # Load all pulse rows that have a matching real Boltz score
    rows_by_smi: dict[str, float] = {}  # smi → boltz_score

    # 1. Direct from PULSE JSONL
    if PULSE_JSONL.exists():
        for line in PULSE_JSONL.read_text().splitlines():
            try:
                r = json.loads(line)
                smi = r.get("smiles", "")
                if smi and smi in boltz_lookup:
                    rows_by_smi[smi] = boltz_lookup[smi]
            except Exception:
                pass

    # 2. Any SMILES in boltz_scores but not in pulse (directly scored in daemon)
    for smi, score in boltz_lookup.items():
        rows_by_smi.setdefault(smi, score)

    n_raw = len(rows_by_smi)
    X, y  = [], []
    n_fail = 0
    for smi, score in rows_by_smi.items():
        fv = extract_features(smi)
        if fv is None:
            n_fail += 1
            continue
        X.append(fv)
        y.append(score)

    print(f"[ART] training rows: {len(y)}  (raw={n_raw}, feat_fail={n_fail})")
    if not X:
        return np.empty((0, FP_NBITS + len(PHYSCHEM_FEATURES))), np.empty(0)
    return np.stack(X), np.array(y, dtype=np.float32)


# ── 5-fold cross-validated R² ─────────────────────────────────────────────────

def _cv_r2(X: np.ndarray, y: np.ndarray) -> float:
    """5-fold CV R² — same approach as nova_art._loso_r2 but 5 folds, not LOO."""
    from sklearn.ensemble import RandomForestRegressor
    n = len(y)
    if n < 5:
        return -1.0
    ss_tot = np.var(y) * n
    if ss_tot == 0:
        return 0.0
    preds = np.empty(n)
    fold_size = max(1, n // 5)
    for start in range(0, n, fold_size):
        val_idx = list(range(start, min(start + fold_size, n)))
        tr_idx  = [i for i in range(n) if i not in val_idx]
        if len(tr_idx) < 4:
            preds[val_idx] = np.mean(y[tr_idx]) if tr_idx else 0.0
            continue
        rf = RandomForestRegressor(n_estimators=50, max_depth=6, random_state=0,
                                   n_jobs=-1)
        rf.fit(X[tr_idx], y[tr_idx])
        preds[val_idx] = rf.predict(X[val_idx])
    ss_res = np.sum((y - preds) ** 2)
    return float(1.0 - ss_res / ss_tot)


# ── Train + gate ──────────────────────────────────────────────────────────────

def train(force: bool = False) -> dict:
    """
    Train the ART model.  Returns a report dict with keys:
      n_rows, ready (bool), r2, reason, ts.
    Atomically deploys MODEL_PATH when gate passes.
    """
    X, y = load_training_data()
    n = len(y)
    report: dict = {
        "n_rows": n,
        "ready":  False,
        "r2":     None,
        "ts":     time.time(),
        "model":  str(MODEL_PATH),
    }

    if n < MIN_ROWS and not force:
        report["reason"] = f"n={n} < MIN_ROWS={MIN_ROWS}"
        REPORT_PATH.write_text(json.dumps(report, indent=2))
        print(f"[ART] not ready: {report['reason']}")
        return report

    r2 = _cv_r2(X, y)
    report["r2"] = round(r2, 4)

    if r2 < MIN_CV_R2 and not force:
        report["reason"] = f"5-fold R²={r2:.3f} < {MIN_CV_R2}"
        REPORT_PATH.write_text(json.dumps(report, indent=2))
        print(f"[ART] not ready: {report['reason']}")
        return report

    from sklearn.ensemble import RandomForestRegressor
    rf = RandomForestRegressor(
        n_estimators=150,
        max_depth=10,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=-1,
    )
    rf.fit(X, y)

    # Atomic deploy: write .new → backup existing → rename
    new_path = MODEL_PATH.with_suffix(".pkl.new")
    with open(new_path, "wb") as fh:
        pickle.dump(rf, fh)
    if MODEL_PATH.exists():
        import shutil
        shutil.copy2(MODEL_PATH, MODEL_BAK)
    new_path.rename(MODEL_PATH)

    report["ready"]  = True
    report["reason"] = "deployed"
    report["n_features"] = int(X.shape[1])
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(f"[ART] deployed  n={n}  R²={r2:.3f}  features={X.shape[1]}")
    return report


def load_model():
    """Load the deployed model, or return None if not present / unparseable."""
    if not MODEL_PATH.exists():
        return None
    try:
        with open(MODEL_PATH, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


def should_retrain() -> bool:
    """
    Return True if RETRAIN_EVERY new scores have accumulated since last
    reported model n_rows.
    """
    current_n = count_boltz_scores()
    if not REPORT_PATH.exists():
        return current_n >= MIN_ROWS
    try:
        rep = json.loads(REPORT_PATH.read_text())
        last_n = rep.get("n_rows", 0)
        return (current_n - last_n) >= RETRAIN_EVERY
    except Exception:
        return False


# ── Inference ─────────────────────────────────────────────────────────────────

def rank_candidates(
    candidates: list[tuple[str, str]],   # (label, smiles)
    model=None,
) -> list[tuple[str, str, float]]:
    """
    Rank (label, smiles) pairs by predicted binding affinity (higher = better).

    Priority:
      1. Real boltz_score from life_boltz_scores.jsonl
      2. ART model prediction (Morgan FP + physchem)
      3. Proxy score fallback
    """
    boltz_lookup = _load_boltz_scores()
    _model = model if model is not None else load_model()

    scored: list[tuple[str, str, float]] = []
    for label, smi in candidates:
        # Priority 1: real score
        if smi in boltz_lookup:
            scored.append((label, smi, boltz_lookup[smi]))
            continue
        # Priority 2 / 3
        fv = extract_features(smi)
        if fv is not None and _model is not None:
            score = float(_model.predict(fv.reshape(1, -1))[0])
        elif fv is not None:
            from .life_pulse import proxy_score as _proxy
            score = _proxy(smi)
        else:
            score = 0.0
        scored.append((label, smi, score))

    return sorted(scored, key=lambda x: x[2], reverse=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Life Compute ART trainer")
    p.add_argument("--force", action="store_true", help="Train even if gate fails")
    p.add_argument("--status", action="store_true", help="Print counts and exit")
    args = p.parse_args()
    if args.status:
        n = count_boltz_scores()
        ready = REPORT_PATH.exists() and json.loads(REPORT_PATH.read_text()).get("ready", False)
        print(f"boltz_scores={n}  model_ready={ready}  retrain_needed={should_retrain()}")
        if REPORT_PATH.exists():
            rep = json.loads(REPORT_PATH.read_text())
            print(f"last_r2={rep.get('r2')}  last_n={rep.get('n_rows')}")
    else:
        train(force=args.force)
