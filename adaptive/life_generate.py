"""
life_generate.py — Generative molecule AI (Phase 4) for Life Compute.

Three complementary generation methods run in the final 15% of each epoch,
after the explore / exploit / refine phases have already scored many molecules
with Boltz2.  By that point the miner has a pool of real binding scores to
learn from, making generative methods most informative.

  Method 1 — Fragment Recombination (up to 50 candidates)
    BRICS-decomposes the top-10 Boltz2-scored molecules, pools all fragments
    from different parents into one library, then calls BRICSBuild to generate
    novel hybrid scaffolds not present in the original vocabulary.

  Method 2 — Scaffold Hopping (up to 20 candidates)
    Takes the single best-scoring molecule and replaces its core ring system
    with bioisosteric alternatives.  Two strategies:
      A. Reaction SMARTS ring swaps for ring-size-preserving heteroatom swaps
         (benzene → pyridine, benzene → pyrimidine).
      B. BRICS recombination: BRICS-decomposes the parent + each alternative
         scaffold (indole, quinoline, thiophene, …) then calls BRICSBuild —
         naturally generates bicyclic analogues and heteroaromatic hybrids.

  Method 3 — Guided Mutation (up to 30 candidates)
    Applies 8 single-step reaction SMARTS mutations (add/remove OH, F, CH3,
    NH2, CF3, COOH) to the top-5 Boltz2-scored molecules.  Only keeps
    mutations that improve the ART pre-filter score over the parent molecule.

Safety gates applied to every candidate from all three methods:
  • RDKit sanitization passes
  • No wildcard atoms (Boltz2 rejects them)
  • No banned atoms: Se, Na, Fe, Zn, B, Si, P
  • MW 200–600 Da
  • ≤ 10 rotatable bonds
  • Passes RDKit PAINS filter
  • Not a duplicate of any previously submitted molecule

Deduplication: uses output/life_submitted_memory.jsonl (the canonical
submission history maintained by life_diversity.SubmissionMemory).  The
request referenced "submitted_weekly.jsonl" but that file does not exist in
this repo — life_submitted_memory.jsonl is the correct dedup source.

Entry point for miner_daemon.py Phase 4:
  generate_candidates(target, art_model, n_total=100)
  → list of (label, smiles, art_score)   ← same tuple shape as life_scout

Output log: output/life_generated.jsonl
  Fields: smiles, method, parent_smiles, art_score, boltz_score, target, ts

All rdkit imports are lazy (inside function bodies with try/except ImportError)
so this module can be imported even when rdkit is not on the Python path —
all three methods will gracefully return empty lists.
"""

from __future__ import annotations

import json
import logging
import time
from itertools import islice
from pathlib import Path
from typing import Optional

log = logging.getLogger("life-miner")

# ── Paths ──────────────────────────────────────────────────────────────────────
LIFE_DIR     = Path(__file__).resolve().parents[1]
OUTPUT_DIR   = LIFE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BOLTZ_SCORES = OUTPUT_DIR / "life_boltz_scores.jsonl"
GEN_JSONL    = OUTPUT_DIR / "life_generated.jsonl"
SUB_MEMORY   = OUTPUT_DIR / "life_submitted_memory.jsonl"

# ── Chemistry constants (mirrors life_art.py / life_pulse.py) ─────────────────
BANNED_ATOMS  = {"Se", "Na", "Fe", "Zn", "B", "Si", "P"}
MIN_MW        = 200.0
MAX_MW        = 600.0
MAX_ROT_BONDS = 10

# ── Alternative scaffolds for scaffold hopping ────────────────────────────────
# Ordered: simpler → more complex.
# BRICS.BRICSDecompose is applied to each, so even scaffolds that don't cleanly
# decompose (thiophene, imidazole) are still usable as BRICSBuild atoms.
ALT_SCAFFOLDS = [
    ("c1ccccc1",          "benzene"),
    ("c1ccncc1",          "pyridine"),
    ("c1ccc2[nH]ccc2c1", "indole"),
    ("c1ccc2ncccc2c1",   "quinoline"),
    ("c1cnc[nH]1",        "imidazole"),
    ("c1cccs1",           "thiophene"),
    ("c1ncncc1",          "pyrimidine"),
]

