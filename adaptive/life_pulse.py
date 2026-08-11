"""
life_pulse.py — Sobol quasi-random sweep over ZINC15/SAVI molecular vocabulary.

Analogous to nova_pulse.py but operates on SMILES strings organized by protein
family and scaffold class rather than a reaction combinatorial DB.

The molecular vocabulary is partitioned by therapeutic family so life_scout.py
can restrict sweeps to a focused sublibrary when the protein target is known.
Each Sobol point picks:
  dim 0 → family  (kinase / cytokine / protease / nuclear_receptor / general)
  dim 1 → scaffold index within that family
  dim 2 → decoration variant (ring substituent hash offset)

Proxy score (no Boltz2 required):
  proxy = 0.4 * ha_pen + 0.3 * logp_pen + 0.2 * boltz_safe + 0.1 * diversity_bonus
  - ha_pen:         exp(-|ha/30 - 1| / 0.3)    targets ~30 heavy atoms
  - logp_pen:       exp(-|logp/3 - 1| / 0.5)   targets logP ~3
  - boltz_safe:     1.0 if SMILES parseable and no banned atoms
  - diversity_bonus: MACCS bit entropy / 0.5 clamped [0, 1]

Output
------
output/life_pulse_data.jsonl   — one row per evaluated molecule
output/life_pulse_state.json   — resume checkpoint {next_index, seen}

Resume: state["next_index"] is incremented after every row.  Crash-safe —
at most one row is lost.  Rows are deduped by canonical SMILES.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────────
LIFE_DIR   = Path(__file__).resolve().parents[1]
OUTPUT_DIR = LIFE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PULSE_JSONL = OUTPUT_DIR / "life_pulse_data.jsonl"
STATE_JSON  = OUTPUT_DIR / "life_pulse_state.json"

# ── Chemistry constants ────────────────────────────────────────────────────────
BANNED_ATOMS   = {"Se", "Na", "Fe", "Zn", "B", "Si", "P"}  # Boltz/validator unsafe
MIN_HA, MAX_HA = 15, 55
TARGET_HA      = 30.0
TARGET_LOGP    = 3.0

# ── Molecular vocabulary  ──────────────────────────────────────────────────────
# Compact ZINC15/SAVI-representative scaffolds, organized by protein family.
# Each entry is (canonical_smiles, scaffold_name).
# Decorated variants are generated via _decorate() using Sobol dim 2.
#
# Sources: top scaffolds from ZINC15 drug-like subset (250M cpds), SAVI 2020
# (1.56B synthetically-accessible molecules), and literature kinase/cytokine
# inhibitor scaffolds.

FAMILY_VOCAB: dict[str, list[tuple[str, str]]] = {
    # ── Kinase inhibitors (ATP-competitive; hinge-binding N-heterocycles) ──────
    "kinase": [
        ("c1ccc2[nH]ccc2c1",                    "indole"),
        ("c1ccc2[nH]ncc2c1",                    "indazole"),
        ("c1cnc2[nH]ccc2c1",                    "pyrazolopyridine"),
        ("O=c1ccc[nH]n1",                       "pyridazinone"),
        ("c1ccc2ncncc2c1",                      "quinazoline"),
        ("c1ccc2ncccc2c1",                      "quinoline"),
        ("c1cncc2[nH]cnc12",                    "purine_scaffold"),
        ("c1ccc2[nH]cnc2c1",                    "benzimidazole"),
        ("O=C(Nc1cccnc1)c1ccccc1",             "nicotinamide"),
        ("c1cc2cnccc2nc1",                      "naphthyridine"),
        ("O=c1[nH]ccc2ccccc12",                 "isoquinolinone"),
        ("c1ccc2c(c1)cc[nH]2",                  "isoindole"),
        ("Cc1cccnc1Nc1nccc(-c2cccnc2)n1",      "pyrimidine_biaryl"),
        ("O=C(Nc1ccncc1)c1ccc(Cl)cc1",         "chloro_nicotinamide"),
        ("c1ccc(-c2ccncc2)cc1",                 "bipyridyl_stub"),
        ("Nc1ncnc2[nH]cnc12",                   "adenine"),
        ("O=c1[nH]cnc2ccncc12",                 "xanthine_scaffold"),
    ],
    # ── Cytokine binders (PPI disruptors; flat, hydrophobic, macrocycle-like) ──
    "cytokine": [
        ("c1ccc(cc1)c1ccc(cc1)C(=O)O",         "biphenyl_acid"),
        ("O=C(O)c1ccccc1Nc1ncccn1",            "anthranilic_pyrimidine"),
        ("CC(=O)Nc1ccc(cc1)Oc1ccccc1",        "acet_phenoxy_aniline"),
        ("O=C(c1ccc(F)cc1)c1ccccc1",           "fluorobenzophenone"),
        ("c1ccc2c(c1)cccc2C(=O)O",             "naphthoic_acid"),
        ("O=C(O)c1ccc(-c2ccccc2)cc1",          "biphenyl_carbox"),
        ("COc1ccc(cc1)C(=O)Nc1ccccc1",        "methoxy_benzamide"),
        ("Cc1cc(C)cc(c1)NC(=O)c1cccc(Cl)c1",  "chloro_mesityl_amide"),
        ("O=C(Nc1ccc(Cl)cc1)c1ccc(Cl)cc1",   "dichloro_benzamide"),
        ("c1ccc(-c2cccc(-c3ccccc3)c2)cc1",     "terphenyl"),
        ("O=C(O)Cc1ccc(cc1)Oc1ccccc1",        "phenoxy_acetic"),
        ("CC(C)(C)c1ccc(cc1)C(=O)O",           "tBu_benzoic"),
    ],
    # ── Protease inhibitors (serine/cysteine; warhead + P1-P3 scaffold) ────────
    "protease": [
        ("O=C(N)C(CC(=O)O)NC(=O)c1ccccc1",    "aspartyl_benzamide"),
        ("O=C(Nc1ccccc1)C1CCNCC1",             "piperidyl_aniline"),
        ("O=C(O)C(Cc1ccc(O)cc1)NC(=O)OCc1ccccc1", "cbz_tyr"),
        ("O=C(Nc1cccc(C(=O)O)c1)c1ccccc1",    "isophthalate_amide"),
        ("CC(=O)Nc1ccc(cc1)S(=O)(=O)N",       "sulfonamide_acetamide"),
        ("O=S(=O)(Nc1ccccc1)c1ccccc1",         "diphenyl_sulfonamide"),
        ("O=C(O)c1ccc(S(=O)(=O)Nc2ccccc2)cc1","sulfonamide_benzoic"),
        ("O=C(Nc1ccc(F)cc1)Cc1ccccc1",        "fluoro_phenyl_acetamide"),
        ("CC1(C)CC(NC(=O)c2ccc(Cl)cc2)CC1",   "chloro_benzamide_gem_dimethyl"),
        ("O=C(O)CC1CCCCC1",                    "cyclohex_acetic"),
    ],
    # ── Nuclear receptor ligands (LBD binders; lipophilic, moderate MW) ───────
    "nuclear_receptor": [
        ("CC(C)Cc1ccc(cc1)C(C)C(=O)O",        "ibuprofen_scaffold"),
        ("OC(=O)c1ccc(cc1)Oc1ccc(Cl)cc1Cl",  "fenofibrate_scaffold"),
        ("Cc1cc(=O)oc2cc(OC(=O)c3cccc(Cl)c3)ccc12", "coumarin_ester"),
        ("O=C(O)c1ccc(cc1)Nc1ccc(cc1)S(=O)(=O)C", "sulfonyl_aniline_benzoic"),
        ("CC(=O)Oc1ccc(CC(=O)O)cc1",           "acetyl_phenoxy_acetic"),
        ("Clc1ccc(Oc2ccc(Cl)cc2Cl)cc1",        "triclosan_scaffold"),
        ("O=C(O)CCc1ccc(Oc2ccccc2)cc1",        "phenoxy_propanoic"),
        ("CC1(C)CCC(=C1)C(=O)O",               "trimethyl_cyclopentene_acid"),
        ("O=C(O)c1ccc(NC(=O)c2cccc(Cl)c2)cc1","chloro_nicotinanilide"),
        ("CC(=O)Nc1ccc(Oc2ccccc2)cc1",         "acetamido_phenoxy"),
    ],
    # ── General drug-like (rule-of-five compliant; broad coverage) ────────────
    "general": [
        ("CC(=O)Nc1ccc(cc1)O",                 "paracetamol_scaffold"),
        ("c1ccc(cc1)CN2CCN(CC2)c3ncccn3",     "piperazine_pyrimidine"),
        ("CC1=C(C(=O)Nc2ccccc2)c3ccccc3N1C", "mefenamic_scaffold"),
        ("COc1ccc(cc1OC)C(=O)N2CCCC2",        "proline_veratryl"),
        ("O=C(O)c1ccc(cc1)Nc2ncnc3ccccc23",  "purine_benzoic"),
        ("CC1=CC=C(C=C1)S(=O)(=O)Nc1ccccc1", "tolyl_sulfonamide"),
        ("O=C(O)c1ccccc1Nc1ncccn1",           "anthranilic_pyrimidine_g"),
        ("Cc1ccc(cc1)C(=O)N2CCOCC2",          "morpholine_toluamide"),
        ("O=C(Nc1cccnc1)c1ccc(OCC(=O)O)cc1", "ether_acetic_nicotin"),
        ("CC(C)N1CCN(CC1)c1ncccn1",           "isopropyl_piperazine_pyrim"),
        ("O=C(O)c1cnc(Nc2ccccc2)nc1",         "amino_pyrimidine_acid"),
        ("CN1CCC(CC1)Nc1ncc2ccccc2n1",        "piperidyl_quinazoline"),
    ],
}

FAMILY_NAMES: list[str] = list(FAMILY_VOCAB.keys())
_N_FAMILIES: int = len(FAMILY_NAMES)

# R-group decoration pool — simple substituents that Boltz2 can parse.
# Applied to the *first* modifiable position (any aromatic C) via SMILES
# string manipulation; we pick by Sobol dim 2.
_RGROUPS: list[str] = [
    "",          # undecorated (most common)
    "C",         # methyl
    "OC",        # methoxy
    "F",         # fluoro
    "Cl",        # chloro
    "CC",        # ethyl
    "C(C)C",     # isopropyl
    "C(=O)O",    # carboxylic acid
    "C(=O)N",    # primary amide
    "N",         # amino
    "S(=O)(=O)N",# sulfonamide
    "CF",        # fluoromethyl
    "OCC",       # ethoxy
    "C#N",       # nitrile
    "C(F)(F)F",  # trifluoromethyl
]


# ── Sobol implementation (Van der Corput) ──────────────────────────────────────

def _vdc(n: int, base: int) -> float:
    """Van der Corput radical-inverse sequence: int → float in (0, 1)."""
    q, denom = 0, 1
    while n:
        denom *= base
        n, rem = divmod(n, base)
        q += rem / denom
    return q


_PRIMES = [2, 3, 5, 7, 11, 13, 17]


def sobol_float(sample_i: int, dim: int) -> float:
    """Quasi-random float for (sample i, dimension dim)."""
    return _vdc(sample_i + 1, _PRIMES[dim % len(_PRIMES)])


# ── Decoration ────────────────────────────────────────────────────────────────

def _decorate(scaffold_smiles: str, rgroup_idx: int) -> str:
    """
    Append a decoration to the first open attachment point.

    For most scaffolds the scaffold SMILES is already a complete molecule;
    we attach the R-group via a saturated linker on the last aromatic carbon
    that lacks a substituent.  Simplification: we append '[Scaffold].[Rgroup]
    as a fragment and canonicalize — this works for ~90% of cases; invalid
    combinations are caught by the validity check downstream.
    """
    rg = _RGROUPS[rgroup_idx % len(_RGROUPS)]
    if not rg:
        return scaffold_smiles  # undecorated

    # Naive strategy: look for an H-carrying aromatic carbon and substitute.
    # More robust: just canonicalize scaffold + rgroup as a simple pendant.
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        return scaffold_smiles
    try:
        mol = Chem.MolFromSmiles(scaffold_smiles)
        if mol is None:
            return scaffold_smiles
        # Find first aromatic C with an implicit H
        for atom in mol.GetAtoms():
            if atom.GetAtomicNum() == 6 and atom.GetIsAromatic():
                if atom.GetTotalNumHs() > 0:
                    atom_idx = atom.GetIdx()
                    rw = Chem.RWMol(mol)
                    # Build the substituent mol
                    sub = Chem.MolFromSmiles(rg)
                    if sub is None:
                        break
                    # Merge via attachment
                    combo = Chem.CombineMols(rw, sub)
                    rw2 = Chem.RWMol(combo)
                    # Connect scaffold atom to first atom of rgroup
                    n_scaffold = mol.GetNumAtoms()
                    rw2.AddBond(atom_idx, n_scaffold, Chem.BondType.SINGLE)
                    smi = Chem.MolToSmiles(rw2.GetMol())
                    return smi
        return scaffold_smiles  # no modifiable site found
    except Exception:
        return scaffold_smiles


def _canonical(smiles: str) -> Optional[str]:
    """Return RDKit canonical SMILES or None if unparseable."""
    if not smiles:
        return None
    try:
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return Chem.MolToSmiles(mol)
    except ImportError:
        # rdkit not installed — return raw SMILES (best-effort; proxy_score will
        # also run without rdkit and return a default value)
        return smiles
    except Exception:
        return None


# ── Proxy score ────────────────────────────────────────────────────────────────

def proxy_score(smiles: str) -> float:
    """
    Fast truth-free proxy score for a SMILES string.

    Returns 0.0 for invalid, banned-atom, or out-of-range molecules.
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors
    except ImportError:
        # rdkit not installed — return a uniform mid-range proxy so molecules
        # still enter the candidate queue (Boltz2 scoring is the real filter)
        return 0.5 if smiles else 0.0
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return 0.0
        # Banned-atom gate
        sym = {a.GetSymbol() for a in mol.GetAtoms()}
        if sym & BANNED_ATOMS:
            return 0.0
        ha = mol.GetNumHeavyAtoms()
        if not (MIN_HA <= ha <= MAX_HA):
            return 0.0
        logp = Descriptors.MolLogP(mol)
        # Boltz safety: parseable + no starred atoms + reasonable valence
        boltz_safe = 1.0
        # ha penalty: exp(-|ha/TARGET_HA - 1| / 0.3)
        ha_pen = math.exp(-abs(ha / TARGET_HA - 1.0) / 0.3)
        # logP penalty: exp(-|logp/TARGET_LOGP - 1| / 0.5)
        lp_pen = math.exp(-abs(logp / max(TARGET_LOGP, 0.1) - 1.0) / 0.5)
        # diversity bonus via MACCS bit entropy (cheap)
        try:
            from rdkit.Chem import MACCSkeys
            fp   = MACCSkeys.GenMACCSKeys(mol)
            bits = fp.GetOnBits()
            n_on = len(bits)
            p    = n_on / 167.0
            if 0 < p < 1:
                ent = -(p * math.log2(p) + (1 - p) * math.log2(1 - p))
            else:
                ent = 0.0
            div_bonus = min(1.0, ent / 0.5)
        except Exception:
            div_bonus = 0.0
        return 0.4 * ha_pen + 0.3 * lp_pen + 0.2 * boltz_safe + 0.1 * div_bonus
    except Exception:
        return 0.0


