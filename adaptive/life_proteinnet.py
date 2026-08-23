"""
life_proteinnet.py — Per-protein ML models for Boltz2 affinity prediction.

One GradientBoostingRegressor per cancer target, trained on real Boltz2 scores
from output/life_boltz_scores.jsonl.  Used to pre-screen ZINC15 candidates
before committing GPU time to Boltz2.

Key benefits
------------
- Large proteins (APC, BRCA1, SMAD4): pre-screen 5 000 candidates, send only
  top 20 to Boltz2 → eliminates wasted scoring on poor molecules.
- Normal proteins: pre-screen 2 000, pass top 100 to the main sampling loop.
- Phase 4 generation filter: discard generated molecules below predicted
  threshold before real Boltz2 is called.

Architecture
------------
- Features: Morgan2048 fingerprint + 8 RDKit physico-chemical descriptors
  (MW, logP, HBD, HBA, TPSA, RotBonds, RingCount, HeavyAtoms) = 2056 dims
- Model: GradientBoostingRegressor (n_estimators=200, max_depth=4, lr=0.05)
- One model per UniProt ID, saved to output/protein_models/{UNIPROT}_model.pkl
- Minimum 30 real Boltz2 scores to train; retrain every 20 new scores
- R² tracked per target in output/protein_models/proteinnet_report.json

Exported API
------------
  train_all()                          → dict  (per-target results)
  pre_screen(smiles, target_id, top_n) → list[str]  (sorted best-first)
  get_model_report()                   → dict
  should_retrain(target_id)            → bool

All rdkit imports are lazy so the module loads cleanly even without rdkit.
All file I/O is guarded; errors are logged but never propagate to the caller.
"""
from __future__ import annotations

import json
import logging
import pickle
import random
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("life-miner")

# ── Paths ──────────────────────────────────────────────────────────────────────
_LIFE_DIR    = Path(__file__).resolve().parents[1]
_OUTPUT_DIR  = _LIFE_DIR / "output"
_BOLTZ_JSONL = _OUTPUT_DIR / "life_boltz_scores.jsonl"
_MODEL_DIR   = _OUTPUT_DIR / "protein_models"
_REPORT_PATH = _MODEL_DIR / "proteinnet_report.json"

# ── Hyper-parameters ──────────────────────────────────────────────────────────
MIN_ROWS_TO_TRAIN  = 30   # need at least this many scored molecules per target
RETRAIN_EVERY      = 20   # retrain when this many new scores appear
MORGAN_RADIUS      = 2
MORGAN_BITS        = 2048

# ── In-memory state ───────────────────────────────────────────────────────────
_models:      dict[str, object] = {}   # target_id → fitted GBR
_row_counts:  dict[str, int]    = {}   # target_id → n rows at last train
_report:      dict              = {}   # full report, written to disk


# ── Feature extraction ────────────────────────────────────────────────────────

def _featurize(smiles: str) -> Optional[list[float]]:
    """Return 2056-dim feature vector or None if rdkit unavailable / invalid."""
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors
        from rdkit.Chem import rdFingerprintGenerator as rfg
    except ImportError:
        return None

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None

    try:
        gen = rfg.GetMorganGenerator(radius=MORGAN_RADIUS, fpSize=MORGAN_BITS)
        fp  = list(gen.GetFingerprintAsNumPy(mol).astype(float))
    except Exception:
        return None

    try:
        desc = [
            Descriptors.MolWt(mol),
            Descriptors.MolLogP(mol),
            rdMolDescriptors.CalcNumHBD(mol),
            rdMolDescriptors.CalcNumHBA(mol),
            Descriptors.TPSA(mol),
            rdMolDescriptors.CalcNumRotatableBonds(mol),
            rdMolDescriptors.CalcNumRings(mol),
            mol.GetNumHeavyAtoms(),
        ]
    except Exception:
        desc = [0.0] * 8

    return fp + desc


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_boltz_rows(target_id: str) -> list[dict]:
    """
    Return all rows from life_boltz_scores.jsonl for this target_id where
    boltz_score is a real float (not None).
    """
    rows: list[dict] = []
    if not _BOLTZ_JSONL.exists():
        return rows
    try:
        for line in _BOLTZ_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("target_id") != target_id:
                continue
            score = row.get("boltz_score")
            if score is None:
                continue
            try:
                float(score)
            except (TypeError, ValueError):
                continue
            smiles = row.get("smiles", "")
            if not smiles:
                continue
            # Skip CRISPR gRNA rows — DNA sequences are not valid SMILES
            if row.get("target_type") == "CRISPR" or row.get("source") == "crispr_generated":
                continue
            rows.append(row)
    except Exception as e:
        log.debug(f"[PROTEINNET] _load_boltz_rows({target_id}): {e}")
    return rows


