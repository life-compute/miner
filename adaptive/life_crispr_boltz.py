"""
life_crispr_boltz.py — Boltz2 GPU inference for CRISPR gRNA–Cas9–DNA ternary complexes.

Builds a 3-chain Boltz2 input YAML and parses the resulting confidence output.
Designed to replace the analytical scorer in life_crispr.py with real GPU inference.

Chain layout
────────────
  Chain A (protein) : SpCas9 REC1 domain — first 200 aa of UniProt P0DOT7.
                      Truncated domain used for speed; no MSA (synthetic construct).
  Chain B (rna)     : Full sgRNA = gRNA 20-mer (RNA alphabet) + 76-nt scaffold.
  Chain C (dna)     : Target protospacer = revcomp(gRNA 20-mer) + NGG PAM (23 nt, DNA).

Score source: confidence JSON (structure-only prediction)
──────────────────────────────────────────────────────────
  Boltz2 affinity prediction is only supported for small-molecule ligand chains.
  RNA/DNA chains are scored via structure confidence instead.

  Primary metric: ipTM (interface predicted TM-score, 0–1), from
      confidence_{mol_id}_{target}_model_0.json

  Conversion to kcal/mol-like affinity:
      affinity_kcal = −6.0 − 3.0 × iptm
      iptm=0.0 → −6.0 kcal/mol (weak / no complex)
      iptm=1.0 → −9.0 kcal/mol (tight complex, near-perfect predicted structure)

  Secondary: confidence_score (overall complex quality, 0–1), ptm.

Fallback chain
──────────────
  1. iptm from confidence JSON → affinity_kcal
  2. None — caller falls back to analytical score from life_crispr.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# ── SpCas9 REC1 domain — first 200 aa of UniProt P0DOT7 ───────────────────────
# Source: UniProt P0DOT7 (Streptococcus pyogenes Cas9 / SpCas9), canonical sequence.
# The REC1 domain (residues 56–713) begins around position 56; using residues 1–200
# (N-terminal + partial REC1) for speed.  No MSA — this is a synthetic/truncated domain.
SPCAS9_REC1_200AA = (
    "MDKKYSIGLDIGTNSVGWAVITDEYKVPSKKFKVLGNTDRHSIKKNLIGALLFDSG"
    "ETAEATRLKRTARRRYTRRKNRICYLQEIFSNEMAKVDDSFFHRLEESFLVEEDKKH"
    "ERHPIFGNIVDEVAYHEKYPTIYHLRKKLVDSTDKADLRLIYLALAHMIKFRGHFLI"
    "EGDLNPDNSDVDKLFIQLVQTYNQLFEENP"
)
assert len(SPCAS9_REC1_200AA) == 200, (
    f"SpCas9 fragment must be exactly 200 aa, got {len(SPCAS9_REC1_200AA)}"
)

# ── sgRNA scaffold (76 nt, RNA alphabet — T replaced with U) ───────────────────
# Canonical SpCas9 sgRNA scaffold from Jinek et al. 2012 / Cong et al. 2013.
# Appended to the 20-mer spacer (also in RNA alphabet) to form the full sgRNA.
SGRNA_SCAFFOLD_RNA = (
    "GUUUUAGAGCUAGAAAUAGCAAGUUAAAAUAAGGCUAGUCCGUUAUCAACUUGAAAA"
    "AGUGGCACCGAGUCGGUGC"
)
assert len(SGRNA_SCAFFOLD_RNA) == 76, (
    f"sgRNA scaffold must be 76 nt, got {len(SGRNA_SCAFFOLD_RNA)}"
)

# Full sgRNA nucleotide count (20-mer + 76-nt scaffold) used as denominator in score
SGRNA_NT_COUNT = 96  # 20 + 76

# ── Nucleotide complement map ──────────────────────────────────────────────────
_COMP_DNA = str.maketrans("ACGTacgt", "TGCAtgca")
_DNA_TO_RNA = str.maketrans("Tt", "Uu")


def _revcomp_dna(seq: str) -> str:
    return seq.upper().translate(_COMP_DNA)[::-1]


def _dna_to_rna(seq: str) -> str:
    """Convert a DNA 20-mer to RNA alphabet (T → U, uppercase)."""
    return seq.upper().translate(_DNA_TO_RNA)


# ── YAML builder ───────────────────────────────────────────────────────────────

def build_crispr_boltz_input_yaml(grna_20mer: str) -> str:
    """
    Return a Boltz2 input YAML string for a SpCas9–sgRNA–DNA ternary complex.

    Parameters
    ----------
    grna_20mer : str
        20-nucleotide gRNA spacer sequence (DNA or RNA alphabet; T/U both accepted).

    Returns
    -------
    str — YAML content ready to write to a .yaml input file for Boltz2.

    Chain A (protein) : SpCas9 REC1 200 aa (P0DOT7[1:200])
    Chain B (rna)     : 20-mer spacer (RNA) + 76-nt sgRNA scaffold
    Chain C (dna)     : revcomp(20-mer) + "NGG" PAM (23 nt, DNA)

    properties.affinity.binder = "B" measures gRNA engagement with the Cas9:DNA complex.
    """
    grna_upper = grna_20mer.upper().replace("U", "T")  # normalise to DNA alpha first
    if len(grna_upper) != 20 or not all(c in "ACGT" for c in grna_upper):
        raise ValueError(f"gRNA must be a 20-mer [ACGT]; got {grna_20mer!r}")

    # Chain B — full sgRNA in RNA alphabet
    spacer_rna = _dna_to_rna(grna_upper)           # 20 nt spacer in RNA alphabet
    sgrna_full = spacer_rna + SGRNA_SCAFFOLD_RNA   # 96 nt total

    # Chain C — protospacer on the non-template strand (revcomp of gRNA) + NGG PAM
    protospacer_dna = _revcomp_dna(grna_upper) + "NGG"  # 23 nt

    # Build YAML manually to avoid PyYAML import at module level
    yaml_lines = [
        "version: 1",
        "sequences:",
        "  - protein:",
        "      id: A",
        f"      sequence: \"{SPCAS9_REC1_200AA}\"",
        "      msa: empty",
        "  - rna:",
        "      id: B",
        f"      sequence: \"{sgrna_full}\"",
        "  - dna:",
        "      id: C",
        f"      sequence: \"{protospacer_dna}\"",
    ]
    return "\n".join(yaml_lines) + "\n"


# ── Output parser ──────────────────────────────────────────────────────────────

def parse_crispr_boltz_affinity(
    predictions_dir: Path,
    mol_id: int,
    target_stem: str = "crispr",
) -> Optional[dict]:
    """
    Read Boltz2 structure-prediction output for a CRISPR complex and return
    confidence metrics as a binding quality proxy.

    Boltz2 affinity is only supported for small-molecule ligands; RNA/DNA chains
    use structure confidence (ipTM) as the binding signal instead.

    Output file: confidence_{mol_id}_{target_stem}_model_0.json
    (written by structure-only predict() — no `properties: affinity` in YAML)

    Returns dict with:
        iptm                float      (interface TM-score, 0–1; primary signal)
        ptm                 float      (predicted TM-score, 0–1)
        confidence_score    float      (overall complex quality, 0–1)
        affinity_kcal       float      −6.0 − 3.0 × iptm  (−6…−9 kcal/mol-like)
        boltz_score         float      same as iptm (for JSONL field compatibility)
        source              str        "boltz2-gpu-crispr"

    Returns None if the prediction directory or confidence file does not exist.
    """
    pred_subdir = predictions_dir / f"{mol_id}_{target_stem}"
    if not pred_subdir.exists():
        return None

    # Confidence file: confidence_{mol_id}_{target_stem}_model_0.json
    conf_file: Optional[Path] = None
    for fname in os.listdir(str(pred_subdir)):
        if fname.startswith("confidence_") and fname.endswith(".json"):
            conf_file = pred_subdir / fname
            break

    if conf_file is None:
        return None

    try:
        with open(conf_file) as fh:
            data = json.load(fh)
    except Exception:
        return None

    iptm             = data.get("iptm")
    ptm              = data.get("ptm")
    confidence_score = data.get("confidence_score")

    if iptm is None:
        return None

    affinity_kcal = round(-6.0 - 3.0 * float(iptm), 4)

    return {
        "iptm":             float(iptm),
        "ptm":              float(ptm)              if ptm              is not None else None,
        "confidence_score": float(confidence_score) if confidence_score is not None else None,
        "affinity_kcal":    affinity_kcal,
        "boltz_score":      float(iptm),   # field compat with JSONL schema
        "source":           "boltz2-gpu-crispr",
    }


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_grna = "CGTGAGCGCTTCGAGATGTT"   # TP53 codon-175 window
    print(f"gRNA: {test_grna}")
    yaml_str = build_crispr_boltz_input_yaml(test_grna)
    print("\n--- Generated YAML ---")
    print(yaml_str)
    print("--- end YAML ---")
    print(f"\nChain B (full sgRNA): {len('CGUUGAGCGCUUCGAGAUGUUGUUUUAGAGCUAGAAAUAGCAAGUUAAAAUAAGGCUAGUCCGUUAUCAACUUGAAAAAGUGCACCGAGUCGGUGC')} nt")
    print(f"Chain C (protospacer + PAM): revcomp({test_grna}) + NGG")