# ── Mutation reaction SMARTS for guided mutation ───────────────────────────────
# All operate on aromatic CH positions (most common and reliable site).
# RunReactants returns one product per matching position, giving positional
# diversity automatically.
_MUTATION_RXNS_SMARTS = [
    ("add_OH",   "[cH:1]>>[c:1]O"),
    ("add_F",    "[cH:1]>>[c:1]F"),
    ("add_CH3",  "[cH:1]>>[c:1]C"),
    ("add_NH2",  "[cH:1]>>[c:1]N"),
    ("add_CF3",  "[cH:1]>>[c:1]C(F)(F)F"),
    ("add_COOH", "[cH:1]>>[c:1]C(=O)O"),
    ("rem_OH",   "[c:1][OH:2]>>[cH:1]"),         # remove phenolic OH
    ("rem_F",    "[c:1][F:2]>>[cH:1]"),           # remove aryl fluoride
]

# ── Scaffold hop reaction SMARTS ───────────────────────────────────────────────
# Ring-size-preserving bioisosteric swaps.
# Pattern:  [c:1]1[cH:2][c:3][c:4][c:5][c:6]1
#   :1, :3-:6 = any aromatic carbon (may carry substituents or be fused)
#   :2         = aromatic CH specifically (the atom to be replaced)
# Product: replace :2 with an aromatic nitrogen.
# RunReactants returns one product per cH position in the ring, giving
# all possible N-substituted regioisomers in one call.
_SCAFFOLD_HOP_SMARTS = [
    # Monoaza: any benzene-like ring → pyridine (N at one position)
    ("bz→py",
     "benzene_to_pyridine",
     "[c:1]1[cH:2][c:3][c:4][c:5][c:6]1>>[c:1]1[n:2][c:3][c:4][c:5][c:6]1"),
    # Diaza: any benzene-like ring → pyrimidine (N at alternating positions)
    ("bz→pym",
     "benzene_to_pyrimidine",
     "[c:1]1[cH:2][c:3][cH:4][c:5][c:6]1>>[c:1]1[n:2][c:3][n:4][c:5][c:6]1"),
]

# ── Lazy-initialized singletons ────────────────────────────────────────────────

_PAINS_CATALOG = None
_MUTATION_RXNS  = None   # list[(name, compiled_rxn)]
_SCAFFOLD_RXNS  = None   # list[(name, desc, compiled_rxn)]


def _get_pains_catalog():
    """Return the PAINS filter catalog, initialised once."""
    global _PAINS_CATALOG
    if _PAINS_CATALOG is None:
        try:
            from rdkit.Chem.FilterCatalog import FilterCatalog, FilterCatalogParams
            params = FilterCatalogParams()
            params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS)
            _PAINS_CATALOG = FilterCatalog(params)
        except Exception as e:
            log.debug(f"[GENERATE] PAINS catalog init failed: {e}")
    return _PAINS_CATALOG


def _get_mutation_rxns() -> list:
    """Return compiled mutation reactions, initialised once."""
    global _MUTATION_RXNS
    if _MUTATION_RXNS is None:
        compiled = []
        try:
            from rdkit.Chem import AllChem
            for name, smarts in _MUTATION_RXNS_SMARTS:
                try:
                    rxn = AllChem.ReactionFromSmarts(smarts)
                    if rxn is not None:
                        compiled.append((name, rxn))
                except Exception as e:
                    log.debug(f"[GENERATE] mutation rxn {name!r} compile error: {e}")
        except ImportError:
            pass
        _MUTATION_RXNS = compiled
    return _MUTATION_RXNS


