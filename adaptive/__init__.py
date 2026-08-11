"""
adaptive/ — Life Compute adaptive molecular search stack.

Analogous to nova_adaptive/ but operates on ZINC15/SAVI molecular SMILES
space instead of reaction combinatorial DB.  All four modules are independent
and can be imported without Boltz2 or Solana present.

Modules
-------
life_pulse     Sobol quasi-random sweep over ZINC15/SAVI molecular vocabulary.
life_art       RandomForest affinity predictor trained on Morgan fingerprints
               + real Boltz2 scores.
life_scout     Protein-family-aware candidate routing and batch generation.
life_diversity Shannon entropy enforcement + Tanimoto deduplication.
"""
from .life_pulse     import run_sweep, get_next_candidates, proxy_score
from .life_art       import train as art_train, rank_candidates, load_model
from .life_scout     import get_focused_candidates, detect_protein_family
from .life_diversity import (
    SubmissionMemory,
    greedy_diverse_select,
    batch_shannon_entropy,
    is_novel,
)

__all__ = [
    "run_sweep", "get_next_candidates", "proxy_score",
    "art_train", "rank_candidates", "load_model",
    "get_focused_candidates", "detect_protein_family",
    "SubmissionMemory", "greedy_diverse_select", "batch_shannon_entropy", "is_novel",
]
