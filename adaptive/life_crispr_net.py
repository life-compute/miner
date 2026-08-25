"""
life_crispr_net.py — Per-target ML models for Boltz2 iptm prediction (CRISPR).

One GradientBoostingRegressor per CRISPR target, trained on real Boltz2 iptm
scores from output/life_boltz_scores.jsonl.  Used to pre-screen gRNA candidates
before committing GPU time to Boltz2.

Architecture
------------
- Features (20 dims per gRNA sequence):
    GC content (1)
    Hamming distance to nearest known hotspot (1)
    PAM-site position flag: 1 if 3′ end avoids NGG tail (1)
    Max homopolymer run length (1)
    Dinucleotide frequencies AA/AC/AG/AT/CA/CC/CG/CT/GA/GC/GG/GT/TA/TC/TG/TT (16)
- Model: GradientBoostingRegressor (n_estimators=200, max_depth=4, lr=0.05)
- One model per target_id, saved to output/crispr_net_models/{TARGET}_model.pkl
- Minimum 15 real Boltz2 iptm scores to train; retrain every 10 new scores
- R² tracked per target in output/crispr_net_models/crispr_net_report.json

Exported API
------------
  train_all()                              → dict  (per-target results)
  pre_screen(seqs, target_id, top_n)       → list[str]  (best-first by predicted iptm)
  get_model_report()                        → dict
  should_retrain(target_id)                → bool

All imports are lazy; errors are logged but never propagate to the caller.
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
_MODEL_DIR   = _OUTPUT_DIR / "crispr_net_models"
_REPORT_PATH = _MODEL_DIR / "crispr_net_report.json"

# ── Hyper-parameters ──────────────────────────────────────────────────────────
MIN_ROWS_TO_TRAIN = 15   # minimum real Boltz2 iptm scores per target
RETRAIN_EVERY     = 10   # retrain when this many new scores appear

# ── In-memory state ───────────────────────────────────────────────────────────
_models:     dict[str, object] = {}   # target_id → fitted GBR
_row_counts: dict[str, int]    = {}   # target_id → n rows at last train
_report:     dict              = {}   # full report, written to disk

# ── Hotspot database (imported lazily to avoid circular dep) ──────────────────
_hotspot_cache: dict[str, list[str]] = {}


def _get_hotspots(target_id: str) -> list[str]:
    """Return known hotspot gRNAs for *target_id*, or []."""
    if not _hotspot_cache:
        try:
            from adaptive.life_crispr import HOTSPOT_GRNAS
            _hotspot_cache.update(HOTSPOT_GRNAS)
        except Exception:
            pass
    return _hotspot_cache.get(target_id, [])


# ── Dinucleotide alphabet (fixed order for reproducible feature vector) ────────
_DINUCS = [
    "AA", "AC", "AG", "AT",
    "CA", "CC", "CG", "CT",
    "GA", "GC", "GG", "GT",
    "TA", "TC", "TG", "TT",
]


# ── Feature extraction ────────────────────────────────────────────────────────

def _hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def _featurize(seq: str, target_id: str) -> Optional[list[float]]:
    """
    Return a 20-dimensional feature vector for a 20-mer gRNA sequence, or None.

    Dimensions:
      0     GC content (0–1)
      1     Hamming distance to nearest hotspot (0–20), or 10 if no hotspots
      2     PAM-site flag: 1.0 if 3′ tail is NOT NGG (i.e. avoids internal PAM)
      3     Max homopolymer run length (1–20)
      4-19  Dinucleotide frequencies (16 dims, each 0–1, sum≤1)
    """
    seq = seq.upper()
    if len(seq) != 20 or not all(c in "ACGT" for c in seq):
        return None

    # 0. GC content
    gc = (seq.count("G") + seq.count("C")) / 20.0

    # 1. Hamming to nearest hotspot
    hotspots = _get_hotspots(target_id)
    if hotspots:
        min_dist = float(min(_hamming(seq, h) for h in hotspots if len(h) == 20))
    else:
        min_dist = 10.0   # neutral when no hotspot reference

    # 2. PAM-site flag (1 = good: 3′ tail is not NGG-like)
    tail = seq[-3:]
    pam_flag = 0.0 if (tail[1] == "G" and tail[2] == "G") else 1.0

    # 3. Max homopolymer run
    max_run = 1
    cur_run = 1
    for i in range(1, 20):
        if seq[i] == seq[i - 1]:
            cur_run += 1
            max_run = max(max_run, cur_run)
        else:
            cur_run = 1
    max_run_norm = max_run / 20.0

    # 4-19. Dinucleotide frequencies (19 possible positions in a 20-mer)
    dinu_counts = {d: 0.0 for d in _DINUCS}
    for i in range(19):
        di = seq[i:i + 2]
        if di in dinu_counts:
            dinu_counts[di] += 1.0
    dinu_feats = [dinu_counts[d] / 19.0 for d in _DINUCS]

    return [gc, min_dist / 20.0, pam_flag, max_run_norm] + dinu_feats


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_boltz_rows(target_id: str) -> list[dict]:
    """
    Return CRISPR rows from life_boltz_scores.jsonl for this target_id where
    iptm is a real float (Boltz2 ran successfully, not analytical fallback).
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
            # Must be a CRISPR row with a real Boltz2 iptm score
            if row.get("target_type") != "CRISPR":
                continue
            iptm = row.get("iptm")
            if iptm is None:
                continue
            try:
                float(iptm)
            except (TypeError, ValueError):
                continue
            seq = row.get("smiles", "")   # CRISPR uses smiles field for gRNA seq
            if not seq or len(seq) != 20:
                continue
            rows.append(row)
    except Exception as e:
        log.debug(f"[CRISPR-NET] _load_boltz_rows({target_id}): {e}")
    return rows