def _get_scaffold_rxns() -> list:
    """Return compiled scaffold-hop reactions, initialised once."""
    global _SCAFFOLD_RXNS
    if _SCAFFOLD_RXNS is None:
        compiled = []
        try:
            from rdkit.Chem import AllChem
            for name, desc, smarts in _SCAFFOLD_HOP_SMARTS:
                try:
                    rxn = AllChem.ReactionFromSmarts(smarts)
                    if rxn is not None:
                        compiled.append((name, desc, rxn))
                except Exception as e:
                    log.debug(f"[GENERATE] scaffold rxn {name!r} compile error: {e}")
        except ImportError:
            pass
        _SCAFFOLD_RXNS = compiled
    return _SCAFFOLD_RXNS


# ── Validation ────────────────────────────────────────────────────────────────

def is_boltz_safe_smiles(smiles: str) -> bool:
    """
    Return True if SMILES is valid for Boltz2 scoring.

    Checks:
      1. RDKit can parse the SMILES (MolFromSmiles returns non-None)
      2. No wildcard / attachment atoms (atomic_num == 0)
      3. No banned atoms (Se, Na, Fe, Zn, B, Si, P)

    Mirrors the validity checks in life_art.extract_features() so that any
    molecule that passes here will also produce a valid feature vector for ART.
    """
    if not smiles:
        return False
    try:
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return False
        if any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()):
            return False
        sym = {a.GetSymbol() for a in mol.GetAtoms()}
        return not bool(sym & BANNED_ATOMS)
    except ImportError:
        # rdkit not available — accept optimistically; Boltz2 will reject at scoring
        return bool(smiles)
    except Exception:
        return False


def _pains_passes(mol) -> bool:
    """
    Return True if the molecule passes the PAINS filter (i.e. is NOT a PAINS hit).
    Returns True when the catalog is unavailable (fail-open).
    """
    catalog = _get_pains_catalog()
    if catalog is None:
        return True
    try:
        return not catalog.HasMatch(mol)
    except Exception:
        return True


def _validate_candidate(smiles: str, seen: set) -> bool:
    """
    Full validation gate for every generated molecule.

    Returns False if ANY of the following:
      • smiles already in `seen` (deduplication)
      • fails is_boltz_safe_smiles()
      • MW outside [200, 600] Da
      • more than 10 rotatable bonds
      • fails PAINS filter

    `seen` should contain canonical SMILES already submitted or generated in
    this run.  The caller is responsible for adding validated SMILES to `seen`.
    """
    if smiles in seen:
        return False
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return False
        # Canonicalize and recheck for duplicates via canonical form
        canon = Chem.MolToSmiles(mol)
        if canon in seen:
            return False
        if not is_boltz_safe_smiles(canon):
            return False
        mw = Descriptors.MolWt(mol)
        if not (MIN_MW <= mw <= MAX_MW):
            return False
        rot = Descriptors.NumRotatableBonds(mol)
        if rot > MAX_ROT_BONDS:
            return False
        if not _pains_passes(mol):
            return False
        return True
    except ImportError:
        # rdkit not available — accept optimistically
        return smiles not in seen
    except Exception:
        return False


def _canonical_smiles(smiles: str) -> Optional[str]:
    """Return RDKit canonical SMILES or None if unparseable."""
    try:
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return Chem.MolToSmiles(mol)
    except Exception:
        return None


# ── Data loaders ──────────────────────────────────────────────────────────────

def _load_top_boltz(n: int) -> list:
    """
    Return up to n records from life_boltz_scores.jsonl, deduplicated by SMILES
    and sorted descending by boltz_score (higher = better binder for Boltz2's
    combined score convention).

    Returns a list of dicts with at least: smiles, boltz_score, target_id.
    """
    best: dict = {}   # canonical_smiles → row dict
    if not BOLTZ_SCORES.exists():
        return []
    for line in BOLTZ_SCORES.read_text().splitlines():
        try:
            row = json.loads(line)
            smi = row.get("smiles", "")
            val = row.get("boltz_score")
            if not smi or val is None:
                continue
            val = float(val)
            if smi not in best or val > float(best[smi]["boltz_score"]):
                best[smi] = row
        except Exception:
            pass
    sorted_rows = sorted(best.values(), key=lambda r: float(r["boltz_score"]), reverse=True)
    return sorted_rows[:n]


