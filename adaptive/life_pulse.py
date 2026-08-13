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

Key classes
-----------
PulseState        — crash-safe JSON checkpoint (resume from any point)
AdaptiveBatchSizer — auto-scales batch size by observed throughput/success rate
EliteMutator       — neighborhood search around top-scoring molecules
TanimotoFilter     — MACCS-based deduplication gate (prevents near-duplicate bloat)
ScoreReporter      — progress reporting, ETA, and per-family summary

Output
------
output/life_pulse_data.jsonl   — one row per evaluated molecule
output/life_pulse_state.json   — resume checkpoint {next_index, seen}
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
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
        ("O=C(O)c1ccc(S(=O)(=O)Nc2ccccc2)cc1", "sulfonamide_benzoic"),
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
        ("O=C(O)c1ccc(NC(=O)c2cccc(Cl)c2)cc1", "chloro_nicotinanilide"),
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


# ── Sobol implementation (Halton / Van der Corput) ─────────────────────────────

def _vdc(n: int, base: int) -> float:
    """Van der Corput radical-inverse: int → float in (0, 1)."""
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
    """Append a decoration to the first open aromatic attachment point."""
    rg = _RGROUPS[rgroup_idx % len(_RGROUPS)]
    if not rg:
        return scaffold_smiles
    try:
        from rdkit import Chem
    except ImportError:
        return scaffold_smiles
    try:
        mol = Chem.MolFromSmiles(scaffold_smiles)
        if mol is None:
            return scaffold_smiles
        for atom in mol.GetAtoms():
            if atom.GetAtomicNum() == 6 and atom.GetIsAromatic():
                if atom.GetTotalNumHs() > 0:
                    atom_idx = atom.GetIdx()
                    rw = Chem.RWMol(mol)
                    sub = Chem.MolFromSmiles(rg)
                    if sub is None:
                        break
                    combo = Chem.CombineMols(rw, sub)
                    rw2 = Chem.RWMol(combo)
                    n_scaffold = mol.GetNumAtoms()
                    rw2.AddBond(atom_idx, n_scaffold, Chem.BondType.SINGLE)
                    smi = Chem.MolToSmiles(rw2.GetMol())
                    return smi
        return scaffold_smiles
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
        return 0.5 if smiles else 0.0
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return 0.0
        sym = {a.GetSymbol() for a in mol.GetAtoms()}
        if sym & BANNED_ATOMS:
            return 0.0
        ha = mol.GetNumHeavyAtoms()
        if not (MIN_HA <= ha <= MAX_HA):
            return 0.0
        logp = Descriptors.MolLogP(mol)
        boltz_safe = 1.0
        ha_pen = math.exp(-abs(ha / TARGET_HA - 1.0) / 0.3)
        lp_pen = math.exp(-abs(logp / max(TARGET_LOGP, 0.1) - 1.0) / 0.5)
        try:
            from rdkit.Chem import MACCSkeys
            fp   = MACCSkeys.GenMACCSKeys(mol)
            n_on = len(fp.GetOnBits())
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