def _all_crispr_target_ids() -> list[str]:
    """All unique CRISPR target_ids in the boltz JSONL with at least 1 iptm score."""
    ids: set[str] = set()
    if not _BOLTZ_JSONL.exists():
        return []
    try:
        for line in _BOLTZ_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("target_type") == "CRISPR" and row.get("iptm") is not None:
                tid = row.get("target_id")
                if tid:
                    ids.add(tid)
    except Exception:
        pass
    return sorted(ids)


# ── Model persistence ─────────────────────────────────────────────────────────

def _model_path(target_id: str) -> Path:
    return _MODEL_DIR / f"{target_id}_model.pkl"


def _save_model(target_id: str, model: object) -> None:
    try:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with _model_path(target_id).open("wb") as fh:
            pickle.dump(model, fh)
    except Exception as e:
        log.debug(f"[CRISPR-NET] save_model({target_id}): {e}")


def _load_model(target_id: str) -> Optional[object]:
    p = _model_path(target_id)
    if not p.exists():
        return None
    try:
        with p.open("rb") as fh:
            return pickle.load(fh)
    except Exception as e:
        log.debug(f"[CRISPR-NET] load_model({target_id}): {e}")
        return None


# ── Report ────────────────────────────────────────────────────────────────────

def _save_report(report: dict) -> None:
    try:
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        _REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug(f"[CRISPR-NET] save_report: {e}")


def get_model_report() -> dict:
    """Return the current in-memory report (also readable from crispr_net_report.json)."""
    if _report:
        return dict(_report)
    if _REPORT_PATH.exists():
        try:
            return json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ── Training ──────────────────────────────────────────────────────────────────

def _train_target(target_id: str, rows: list[dict]) -> Optional[dict]:
    """Train a GBR for one CRISPR target. Returns result dict or None on failure."""
    try:
        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.model_selection import cross_val_score
        import numpy as np
    except ImportError as e:
        log.debug(f"[CRISPR-NET] sklearn unavailable: {e}")
        return None

    X, y = [], []
    for row in rows:
        seq   = row.get("smiles", "")
        feats = _featurize(seq, target_id)
        if feats is None:
            continue
        iptm = row.get("iptm")
        if iptm is None:
            continue
        try:
            y_val = float(iptm)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        X.append(feats)
        y.append(y_val)

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

    # R² via k-fold CV (capped at available samples)
    n_splits = min(5, len(X))
    try:
        cv_scores = cross_val_score(model, X_arr, y_arr, cv=n_splits, scoring="r2")
        r2 = float(np.mean(cv_scores))
    except Exception:
        r2 = float(model.score(X_arr, y_arr))   # train-set R² as fallback
    r2 = max(-1.0, min(1.0, r2))

    _save_model(target_id, model)
    _models[target_id] = model
    _row_counts[target_id] = len(rows)

    return {"n": len(X), "r2": round(r2, 4)}