def _load_submitted_smiles() -> set:
    """
    Return set of SMILES already in the submission memory.

    Reads output/life_submitted_memory.jsonl — the canonical dedup store
    maintained by life_diversity.SubmissionMemory.  The "submitted_weekly.jsonl"
    referenced in the design doc does not exist; this is its functional equivalent.
    """
    submitted: set = set()
    if not SUB_MEMORY.exists():
        return submitted
    for line in SUB_MEMORY.read_text().splitlines():
        try:
            row = json.loads(line)
            smi = row.get("smiles", "")
            if smi:
                submitted.add(smi)
        except Exception:
            pass
    return submitted


# ── Output ────────────────────────────────────────────────────────────────────

def _append_generated(records: list) -> None:
    """
    Append generated candidate records to output/life_generated.jsonl.
    Each record must be a JSON-serialisable dict.
    """
    if not records:
        return
    try:
        with GEN_JSONL.open("a") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
    except Exception as e:
        log.warning(f"[GENERATE] Failed to write life_generated.jsonl: {e}")


# ── Method 1: Fragment Recombination ─────────────────────────────────────────

def fragment_recombination(target_id: str, n_max: int = 50) -> list:
    """
    Fragment Recombination: BRICS-decompose top-10 Boltz2 molecules,
    pool all unique fragments, then recombine with BRICSBuild.

    Why cross-parent: fragment pools from different high-scoring molecules
    carry complementary structural features.  Combining them produces hybrids
    that no single parent could generate alone.

    Parameters
    ----------
    target_id : str — used only for logging and the output record field.
    n_max     : int — maximum number of validated candidates to return.

    Returns
    -------
    list of dicts with keys: smiles, method, parent_smiles, art_score,
    boltz_score, target, ts.  art_score and boltz_score are None until filled
    in by generate_candidates().
    """
    records: list = []
    try:
        from rdkit import Chem
        from rdkit.Chem import BRICS
    except ImportError:
        log.debug("[GENERATE] rdkit unavailable — skipping fragment_recombination")
        return records

    top10 = _load_top_boltz(10)
    if not top10:
        log.debug("[GENERATE] Method 1: no Boltz scores yet — skipping")
        return records

    submitted = _load_submitted_smiles()
    seen      = set(submitted)

    # ── BRICS decompose each parent ────────────────────────────────────────────
    all_frag_mols: list    = []
    seen_frag_canon: set   = set()

    for row in top10:
        smi = row.get("smiles", "")
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        try:
            frags = BRICS.BRICSDecompose(mol, minFragmentSize=3)
        except Exception:
            continue
        for f in frags:
            try:
                fm = Chem.MolFromSmiles(f)
                if fm is None:
                    continue
                cs = Chem.MolToSmiles(fm)
                if cs not in seen_frag_canon:
                    seen_frag_canon.add(cs)
                    all_frag_mols.append(fm)
            except Exception:
                continue

    if not all_frag_mols:
        log.debug("[GENERATE] Method 1: BRICS produced no fragments")
        return records

    # ── BRICSBuild — limit to 200 candidates to keep runtime bounded ──────────
    try:
        gen       = BRICS.BRICSBuild(all_frag_mols)
        candidates = list(islice(gen, 200))
    except Exception as e:
        log.debug(f"[GENERATE] Method 1: BRICSBuild failed: {e}")
        return records

    # Use top-1 parent as the representative "parent_smiles" in the log
    top1_smi = top10[0].get("smiles", "") if top10 else ""

    for mol in candidates:
        if mol is None:
            continue
        try:
            Chem.SanitizeMol(mol)
            smi = Chem.MolToSmiles(mol)
        except Exception:
            continue
        if _validate_candidate(smi, seen):
            seen.add(smi)
            records.append({
                "smiles":        smi,
                "method":        "fragment_recombination",
                "parent_smiles": top1_smi,
                "art_score":     None,
                "boltz_score":   None,
                "target":        target_id,
                "ts":            time.time(),
            })
            if len(records) >= n_max:
                break

    return records


