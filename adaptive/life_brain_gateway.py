"""
life_brain_gateway.py — Thin inference gateway for miner_daemon.py.

PURPOSE
-------
This file is the ONLY life-brain file imported by miner_daemon.py.
It never imports life_brain.py or life_brain_model.py directly.
Instead it reads the model checkpoint (.pt) and report (.json) from disk
at startup, exactly as life_proteinnet.py reads its .pkl files.

ISOLATION GUARANTEES
--------------------
- CPU-only: CUDA_VISIBLE_DEVICES="" set before any torch import.
- If the model file is absent or a branch is not trusted, returns None
  and the caller's existing fallback runs unchanged — zero regression risk.
- life_brain_runner.py (separate PM2 process) owns all training.
  This module only reads the files that runner produces.

API (mirrors the shape of life_proteinnet.pre_screen and life_crispr_net.pre_screen)
---
  is_trusted(branch)                         → bool
  pre_screen_smiles(smiles_list, target_id, branch, top_n) → list[str] | None
  pre_screen_crispr(seqs, target_id, top_n)  → list[str] | None
  get_report()                               → dict
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

# Belt-and-suspenders CPU enforcement at import time
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

log = logging.getLogger("life-miner")   # same logger as miner_daemon.py

# ── Paths ─────────────────────────────────────────────────────────────────────
_LIFE_DIR     = Path(__file__).resolve().parents[1]
_OUTPUT_DIR   = _LIFE_DIR / "output"
_MODEL_PATH   = _OUTPUT_DIR / "life_brain_model.pt"
_REPORT_PATH  = _OUTPUT_DIR / "life_brain_report.json"

# ── In-memory cache ───────────────────────────────────────────────────────────
_model             = None   # loaded nn.Module or None
_report: dict      = {}
_report_ts: float  = 0.0
_REPORT_TTL        = 60.0   # seconds before re-reading report from disk


# ── Report reader ─────────────────────────────────────────────────────────────

def _refresh_report() -> None:
    global _report, _report_ts
    if time.time() - _report_ts < _REPORT_TTL:
        return
    if not _REPORT_PATH.exists():
        return
    try:
        _report = json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
        _report_ts = time.time()
    except Exception as e:
        log.debug(f"[LIFE-BRAIN] gateway: refresh_report failed: {e}")


def get_report() -> dict:
    _refresh_report()
    return dict(_report)


def is_trusted(branch: str) -> bool:
    """
    Return True only if the named branch has passed LOTO CV and is marked trusted.
    Reads from the cached/disk report — no model needed.
    """
    _refresh_report()
    return bool(_report.get("branches", {}).get(branch, {}).get("trusted", False))


# ── Model loader ──────────────────────────────────────────────────────────────

def _load_model():
    global _model
    if _model is not None:
        return _model
    if not _MODEL_PATH.exists():
        return None
    try:
        import torch
        # Import model class without importing life_brain.py
        # We import life_brain_model.py which has no training dependencies
        from adaptive.life_brain_model import LifeBrainModel
        m = LifeBrainModel.build()
        m.load_state_dict(
            torch.load(_MODEL_PATH, map_location=torch.device("cpu"))
        )
        m.eval()
        _model = m
        log.info("[LIFE-BRAIN] gateway: model loaded from disk")
        return _model
    except Exception as e:
        log.debug(f"[LIFE-BRAIN] gateway: model load failed (non-fatal): {e}")
        return None


# ── Pre-screen APIs ───────────────────────────────────────────────────────────

def pre_screen_smiles(
    smiles_list: list[str],
    target_id: int,
    branch: str = "protein",
    top_n: int = 100,
) -> Optional[list[str]]:
    """
    Pre-screen a list of SMILES through the LIFE-BRAIN protein or mRNA branch.

    Returns the top_n SMILES sorted by predicted affinity (most negative first),
    or None if the branch is not trusted or the model is unavailable.

    Caller must fall back to the existing pre-screener when None is returned.
    """
    if not is_trusted(branch):
        return None

    model = _load_model()
    if model is None:
        return None

    try:
        import torch
        import numpy as np
        from adaptive.life_brain_model import featurize_smiles

        cpu = torch.device("cpu")
        scored: list[tuple[float, str]] = []

        for smi in smiles_list:
            feats = featurize_smiles(smi)
            if feats is None:
                continue
            try:
                x   = torch.tensor([feats], dtype=torch.float32, device=cpu)
                emb = model.get_embed(torch.tensor([target_id], device=cpu))
                inp = torch.cat([x, emb], dim=1)
                net = getattr(model, branch)
                net.eval()
                with torch.no_grad():
                    out = net(inp)
                pred = float(out[0, 0].item())
                scored.append((pred, smi))
            except Exception:
                continue

        if not scored:
            return None

        # Lower (more negative) affinity = better binder → sort ascending
        scored.sort(key=lambda t: t[0])
        result = [smi for _, smi in scored[:top_n]]
        log.info(
            f"[LIFE-BRAIN] {branch} branch pre-screen: "
            f"{len(smiles_list):,} → top {len(result)} for target_id={target_id}"
        )
        return result

    except Exception as e:
        log.debug(f"[LIFE-BRAIN] pre_screen_smiles failed (non-fatal): {e}")
        return None


def pre_screen_crispr(
    seqs: list[str],
    target_id: int,
    top_n: int = 5,
) -> Optional[list[str]]:
    """
    Pre-screen gRNA 20-mers through the LIFE-BRAIN CRISPR branch.

    Returns the top_n sequences sorted by predicted iptm (highest first),
    or None if the branch is not trusted or the model is unavailable.
    """
    if not is_trusted("crispr"):
        return None

    model = _load_model()
    if model is None:
        return None

    try:
        import torch
        from adaptive.life_brain_model import featurize_crispr_handcrafted, onehot_crispr

        cpu = torch.device("cpu")
        scored: list[tuple[float, str]] = []

        for seq in seqs:
            oh = onehot_crispr(seq)
            hc = featurize_crispr_handcrafted(seq)
            if oh is None or hc is None:
                continue
            try:
                oh_t = torch.tensor([oh], dtype=torch.float32, device=cpu)
                hc_t = torch.tensor([hc], dtype=torch.float32, device=cpu)
                emb  = model.get_embed(torch.tensor([target_id], device=cpu))
                model.crispr.eval()
                with torch.no_grad():
                    out = model.crispr(oh_t, hc_t, emb)
                pred = float(out[0, 0].item())
                scored.append((pred, seq))
            except Exception:
                continue

        if not scored:
            return None

        # Higher predicted iptm = better → sort descending
        scored.sort(key=lambda t: t[0], reverse=True)
        result = [seq for _, seq in scored[:top_n]]
        log.info(
            f"[LIFE-BRAIN] CRISPR branch pre-screen: "
            f"{len(seqs)} → top {len(result)} for target_id={target_id}"
        )
        return result

    except Exception as e:
        log.debug(f"[LIFE-BRAIN] pre_screen_crispr failed (non-fatal): {e}")
        return None