def should_retrain(target_id: str) -> bool:
    """True if ≥10 new Boltz2 iptm scores have appeared since the last train."""
    if target_id not in _row_counts:
        return True
    rows = _load_boltz_rows(target_id)
    return len(rows) >= _row_counts[target_id] + RETRAIN_EVERY


def train_all() -> dict:
    """
    Train (or retrain) CRISPR-Net models for all targets with enough data.

    Returns
    -------
    dict — per-target results keyed by target_id
    """
    all_tids = _all_crispr_target_ids()
    results: dict = {}

    for tid in all_tids:
        rows = _load_boltz_rows(tid)
        n    = len(rows)

        # Load existing model into memory if not already there
        if tid not in _models:
            existing = _load_model(tid)
            if existing is not None:
                _models[tid] = existing

        if n < MIN_ROWS_TO_TRAIN:
            results[tid] = {"status": "learning", "n": n}
            continue

        if not should_retrain(tid) and tid in _models:
            prev = _report.get("models", {}).get(tid, {})
            results[tid] = {
                "status":       "ready",
                "n":            n,
                "r2":           prev.get("r2"),
                "last_trained": prev.get("last_trained"),
            }
            continue

        # Train / retrain
        res = _train_target(tid, rows)
        if res is None:
            results[tid] = {"status": "failed", "n": n}
            continue

        prev_r2 = _report.get("models", {}).get(tid, {}).get("r2")
        action  = "improved" if (prev_r2 is not None and res["r2"] > prev_r2) else "trained"
        log.info(f"[CRISPR-NET] {tid} model {action}: n={res['n']} R²={res['r2']:.2f}")

        results[tid] = {
            "status":       "ready",
            "n":            res["n"],
            "r2":           res["r2"],
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
    seqs:      list[str],
    target_id: str,
    top_n:     int = 5,
) -> list[str]:
    """
    Pre-screen a list of gRNA 20-mer sequences through the CRISPR-Net model.

    Returns the top_n sequences sorted by predicted Boltz2 iptm score
    (highest iptm = strongest complex = best).  Falls back to a random
    sample of top_n if no model is ready for this target.

    Parameters
    ----------
    seqs      : list of 20-mer gRNA sequences (DNA alphabet)
    target_id : e.g. "TP53_CRISPR"
    top_n     : how many top candidates to return (default 5)

    Returns
    -------
    list[str] — up to top_n sequences, best-first by predicted iptm
    """
    model = _models.get(target_id)
    if model is None:
        model = _load_model(target_id)
        if model is not None:
            _models[target_id] = model

    if model is None or not seqs:
        return random.sample(seqs, min(top_n, len(seqs)))

    try:
        import numpy as np
    except ImportError:
        return random.sample(seqs, min(top_n, len(seqs)))

    scored: list[tuple[float, str]] = []
    for seq in seqs:
        feats = _featurize(seq, target_id)
        if feats is None:
            continue
        try:
            pred = float(model.predict(np.array([feats], dtype=np.float32))[0])  # type: ignore[union-attr]
            scored.append((pred, seq))
        except Exception:
            pass

    if not scored:
        return random.sample(seqs, min(top_n, len(seqs)))

    # Higher predicted iptm = better → sort descending
    scored.sort(key=lambda x: x[0], reverse=True)
    return [seq for _, seq in scored[:top_n]]


# ── CLI for offline testing ────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    p = argparse.ArgumentParser(description="CRISPR-Net — per-target ML scorer")
    p.add_argument("--train", action="store_true", help="Train all CRISPR-Net models")
    p.add_argument("--report", action="store_true", help="Print model report")
    args = p.parse_args()

    if args.train:
        res = train_all()
        print(f"\nTrained {len(res)} targets:")
        for tid, v in sorted(res.items()):
            r2_str = f"R²={v['r2']:.3f}" if v.get("r2") is not None else "R²=—"
            print(f"  {tid:<20} n={v['n']:<4} {r2_str:<12} status={v['status']}")
    elif args.report:
        rep = get_model_report()
        print(json.dumps(rep, indent=2))
    else:
        p.print_help()