# ── Method 2: Scaffold Hopping ────────────────────────────────────────────────

def scaffold_hopping(target_id: str, n_max: int = 20) -> list:
    """
    Scaffold Hopping: replace the core ring of the best Boltz2 molecule
    with bioisosteric alternatives.

    Two strategies are applied in sequence until n_max is reached:

    A. Reaction SMARTS ring swaps (fast, ring-size-preserving):
       • benzene → pyridine   (monoaza: one cH → n at each ring position)
       • benzene → pyrimidine (diaza: alternating cH → n pairs)
       RunReactants returns ALL positional regioisomers in one call.

    B. BRICS recombination with alt scaffolds (broader, covers bicyclics):
       BRICS-decomposes the parent to get its R-group fragments, then
       BRICSDecomposes each alternative scaffold (indole, quinoline, thiophene,
       imidazole, …) and calls BRICSBuild on the combined fragment pool.
       Naturally generates indole/quinoline/thiophene derivatives without
       needing SMARTS for fused-ring insertions.

    Parameters
    ----------
    target_id : str
    n_max     : int — maximum candidates to return.

    Returns
    -------
    list of dicts with generation metadata.
    """
    records: list = []
    try:
        from rdkit import Chem
        from rdkit.Chem import BRICS
    except ImportError:
        log.debug("[GENERATE] rdkit unavailable — skipping scaffold_hopping")
        return records

    top1 = _load_top_boltz(1)
    if not top1:
        log.debug("[GENERATE] Method 2: no Boltz scores yet — skipping")
        return records

    parent_smi = top1[0].get("smiles", "")
    parent_mol = Chem.MolFromSmiles(parent_smi)
    if parent_mol is None:
        return records

    submitted = _load_submitted_smiles()
    seen      = set(submitted)

    # ── Strategy A: Reaction SMARTS ring swaps ────────────────────────────────
    for name, desc, rxn in _get_scaffold_rxns():
        try:
            products = rxn.RunReactants((parent_mol,))
        except Exception as e:
            log.debug(f"[GENERATE] scaffold rxn {name} RunReactants failed: {e}")
            continue
        for product_tuple in products:
            for prod_mol in product_tuple:
                try:
                    Chem.SanitizeMol(prod_mol)
                    prod_smi = Chem.MolToSmiles(prod_mol)
                except Exception:
                    continue
                if _validate_candidate(prod_smi, seen):
                    seen.add(prod_smi)
                    records.append({
                        "smiles":        prod_smi,
                        "method":        f"scaffold_hop:{name}",
                        "parent_smiles": parent_smi,
                        "art_score":     None,
                        "boltz_score":   None,
                        "target":        target_id,
                        "ts":            time.time(),
                    })
                    if len(records) >= n_max:
                        return records

    # ── Strategy B: BRICS recombination with alt scaffolds ───────────────────
    # Decompose the parent into its constituent fragments
    parent_frag_mols: list = []
    try:
        frags = BRICS.BRICSDecompose(parent_mol, minFragmentSize=3)
        for f in frags:
            fm = Chem.MolFromSmiles(f)
            if fm is not None:
                parent_frag_mols.append(fm)
    except Exception:
        pass   # no parent fragments — Strategy B still runs with alt frags alone

    for alt_smi, alt_name in ALT_SCAFFOLDS:
        if len(records) >= n_max:
            break
        alt_mol = Chem.MolFromSmiles(alt_smi)
        if alt_mol is None:
            continue

        # Decompose alt scaffold; if it doesn't cleanly decompose use it whole
        alt_frag_mols: list = []
        try:
            alt_frags = BRICS.BRICSDecompose(alt_mol, minFragmentSize=1)
            for f in alt_frags:
                fm = Chem.MolFromSmiles(f)
                if fm is not None:
                    alt_frag_mols.append(fm)
        except Exception:
            pass
        if not alt_frag_mols:
            alt_frag_mols = [alt_mol]

        combo_frags = alt_frag_mols + parent_frag_mols
        try:
            gen   = BRICS.BRICSBuild(combo_frags)
            built = list(islice(gen, 30))
        except Exception as e:
            log.debug(f"[GENERATE] Method 2 BRICSBuild ({alt_name}) failed: {e}")
            continue

        for mol in built:
            if mol is None:
                continue
            try:
                Chem.SanitizeMol(mol)
                smi = Chem.MolToSmiles(mol)
            except Exception:
                continue
            if _validate_candidate(smi, seen):
                seen.add(smi)
                records.append({
                    "smiles":        smi,
                    "method":        f"scaffold_hop:brics_{alt_name}",
                    "parent_smiles": parent_smi,
                    "art_score":     None,
                    "boltz_score":   None,
                    "target":        target_id,
                    "ts":            time.time(),
                })
                if len(records) >= n_max:
                    break

    return records


