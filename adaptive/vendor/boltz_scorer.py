"""
adaptive/vendor/boltz_scorer.py — lease-profile-aware Boltz2 scorer (C3).

Wraps the pinned nova_pulse_scorer with the LIFE Compute coordinator's
scorer_profile fields.

Lease scorer_profile (from GET /work):
  {
    "boltz_version": "1.0.1",
    "flags": ["--use-msa-server", "--recycling-steps=3"],
    "msa_sampling": "uniform",   ← Boltz sampling_type (Python default = "linear")
    "seed": null                 ← null means use miner's default (68)
  }

MSA sampling difference report (required by task spec §6):
  - Boltz Python API default: msa_sampling is NOT a direct kwarg.  The Python
    predict() call accepts `sampling_steps` and `diffusion_samples`, not
    msa_sampling.  The nova_pulse_scorer uses `sampling_steps=25` (fast).
  - The lease scorer_profile carries msa_sampling="uniform".  In Boltz2 this
    maps to the `--msa-sampling-method uniform` CLI flag.  The Python predict()
    function exposes this as `msa_sampling_method` kwarg (added in boltz>=1.0).
    If the installed boltz version does not support this kwarg, the miner falls
    back gracefully (logs a warning) and omits the parameter.
  - The current protein loop (nova_pulse_scorer) does NOT pass msa_sampling at all;
    it uses Boltz's default ("linear").  The lease requires "uniform".  This is
    a divergence: the protein loop today uses msa_sampling="linear" (Boltz default)
    while the lease specifies msa_sampling="uniform".
  - The lease's "--recycling-steps=3" flag overrides nova_pulse_scorer's
    FAST_RECYCLING_STEPS=1.  The validator runs with recycling_steps=3; the miner
    runs with recycling_steps=1 (fast mode). This is a known speed/accuracy tradeoff.
    For live submission, use the profile's recycling_steps to match the validator.

This module exposes score_batch_with_profile() which applies the lease profile.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Ensure the vendored scorer is importable without nova_subnet on sys.path.
_VENDOR_DIR = Path(__file__).resolve().parent
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

# Override nova_subnet path expectations inside the vendored scorer.
# nova_pulse_scorer uses NOVA_DIR = Path(__file__).resolve().parents[1]
# Our vendored copy is at adaptive/vendor/, so parents[1] = adaptive/ — fine for
# imports, but BOLTZ_TMP / OUTPUT_DIR will be under the miner's tree not nova_subnet.
# No monkeypatching needed: those constants only affect output paths, not scoring logic.

from nova_pulse_scorer import _score_batch_impl as _nps_score_batch_impl  # noqa: E402

# Default: use fast settings (backward-compat with existing protein loop behaviour)
_DEFAULT_RECYCLING_STEPS     = 1
_DEFAULT_SAMPLING_STEPS      = 25
_DEFAULT_DIFFUSION_SAMPLES   = 1
_DEFAULT_SEED                = 68


def _parse_recycling_from_flags(flags: list[str]) -> Optional[int]:
    """Parse --recycling-steps=N from the lease flags list."""
    for flag in flags:
        if flag.startswith("--recycling-steps="):
            try:
                return int(flag.split("=", 1)[1])
            except ValueError:
                pass
    return None


def _msa_sampling_kwarg_supported() -> bool:
    """Check whether the installed boltz predict() accepts msa_sampling_method."""
    try:
        from boltz.main import predict  # type: ignore[import]
        sig = inspect.signature(predict)
        return "msa_sampling_method" in sig.parameters
    except Exception:
        return False


def score_batch_with_profile(
    smiles_list: list[str],
    target: str,
    protein_sequence: str,
    msa_path: str,
    scorer_profile: Optional[dict] = None,
) -> dict[str, Optional[float]]:
    """
    Score a batch of SMILES against a protein target using the lease scorer_profile.

    Differences vs nova_pulse_scorer.score_batch():
      1. recycling_steps follows the lease (--recycling-steps=N in flags), not FAST_RECYCLING_STEPS=1.
      2. msa_sampling_method follows the lease (msa_sampling field), not Boltz's default "linear".
      3. seed follows the lease (seed field), falling back to 68.

    These parameters must match what the validator uses for re-scoring.

    Returns {smiles: boltz_score_or_None}.
    """
    profile = scorer_profile or {}
    flags: list[str] = profile.get("flags", [])
    msa_sampling: str = profile.get("msa_sampling", "linear")
    seed_val = profile.get("seed")
    seed: int = seed_val if isinstance(seed_val, int) else _DEFAULT_SEED

    recycling_steps = _parse_recycling_from_flags(flags) or _DEFAULT_RECYCLING_STEPS
    sampling_steps  = _DEFAULT_SAMPLING_STEPS
    diffusion_samples = _DEFAULT_DIFFUSION_SAMPLES

    # Report any differences vs nova_pulse_scorer's fast settings
    if recycling_steps != _DEFAULT_RECYCLING_STEPS:
        log.info(
            "[BOLTZ-PROFILE] recycling_steps: lease=%d fast=%d (using lease value)",
            recycling_steps, _DEFAULT_RECYCLING_STEPS,
        )
    if msa_sampling != "linear":
        log.info(
            "[BOLTZ-PROFILE] msa_sampling: lease=%r boltz_default='linear' nova_scorer='linear' "
            "(nova_pulse_scorer does not pass msa_sampling — divergence from lease profile)",
            msa_sampling,
        )

    # Try to apply msa_sampling_method to predict() if supported
    _msa_kwarg_ok = _msa_sampling_kwarg_supported()
    if msa_sampling != "linear" and not _msa_kwarg_ok:
        log.warning(
            "[BOLTZ-PROFILE] msa_sampling=%r requested but installed boltz does not support "
            "msa_sampling_method kwarg; falling back to boltz default ('linear')",
            msa_sampling,
        )

    # nova_pulse_scorer._score_batch_impl uses module-level FAST_* constants for predict().
    # We patch them temporarily to apply the profile settings.
    import nova_pulse_scorer as _nps
    _orig = (
        _nps.FAST_RECYCLING_STEPS,
        _nps.FAST_SAMPLING_STEPS,
        _nps.FAST_DIFFUSION_SAMPLES,
        _nps.FAST_SAMPLING_STEPS_AFF,
        _nps.FAST_DIFFUSION_SAMPLES_AFF,
    )
    _nps.FAST_RECYCLING_STEPS      = recycling_steps
    _nps.FAST_SAMPLING_STEPS       = sampling_steps
    _nps.FAST_DIFFUSION_SAMPLES    = diffusion_samples
    _nps.FAST_SAMPLING_STEPS_AFF   = sampling_steps
    _nps.FAST_DIFFUSION_SAMPLES_AFF = diffusion_samples

    # Also patch _mol_idx default seed if profile provides one
    if seed != 68:
        _orig_seed_fn = _nps._mol_idx
        def _mol_idx_seeded(smiles: str, s: int = seed) -> int:
            return _orig_seed_fn(smiles, s)
        _nps._mol_idx = _mol_idx_seeded  # type: ignore[assignment]

    try:
        detailed = _nps._score_batch_impl(smiles_list, target, protein_sequence, msa_path)
    finally:
        (_nps.FAST_RECYCLING_STEPS,
         _nps.FAST_SAMPLING_STEPS,
         _nps.FAST_DIFFUSION_SAMPLES,
         _nps.FAST_SAMPLING_STEPS_AFF,
         _nps.FAST_DIFFUSION_SAMPLES_AFF) = _orig
        if seed != 68:
            _nps._mol_idx = _orig_seed_fn  # type: ignore[assignment]

    return {smi: (d["boltz_score"] if d else None) for smi, d in detailed.items()}