def _all_target_ids() -> list[str]:
    """All unique target_ids that appear in the boltz JSONL."""
    ids: set[str] = set()
    if not _BOLTZ_JSONL.exists():
        return []
    try:
        for line in _BOLTZ_JSONL.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                tid = row.get("target_id")
                if tid:
                    ids.add(tid)
            except Exception:
                pass
    except Exception:
        pass
    return sorted(ids)


# ── Model persistence ─────────────────────────────────────────────────────────

def _model_path(uniprot_id: str) -> Path:
    return _MODEL_DIR / f"{uniprot_id}_model.pkl"


def _save_model(uniprot_id: str, model: object) -> None:
    try:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with _model_path(uniprot_id).open("wb") as fh:
            pickle.dump(model, fh)
    except Exception as e:
        log.debug(f"[PROTEINNET] save_model({uniprot_id}): {e}")


def _load_model(uniprot_id: str) -> Optional[object]:
    p = _model_path(uniprot_id)
    if not p.exists():
        return None
    try:
        with p.open("rb") as fh:
            return pickle.load(fh)
    except Exception as e:
        log.debug(f"[PROTEINNET] load_model({uniprot_id}): {e}")
        return None


# ── Report ────────────────────────────────────────────────────────────────────

def _save_report(report: dict) -> None:
    try:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        _REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug(f"[PROTEINNET] save_report: {e}")


def get_model_report() -> dict:
    """Return the current in-memory report (also readable from proteinnet_report.json)."""
    if _report:
        return dict(_report)
    # Try loading from disk if not initialised yet
    if _REPORT_PATH.exists():
        try:
            return json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ── Training ──────────────────────────────────────────────────────────────────

def _train_target(target_id: str, uniprot_id: str, rows: list[dict]) -> Optional[dict]:
    """
    Train a GBR for one target.  Returns result dict or None on failure.
    uniprot_id is used for the model filename; target_id for logging/dict key.
    """
    try:
        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.model_selection import cross_val_score
        import numpy as np
    except ImportError as e:
        log.debug(f"[PROTEINNET] sklearn unavailable: {e}")
        return None

    X, y = [], []
    for row in rows:
        feats = _featurize(row["smiles"])
        if feats is None:
            continue
        X.append(feats)
        y.append(float(row["boltz_score"]))

    if len(X) < MIN_ROWS_TO_TRAIN:
        return None

    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.float32)

    model = GradientBoostingRegressor(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        random_state=42,
    )
    model.fit(X_arr, y_arr)

    # R² via 5-fold CV (capped at available samples)
    n_splits = min(5, len(X))
    try:
        cv_scores = cross_val_score(model, X_arr, y_arr, cv=n_splits, scoring="r2")
        r2 = float(np.mean(cv_scores))
    except Exception:
        r2 = float(model.score(X_arr, y_arr))   # train-set R² as fallback
    r2 = max(-1.0, min(1.0, r2))   # clamp: near-zero-variance targets (e.g. SMAD4) produce ±1e5

    _save_model(uniprot_id, model)
    _models[target_id] = model
    _row_counts[target_id] = len(rows)

    return {"n": len(X), "r2": round(r2, 4), "uniprot_id": uniprot_id}


def should_retrain(target_id: str) -> bool:
    """True if ≥20 new Boltz2 scores have appeared since the last train."""
    if target_id not in _row_counts:
        return True   # never trained
    rows = _load_boltz_rows(target_id)
    return len(rows) >= _row_counts[target_id] + RETRAIN_EVERY