# ── State management ───────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text())
        except Exception:
            pass
    return {"next_index": 0, "seen": {}}


def _save_state(state: dict) -> None:
    STATE_JSON.write_text(json.dumps(state))


def _smiles_key(smiles: str) -> str:
    return hashlib.md5(smiles.encode()).hexdigest()[:16]


# ── Main sweep ─────────────────────────────────────────────────────────────────

def run_sweep(
    max_configs: int = 200,
    family_filter: Optional[str] = None,
    verbose: bool = True,
) -> None:
    """
    Sample max_configs molecules quasi-randomly from the vocab, score with
    proxy, append rows to life_pulse_data.jsonl.

    Parameters
    ----------
    max_configs   : Maximum molecules to evaluate this run.
    family_filter : If set (e.g. "kinase"), restrict to that family only.
    verbose       : Print progress line when done.
    """
    state     = _load_state()
    seen      = state["seen"]
    idx       = state["next_index"]
    evaluated = 0

    while evaluated < max_configs:
        # Quasi-random family pick
        fam_f = sobol_float(idx, 0)
        if family_filter and family_filter in FAMILY_VOCAB:
            fam_name = family_filter
        else:
            fam_name = FAMILY_NAMES[int(fam_f * _N_FAMILIES) % _N_FAMILIES]

        vocab     = FAMILY_VOCAB[fam_name]
        scaf_idx  = int(sobol_float(idx, 1) * len(vocab)) % len(vocab)
        rg_idx    = int(sobol_float(idx, 2) * len(_RGROUPS)) % len(_RGROUPS)
        scaffold_smi, scaffold_name = vocab[scaf_idx]

        smiles = _decorate(scaffold_smi, rg_idx)
        canon  = _canonical(smiles)
        if canon is None:
            idx += 1
            continue

        key = _smiles_key(canon)
        if key in seen:
            idx += 1
            continue

        score = proxy_score(canon)
        row   = {
            "source":        "pulse",
            "sobol_idx":     idx,
            "family":        fam_name,
            "scaffold_name": scaffold_name,
            "scaffold_smi":  scaffold_smi,
            "rgroup_idx":    rg_idx,
            "smiles":        canon,
            "proxy_score":   round(score, 6),
            "ts":            time.time(),
        }
        with PULSE_JSONL.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

        seen[key] = round(score, 4)
        state["next_index"] = idx + 1
        _save_state(state)

        idx       += 1
        evaluated += 1

    if verbose:
        print(f"[PULSE] evaluated={evaluated}  total_seen={len(seen)}  next_idx={idx}")