# ── Method 3: Guided Mutation ─────────────────────────────────────────────────

def guided_mutation(
    target_id: str,
    art_model,
    n_max: int = 30,
) -> list:
    """
    Guided Mutation: apply single-step FG mutations to the top-5 Boltz2
    molecules and keep only those that improve the ART pre-filter score.

    Mutations (all applied via reaction SMARTS to aromatic CH positions):
      add_OH, add_F, add_CH3, add_NH2, add_CF3, add_COOH — add FG to ring
      rem_OH, rem_F                                        — remove FG from ring

    RunReactants returns one product tuple per matching position, automatically
    generating all regioisomers.  Only products with art_score > parent_art_score
    pass the gate.

    When art_model is None (ART not yet trained), life_art.rank_candidates()
    falls back to the proxy scorer.  Mutations that improve the proxy score
    are still retained — the gate is not bypassed.

    Parameters
    ----------
    target_id : str
    art_model : fitted sklearn model or None
    n_max     : int

    Returns
    -------
    list of dicts with art_score pre-filled.
    """
    records: list = []
    try:
        from rdkit import Chem
    except ImportError:
        log.debug("[GENERATE] rdkit unavailable — skipping guided_mutation")
        return records

    top5 = _load_top_boltz(5)
    if not top5:
        log.debug("[GENERATE] Method 3: no Boltz scores yet — skipping")
        return records

    mutation_rxns = _get_mutation_rxns()
    if not mutation_rxns:
        log.debug("[GENERATE] Method 3: no compiled mutation reactions")
        return records

    # Import ART scoring — support both relative and absolute import paths
    rank_candidates = None
    try:
        from .life_art import rank_candidates         # inside adaptive package
    except ImportError:
        try:
            from adaptive.life_art import rank_candidates  # from miner root
        except ImportError:
            log.debug("[GENERATE] life_art not importable — Method 3 disabled")
            return records

    submitted = _load_submitted_smiles()
    seen      = set(submitted)

    for row in top5:
        if len(records) >= n_max:
            break
        parent_smi = row.get("smiles", "")
        parent_mol = Chem.MolFromSmiles(parent_smi)
        if parent_mol is None:
            continue

        # Baseline ART score for the parent molecule
        try:
            parent_ranked = rank_candidates([("parent", parent_smi)], art_model)
            parent_art    = parent_ranked[0][2] if parent_ranked else 0.0
        except Exception:
            parent_art = 0.0

        # Apply each mutation reaction
        for mut_name, rxn in mutation_rxns:
            if len(records) >= n_max:
                break
            try:
                products = rxn.RunReactants((parent_mol,))
            except Exception as e:
                log.debug(f"[GENERATE] mutation {mut_name} RunReactants failed: {e}")
                continue

            for product_tuple in products:
                if len(records) >= n_max:
                    break
                for prod_mol in product_tuple:
                    try:
                        Chem.SanitizeMol(prod_mol)
                        prod_smi = Chem.MolToSmiles(prod_mol)
                    except Exception:
                        continue
                    if not _validate_candidate(prod_smi, seen):
                        continue
                    # ART score gate: only keep if this mutation improves score
                    try:
                        prod_ranked = rank_candidates([("mut", prod_smi)], art_model)
                        prod_art    = prod_ranked[0][2] if prod_ranked else 0.0
                    except Exception:
                        prod_art = 0.0
                    if prod_art <= parent_art:
                        continue   # no improvement — discard
                    # Passes all gates
                    seen.add(prod_smi)
                    records.append({
                        "smiles":        prod_smi,
                        "method":        f"guided_mutation:{mut_name}",
                        "parent_smiles": parent_smi,
                        "art_score":     round(prod_art, 6),
                        "boltz_score":   None,
                        "target":        target_id,
                        "ts":            time.time(),
                    })
                    if len(records) >= n_max:
                        break

    return records


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_candidates(
    target: dict,
    art_model=None,
    n_total: int = 100,
) -> list:
    """
    Phase 4 entry point.  Runs all three generation methods, deduplicates
    across methods, ART-ranks all survivors, appends to life_generated.jsonl,
    and returns the top n_total in miner_daemon tuple format.

    Parameters
    ----------
    target    : target dict (keys: id, uniprot_id, protein_sequence, …)
    art_model : fitted sklearn RF model, or None (proxy scorer used)
    n_total   : soft cap on total candidates returned (default 100 = 50+20+30)

    Returns
    -------
    list of (label, smiles, art_score) — identical shape to life_scout output,
    ready for Boltz2 scoring in miner_daemon.  Empty list if all methods fail
    or rdkit is unavailable.
    """
    target_id = target.get("id", "unknown")
    log.info(f"[GENERATE] Phase 4 start — target={target_id}")

    all_records: list = []

    # ── Method 1: Fragment Recombination ────────────────────────────────────
    try:
        m1 = fragment_recombination(target_id, n_max=50)
        all_records.extend(m1)
        log.info(f"[GENERATE] Method 1 (fragment_recombination): {len(m1)} candidates")
    except Exception as e:
        log.warning(f"[GENERATE] Method 1 failed (non-fatal): {e}")

    # ── Method 2: Scaffold Hopping ──────────────────────────────────────────
    try:
        m2 = scaffold_hopping(target_id, n_max=20)
        all_records.extend(m2)
        log.info(f"[GENERATE] Method 2 (scaffold_hopping): {len(m2)} candidates")
    except Exception as e:
        log.warning(f"[GENERATE] Method 2 failed (non-fatal): {e}")

    # ── Method 3: Guided Mutation ───────────────────────────────────────────
    try:
        m3 = guided_mutation(target_id, art_model, n_max=30)
        all_records.extend(m3)
        log.info(f"[GENERATE] Method 3 (guided_mutation): {len(m3)} candidates")
    except Exception as e:
        log.warning(f"[GENERATE] Method 3 failed (non-fatal): {e}")

    if not all_records:
        log.info("[GENERATE] Phase 4: no candidates generated — returning empty")
        return []

    # ── Cross-method deduplication by canonical SMILES ──────────────────────
    seen_canon:     set  = set()
    unique_records: list = []
    try:
        from rdkit import Chem
        for rec in all_records:
            mol = Chem.MolFromSmiles(rec["smiles"])
            if mol is None:
                continue
            canon = Chem.MolToSmiles(mol)
            if canon not in seen_canon:
                seen_canon.add(canon)
                rec["smiles"] = canon   # normalise to canonical form
                unique_records.append(rec)
    except ImportError:
        # Fallback: deduplicate by raw SMILES string
        seen_raw: set = set()
        for rec in all_records:
            if rec["smiles"] not in seen_raw:
                seen_raw.add(rec["smiles"])
                unique_records.append(rec)

    log.info(f"[GENERATE] Unique candidates after cross-method dedup: {len(unique_records)}")

    # ── ART pre-filter: score and sort ──────────────────────────────────────
    rank_candidates = None
    try:
        from .life_art import rank_candidates
    except ImportError:
        try:
            from adaptive.life_art import rank_candidates
        except ImportError:
            pass

    if rank_candidates is not None:
        try:
            labeled = [
                (f"gen:{rec['method'][:20]}", rec["smiles"])
                for rec in unique_records
            ]
            ranked       = rank_candidates(labeled, art_model)
            art_score_map: dict = {smi: score for _, smi, score in ranked}
            for rec in unique_records:
                # Only overwrite art_score if not already set (Method 3 fills it in)
                if rec.get("art_score") is None:
                    rec["art_score"] = round(art_score_map.get(rec["smiles"], 0.0), 6)
            unique_records.sort(
                key=lambda r: r.get("art_score") or 0.0,
                reverse=True,
            )
            log.info(
                f"[GENERATE] ART pre-filter complete — "
                f"top_score={unique_records[0].get('art_score', '?'):.4f}"
                if unique_records else "[GENERATE] ART pre-filter: no records"
            )
        except Exception as e:
            log.warning(f"[GENERATE] ART pre-filter failed (non-fatal): {e}")

    # Keep top n_total
    final_records = unique_records[:n_total]

    # ── Persist to output/life_generated.jsonl ───────────────────────────────
    _append_generated(final_records)
    log.info(
        f"[GENERATE] Phase 4 complete — {len(final_records)} candidates "
        f"appended to life_generated.jsonl"
    )

    # ── Return in miner_daemon format: list[(label, smiles, art_score)] ─────
    return [
        (
            f"gen:{rec['method'][:20]}",
            rec["smiles"],
            float(rec.get("art_score") or 0.0),
        )
        for rec in final_records
    ]


