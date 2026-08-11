"""
life_scout.py — Protein-family-aware candidate routing for Life Compute.

Analogous to nova_scout.py (CAMK1 hard-coded filter) but generalized:
detects the protein family from the target's UniProt ID or sequence motif,
then routes to the focused sub-vocabulary in life_pulse.FAMILY_VOCAB.

Protein family detection
------------------------
Priority:
  1. UNIPROT_FAMILY_MAP — curated {uniprot_id: family} for known targets.
  2. Sequence motif scan — DFG-loop / cytokine / etc. keywords in sequence.
  3. "general" fallback.

Focused filter per family
-------------------------
  kinase:         MW 280–500, logP 2.5–7, HBD ≤ 4, HBA ≤ 8, ≥2 arom rings, ≥1 N-het
  cytokine:       MW 250–600, logP 1.5–6, flat/aromatic preference
  protease:       MW 200–600, no specific scaffold constraint beyond Lipinski
  nuclear_receptor: MW 250–550, logP 2–8
  general:        Lipinski Ro5 only

Output
------
Returns (smiles_list, diagnostics) — smiles_list is ART-ranked and
diversity-filtered, ready for submission.  Caller decides how many
to send based on epoch budget.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

# ── Curated family map ────────────────────────────────────────────────────────
# UniProt ID → family.  Add entries here when a new target is registered.
UNIPROT_FAMILY_MAP: dict[str, str] = {
    # Kinases
    "Q9UQM7": "kinase",      # CAMK1  (NOVA canonical target)
    "P00533": "kinase",      # EGFR
    "P15056": "kinase",      # BRAF
    "P06213": "kinase",      # Insulin receptor
    "P06239": "kinase",      # LCK
    "O14965": "kinase",      # Aurora A
    "Q13882": "kinase",      # PTK6
    "P04637": "nuclear_receptor",  # TP53 (transcription factor, treated as NR-like)
    # Cytokines / PPI
    "P01375": "cytokine",    # TNF-alpha
    "P05231": "cytokine",    # IL-6
    "P60568": "cytokine",    # IL-2
    "P01579": "cytokine",    # IFN-gamma
    # Proteases
    "P00760": "protease",    # Trypsin
    "P07339": "protease",    # Cathepsin D
    "P00742": "protease",    # Factor Xa
    "P56817": "protease",    # BACE1
    # Nuclear receptors
    "P04150": "nuclear_receptor",  # GR
    "P10275": "nuclear_receptor",  # AR
    "P03372": "nuclear_receptor",  # ER-alpha
}

# Sequence motif keywords for fallback detection
_KINASE_MOTIFS    = ["DFG", "GXGXXG", "HRD", "APE"]  # DFG-loop is canonical
_CYTOKINE_MOTIFS  = ["XXXXXXX"]  # pattern placeholder; rely on UniProt map primarily
_PROTEASE_MOTIFS  = ["CATALYTIC TRIAD", "HIS", "SER", "CYS"]

# Per-family hard filters (property ranges)
_FAMILY_FILTERS: dict[str, dict] = {
    "kinase": {
        "mw_min": 280.0, "mw_max": 500.0,
        "logp_min": 2.5, "logp_max": 7.0,
        "hbd_max": 4, "hba_max": 8,
        "min_arom_rings": 2,
        "require_n_het": True,
    },
    "cytokine": {
        "mw_min": 250.0, "mw_max": 600.0,
        "logp_min": 1.5, "logp_max": 6.5,
        "hbd_max": 6, "hba_max": 10,
        "min_arom_rings": 1,
        "require_n_het": False,
    },
    "protease": {
        "mw_min": 200.0, "mw_max": 600.0,
        "logp_min": 0.0, "logp_max": 6.0,
        "hbd_max": 8, "hba_max": 12,
        "min_arom_rings": 0,
        "require_n_het": False,
    },
    "nuclear_receptor": {
        "mw_min": 250.0, "mw_max": 550.0,
        "logp_min": 2.0, "logp_max": 8.0,
        "hbd_max": 5, "hba_max": 8,
        "min_arom_rings": 1,
        "require_n_het": False,
    },
    "general": {
        "mw_min": 150.0, "mw_max": 500.0,
        "logp_min": -1.0, "logp_max": 5.0,
        "hbd_max": 5, "hba_max": 10,
        "min_arom_rings": 0,
        "require_n_het": False,
    },
}

# N-heterocycle SMARTS for kinase hinge-binding check
_NHET_SMARTS: list[tuple[str, str]] = [
    ("pyridine",         "n1ccccc1"),
    ("pyrimidine",       "n1cnccc1"),
    ("pyrazole",         "c1cc[nH]n1"),
    ("indole",           "c1ccc2[nH]ccc2c1"),
    ("indazole",         "c1ccc2[nH]ncc2c1"),
    ("quinazoline",      "c1ccc2ncncc2c1"),
    ("benzimidazole",    "c1ccc2[nH]cnc2c1"),
    ("purine",           "c1ncnc2[nH]cnc12"),
    ("pyridazinone",     "O=c1ccc[nH]n1"),
    ("triazole",         "c1cn[nH]n1"),
    ("aromatic_N",       "[n]"),
]
_COMPILED_NHET: list = []  # lazy-init


LIFE_DIR    = Path(__file__).resolve().parents[1]
OUTPUT_DIR  = LIFE_DIR / "output"
SCOUT_LOG   = OUTPUT_DIR / "life_scout_log.jsonl"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Family detection ──────────────────────────────────────────────────────────

def detect_protein_family(
    uniprot_id: Optional[str] = None,
    sequence: Optional[str] = None,
) -> str:
    """
    Detect protein family from UniProt ID (priority) or sequence motif.
    Returns one of: kinase / cytokine / protease / nuclear_receptor / general.
    """
    if uniprot_id and uniprot_id.upper() in UNIPROT_FAMILY_MAP:
        return UNIPROT_FAMILY_MAP[uniprot_id.upper()]
    if sequence:
        seq_upper = sequence.upper()
        if "DFG" in seq_upper:
            return "kinase"
        if any(m in seq_upper for m in ("CYTOKINE", "INTERLEUKIN", "INTERFERON")):
            return "cytokine"
        if "CATALYTIC" in seq_upper or seq_upper.count("HIS") >= 2:
            return "protease"
    return "general"


# ── Focused hard filter ────────────────────────────────────────────────────────

def _get_nhet_patterns():
    global _COMPILED_NHET
    if not _COMPILED_NHET:
        from rdkit import Chem
        for name, smarts in _NHET_SMARTS:
            pat = Chem.MolFromSmarts(smarts)
            if pat is not None:
                _COMPILED_NHET.append((name, pat))
    return _COMPILED_NHET


def focused_family_filter(
    smiles: str,
    family: str,
) -> tuple[bool, str]:
    """
    Apply family-specific property filter to a SMILES string.

    Returns (True, scaffold_label) on pass, (False, reason) on fail.
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return False, "invalid_smiles"

        cfg = _FAMILY_FILTERS.get(family, _FAMILY_FILTERS["general"])

        mw = Descriptors.MolWt(mol)
        if not (cfg["mw_min"] <= mw <= cfg["mw_max"]):
            return False, f"mw={mw:.1f}_out_of_range"

        logp = Descriptors.MolLogP(mol)
        if not (cfg["logp_min"] <= logp <= cfg["logp_max"]):
            return False, f"logp={logp:.2f}_out_of_range"

        hbd = Descriptors.NumHDonors(mol)
        if hbd > cfg["hbd_max"]:
            return False, f"hbd={hbd}_exceeds_{cfg['hbd_max']}"

        hba = Descriptors.NumHAcceptors(mol)
        if hba > cfg["hba_max"]:
            return False, f"hba={hba}_exceeds_{cfg['hba_max']}"

        n_arom = rdMolDescriptors.CalcNumAromaticRings(mol)
        if n_arom < cfg["min_arom_rings"]:
            return False, f"arom_rings={n_arom}_below_{cfg['min_arom_rings']}"

        if cfg.get("require_n_het"):
            for name, pat in _get_nhet_patterns():
                if mol.HasSubstructMatch(pat):
                    return True, name
            return False, "no_N_heterocycle"

        return True, family
    except Exception as e:
        return False, f"exception:{e}"