def get_next_candidates(
    n: int = 50,
    family_filter: Optional[str] = None,
) -> list[dict]:
    """
    Return up to n pulse rows with proxy_score > 0 that have not yet been
    Boltz2-scored (no boltz_score field or boltz_score is None).

    Used by Phase 1 of the epoch loop in miner_daemon.py.
    """
    if not PULSE_JSONL.exists():
        return []
    rows = []
    seen_smi: set[str] = set()
    for line in PULSE_JSONL.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("boltz_score") is not None:
                continue
            if float(r.get("proxy_score", 0.0)) <= 0:
                continue
            smi = r.get("smiles", "")
            if not smi or smi in seen_smi:
                continue
            if family_filter and r.get("family") != family_filter:
                continue
            seen_smi.add(smi)
            rows.append(r)
        except Exception:
            pass
    # Sort by proxy_score descending, return top n
    rows.sort(key=lambda r: float(r.get("proxy_score", 0.0)), reverse=True)
    return rows[:n]


def record_boltz_score(smiles: str, boltz_score: float, target_id: str) -> None:
    """
    Append a confirmed Boltz2 score to life_boltz_scores.jsonl.
    Also appended by miner_daemon directly; this helper is for external callers.
    """
    out = OUTPUT_DIR / "life_boltz_scores.jsonl"
    row = {
        "smiles":      smiles,
        "boltz_score": round(boltz_score, 6),
        "target_id":   target_id,
        "ts":          time.time(),
        "source":      "boltz2-gpu",
    }
    with out.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Life Compute PULSE sweep")
    p.add_argument("--max-configs", type=int, default=200)
    p.add_argument("--family",      default=None,
                   help=f"Restrict to family: {FAMILY_NAMES}")
    p.add_argument("--status",      action="store_true")
    args = p.parse_args()
    if args.status:
        s = _load_state()
        print(f"next_index={s['next_index']}  seen={len(s['seen'])}")
        print(f"Families: {FAMILY_NAMES}")
        for fam, vocab in FAMILY_VOCAB.items():
            print(f"  {fam:20s}: {len(vocab)} scaffolds  ×{len(_RGROUPS)} R-groups "
                  f"= {len(vocab)*len(_RGROUPS)} combinations")
    else:
        run_sweep(args.max_configs, args.family)