def train_all(target_uniprot_map: Optional[dict[str, str]] = None) -> dict:
    """
    Train (or retrain) models for all targets with enough data.

    Parameters
    ----------
    target_uniprot_map : {target_id: uniprot_id}, e.g. {"TP53": "P04637"}.
        If None, target_id is used as the model filename (degrades gracefully).

    Returns
    -------
    dict  — per-target results keyed by target_id
    """
    t_map = target_uniprot_map or {}
    all_tids = _all_target_ids()
    results: dict = {}

    for tid in all_tids:
        rows = _load_boltz_rows(tid)
        n    = len(rows)
        uid  = t_map.get(tid, tid)

        # Load existing model into memory if not already there
        if tid not in _models:
            existing = _load_model(uid)
            if existing is not None:
                _models[tid] = existing

        if n < MIN_ROWS_TO_TRAIN:
            results[tid] = {"status": "learning", "n": n, "uniprot_id": uid}
            continue

        if not should_retrain(tid) and tid in _models:
            # Already current — pull R² from report if available
            prev = _report.get("models", {}).get(tid, {})
            results[tid] = {
                "status": "ready",
                "n": n,
                "r2": prev.get("r2"),
                "uniprot_id": uid,
                "last_trained": prev.get("last_trained"),
            }
            continue

        # Train / retrain
        res = _train_target(tid, uid, rows)
        if res is None:
            results[tid] = {"status": "failed", "n": n, "uniprot_id": uid}
            continue

        prev_r2 = _report.get("models", {}).get(tid, {}).get("r2")
        action  = "improved" if (prev_r2 is not None and res["r2"] > prev_r2) else "trained"
        log.info(f"[PROTEINNET] {tid}/{uid} model {action}: n={res['n']} R²={res['r2']:.2f}")

        results[tid] = {
            "status":       "ready",
            "n":            res["n"],
            "r2":           res["r2"],
            "uniprot_id":   uid,
            "last_trained": time.time(),
        }

    # Update global report
    _report.clear()
    _report["models"]       = results
    _report["generated_at"] = time.time()
    _report["n_ready"]      = sum(1 for v in results.values() if v.get("status") == "ready")
    _report["n_total"]      = len(results)
    _save_report(_report)

    return results


# ── Inference ─────────────────────────────────────────────────────────────────

def pre_screen(
    smiles_list: list[str],
    target_id:   str,
    top_n:       int = 100,
) -> list[str]:
    """
    Pre-screen a list of SMILES through the ProteinNet model for target_id.

    Returns the top_n SMILES sorted by predicted Boltz2 affinity (most negative
    = strongest binder = best).  Falls back to a random sample of top_n if no
    model is ready for this target.

    Parameters
    ----------
    smiles_list : list of SMILES strings (up to ~5000 recommended)
    target_id   : e.g. "TP53", "APC"
    top_n       : how many top candidates to return

    Returns
    -------
    list[str] — up to top_n SMILES, best-first
    """
    model = _models.get(target_id)
    if model is None:
        # Try loading from disk (e.g. after daemon restart)
        uid   = _report.get("models", {}).get(target_id, {}).get("uniprot_id", target_id)
        model = _load_model(uid)
        if model is not None:
            _models[target_id] = model

    if model is None or not smiles_list:
        # No model yet — return a random sample
        sampled = random.sample(smiles_list, min(top_n, len(smiles_list)))
        return sampled

    try:
        import numpy as np
    except ImportError:
        return random.sample(smiles_list, min(top_n, len(smiles_list)))

    scored: list[tuple[float, str]] = []
    for smi in smiles_list:
        feats = _featurize(smi)
        if feats is None:
            continue
        try:
            pred = float(model.predict(np.array([feats], dtype=np.float32))[0])
            scored.append((pred, smi))
        except Exception:
            pass

    if not scored:
        return random.sample(smiles_list, min(top_n, len(smiles_list)))

    # Lower (more negative) predicted affinity = better binder → sort ascending
    scored.sort(key=lambda x: x[0])
    return [smi for _, smi in scored[:top_n]]