# ── CLI for offline testing ────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    p = argparse.ArgumentParser(description="Life Compute molecule generator (Phase 4)")
    p.add_argument("--method",    choices=["all", "frag", "hop", "mutate"],
                   default="all", help="Which generation method to run")
    p.add_argument("--target-id", default="TP53",
                   help="Target ID for logging (default: TP53)")
    p.add_argument("--n",         type=int, default=50,
                   help="Max candidates to generate (default: 50)")
    p.add_argument("--status",    action="store_true",
                   help="Print counts from life_boltz_scores.jsonl and exit")
    args = p.parse_args()

    if args.status:
        rows = _load_top_boltz(10)
        print(f"Top Boltz scores available: {len(rows)}")
        for r in rows[:5]:
            print(f"  {r['boltz_score']:+.6f}  {r['smiles'][:60]}")
        import sys; sys.exit(0)

    dummy_target = {"id": args.target_id}

    if args.method in ("frag", "all"):
        r = fragment_recombination(args.target_id, n_max=args.n)
        print(f"\nMethod 1 — fragment_recombination: {len(r)} candidates")
        for rec in r[:3]:
            print(f"  {rec['smiles'][:70]}")

    if args.method in ("hop", "all"):
        r = scaffold_hopping(args.target_id, n_max=20)
        print(f"\nMethod 2 — scaffold_hopping: {len(r)} candidates")
        for rec in r[:3]:
            print(f"  [{rec['method']}] {rec['smiles'][:70]}")

    if args.method in ("mutate", "all"):
        r = guided_mutation(args.target_id, None, n_max=30)
        print(f"\nMethod 3 — guided_mutation: {len(r)} candidates")
        for rec in r[:3]:
            print(f"  [{rec['method']}] art={rec['art_score']}  {rec['smiles'][:60]}")

    if args.method == "all":
        results = generate_candidates(dummy_target, None, n_total=args.n)
        print(f"\ngenerate_candidates total: {len(results)}")
        for label, smi, score in results[:5]:
            print(f"  [{label}] art={score:.4f}  {smi[:60]}")