def _smiles_key(smiles: str) -> str:
    return hashlib.md5(smiles.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
# PulseState — crash-safe JSON checkpoint
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PulseState:
    """
    Persistent checkpoint for the PULSE sweep.

    Attributes
    ----------
    next_index : int
        Sobol sequence index to evaluate next (monotonically incremented).
    seen : dict[str, float]
        Maps SMILES MD5-16 key → proxy_score for all molecules already written
        to the JSONL.  Used for exact-duplicate suppression.
    elite_pool : list[dict]
        Up to ``elite_capacity`` top-scoring rows kept in memory for
        EliteMutator to mine.  Not persisted across restart for simplicity
        (repopulated on first sweep run).
    total_evaluated : int
        Cumulative count of molecules written to the JSONL since the sweep
        was first created.
    tanimoto_attempts : int
        Lifetime count of TanimotoFilter.is_duplicate() calls.
    tanimoto_passes : int
        Lifetime count of calls returning False (novel molecules).
    mutant_attempted : int
        Lifetime count of EliteMutator rows generated (before filter).
    mutant_accepted : int
        Lifetime count of EliteMutator rows written to JSONL.
    current_batch_size : int
        Most recent AdaptiveBatchSizer batch size (for dashboard display).
    last_sweep_ts : float
        Unix timestamp of the last completed sweep batch (for ACTIVE/IDLE).
    """
    next_index:        int               = 0
    seen:              dict[str, float]  = field(default_factory=dict)
    elite_pool:        list[dict]        = field(default_factory=list)
    total_evaluated:   int               = 0
    elite_capacity:    int               = 50
    tanimoto_attempts: int               = 0
    tanimoto_passes:   int               = 0
    mutant_attempted:  int               = 0
    mutant_accepted:   int               = 0
    current_batch_size: int              = 200
    last_sweep_ts:     float             = 0.0

    # ── persistence ───────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path = STATE_JSON) -> "PulseState":
        """Load from JSON, returning a fresh state if the file is absent/corrupt."""
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                return cls(
                    next_index=raw.get("next_index", 0),
                    seen=raw.get("seen", {}),
                    elite_pool=raw.get("elite_pool", []),
                    total_evaluated=raw.get("total_evaluated", 0),
                    elite_capacity=raw.get("elite_capacity", 50),
                    tanimoto_attempts=raw.get("tanimoto_attempts", 0),
                    tanimoto_passes=raw.get("tanimoto_passes", 0),
                    mutant_attempted=raw.get("mutant_attempted", 0),
                    mutant_accepted=raw.get("mutant_accepted", 0),
                    current_batch_size=raw.get("current_batch_size", 200),
                    last_sweep_ts=raw.get("last_sweep_ts", 0.0),
                )
            except Exception:
                pass
        return cls()

    def save(self, path: Path = STATE_JSON) -> None:
        """Atomically write state to JSON (write-then-rename)."""
        tmp = path.with_suffix(".tmp")
        d   = asdict(self)
        d.pop("elite_pool", None)          # elite pool is ephemeral
        d["elite_pool"] = self.elite_pool  # keep serialized but trimmed
        tmp.write_text(json.dumps(d))
        tmp.replace(path)

    # ── elite pool helpers ────────────────────────────────────────────────────

    def update_elite(self, row: dict) -> None:
        """Insert a row into the elite pool if it beats the worst incumbent."""
        score = float(row.get("proxy_score", 0.0))
        if score <= 0:
            return
        self.elite_pool.append(row)
        self.elite_pool.sort(key=lambda r: float(r.get("proxy_score", 0.0)), reverse=True)
        if len(self.elite_pool) > self.elite_capacity:
            self.elite_pool = self.elite_pool[:self.elite_capacity]

    @property
    def elite_threshold(self) -> float:
        """Minimum proxy_score to beat the worst elite incumbent."""
        if not self.elite_pool:
            return 0.0
        return float(self.elite_pool[-1].get("proxy_score", 0.0))


# ══════════════════════════════════════════════════════════════════════════════
# AdaptiveBatchSizer — scales batch size by observed throughput / success rate
# ══════════════════════════════════════════════════════════════════════════════

class AdaptiveBatchSizer:
    """
    Dynamically adjusts how many molecules to target per sweep batch.

    After each batch the caller reports ``n_requested`` (how many were asked
    for) and ``n_delivered`` (how many passed all filters and were written).
    The sizer keeps a rolling window of success rates and adjusts the next
    batch size to hit ``target_rate`` yield.

    Parameters
    ----------
    initial_size   : Starting batch size.
    min_size       : Hard floor — never go below this.
    max_size       : Hard ceiling — never go above this.
    target_rate    : Desired fraction of Sobol samples that pass all filters
                     (canonical + not-seen + proxy > 0).  Default 0.5.
    window         : Rolling window of recent (requested, delivered) pairs.
    step_factor    : Multiplicative increase/decrease step per adjustment.
    """

    def __init__(
        self,
        initial_size: int   = 200,
        min_size:     int   = 20,
        max_size:     int   = 2000,
        target_rate:  float = 0.50,
        window:       int   = 5,
        step_factor:  float = 1.25,
    ) -> None:
        self._size        = initial_size
        self._min         = min_size
        self._max         = max_size
        self._target      = target_rate
        self._step        = step_factor
        self._history: deque[tuple[int, int]] = deque(maxlen=window)

    @property
    def current(self) -> int:
        """Current recommended batch size."""
        return self._size

    def report(self, n_requested: int, n_delivered: int) -> None:
        """
        Called after each batch.  Updates rolling rate and adjusts batch size.

        Parameters
        ----------
        n_requested : Total Sobol samples iterated (including skipped).
        n_delivered : Molecules that passed all filters and were written.
        """
        if n_requested <= 0:
            return
        self._history.append((n_requested, n_delivered))
        total_req = sum(r for r, _ in self._history)
        total_del = sum(d for _, d in self._history)
        rate = total_del / total_req if total_req > 0 else 0.5
        if rate < self._target * 0.8:
            # Yield too low → shrink to avoid wasting iterations
            self._size = max(self._min, int(self._size / self._step))
        elif rate > self._target * 1.2:
            # Yield high → grow to explore faster
            self._size = min(self._max, int(self._size * self._step))

    def status(self) -> dict:
        """Return a summary dict for logging."""
        total_req = sum(r for r, _ in self._history) or 1
        total_del = sum(d for _, d in self._history)
        return {
            "batch_size":     self._size,
            "rolling_rate":   round(total_del / total_req, 3),
            "target_rate":    self._target,
            "window_batches": len(self._history),
        }


# ══════════════════════════════════════════════════════════════════════════════
# EliteMutator — neighborhood exploration around top-scoring molecules
# ══════════════════════════════════════════════════════════════════════════════

class EliteMutator:
    """
    Generates structural neighbours of elite molecules to exploit high-scoring
    regions of chemical space, complementing the Sobol global sweep.

    Strategy
    --------
    For each elite row, try swapping its R-group to each of ``_RGROUPS``
    and returning the decorated SMILES if it hasn't been seen before.  This
    is a simple local perturbation; future versions may add atom substitution
    or ring-opening mutations via RDKit.

    Parameters
    ----------
    n_mutations : Number of mutant SMILES to generate per call to ``generate``.
    """

    def __init__(self, n_mutations: int = 30) -> None:
        self._n = n_mutations

    def generate(
        self,
        state:  PulseState,
        filter: "TanimotoFilter",
    ) -> list[dict]:
        """
        Return up to ``n_mutations`` new (not-seen, not-similar) SMILES rows
        derived from the current elite pool.  Rows have the same schema as
        PULSE JSONL rows with ``source="mutant"``.
        """
        results: list[dict] = []
        if not state.elite_pool:
            return results
        for elite_row in state.elite_pool:
            if len(results) >= self._n:
                break
            scaffold_smi = elite_row.get("scaffold_smi", elite_row.get("smiles", ""))
            for rg_idx in range(len(_RGROUPS)):
                if len(results) >= self._n:
                    break
                smiles = _decorate(scaffold_smi, rg_idx)
                canon  = _canonical(smiles)
                if canon is None:
                    continue
                key = _smiles_key(canon)
                if key in state.seen:
                    continue
                if filter.is_duplicate(canon):
                    continue
                score = proxy_score(canon)
                if score <= 0:
                    continue
                row = {
                    "source":        "mutant",
                    "sobol_idx":     -1,
                    "family":        elite_row.get("family", "general"),
                    "scaffold_name": elite_row.get("scaffold_name", ""),
                    "scaffold_smi":  scaffold_smi,
                    "rgroup_idx":    rg_idx,
                    "smiles":        canon,
                    "proxy_score":   round(score, 6),
                    "ts":            time.time(),
                    "parent_score":  elite_row.get("proxy_score"),
                }
                results.append(row)
        return results


# ══════════════════════════════════════════════════════════════════════════════
# TanimotoFilter — MACCS-based near-duplicate gate
# ══════════════════════════════════════════════════════════════════════════════

class TanimotoFilter:
    """
    Prevents near-duplicate molecules from entering the PULSE pool.

    Uses MACCS 167-bit keys and Tanimoto similarity.  A molecule is rejected
    if its Tanimoto to any molecule already in the filter exceeds
    ``threshold``.

    Falls back to exact MD5-key deduplication when RDKit is unavailable.

    Parameters
    ----------
    threshold    : Tanimoto cutoff (default 0.85 — 85% structural similarity).
    max_fp_cache : Maximum fingerprints to keep in memory (LRU-like: oldest
                   entries are dropped when the cache exceeds this limit).
    """

    def __init__(self, threshold: float = 0.85, max_fp_cache: int = 5000) -> None:
        self._threshold  = threshold
        self._max_cache  = max_fp_cache
        self._fps:  list  = []   # list of RDKit ExplicitBitVect
        self._keys: deque = deque()  # parallel MD5 keys for fallback tracking

    def is_duplicate(self, canon_smiles: str) -> bool:
        """
        Return True if ``canon_smiles`` is too similar to any stored molecule.
        Also registers the molecule in the cache (side effect: always call
        *before* adding to state.seen when you want the filter to learn it).
        """
        try:
            from rdkit import Chem
            from rdkit.Chem import MACCSkeys, DataStructs
            mol = Chem.MolFromSmiles(canon_smiles)
            if mol is None:
                return False
            fp = MACCSkeys.GenMACCSKeys(mol)
            # Check similarity against stored fingerprints
            for stored_fp in self._fps:
                sim = DataStructs.TanimotoSimilarity(fp, stored_fp)
                if sim >= self._threshold:
                    return True
            # Novel — store it
            self._fps.append(fp)
            key = _smiles_key(canon_smiles)
            self._keys.append(key)
            if len(self._fps) > self._max_cache:
                self._fps.pop(0)
                self._keys.popleft()
            return False
        except ImportError:
            # No RDKit: fall back to exact key
            key = _smiles_key(canon_smiles)
            if key in self._keys:
                return True
            self._keys.append(key)
            if len(self._keys) > self._max_cache:
                self._keys.popleft()
            return False
        except Exception:
            return False

    @property
    def size(self) -> int:
        return len(self._fps) or len(self._keys)


# ══════════════════════════════════════════════════════════════════════════════
# ScoreReporter — progress reporting and per-family summary
# ══════════════════════════════════════════════════════════════════════════════

class ScoreReporter:
    """
    Formats and prints progress updates during the PULSE sweep.

    Parameters
    ----------
    report_every : Print a progress line every N molecules evaluated.
    """

    def __init__(self, report_every: int = 50) -> None:
        self._every     = report_every
        self._start_ts  = time.time()
        self._last_n    = 0
        self._family_counts: dict[str, int]   = {}
        self._family_scores: dict[str, float] = {}

    def record(self, row: dict) -> None:
        """Register an evaluated row."""
        fam   = row.get("family", "unknown")
        score = float(row.get("proxy_score", 0.0))
        self._family_counts[fam] = self._family_counts.get(fam, 0) + 1
        self._family_scores[fam] = self._family_scores.get(fam, 0.0) + score

    def maybe_print(self, state: PulseState, sizer: AdaptiveBatchSizer) -> bool:
        """
        Print a progress line if enough new molecules have been evaluated since
        the last report.  Returns True if a line was printed.
        """
        n = state.total_evaluated
        if n - self._last_n < self._every:
            return False
        self._last_n = n
        elapsed = time.time() - self._start_ts
        rate    = n / max(elapsed, 1)
        top     = state.elite_pool[0].get("proxy_score", 0) if state.elite_pool else 0
        print(
            f"[PULSE] n={n:>6}  idx={state.next_index:>7}  "
            f"seen={len(state.seen):>6}  "
            f"top_proxy={float(top):.4f}  "
            f"rate={rate:.1f}/s  "
            f"batch={sizer.current}"
        )
        return True

    def summary(self, state: PulseState) -> None:
        """Print a final per-family summary."""
        print(f"\n{'─'*60}")
        print(f"[PULSE] Final summary  total={state.total_evaluated}  "
              f"seen={len(state.seen)}  idx={state.next_index}")
        for fam in FAMILY_NAMES:
            cnt = self._family_counts.get(fam, 0)
            if cnt == 0:
                continue
            avg = self._family_scores.get(fam, 0.0) / cnt
            print(f"  {fam:20s}  n={cnt:>5}  avg_proxy={avg:.4f}")
        if state.elite_pool:
            print(f"\n  Top {min(5, len(state.elite_pool))} elite molecules:")
            for i, row in enumerate(state.elite_pool[:5], 1):
                print(f"    {i}. [{row.get('family','?'):15s}] "
                      f"proxy={float(row.get('proxy_score',0)):.4f}  "
                      f"{row.get('smiles','')[:60]}")
        print(f"{'─'*60}")


# ══════════════════════════════════════════════════════════════════════════════
# Main sweep
# ══════════════════════════════════════════════════════════════════════════════

def run_sweep(
    max_configs:   int            = 200,
    family_filter: Optional[str]  = None,
    verbose:       bool           = True,
    max_attempts:  int            = 0,
    tanimoto_threshold: float     = 0.85,
    elite_fraction: float         = 0.10,
    use_mutants:   bool           = True,
) -> None:
    """
    Sample ``max_configs`` molecules quasi-randomly from the vocab, score with
    proxy, append rows to life_pulse_data.jsonl.

    Parameters
    ----------
    max_configs        : Maximum molecules to evaluate this run.
    family_filter      : If set, restrict to that protein family only.
    verbose            : Print progress and summary.
    max_attempts       : Hard iteration cap (0 = auto: max_configs * 50 or 5_000).
    tanimoto_threshold : TanimotoFilter cutoff (default 0.85).
    elite_fraction     : After Sobol sweep, fill up to this fraction of
                         max_configs with EliteMutator molecules.
    use_mutants        : Whether to run EliteMutator after the Sobol phase.
    """
    state    = PulseState.load(STATE_JSON)
    tfilter  = TanimotoFilter(threshold=tanimoto_threshold)
    sizer    = AdaptiveBatchSizer(initial_size=max_configs)
    reporter = ScoreReporter(report_every=max(10, max_configs // 10))
    mutator  = EliteMutator(n_mutations=max(10, int(max_configs * elite_fraction)))

    _max_attempts = max_attempts if max_attempts > 0 else max(max_configs * 50, 5000)
    evaluated     = 0
    attempts      = 0
    idx           = state.next_index

    # ── Phase 1: Sobol sweep ──────────────────────────────────────────────────
    while evaluated < max_configs:
        attempts += 1
        if attempts > _max_attempts:
            if verbose:
                print(
                    f"[PULSE] capped at {_max_attempts} iterations "
                    f"(evaluated={evaluated}/{max_configs}  seen={len(state.seen)})"
                )
            break

        fam_f = sobol_float(idx, 0)
        if family_filter and family_filter in FAMILY_VOCAB:
            fam_name = family_filter
        else:
            fam_name = FAMILY_NAMES[int(fam_f * _N_FAMILIES) % _N_FAMILIES]

        vocab        = FAMILY_VOCAB[fam_name]
        scaf_idx     = int(sobol_float(idx, 1) * len(vocab)) % len(vocab)
        rg_idx       = int(sobol_float(idx, 2) * len(_RGROUPS)) % len(_RGROUPS)
        scaffold_smi, scaffold_name = vocab[scaf_idx]

        smiles = _decorate(scaffold_smi, rg_idx)
        canon  = _canonical(smiles)
        if canon is None:
            idx += 1
            continue

        key = _smiles_key(canon)
        state.tanimoto_attempts += 1
        is_dup = key in state.seen or tfilter.is_duplicate(canon)
        if not is_dup:
            state.tanimoto_passes += 1
        if is_dup:
            idx += 1
            continue

        score = proxy_score(canon)
        if score <= 0:
            idx += 1
            continue

        row = {
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

        state.seen[key]      = round(score, 4)
        state.next_index     = idx + 1
        state.total_evaluated += 1
        state.update_elite(row)
        reporter.record(row)
        state.current_batch_size = sizer.current
        state.last_sweep_ts = time.time()
        state.save(STATE_JSON)

        idx       += 1
        evaluated += 1
        reporter.maybe_print(state, sizer)

    sizer.report(n_requested=attempts, n_delivered=evaluated)

    # ── Phase 2: EliteMutator ─────────────────────────────────────────────────
    if use_mutants and state.elite_pool:
        mutant_target = max(10, int(max_configs * elite_fraction))
        mutants = mutator.generate(state=state, filter=tfilter)
        state.mutant_attempted += len(mutants)
        written = 0
        for row in mutants[:mutant_target]:
            key   = _smiles_key(row["smiles"])
            if key in state.seen:
                continue
            with PULSE_JSONL.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            state.seen[key]       = round(float(row.get("proxy_score", 0.0)), 4)
            state.total_evaluated += 1
            state.update_elite(row)
            reporter.record(row)
            written += 1
        state.mutant_accepted += written
        state.save(STATE_JSON)
        if verbose and written:
            print(f"[PULSE] +{written} mutant molecules from elite pool")

    if verbose:
        reporter.summary(state)


# ── Public helpers (used by miner_daemon.py) ───────────────────────────────────

def get_next_candidates(
    n:             int           = 50,
    family_filter: Optional[str] = None,
) -> list[dict]:
    """
    Return up to n pulse rows with proxy_score > 0 that have not yet been
    Boltz2-scored (no boltz_score field or boltz_score is None).

    Rows are sorted by proxy_score descending.
    Used by Phase 1 of the epoch loop in miner_daemon.py.
    """
    if not PULSE_JSONL.exists():
        return []
    rows: list[dict] = []
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


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Life Compute PULSE sweep")
    p.add_argument("--max-configs", type=int, default=200,
                   help="Molecules to evaluate this run (default 200)")
    p.add_argument("--family", default=None,
                   help=f"Restrict to family: {FAMILY_NAMES}")
    p.add_argument("--no-mutants", action="store_true",
                   help="Skip EliteMutator phase")
    p.add_argument("--tanimoto", type=float, default=0.85,
                   help="TanimotoFilter threshold (default 0.85)")
    p.add_argument("--status", action="store_true",
                   help="Print current state and exit without running GATK")
    args = p.parse_args()

    if args.status:
        s = PulseState.load()
        print(f"next_index={s.next_index}  seen={len(s.seen)}  "
              f"total_evaluated={s.total_evaluated}  elite={len(s.elite_pool)}")
        print(f"Families: {FAMILY_NAMES}")
        for fam, vocab in FAMILY_VOCAB.items():
            print(f"  {fam:20s}: {len(vocab)} scaffolds × {len(_RGROUPS)} R-groups "
                  f"= {len(vocab)*len(_RGROUPS)} combinations")
    else:
        run_sweep(
            max_configs=args.max_configs,
            family_filter=args.family,
            tanimoto_threshold=args.tanimoto,
            use_mutants=not args.no_mutants,
        )
