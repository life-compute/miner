"""
coordinator/reference_scoring/runner.py — Boltz runner for the miner.

Implements the BoltzRunner protocol defined in coordinator_client/rescore.py so
that coordinator_mode protein scoring uses the validator's exact code path:

  rescore.protein_yaml  → YAML dict
  rescore.boltz_flags_for → validated argv tokens
  rescore.seed_for       → integer seed
  boltz.main.predict()   → run Boltz 2.2.1 (function call, not CLI)
  read affinity_*/confidence_* JSON → metrics dict
  rescore.protein_dg(affinity_pred_value) → kcal/mol

This runner runs in the nova_subnet venv (NOVA_VENV in loop.py) because
predict() requires boltz + torch; it is invoked as a subprocess from loop.py
via _score_protein_ref(), which replaces the old _BOLTZ_HELPER path.

The BoltzRunner protocol (rescore.py):

    class BoltzRunner(Protocol):
        def run(self, *, yaml_inputs: dict, flags: list[str],
                seed: int, timeout_s: float) -> dict[str, Any]: ...

This module also provides the top-level helper run_protein_reference() which
loop.py calls from its subprocess helper script.  The helper script prints one
JSON line to stdout:

    {"raw": <float>, "score": <float>, "smiles": <str>}

On per-molecule failure (bad SMILES, conformer) it exits with code 2 and
writes the pattern to stderr so loop.py can classify it as PerMoleculeError.
On systemic failure it exits with code 1.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── The reference package lives alongside this file ──────────────────────────
# Import via the package path (coordinator.reference_scoring.*) so that the
# relative imports inside rescore.py ("from . import boltzspec, hashpin") resolve
# correctly.  The subprocess helper adds _MINER_DIR to sys.path, which makes
# "coordinator" importable as a package, so the package-qualified form works.
from coordinator.reference_scoring.rescore import (  # noqa: E402
    protein_yaml,
    boltz_flags_for,
    seed_for,
    protein_dg,
    RescoreFailed,
)
from coordinator.reference_scoring.hashpin import sequence_from_msa  # noqa: E402
from coordinator.reference_scoring.contract import ScorerProfile     # noqa: E402


# ── BoltzRunner implementation ────────────────────────────────────────────────

class MinervBoltzRunner:
    """BoltzRunner that calls boltz.main.predict() directly (function API).

    The validator's BoltzDaemonRunner uses predict.main([...], standalone_mode=False)
    because its boltz install (on the 4060 rig) has boltz as a click CLI entry point.
    The miner's nova_subnet venv exposes predict as a plain Python function
    (boltz.main.predict), so we call it directly.  The input/output path logic is
    otherwise identical to BoltzDaemonRunner.run().
    """

    def run(self, *, yaml_inputs: dict[str, Any], flags: list[str],
            seed: int, timeout_s: float) -> dict[str, Any]:
        import yaml as _yaml
        from boltz.main import predict as boltz_predict

        with tempfile.TemporaryDirectory(prefix="life-miner-ref-") as root:
            in_dir = Path(root) / "inputs"
            out_dir = Path(root) / "outputs"
            in_dir.mkdir()
            out_dir.mkdir()
            os.chmod(root, stat.S_IRWXU)  # 700: owner-only

            (in_dir / "input.yaml").write_text(
                _yaml.safe_dump(yaml_inputs, sort_keys=False,
                                default_flow_style=False))

            boltz_start = time.time()

            # Parse the flag list into kwargs for the predict() function.
            # validate_tokens() has already checked spelling and form; we just
            # need to map argv tokens to Python kwargs.
            kwargs = _flags_to_kwargs(flags)
            kwargs["seed"] = seed

            boltz_predict(str(in_dir), str(out_dir), **kwargs)

            pred_dir = out_dir / "boltz_results_inputs" / "predictions" / "input"
            metrics: dict[str, Any] = {}
            for fp in (pred_dir.iterdir() if pred_dir.exists() else []):
                if not fp.name.startswith(("affinity", "confidence")):
                    continue
                if fp.stat().st_mtime <= boltz_start:
                    log.warning("SECURITY WARNING  %s predates run start — ignoring",
                                fp.name)
                    continue
                try:
                    metrics.update(json.loads(fp.read_text()))
                except Exception as exc:
                    log.warning("  could not parse %s: %s", fp.name, exc)
            return metrics


# ── argv → kwargs mapper ──────────────────────────────────────────────────────

# boltzspec.OPTION_SPEC keys that are SWITCH (is_flag=True in click).
# When a SWITCH is present it maps to True; absent → default (False).
_SWITCHES = frozenset({
    "write_full_pae", "write_full_pde", "override", "use_msa_server",
    "use_potentials", "affinity_mw_correction", "subsample_msa",
    "no_kernels", "write_embeddings",
})

# Rename: CLI name → Python function param name.
_CLI_TO_KWARG = {
    "preprocessing-threads": "preprocessing_threads",
}


def _flags_to_kwargs(flags: list[str]) -> dict[str, Any]:
    """Turn the validated argv token list into predict() kwargs.

    The tokens have already been validated by boltzspec.validate_tokens(), so
    all names are known, forms are correct, and there are no duplicates.
    """
    kwargs: dict[str, Any] = {}
    toks = [str(f) for f in flags]
    i = 0
    while i < len(toks):
        tok = toks[i]
        assert tok.startswith("--"), f"unexpected non-flag token: {tok!r}"
        name, eq, inline = tok[2:].partition("=")
        py_name = _CLI_TO_KWARG.get(name, name)
        if name in _SWITCHES:
            kwargs[py_name] = True
            i += 1
        elif eq:
            kwargs[py_name] = _coerce(py_name, inline)
            i += 1
        else:
            val = toks[i + 1]
            kwargs[py_name] = _coerce(py_name, val)
            i += 2
    return kwargs


def _coerce(name: str, val: str) -> Any:
    """Cast a string argv value to the expected Python type."""
    _INT_PARAMS = frozenset({
        "devices", "recycling_steps", "sampling_steps", "diffusion_samples",
        "sampling_steps_affinity", "diffusion_samples_affinity",
        "max_parallel_samples", "num_workers", "num_subsampled_msa",
        "max_msa_seqs", "seed",
    })
    _FLOAT_PARAMS = frozenset({"step_scale"})
    if name in _INT_PARAMS:
        return int(val)
    if name in _FLOAT_PARAMS:
        return float(val)
    return val


# ── Top-level helper called from the subprocess script ───────────────────────

def run_protein_reference(
    smiles: str,
    msa_path: str,
    scorer_profile_dict: dict[str, Any],
    default_seed: int = 68,
    num_subsampled_msa: int = 1024,
    timeout_s: float = 1200.0,
) -> dict[str, Any]:
    """Run the reference protein scorer and return the raw metrics + score.

    Returns a dict with keys:
        raw       float — affinity_pred_value from Boltz
        score     float — protein_dg(raw) in kcal/mol
        smiles    str
    Raises RescoreFailed (systemic) or ValueError/RuntimeError (per-molecule).
    """
    profile = ScorerProfile.from_dict(scorer_profile_dict)
    flags = boltz_flags_for(profile, "protein", True, num_subsampled_msa)
    the_seed = seed_for(profile, default_seed)

    sequence = sequence_from_msa(msa_path)
    if not sequence:
        raise RescoreFailed("INTERNAL_ERROR",
                            f"could not read query sequence from {msa_path}")

    yaml_inputs = protein_yaml(sequence, smiles, msa_path)
    runner = MinervBoltzRunner()
    metrics = runner.run(yaml_inputs=yaml_inputs, flags=list(flags),
                         seed=the_seed, timeout_s=timeout_s)

    pred = metrics.get("affinity_pred_value")
    if pred is None:
        raise RescoreFailed("INTERNAL_ERROR",
                            f"no affinity_pred_value in Boltz output "
                            f"(keys={sorted(metrics)})")

    score = protein_dg(pred)
    return {"raw": float(pred), "score": score, "smiles": smiles}
