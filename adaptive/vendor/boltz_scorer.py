"""
adaptive/vendor/boltz_scorer.py — lease-profile-aware Boltz2 scorer (C1g).

Wraps the pinned nova_pulse_scorer with the LIFE Compute coordinator's
scorer_profile fields as of API contract C1g.

C1g scorer_profile shape (from GET /work):
  {
    "boltz_version": "2.2.1",
    "flags": {                          ← dict of param → value (C1g; was list[str])
      "recycling_steps": 1,
      "sampling_steps": 25,
      "diffusion_samples": 1,
      "sampling_steps_affinity": 25,
      "diffusion_samples_affinity": 1,
      "affinity_mw_correction": true,
      "no_kernels": true,
      "output_format": "mmcif",
      "override": true,
      "num_workers": 0,
      "accelerator": "gpu",
      "devices": 1,
      "model": "boltz2",
      "use_potentials": false,
      "max_parallel_samples": 5,
      "msa_pairing_strategy": "greedy",
      "use_msa_server": false
    },
    "msa_sampling": {                   ← dict (C1g; was plain string)
      "subsample_msa": true,
      "num_subsampled_msa": 1024,
      "max_msa_seqs": 1024
    },
    "seed": 68                          ← int (null → use default 68)
  }

CLI equivalent (protein):
  boltz predict \\
    --recycling-steps 1 \\
    --sampling-steps 25 \\
    --diffusion-samples 1 \\
    --sampling-steps-affinity 25 \\
    --diffusion-samples-affinity 1 \\
    --affinity-mw-correction \\
    --no-kernels \\
    --output-format mmcif \\
    --override \\
    --num-workers 0 \\
    --accelerator gpu \\
    --devices 1 \\
    --model boltz2 \\
    --use-potentials false \\
    --max-parallel-samples 5 \\
    --msa-pairing-strategy greedy \\
    --subsample-msa \\
    --num-subsampled-msa 1024 \\
    --max-msa-seqs 1024 \\
    --seed 68 \\
    <input_dir>

mRNA profile: same flags but msa_sampling.subsample_msa=False, max_msa_seqs=8192.
CRISPR profile: no affinity keys; same msa_sampling as mRNA.

MSA sampling change (C1g vs prior):
  The C1g msa_sampling dict replaces the old plain string
  (e.g. "uniform").  The Boltz Python predict() API exposes the
  corresponding kwargs as subsample_msa (bool), num_subsampled_msa (int),
  max_msa_seqs (int).  These are patched into nova_pulse_scorer's
  module-level constants when they differ from the scorer's defaults.

SAFETY: protein_sequence MUST come from the alignment file
(_sequence_from_msa), not from targets.json.  Callers that cannot obtain
the sequence from the MSA must skip the target; this function refuses an
empty sequence as a second line of defense.

This module exposes score_batch_with_profile() which applies the lease profile.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Union

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


def _flags_get(flags: Union[dict, list], key: str, default=None):
    """
    Unified accessor for the 'flags' field, which is:
      - C1g: a dict  {param_name: value, ...}
      - C1f and earlier: a list[str] of CLI args like "--recycling-steps=3"

    For dict flags, returns flags[key] or default.
    For list flags, parses '--<key>=N' style entries.
    """
    if isinstance(flags, dict):
        return flags.get(key, default)
    # Legacy list[str] fallback
    cli_key = "--" + key.replace("_", "-")
    for flag in flags:
        if isinstance(flag, str) and flag.startswith(cli_key + "="):
            try:
                return type(default)(flag.split("=", 1)[1])
            except (ValueError, TypeError):
                pass
        if flag == cli_key and default is True:
            return True  # boolean flag with no value
    return default


def _msa_sampling_kwarg_supported() -> bool:
    """Check whether the installed boltz predict() accepts subsample_msa kwarg (C1g)."""
    try:
        from boltz.main import predict  # type: ignore[import]
        sig = inspect.signature(predict)
        return "subsample_msa" in sig.parameters
    except Exception:
        return False


def profile_to_cli_args(
    scorer_profile: dict,
    modality: str = "protein",
) -> list[str]:
    """
    Convert a C1g scorer_profile to the equivalent boltz CLI argument list.

    This is the canonical source for the CLI args shown in the report (task §4).
    The validator uses this same mapping when re-scoring.

    Returns a list of strings like:
      ["--recycling-steps", "1", "--sampling-steps", "25", "--seed", "68", ...]
    """
    flags = scorer_profile.get("flags", {})
    msa   = scorer_profile.get("msa_sampling", {})
    seed  = scorer_profile.get("seed") or _DEFAULT_SEED

    args: list[str] = []

    # ── Integer / float flags ──────────────────────────────────────────────────
    for param, cli in [
        ("recycling_steps",            "--recycling-steps"),
        ("sampling_steps",             "--sampling-steps"),
        ("diffusion_samples",          "--diffusion-samples"),
        ("sampling_steps_affinity",    "--sampling-steps-affinity"),
        ("diffusion_samples_affinity", "--diffusion-samples-affinity"),
        ("num_workers",                "--num-workers"),
        ("devices",                    "--devices"),
        ("max_parallel_samples",       "--max-parallel-samples"),
    ]:
        val = _flags_get(flags, param)
        if val is not None:
            args += [cli, str(val)]

    # ── String flags ───────────────────────────────────────────────────────────
    for param, cli in [
        ("output_format",         "--output-format"),
        ("accelerator",           "--accelerator"),
        ("model",                 "--model"),
        ("msa_pairing_strategy",  "--msa-pairing-strategy"),
    ]:
        val = _flags_get(flags, param)
        if val is not None:
            args += [cli, str(val)]

    # ── Boolean flags (present → enabled) ─────────────────────────────────────
    for param, cli in [
        ("affinity_mw_correction", "--affinity-mw-correction"),
        ("no_kernels",             "--no-kernels"),
        ("override",               "--override"),
        ("use_msa_server",         "--use-msa-server"),
        ("use_potentials",         "--use-potentials"),
    ]:
        val = _flags_get(flags, param)
        if val:
            args.append(cli)

    # ── msa_sampling dict ──────────────────────────────────────────────────────
    if isinstance(msa, dict):
        if msa.get("subsample_msa"):
            args.append("--subsample-msa")
        n_sub = msa.get("num_subsampled_msa")
        if n_sub is not None:
            args += ["--num-subsampled-msa", str(n_sub)]
        max_msa = msa.get("max_msa_seqs")
        if max_msa is not None:
            args += ["--max-msa-seqs", str(max_msa)]
    elif isinstance(msa, str):
        # Legacy: plain string like "uniform"
        args += ["--msa-sampling-method", msa]

    # ── Seed ──────────────────────────────────────────────────────────────────
    args += ["--seed", str(seed)]

    return args


def score_batch_with_profile(
    smiles_list: list[str],
    target: str,
    protein_sequence: str,
    msa_path: str,
    scorer_profile: Optional[dict] = None,
) -> dict[str, Optional[float]]:
    """
    Score a batch of SMILES against a protein target using the lease scorer_profile.

    C1g profile shape consumed here:
      - flags: dict {param_name: value} (C1g)  or legacy list[str] (C1f)
      - msa_sampling: dict {subsample_msa, num_subsampled_msa, max_msa_seqs} (C1g)
                      or plain string like "uniform" (C1f legacy)
      - seed: int or null (null → 68)

    Parameters applied to nova_pulse_scorer:
      1. recycling_steps    — from flags["recycling_steps"]
      2. sampling_steps     — from flags["sampling_steps"]
      3. diffusion_samples  — from flags["diffusion_samples"]
      4. subsample_msa      — from msa_sampling["subsample_msa"]
      5. num_subsampled_msa — from msa_sampling["num_subsampled_msa"]
      6. max_msa_seqs       — from msa_sampling["max_msa_seqs"]
      7. seed               — from scorer_profile["seed"]

    All of these must come exclusively from the lease profile; the miner may not
    override them (API contract §2.3: 422 profile_override_refused).

    SAFETY: protein_sequence MUST come from the alignment file (_sequence_from_msa),
    not from targets.json.  If the caller could not obtain a sequence from the MSA,
    it must skip the target and not call this function.  Passing an empty or None
    sequence is refused here as a second line of defense.

    Returns {smiles: boltz_score_or_None}.
    """
    if not protein_sequence:
        log.error(
            "[BOLTZ-PROFILE] score_batch_with_profile called with empty protein_sequence "
            "(targets.json fallback was refused upstream); skipping batch for target=%s",
            target,
        )
        return {smi: None for smi in smiles_list}

    profile  = scorer_profile or {}
    flags    = profile.get("flags", {})
    msa_dict = profile.get("msa_sampling", {})
    seed_val = profile.get("seed")
    seed: int = seed_val if isinstance(seed_val, int) else _DEFAULT_SEED

    # ── Extract scoring parameters from profile ────────────────────────────────
    recycling_steps   = _flags_get(flags, "recycling_steps",   _DEFAULT_RECYCLING_STEPS)
    sampling_steps    = _flags_get(flags, "sampling_steps",    _DEFAULT_SAMPLING_STEPS)
    diffusion_samples = _flags_get(flags, "diffusion_samples", _DEFAULT_DIFFUSION_SAMPLES)

    # msa_sampling: C1g is a dict; C1f is a string (legacy)
    if isinstance(msa_dict, dict):
        subsample_msa     = msa_dict.get("subsample_msa", False)
        num_subsampled_msa = msa_dict.get("num_subsampled_msa")
        max_msa_seqs      = msa_dict.get("max_msa_seqs")
    else:
        # Legacy string (C1f) — map to Boltz predict() kwargs if possible
        subsample_msa     = (msa_dict == "uniform")
        num_subsampled_msa = None
        max_msa_seqs      = None

    # ── Report parameter differences vs nova_pulse_scorer defaults ─────────────
    if recycling_steps != _DEFAULT_RECYCLING_STEPS:
        log.info(
            "[BOLTZ-PROFILE] recycling_steps: lease=%d fast=%d (using lease value)",
            recycling_steps, _DEFAULT_RECYCLING_STEPS,
        )
    if subsample_msa or num_subsampled_msa or max_msa_seqs:
        log.info(
            "[BOLTZ-PROFILE] msa_sampling: subsample=%s num_sub=%s max=%s "
            "(nova_scorer default: no subsampling)",
            subsample_msa, num_subsampled_msa, max_msa_seqs,
        )

    # ── Patch nova_pulse_scorer constants temporarily ──────────────────────────
    # nova_pulse_scorer uses module-level FAST_* constants inside predict().
    import nova_pulse_scorer as _nps

    _orig_fast = (
        _nps.FAST_RECYCLING_STEPS,
        _nps.FAST_SAMPLING_STEPS,
        _nps.FAST_DIFFUSION_SAMPLES,
        _nps.FAST_SAMPLING_STEPS_AFF,
        _nps.FAST_DIFFUSION_SAMPLES_AFF,
    )
    _nps.FAST_RECYCLING_STEPS       = recycling_steps
    _nps.FAST_SAMPLING_STEPS        = sampling_steps
    _nps.FAST_DIFFUSION_SAMPLES     = diffusion_samples
    _nps.FAST_SAMPLING_STEPS_AFF    = sampling_steps
    _nps.FAST_DIFFUSION_SAMPLES_AFF = diffusion_samples

    # Patch seed if not the default
    _orig_seed_fn = None
    if seed != _DEFAULT_SEED:
        _orig_seed_fn = _nps._mol_idx

        def _mol_idx_seeded(smiles: str, s: int = seed) -> int:
            return _orig_seed_fn(smiles, s)   # type: ignore[misc]

        _nps._mol_idx = _mol_idx_seeded  # type: ignore[assignment]

    # Patch msa_sampling kwargs if supported by installed boltz
    _msa_patched = False
    _orig_msa_attrs: dict = {}
    _msa_ok = _msa_sampling_kwarg_supported()
    if (subsample_msa or num_subsampled_msa or max_msa_seqs) and _msa_ok:
        for attr, val in [
            ("FAST_SUBSAMPLE_MSA", subsample_msa),
            ("FAST_NUM_SUBSAMPLED_MSA", num_subsampled_msa),
            ("FAST_MAX_MSA_SEQS", max_msa_seqs),
        ]:
            if val is not None and hasattr(_nps, attr):
                _orig_msa_attrs[attr] = getattr(_nps, attr)
                setattr(_nps, attr, val)
                _msa_patched = True
    elif (subsample_msa or num_subsampled_msa or max_msa_seqs) and not _msa_ok:
        log.warning(
            "[BOLTZ-PROFILE] msa_sampling requested but installed boltz/nova_pulse_scorer "
            "does not expose FAST_SUBSAMPLE_MSA constants; proceeding without msa subsampling"
        )

    try:
        detailed = _nps._score_batch_impl(smiles_list, target, protein_sequence, msa_path)
    finally:
        # Restore FAST_* constants
        (
            _nps.FAST_RECYCLING_STEPS,
            _nps.FAST_SAMPLING_STEPS,
            _nps.FAST_DIFFUSION_SAMPLES,
            _nps.FAST_SAMPLING_STEPS_AFF,
            _nps.FAST_DIFFUSION_SAMPLES_AFF,
        ) = _orig_fast
        if _orig_seed_fn is not None:
            _nps._mol_idx = _orig_seed_fn  # type: ignore[assignment]
        if _msa_patched:
            for attr, orig_val in _orig_msa_attrs.items():
                setattr(_nps, attr, orig_val)

    return {smi: (d["boltz_score"] if d else None) for smi, d in detailed.items()}
