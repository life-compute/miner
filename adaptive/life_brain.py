"""
life_brain.py — LIFE-BRAIN training engine.

Responsibilities
----------------
- Load output/life_brain_dataset.jsonl (written by life_brain_ingest.py)
- Train the three-branch PyTorch model jointly
- Run leave-one-target-out (LOTO) CV per branch
- Compare per-branch held-out MAE against the existing fallbacks
- Mark branches as "trusted" when they beat the fallback
- Write output/life_brain_report.json and output/life_brain_model.pt
- Export output/life_brain_snapshot.json after every successful retrain
  (Part C wires the snapshot push; stub exported here as _post_retrain_hook)

CPU-only guarantee
------------------
CUDA_VISIBLE_DEVICES="" is set by life_brain_runner.py before any import.
All tensor ops below also pass device=_CPU explicitly as belt-and-suspenders.
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("life-brain")

# ── Paths ─────────────────────────────────────────────────────────────────────
_LIFE_DIR     = Path(__file__).resolve().parents[1]
_OUTPUT_DIR   = _LIFE_DIR / "output"
_DATASET_PATH = _OUTPUT_DIR / "life_brain_dataset.jsonl"
_REPORT_PATH  = _OUTPUT_DIR / "life_brain_report.json"
_MODEL_PATH   = _OUTPUT_DIR / "life_brain_model.pt"
_SNAPSHOT_PATH= _OUTPUT_DIR / "life_brain_snapshot.json"

# Fallback model report paths (for LOTO comparison)
_PNET_REPORT_PATH  = _OUTPUT_DIR / "protein_models"  / "proteinnet_report.json"
_CNET_REPORT_PATH  = _OUTPUT_DIR / "crispr_net_models" / "crispr_net_report.json"

# ── Hyper-parameters ──────────────────────────────────────────────────────────
MIN_ROWS_PER_BRANCH = 150   # minimum confirmed rows to attempt first training
RETRAIN_EVERY       = 20    # retrain after this many new confirmed rows (any modality)
LOTO_MIN_HELD       = 5     # skip held-out target if fewer than this many rows
LOTO_MIN_TARGETS    = 3     # need at least this many eligible targets for LOTO
EPOCHS              = 30    # training epochs per retrain
LR                  = 3e-4
EDGE_SIMILARITY_THRESHOLD = 0.7   # cosine sim threshold for snapshot edges

# ── In-memory state ───────────────────────────────────────────────────────────
_model          = None   # loaded nn.Module or None
_report: dict   = {}     # last written report
_row_count_at_last_train: int = 0

# ── CPU device ────────────────────────────────────────────────────────────────
import os as _os
_os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")   # belt-and-suspenders

def _cpu():
    import torch
    return torch.device("cpu")


# ── Data loading ──────────────────────────────────────────────────────────────

def load_dataset() -> list[dict]:
    """
    Load all rows from life_brain_dataset.jsonl.
    Each row must have: target_id, modality, sequence, label, miner_wallet, ts.
    """
    rows: list[dict] = []
    if not _DATASET_PATH.exists():
        return rows
    try:
        for line in _DATASET_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception as e:
        log.warning(f"[LIFE-BRAIN] load_dataset failed: {e}")
    return rows


def _split_by_modality(rows: list[dict]) -> dict[str, list[dict]]:
    """Split confirmed rows into protein / mrna / crispr buckets."""
    buckets: dict[str, list[dict]] = {"protein": [], "mrna": [], "crispr": []}
    for r in rows:
        m = r.get("modality", "protein")
        if m in buckets:
            buckets[m].append(r)
    return buckets


# ── Featurization (batch) ─────────────────────────────────────────────────────

def _featurize_smiles_batch(rows: list[dict]):
    """Return (X, y, target_ids) numpy arrays for protein/mRNA rows."""
    try:
        import numpy as np
        from adaptive.life_brain_model import featurize_smiles
    except ImportError as e:
        log.warning(f"[LIFE-BRAIN] featurize_smiles import failed: {e}")
        return None, None, None

    X, y, tids = [], [], []
    for r in rows:
        feats = featurize_smiles(r.get("sequence", ""))
        if feats is None:
            continue
        label = r.get("label")
        if label is None:
            continue
        try:
            y.append(float(label))
        except (TypeError, ValueError):
            continue
        X.append(feats)
        tids.append(int(r.get("target_id", 0)))
    if not X:
        return None, None, None
    return (np.array(X, dtype=np.float32),
            np.array(y, dtype=np.float32),
            np.array(tids, dtype=np.int64))


def _featurize_crispr_batch(rows: list[dict]):
    """Return (seq_ohs, handcrafteds, y, target_ids) arrays for CRISPR rows."""
    try:
        import numpy as np
        from adaptive.life_brain_model import featurize_crispr_handcrafted, onehot_crispr
    except ImportError as e:
        log.warning(f"[LIFE-BRAIN] CRISPR featurize import failed: {e}")
        return None, None, None, None

    ohs, hcs, y, tids = [], [], [], []
    for r in rows:
        seq = r.get("sequence", "")
        oh  = onehot_crispr(seq)
        hc  = featurize_crispr_handcrafted(seq)
        if oh is None or hc is None:
            continue
        label = r.get("label")
        if label is None:
            continue
        try:
            y.append(float(label))
        except (TypeError, ValueError):
            continue
        ohs.append(oh)
        hcs.append(hc)
        tids.append(int(r.get("target_id", 3000)))
    if not ohs:
        return None, None, None, None
    return (np.array(ohs,  dtype=np.float32),
            np.array(hcs,  dtype=np.float32),
            np.array(y,    dtype=np.float32),
            np.array(tids, dtype=np.int64))


# ── Branch MAE helpers ────────────────────────────────────────────────────────

def _mae(preds, labels) -> float:
    import numpy as np
    return float(np.mean(np.abs(np.array(preds) - np.array(labels))))


def _fallback_mae_protein(held_target_id: int, held_rows: list[dict]) -> Optional[float]:
    """Predict held-out rows with existing ProteinNet GBR; return MAE or None."""
    try:
        import pickle
        import numpy as np
        from adaptive.life_brain_model import featurize_smiles
        # Try loading pkl from protein_models/
        report_path = _PNET_REPORT_PATH / "proteinnet_report.json"
        if not report_path.exists():
            return None
        report  = json.loads(report_path.read_text())
        uid     = report.get("models", {}).get(str(held_target_id), {}).get("uniprot_id", str(held_target_id))
        pkl_path = _PNET_REPORT_PATH / f"{uid}_model.pkl"
        if not pkl_path.exists():
            return None
        with pkl_path.open("rb") as fh:
            gbr = pickle.load(fh)
        X, y = [], []
        for r in held_rows:
            feats = featurize_smiles(r.get("sequence", ""))
            if feats is None:
                continue
            try:
                y.append(float(r["label"]))
            except Exception:
                continue
            X.append(feats)
        if not X:
            return None
        preds = gbr.predict(np.array(X, dtype=np.float32))
        return _mae(preds, y)
    except Exception:
        return None


def _fallback_mae_crispr(held_target_id: int, held_rows: list[dict]) -> Optional[float]:
    """Predict held-out rows with existing CRISPR-Net GBR; return MAE or None."""
    try:
        import pickle
        import numpy as np
        from adaptive.life_brain_model import featurize_crispr_handcrafted
        tid_str  = str(held_target_id)
        pkl_path = _LIFE_DIR / "output" / "crispr_net_models" / f"{tid_str}_model.pkl"
        if not pkl_path.exists():
            return None
        with pkl_path.open("rb") as fh:
            gbr = pickle.load(fh)
        X, y = [], []
        for r in held_rows:
            hc = featurize_crispr_handcrafted(r.get("sequence", ""))
            if hc is None:
                continue
            try:
                y.append(float(r["label"]))
            except Exception:
                continue
            X.append(hc)
        if not X:
            return None
        preds = gbr.predict(np.array(X, dtype=np.float32))
        return _mae(preds, y)
    except Exception:
        return None


# ── LOTO cross-validation ─────────────────────────────────────────────────────

def _loto_cv_smiles(
    rows: list[dict],
    model_module,
    branch_attr: str,
    fallback_fn,
    modality_name: str,
) -> dict:
    """
    Leave-one-target-out CV for protein or mRNA branch.

    Returns dict with keys: brain_maes, fallback_maes, per_target, trusted.
    """
    import torch, numpy as np
    from adaptive.life_brain_model import featurize_smiles

    target_groups: dict[int, list[dict]] = {}
    for r in rows:
        tid = int(r.get("target_id", 0))
        target_groups.setdefault(tid, []).append(r)

    eligible_targets = [tid for tid, rs in target_groups.items() if len(rs) >= LOTO_MIN_HELD]
    if len(eligible_targets) < LOTO_MIN_TARGETS:
        return {"trusted": False, "reason": f"only {len(eligible_targets)} eligible targets (need {LOTO_MIN_TARGETS})",
                "brain_maes": [], "fallback_maes": [], "per_target": {}}

    cpu = _cpu()
    brain_maes, fallback_maes, per_target = [], [], {}

    for held_tid in eligible_targets:
        train_rows = [r for tid, rs in target_groups.items() if tid != held_tid for r in rs]
        held_rows  = target_groups[held_tid]

        if len(train_rows) < MIN_ROWS_PER_BRANCH:
            continue

        # Build a temporary copy of the full model, train on train_rows
        tmp_model = _build_fresh_model()
        if tmp_model is None:
            continue
        optim = torch.optim.Adam(tmp_model.parameters(), lr=LR)
        _train_smiles_branch(tmp_model, branch_attr, train_rows, optim, EPOCHS, cpu)

        # Evaluate on held_rows
        branch_net = getattr(tmp_model, branch_attr)
        branch_net.eval()
        preds, actuals = [], []
        with torch.no_grad():
            for r in held_rows:
                feats = featurize_smiles(r.get("sequence", ""))
                if feats is None:
                    continue
                try:
                    actual = float(r["label"])
                except Exception:
                    continue
                x       = torch.tensor([feats], dtype=torch.float32, device=cpu)
                emb     = tmp_model.get_embed(torch.tensor([int(r.get("target_id", 0))], device=cpu))
                inp     = torch.cat([x, emb], dim=1)
                out     = branch_net(inp)
                preds.append(float(out[0, 0].item()))
                actuals.append(actual)

        if not preds:
            continue

        b_mae  = _mae(preds, actuals)
        f_mae  = fallback_fn(held_tid, held_rows)
        brain_maes.append(b_mae)
        per_target[str(held_tid)] = {"brain_mae": round(b_mae, 4), "fallback_mae": round(f_mae, 4) if f_mae else None, "n_held": len(held_rows)}
        if f_mae is not None:
            fallback_maes.append(f_mae)

    if not brain_maes:
        return {"trusted": False, "reason": "no LOTO splits produced valid predictions",
                "brain_maes": [], "fallback_maes": [], "per_target": per_target}

    import statistics
    brain_median   = statistics.median(brain_maes)
    trusted = False
    if fallback_maes:
        fallback_median = statistics.median(fallback_maes)
        trusted = brain_median < fallback_median
        reason  = f"brain_median_MAE={brain_median:.4f} {'<' if trusted else '>='} fallback_median_MAE={fallback_median:.4f}"
    else:
        reason = f"no fallback models available for comparison; brain_median_MAE={brain_median:.4f}"

    return {
        "trusted":        trusted,
        "reason":         reason,
        "brain_maes":     [round(m, 4) for m in brain_maes],
        "fallback_maes":  [round(m, 4) for m in fallback_maes],
        "per_target":     per_target,
    }


def _loto_cv_crispr(rows: list[dict], model_module) -> dict:
    """LOTO CV for CRISPR branch."""
    import torch, numpy as np
    from adaptive.life_brain_model import featurize_crispr_handcrafted, onehot_crispr

    target_groups: dict[int, list[dict]] = {}
    for r in rows:
        tid = int(r.get("target_id", 3000))
        target_groups.setdefault(tid, []).append(r)

    eligible_targets = [tid for tid, rs in target_groups.items() if len(rs) >= LOTO_MIN_HELD]
    if len(eligible_targets) < LOTO_MIN_TARGETS:
        return {"trusted": False, "reason": f"only {len(eligible_targets)} eligible targets",
                "brain_maes": [], "fallback_maes": [], "per_target": {}}

    cpu = _cpu()
    brain_maes, fallback_maes, per_target = [], [], {}

    for held_tid in eligible_targets:
        train_rows = [r for tid, rs in target_groups.items() if tid != held_tid for r in rs]
        held_rows  = target_groups[held_tid]
        if len(train_rows) < MIN_ROWS_PER_BRANCH:
            continue

        tmp_model = _build_fresh_model()
        if tmp_model is None:
            continue
        optim = torch.optim.Adam(tmp_model.parameters(), lr=LR)
        _train_crispr_branch(tmp_model, train_rows, optim, EPOCHS, cpu)

        tmp_model.crispr.eval()
        preds, actuals = [], []
        with torch.no_grad():
            for r in held_rows:
                seq = r.get("sequence", "")
                oh  = onehot_crispr(seq)
                hc  = featurize_crispr_handcrafted(seq)
                if oh is None or hc is None:
                    continue
                try:
                    actual = float(r["label"])
                except Exception:
                    continue
                oh_t    = torch.tensor([oh],  dtype=torch.float32, device=cpu)
                hc_t    = torch.tensor([hc],  dtype=torch.float32, device=cpu)
                emb     = tmp_model.get_embed(torch.tensor([int(r.get("target_id", 3000))], device=cpu))
                out     = tmp_model.crispr(oh_t, hc_t, emb)
                preds.append(float(out[0, 0].item()))
                actuals.append(actual)

        if not preds:
            continue

        b_mae = _mae(preds, actuals)
        f_mae = _fallback_mae_crispr(held_tid, held_rows)
        brain_maes.append(b_mae)
        per_target[str(held_tid)] = {"brain_mae": round(b_mae, 4), "fallback_mae": round(f_mae, 4) if f_mae else None, "n_held": len(held_rows)}
        if f_mae is not None:
            fallback_maes.append(f_mae)

    if not brain_maes:
        return {"trusted": False, "reason": "no LOTO splits",
                "brain_maes": [], "fallback_maes": [], "per_target": per_target}

    import statistics
    brain_median = statistics.median(brain_maes)
    if fallback_maes:
        fallback_median = statistics.median(fallback_maes)
        trusted = brain_median < fallback_median
        reason  = f"brain_median_MAE={brain_median:.4f} {'<' if trusted else '>='} fallback={fallback_median:.4f}"
    else:
        trusted = False
        reason  = f"no fallback models; brain_median_MAE={brain_median:.4f}"

    return {
        "trusted":       trusted,
        "reason":        reason,
        "brain_maes":    [round(m, 4) for m in brain_maes],
        "fallback_maes": [round(m, 4) for m in fallback_maes],
        "per_target":    per_target,
    }


# ── Training helpers ──────────────────────────────────────────────────────────

def _build_fresh_model():
    """Instantiate a new untrained LifeBrainModel on CPU."""
    try:
        from adaptive.life_brain_model import LifeBrainModel
        return LifeBrainModel.build()
    except Exception as e:
        log.warning(f"[LIFE-BRAIN] _build_fresh_model failed: {e}")
        return None


def _train_smiles_branch(model, branch_attr: str, rows: list[dict], optim, epochs: int, cpu):
    """Train one smiles-based branch (protein or mrna) in-place."""
    import torch
    from adaptive.life_brain_model import LifeBrainModel, featurize_smiles

    branch = getattr(model, branch_attr)
    branch.train()
    model.embedding.train()

    # Build tensors
    X_list, y_list, tid_list = [], [], []
    for r in rows:
        feats = featurize_smiles(r.get("sequence", ""))
        if feats is None:
            continue
        try:
            y_list.append(float(r["label"]))
        except Exception:
            continue
        X_list.append(feats)
        tid_list.append(int(r.get("target_id", 0)))

    if len(X_list) < 2:
        return

    import numpy as np
    X   = torch.tensor(np.array(X_list,   dtype=np.float32), device=cpu)
    y   = torch.tensor(np.array(y_list,   dtype=np.float32), device=cpu)
    tids= torch.tensor(np.array(tid_list, dtype=np.int64),   device=cpu)

    for _ in range(epochs):
        optim.zero_grad()
        emb  = model.get_embed(tids)         # (N, 16)
        inp  = torch.cat([X, emb], dim=1)    # (N, 2072)
        out  = branch(inp)                   # (N, 2)
        loss = LifeBrainModel.gaussian_nll(out, y)
        loss.backward()
        optim.step()


def _train_crispr_branch(model, rows: list[dict], optim, epochs: int, cpu):
    """Train the CRISPR branch in-place."""
    import torch
    from adaptive.life_brain_model import LifeBrainModel, featurize_crispr_handcrafted, onehot_crispr

    model.crispr.train()
    model.embedding.train()

    ohs, hcs, y_list, tid_list = [], [], [], []
    for r in rows:
        seq = r.get("sequence", "")
        oh  = onehot_crispr(seq)
        hc  = featurize_crispr_handcrafted(seq)
        if oh is None or hc is None:
            continue
        try:
            y_list.append(float(r["label"]))
        except Exception:
            continue
        ohs.append(oh)
        hcs.append(hc)
        tid_list.append(int(r.get("target_id", 3000)))

    if len(ohs) < 2:
        return

    import numpy as np
    OH  = torch.tensor(np.array(ohs,     dtype=np.float32), device=cpu)
    HC  = torch.tensor(np.array(hcs,     dtype=np.float32), device=cpu)
    y   = torch.tensor(np.array(y_list,  dtype=np.float32), device=cpu)
    tids= torch.tensor(np.array(tid_list,dtype=np.int64),   device=cpu)

    for _ in range(epochs):
        optim.zero_grad()
        emb  = model.get_embed(tids)
        out  = model.crispr(OH, HC, emb)
        loss = LifeBrainModel.gaussian_nll(out, y)
        loss.backward()
        optim.step()


# ── Joint training ────────────────────────────────────────────────────────────

def _train_joint(model, buckets: dict[str, list[dict]], cpu):
    """
    Train all three branches jointly for EPOCHS epochs.
    Branches are weighted inversely to row count to prevent one branch dominating.
    """
    import torch
    from adaptive.life_brain_model import (
        LifeBrainModel, featurize_smiles, featurize_crispr_handcrafted, onehot_crispr
    )
    import numpy as np

    optim = torch.optim.Adam(model.parameters(), lr=LR)

    n_p = len(buckets.get("protein", []))
    n_m = len(buckets.get("mrna",    []))
    n_c = len(buckets.get("crispr",  []))
    total = max(n_p + n_m + n_c, 1)
    # Inverse-frequency weights (branch with fewer rows gets higher weight)
    w_p = total / max(n_p, 1)
    w_m = total / max(n_m, 1)
    w_c = total / max(n_c, 1)
    # Normalise
    w_sum = w_p + w_m + w_c
    w_p, w_m, w_c = w_p / w_sum, w_m / w_sum, w_c / w_sum

    # Precompute tensors
    def _smiles_tensors(rows):
        X_list, y_list, tid_list = [], [], []
        for r in rows:
            feats = featurize_smiles(r.get("sequence", ""))
            if feats is None:
                continue
            try:
                y_list.append(float(r["label"]))
            except Exception:
                continue
            X_list.append(feats)
            tid_list.append(int(r.get("target_id", 0)))
        if not X_list:
            return None, None, None
        return (
            torch.tensor(np.array(X_list,   dtype=np.float32), device=cpu),
            torch.tensor(np.array(y_list,   dtype=np.float32), device=cpu),
            torch.tensor(np.array(tid_list, dtype=np.int64),   device=cpu),
        )

    def _crispr_tensors(rows):
        ohs, hcs, y_list, tid_list = [], [], [], []
        for r in rows:
            seq = r.get("sequence", "")
            oh  = onehot_crispr(seq)
            hc  = featurize_crispr_handcrafted(seq)
            if oh is None or hc is None:
                continue
            try:
                y_list.append(float(r["label"]))
            except Exception:
                continue
            ohs.append(oh)
            hcs.append(hc)
            tid_list.append(int(r.get("target_id", 3000)))
        if not ohs:
            return None, None, None, None
        return (
            torch.tensor(np.array(ohs,     dtype=np.float32), device=cpu),
            torch.tensor(np.array(hcs,     dtype=np.float32), device=cpu),
            torch.tensor(np.array(y_list,  dtype=np.float32), device=cpu),
            torch.tensor(np.array(tid_list,dtype=np.int64),   device=cpu),
        )

    Xp, yp, tidp = _smiles_tensors(buckets.get("protein", []))
    Xm, ym, tidm = _smiles_tensors(buckets.get("mrna",    []))
    crispr_data   = _crispr_tensors(buckets.get("crispr",  []))

    model.protein.train()
    model.mrna.train()
    model.crispr.train()
    model.embedding.train()

    for epoch in range(EPOCHS):
        optim.zero_grad()
        total_loss = torch.tensor(0.0, device=cpu, requires_grad=False)
        total_loss = total_loss * 1.0   # ensure it's a leaf; we sum below

        branch_losses = []

        if Xp is not None and yp is not None and len(Xp) > 1:
            emb  = model.get_embed(tidp)
            inp  = torch.cat([Xp, emb], dim=1)
            out  = model.protein(inp)
            lp   = LifeBrainModel.gaussian_nll(out, yp)
            branch_losses.append(w_p * lp)

        if Xm is not None and ym is not None and len(Xm) > 1:
            emb  = model.get_embed(tidm)
            inp  = torch.cat([Xm, emb], dim=1)
            out  = model.mrna(inp)
            lm   = LifeBrainModel.gaussian_nll(out, ym)
            branch_losses.append(w_m * lm)

        OH, HC, yc, tidc = crispr_data
        if OH is not None and yc is not None and len(OH) > 1:
            emb  = model.get_embed(tidc)
            out  = model.crispr(OH, HC, emb)
            lc   = LifeBrainModel.gaussian_nll(out, yc)
            branch_losses.append(w_c * lc)

        if not branch_losses:
            continue

        import torch as _torch
        loss = _torch.stack(branch_losses).sum()
        loss.backward()
        optim.step()

        if epoch % 10 == 0:
            log.debug(f"[LIFE-BRAIN] epoch {epoch}/{EPOCHS} loss={loss.item():.4f}")


# ── Snapshot export ───────────────────────────────────────────────────────────

def export_snapshot(model, report: dict, rows: list[dict]) -> None:
    """
    Export output/life_brain_snapshot.json from current model embeddings.
    Called after every successful retrain.
    Nodes + edges are derived entirely from the trained embedding table.
    """
    try:
        import torch
        import numpy as np
        from sklearn.decomposition import TruncatedSVD

        cpu = _cpu()
        model.eval()

        # Collect unique trusted target_ids from report
        unique_targets: dict[int, dict] = {}   # tid → metadata
        for row in rows:
            tid = int(row.get("target_id", 0))
            if tid not in unique_targets:
                modality  = row.get("modality", "protein")
                unique_targets[tid] = {
                    "target_id":   tid,
                    "target_name": row.get("target_name", str(tid)),
                    "modality":    modality,
                    "row_count":   0,
                }
            unique_targets[tid]["row_count"] += 1

        if not unique_targets:
            _write_empty_snapshot(report)
            return

        # Grab embeddings for all known target_ids
        tid_list = sorted(unique_targets.keys())
        tids_t   = torch.tensor(tid_list, dtype=torch.long, device=cpu)
        with torch.no_grad():
            embeds = model.get_embed(tids_t).numpy()   # (N, 16)

        # PCA/SVD → 3-dim coordinates
        n_components = min(3, embeds.shape[0], embeds.shape[1])
        if n_components < 3:
            # Pad with zeros if fewer than 3 components possible
            coords = np.zeros((embeds.shape[0], 3), dtype=np.float32)
            coords[:, :n_components] = embeds[:, :n_components]
        else:
            svd = TruncatedSVD(n_components=3, random_state=42)
            coords = svd.fit_transform(embeds).astype(np.float32)
            # Normalise to [-1, 1]
            max_abs = np.abs(coords).max()
            if max_abs > 0:
                coords /= max_abs

        # Trust flag per branch from report
        branch_trust = {
            "protein": report.get("branches", {}).get("protein", {}).get("trusted", False),
            "mrna":    report.get("branches", {}).get("mrna",    {}).get("trusted", False),
            "crispr":  report.get("branches", {}).get("crispr",  {}).get("trusted", False),
        }

        # Build nodes
        nodes = []
        for i, tid in enumerate(tid_list):
            meta  = unique_targets[tid]
            mod   = meta["modality"]
            nodes.append({
                "target_id":   tid,
                "target_name": meta["target_name"],
                "modality":    mod,
                "x": round(float(coords[i, 0]), 4),
                "y": round(float(coords[i, 1]), 4),
                "z": round(float(coords[i, 2]), 4),
                "row_count":   meta["row_count"],
                "trusted":     branch_trust.get(mod, False),
                "last_updated": time.time(),
            })

        # Build edges: cosine similarity between trusted embeddings only
        trusted_idx = [i for i, tid in enumerate(tid_list) if branch_trust.get(unique_targets[tid]["modality"], False)]
        edges = []
        if len(trusted_idx) >= 2:
            trusted_embeds = embeds[trusted_idx]
            norms = np.linalg.norm(trusted_embeds, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            normed = trusted_embeds / norms
            for ii in range(len(trusted_idx)):
                for jj in range(ii + 1, len(trusted_idx)):
                    sim = float(np.dot(normed[ii], normed[jj]))
                    if sim >= EDGE_SIMILARITY_THRESHOLD:
                        edges.append({
                            "target_a":  tid_list[trusted_idx[ii]],
                            "target_b":  tid_list[trusted_idx[jj]],
                            "similarity": round(sim, 4),
                        })

        unique_miners = len({r.get("miner_wallet", "") for r in rows if r.get("miner_wallet")})

        snapshot = {
            "nodes": nodes,
            "edges": edges,
            "meta": {
                "total_rows_ingested": len(rows),
                "unique_miner_count":  unique_miners,
                "last_trained":        time.time(),
                "branch_trust":        branch_trust,
            },
        }
        _SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        log.info(f"[LIFE-BRAIN] Snapshot exported: {len(nodes)} nodes, {len(edges)} edges → {_SNAPSHOT_PATH}")

    except Exception as e:
        log.error(f"[LIFE-BRAIN] export_snapshot failed: {e}", exc_info=True)


def _write_empty_snapshot(report: dict) -> None:
    snap = {
        "nodes": [], "edges": [],
        "meta": {"total_rows_ingested": 0, "unique_miner_count": 0,
                 "last_trained": None, "branch_trust": {}},
    }
    try:
        _SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Report I/O ────────────────────────────────────────────────────────────────

def _save_report(report: dict) -> None:
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"[LIFE-BRAIN] save_report failed: {e}")


def get_report() -> dict:
    if _report:
        return dict(_report)
    if _REPORT_PATH.exists():
        try:
            return json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ── Main retrain entry point ──────────────────────────────────────────────────

def retrain(rows: list[dict]) -> dict:
    """
    Full retrain + LOTO CV cycle.

    Parameters
    ----------
    rows : confirmed submission rows from life_brain_dataset.jsonl

    Returns
    -------
    dict — updated report (also written to disk)
    """
    global _model, _report, _row_count_at_last_train
    t0 = time.time()

    buckets = _split_by_modality(rows)
    n_p = len(buckets["protein"])
    n_m = len(buckets["mrna"])
    n_c = len(buckets["crispr"])
    total = n_p + n_m + n_c

    log.info(f"[LIFE-BRAIN] retrain start: protein={n_p}, mRNA={n_m}, CRISPR={n_c}")

    # ── Build + train full model ───────────────────────────────────────────────
    model = _build_fresh_model()
    if model is None:
        log.error("[LIFE-BRAIN] Could not build model — torch unavailable?")
        return _report

    cpu = _cpu()
    _train_joint(model, buckets, cpu)

    # ── Save model weights ─────────────────────────────────────────────────────
    try:
        import torch
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        torch.save(model.state_dict(), _MODEL_PATH)
        log.info(f"[LIFE-BRAIN] Model saved → {_MODEL_PATH}")
    except Exception as e:
        log.warning(f"[LIFE-BRAIN] Model save failed: {e}")

    # ── LOTO CV per branch ─────────────────────────────────────────────────────
    branch_results: dict[str, dict] = {}

    for branch_name, branch_attr, fallback_fn in [
        ("protein", "protein", lambda tid, rs: _fallback_mae_protein(tid, rs)),
        ("mrna",    "mrna",    lambda tid, rs: _fallback_mae_protein(tid, rs)),  # same fallback type
    ]:
        branch_rows = buckets.get(branch_name, [])
        if len(branch_rows) < MIN_ROWS_PER_BRANCH:
            branch_results[branch_name] = {
                "trusted": False,
                "n": len(branch_rows),
                "reason": f"insufficient data: {len(branch_rows)}/{MIN_ROWS_PER_BRANCH}",
            }
            log.info(f"[LIFE-BRAIN] {branch_name} branch not yet trusted "
                     f"(n={len(branch_rows)}, min={MIN_ROWS_PER_BRANCH}) — using fallback")
        else:
            cv = _loto_cv_smiles(branch_rows, model, branch_attr, fallback_fn, branch_name)
            cv["n"] = len(branch_rows)
            branch_results[branch_name] = cv
            status = "trusted" if cv["trusted"] else "not yet trusted"
            log.info(f"[LIFE-BRAIN] {branch_name} branch {status} "
                     f"(n={len(branch_rows)}, {cv.get('reason', '')})"
                     + (" — using for pre-screen" if cv["trusted"] else " — using fallback"))

    crispr_rows = buckets.get("crispr", [])
    if len(crispr_rows) < MIN_ROWS_PER_BRANCH:
        branch_results["crispr"] = {
            "trusted": False,
            "n": len(crispr_rows),
            "reason": f"insufficient data: {len(crispr_rows)}/{MIN_ROWS_PER_BRANCH}",
        }
        log.info(f"[LIFE-BRAIN] CRISPR branch not yet trusted "
                 f"(n={len(crispr_rows)}, min={MIN_ROWS_PER_BRANCH}) — using CRISPR-Net fallback")
    else:
        cv = _loto_cv_crispr(crispr_rows, model)
        cv["n"] = len(crispr_rows)
        branch_results["crispr"] = cv
        status = "trusted" if cv["trusted"] else "not yet trusted"
        log.info(f"[LIFE-BRAIN] CRISPR branch {status} "
                 f"(n={len(crispr_rows)}, {cv.get('reason', '')})"
                 + (" — using for pre-screen" if cv["trusted"] else " — using CRISPR-Net fallback"))

    elapsed = time.time() - t0
    unique_miners = len({r.get("miner_wallet", "") for r in rows if r.get("miner_wallet")})

    report: dict = {
        "total_rows":          total,
        "unique_miners":       unique_miners,
        "branches":            branch_results,
        "last_trained":        time.time(),
        "train_elapsed_s":     round(elapsed, 1),
        "generated_at":        time.time(),
    }

    _model = model
    _report.clear()
    _report.update(report)
    _save_report(report)
    _row_count_at_last_train = total

    # Export snapshot for Part C
    try:
        export_snapshot(model, report, rows)
    except Exception as e:
        log.warning(f"[LIFE-BRAIN] export_snapshot raised: {e}")

    return report


def should_retrain(rows: list[dict]) -> bool:
    """True if ≥20 new confirmed rows have appeared since last retrain."""
    return len(rows) >= _row_count_at_last_train + RETRAIN_EVERY


# ── Startup: load persisted model if available ────────────────────────────────

def load_persisted_model() -> bool:
    """Load existing model.pt into _model at startup. Returns True if loaded."""
    global _model
    if not _MODEL_PATH.exists():
        return False
    try:
        import torch
        from adaptive.life_brain_model import LifeBrainModel
        m = LifeBrainModel.build()
        m.load_state_dict(torch.load(_MODEL_PATH, map_location=_cpu()))
        m.eval()
        _model = m
        log.info(f"[LIFE-BRAIN] Loaded persisted model from {_MODEL_PATH}")
        return True
    except Exception as e:
        log.warning(f"[LIFE-BRAIN] load_persisted_model failed: {e}")
        return False


def load_persisted_report() -> None:
    """Load existing report.json into _report at startup."""
    global _report, _row_count_at_last_train
    if _REPORT_PATH.exists():
        try:
            data = json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
            _report.clear()
            _report.update(data)
            _row_count_at_last_train = data.get("total_rows", 0)
            log.info(f"[LIFE-BRAIN] Loaded persisted report ({_row_count_at_last_train} rows at last train)")
        except Exception as e:
            log.warning(f"[LIFE-BRAIN] load_persisted_report failed: {e}")


# ── Inference: public predict functions (used by gateway) ────────────────────

def predict_smiles(smiles: str, target_id: int, branch: str = "protein"):
    """
    Return (predicted_value, log_variance) or None if model not available.
    branch: 'protein' or 'mrna'
    """
    if _model is None:
        return None
    try:
        import torch
        from adaptive.life_brain_model import featurize_smiles
        feats = featurize_smiles(smiles)
        if feats is None:
            return None
        cpu  = _cpu()
        x    = torch.tensor([feats], dtype=torch.float32, device=cpu)
        emb  = _model.get_embed(torch.tensor([target_id], device=cpu))
        inp  = torch.cat([x, emb], dim=1)
        net  = getattr(_model, branch)
        net.eval()
        with torch.no_grad():
            out = net(inp)
        return float(out[0, 0].item()), float(out[0, 1].item())
    except Exception as e:
        log.debug(f"[LIFE-BRAIN] predict_smiles failed: {e}")
        return None


def predict_crispr(seq: str, target_id: int):
    """Return (predicted_iptm, log_variance) or None."""
    if _model is None:
        return None
    try:
        import torch
        from adaptive.life_brain_model import featurize_crispr_handcrafted, onehot_crispr
        oh = onehot_crispr(seq)
        hc = featurize_crispr_handcrafted(seq)
        if oh is None or hc is None:
            return None
        cpu   = _cpu()
        oh_t  = torch.tensor([oh], dtype=torch.float32, device=cpu)
        hc_t  = torch.tensor([hc], dtype=torch.float32, device=cpu)
        emb   = _model.get_embed(torch.tensor([target_id], device=cpu))
        _model.crispr.eval()
        with torch.no_grad():
            out = _model.crispr(oh_t, hc_t, emb)
        return float(out[0, 0].item()), float(out[0, 1].item())
    except Exception as e:
        log.debug(f"[LIFE-BRAIN] predict_crispr failed: {e}")
        return None


def branch_trusted(branch: str) -> bool:
    """Check live report for trust flag."""
    rep = get_report()
    return bool(rep.get("branches", {}).get(branch, {}).get("trusted", False))