def passes_general_filter(smiles: str) -> tuple[bool, str]:
    """Fast general gate: parseable, no banned atoms, Lipinski Ro5."""
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors
        from .life_pulse import BANNED_ATOMS
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return False, "invalid_smiles"
        sym = {a.GetSymbol() for a in mol.GetAtoms()}
        if sym & BANNED_ATOMS:
            return False, "banned_atom"
        mw = Descriptors.MolWt(mol)
        if mw > 600:
            return False, f"mw={mw:.1f}>600"
        return True, "ok"
    except Exception as e:
        return False, f"exception:{e}"


# ── Focused candidate batch ───────────────────────────────────────────────────

def get_focused_candidates(
    target: dict,
    n: int = 50,
    phase: str = "explore",
    best_smiles: Optional[list[str]] = None,
    art_model=None,
) -> tuple[list[tuple[str, str, float]], dict]:
    """
    Generate up to n diverse, ART-ranked candidates for a given target.

    Parameters
    ----------
    target      : target dict from fetch_targets() with keys:
                    uniprot_id, id, protein_sequence.
    n           : batch size target.
    phase       : "explore" (broad Sobol), "exploit" (ART pre-filter),
                  or "refine" (neighbourhood of best_smiles).
    best_smiles : For "refine" phase — list of top-scoring SMILES from prior
                  rounds; we include their family's vocab preferentially.
    art_model   : Pre-loaded ART model (avoids repeated disk reads).

    Returns
    -------
    (candidates, diag)
    candidates  : list of (label, smiles, predicted_score), sorted descending.
    diag        : diagnostics dict.
    """
    from .life_pulse   import FAMILY_VOCAB, get_next_candidates, run_sweep, proxy_score
    from .life_art     import rank_candidates, extract_features, load_model
    from .life_diversity import greedy_diverse_select

    uniprot  = target.get("uniprot_id", "")
    sequence = target.get("protein_sequence", "")
    family   = detect_protein_family(uniprot, sequence)

    diag: dict = {
        "target_id":    target.get("id", ""),
        "uniprot_id":   uniprot,
        "family":       family,
        "phase":        phase,
        "n_target":     n,
        "ts":           time.time(),
    }

    _model = art_model if art_model is not None else load_model()

    # ── Phase: explore (broad Sobol from pulse data) ──────────────────────────
    if phase == "explore":
        pulse_rows = get_next_candidates(n=n * 4, family_filter=family)
        if len(pulse_rows) < n:
            # Top-up by running more Sobol
            run_sweep(max_configs=min(200, n * 3), family_filter=family, verbose=False)
            pulse_rows = get_next_candidates(n=n * 4, family_filter=family)

        candidates_raw: list[tuple[str, str]] = [
            (r.get("scaffold_name", r["smiles"][:12]), r["smiles"])
            for r in pulse_rows
        ]
        n_filtered = 0
        filtered: list[tuple[str, str]] = []
        for label, smi in candidates_raw:
            ok, reason = focused_family_filter(smi, family)
            if ok:
                filtered.append((label, smi))
            else:
                n_filtered += 1
        # Fallback: if focused filter too strict, use general
        if not filtered:
            filtered = [(label, smi) for label, smi in candidates_raw
                        if passes_general_filter(smi)[0]]

        diag["n_pulse_rows"]    = len(pulse_rows)
        diag["n_family_filtered"]= n_filtered
        diag["n_passed_filter"] = len(filtered)

        ranked = rank_candidates(filtered, model=_model)

    # ── Phase: exploit (ART pre-filter → score top 25%) ──────────────────────
    elif phase == "exploit":
        pulse_rows = get_next_candidates(n=n * 8, family_filter=family)
        if not pulse_rows:
            run_sweep(max_configs=400, family_filter=family, verbose=False)
            pulse_rows = get_next_candidates(n=n * 8, family_filter=family)

        all_cands = [(r.get("scaffold_name", r["smiles"][:12]), r["smiles"])
                     for r in pulse_rows]
        # ART pre-score all candidates, keep top 25%
        pre_ranked = rank_candidates(all_cands, model=_model)
        top_25     = pre_ranked[:max(n, len(pre_ranked) // 4)]
        # Apply focused filter on pre-selected top 25%
        filtered_exploit: list[tuple[str, str]] = []
        for label, smi, _ in top_25:
            ok, _ = focused_family_filter(smi, family)
            if ok:
                filtered_exploit.append((label, smi))
        if not filtered_exploit:
            filtered_exploit = [(label, smi) for label, smi, _ in top_25
                                if passes_general_filter(smi)[0]]

        diag["n_pulse_rows"]     = len(pulse_rows)
        diag["n_art_pre_scored"] = len(pre_ranked)
        diag["n_top25"]          = len(top_25)
        diag["n_passed_filter"]  = len(filtered_exploit)

        ranked = rank_candidates(filtered_exploit, model=_model)

    # ── Phase: refine (neighbourhood of best molecules) ──────────────────────
    elif phase == "refine":
        if not best_smiles:
            # No best yet — fall back to exploit
            return get_focused_candidates(target, n=n, phase="exploit",
                                          art_model=art_model)
        # Include best SMILES + pulse candidates near them in chemical space
        pulse_rows = get_next_candidates(n=n * 4, family_filter=family)
        pulse_cands = [(r.get("scaffold_name", r["smiles"][:12]), r["smiles"])
                       for r in pulse_rows]
        # Best molecules are always included
        best_cands = [("best_prior", smi) for smi in best_smiles[:20]]
        all_refine = best_cands + pulse_cands

        filtered_refine: list[tuple[str, str]] = []
        for label, smi in all_refine:
            ok, _ = passes_general_filter(smi)
            if ok:
                filtered_refine.append((label, smi))

        diag["n_best_seeds"]    = len(best_smiles)
        diag["n_pulse_rows"]    = len(pulse_rows)
        diag["n_passed_filter"] = len(filtered_refine)

        ranked = rank_candidates(filtered_refine, model=_model)

    else:
        raise ValueError(f"Unknown phase: {phase!r}. Use explore/exploit/refine.")

    # Diversity filter before returning
    diverse = greedy_diverse_select(ranked, n=n, sim_threshold=0.65)
    diag["n_ranked"]  = len(ranked)
    diag["n_diverse"] = len(diverse)
    if diverse:
        diag["best_score"]  = round(diverse[0][2], 5)
        diag["worst_score"] = round(diverse[-1][2], 5)

    _log_scout(diag)
    return diverse, diag


# ── Logging ───────────────────────────────────────────────────────────────────

def _log_scout(diag: dict) -> None:
    with SCOUT_LOG.open("a") as fh:
        fh.write(json.dumps(diag) + "\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Life Scout: protein-family-aware routing")
    p.add_argument("--uniprot", default="Q9UQM7")
    p.add_argument("--phase",   default="explore",
                   choices=["explore", "exploit", "refine"])
    p.add_argument("--n",       type=int, default=20)
    args = p.parse_args()

    target = {"uniprot_id": args.uniprot, "id": args.uniprot,
              "protein_sequence": ""}
    family = detect_protein_family(args.uniprot)
    print(f"[SCOUT] {args.uniprot} → family={family}")
    cands, d = get_focused_candidates(target, n=args.n, phase=args.phase)
    print(f"[SCOUT] returned {len(cands)} candidates")
    for label, smi, score in cands[:5]:
        print(f"  {score:.4f}  {label:20s}  {smi}")
    print(f"[SCOUT] diag: {json.dumps(d, indent=2)}")
