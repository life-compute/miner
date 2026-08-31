"""
life_mrna_boltz.py — Boltz2 GPU inference for mRNA-target small-molecule docking.

Builds a 2-chain Boltz2 input YAML and parses the resulting confidence / affinity
output.  Designed as a dedicated scoring path for the 30 mRNA silencing targets
(IDs 2000-2029), separate from the protein-docking path in nova_pulse_scorer.py.

Chain layout
────────────
  Chain A (rna)    : Target mRNA region (rna_sequence from targets.json).
                     This is the actual RNA stem-loop / IRES the drug must bind.
  Chain B (ligand) : Candidate small molecule (SMILES).

Score source
────────────
  Boltz2 accepts ``properties.affinity.binder = "B"`` for ligand chains even
  when the receptor is RNA.  When Boltz2 produces an affinity JSON for the run
  we use:
      boltz_score = (affinity_probability_binary - affinity_pred_value)
                    / heavy_atom_count          # validator formula

  When the affinity JSON is absent (Boltz2 declined affinity prediction for this
  RNA receptor), we fall back to ipTM from the confidence JSON, same as the
  CRISPR path:
      boltz_score = iptm
      affinity_kcal = -6.0 - 3.0 × iptm   (range -6 to -9 kcal/mol-like)

  The caller (run_boltz2_mrna_scoring) always returns a ``boltz_score`` field
  compatible with ``_boltz_score_to_affinity()`` in miner_daemon.py, or None on
  total failure.

Threshold calibration
─────────────────────
  Do NOT hardcode a hit threshold here.  The main loop uses:
      eff_thresh = ref_scores[tid] + 0.5   (if a ref compound was screened)
              or   target["target_score_threshold"]   (default -7.0)
  Calibrate from observed ipTM distributions after the first ~50 mRNA rounds.
  Expected range: iptm 0.2-0.5 for weak binders, 0.6+ for promising hits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


# ── YAML builder ───────────────────────────────────────────────────────────────

def build_mrna_boltz_input_yaml(rna_sequence: str, smiles: str) -> str:
    """
    Return a Boltz2 input YAML string for an mRNA-target small-molecule complex.

    Parameters
    ----------
    rna_sequence : str
        RNA sequence of the target region (from targets.json ``rna_sequence``
        field).  Must be in RNA alphabet (A/C/G/U).  T is silently converted
        to U for Boltz2 compatibility.
    smiles : str
        Candidate small molecule in SMILES notation.

    Returns
    -------
    str — YAML content ready to write to a .yaml input file for Boltz2.

    Chain A (rna)    : target mRNA region
    Chain B (ligand) : candidate molecule
    properties.affinity.binder = "B" requests affinity prediction for the ligand.
    Falls back to ipTM from confidence JSON when affinity prediction is skipped.
    """
    # Normalise to RNA alphabet — Boltz2 requires U not T for RNA chains
    rna_norm = rna_sequence.strip().upper().replace("T", "U")
    if not rna_norm:
        raise ValueError("rna_sequence is empty")
    if not all(c in "ACGU" for c in rna_norm):
        bad = sorted({c for c in rna_norm if c not in "ACGU"})
        raise ValueError(f"rna_sequence contains non-RNA characters: {bad}")

    # Build YAML manually — no PyYAML dependency at module level
    yaml_lines = [
        "version: 1",
        "sequences:",
        "  - rna:",
        "      id: A",
        f'      sequence: "{rna_norm}"',
        "  - ligand:",
        "      id: B",
        f'      smiles: "{smiles}"',
        "properties:",
        "  - affinity:",
        "      binder: B",
    ]
    return "\n".join(yaml_lines) + "\n"


# ── Output parser ──────────────────────────────────────────────────────────────

def parse_mrna_boltz_affinity(
    predictions_dir: Path,
    mol_id: int,
    target_stem: str,
) -> Optional[dict]:
    """
    Read Boltz2 output for an mRNA-target complex and return scoring metrics.

    Tries affinity JSON first (``affinity_{mol_id}_{target_stem}.json``).
    Falls back to ipTM from ``confidence_{mol_id}_{target_stem}_model_0.json``
    when the affinity step was skipped, identical to the CRISPR fallback.

    Output directory layout (Boltz2 writes to):
        <out_dir>/boltz_results_inputs/predictions/{mol_id}_{target_stem}/

    Returns dict with:
        boltz_score       float   primary signal (affinity combo score or iptm)
        affinity_kcal     float   -6.0 - 3.0 × iptm  (when falling back to iptm)
                                  or None              (when affinity score used)
        iptm              float | None
        score_source      str     "affinity" | "iptm_fallback"
        model             str     "boltz2-gpu-mrna"

    Returns None if neither output file exists.
    """
    pred_subdir = predictions_dir / f"{mol_id}_{target_stem}"
    if not pred_subdir.exists():
        return None

    # ── Try affinity JSON first ──────────────────────────────────────────────
    affinity_file: Optional[Path] = None
    confidence_file: Optional[Path] = None
    for fname in os.listdir(str(pred_subdir)):
        if fname.startswith("affinity_") and fname.endswith(".json"):
            affinity_file = pred_subdir / fname
        if fname.startswith("confidence_") and fname.endswith(".json"):
            confidence_file = pred_subdir / fname

    if affinity_file is not None and affinity_file.exists():
        try:
            with open(affinity_file) as fh:
                aff_data = json.load(fh)
            # Also pull iptm from confidence file if available
            iptm = None
            if confidence_file and confidence_file.exists():
                with open(confidence_file) as fh2:
                    iptm = json.load(fh2).get("iptm")

            v0 = aff_data.get("affinity_probability_binary")
            v1 = aff_data.get("affinity_pred_value")
            if v0 is not None and v1 is not None:
                # Defer heavy_atom_count normalisation to caller — return raw
                # components so caller can compute (v0 - v1) / hac if needed.
                # For now return the unnormalised difference as boltz_score
                # (consistent with how nova_pulse_scorer returns it when hac=1).
                boltz_score = float(v0) - float(v1)
                return {
                    "boltz_score":   boltz_score,
                    "affinity_kcal": None,           # affinity path; no iptm conversion
                    "iptm":          float(iptm) if iptm is not None else None,
                    "score_source":  "affinity",
                    "model":         "boltz2-gpu-mrna",
                }
        except Exception:
            pass  # fall through to iptm fallback

    # ── Fallback: ipTM from confidence JSON (same as CRISPR) ────────────────
    if confidence_file is None or not confidence_file.exists():
        return None

    try:
        with open(confidence_file) as fh:
            conf_data = json.load(fh)
    except Exception:
        return None

    iptm = conf_data.get("iptm")
    if iptm is None:
        return None

    affinity_kcal = round(-6.0 - 3.0 * float(iptm), 4)

    return {
        "boltz_score":   float(iptm),      # field compat with JSONL schema
        "affinity_kcal": affinity_kcal,
        "iptm":          float(iptm),
        "score_source":  "iptm_fallback",
        "model":         "boltz2-gpu-mrna",
    }


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_rna   = "AUGCCCAGCGAGGAGAGCUUCACCCCCACCGCCCAGCUCCCGCAACAGCGGCGGCAGCAGCC"
    test_smiles = "O=C(/C=C/c1ccc(Cl)cc1)Nc1ccc(NC(=S)Nc2ccc(Cl)cc2)cc1"
    print(f"RNA  : {test_rna}")
    print(f"SMILES: {test_smiles[:60]}…")
    yaml_str = build_mrna_boltz_input_yaml(test_rna, test_smiles)
    print("\n--- Generated YAML ---")
    print(yaml_str)
    print("--- end YAML ---")
    print(f"Chain A (rna) length: {len(test_rna)} nt")
