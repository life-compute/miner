# LIFE Compute — adaptive/

LIFE Compute rewards better molecule discovery.

The tools provided here are starting points:

| Module | What it does |
|---|---|
| `life_generate.py` | Generative AI: BRICS fragment recombination, scaffold hopping, guided mutation from your best-scoring hits |
| `life_chembl.py` | Download known actives from ChEMBL as high-quality seeds; cross-reference your hits for novelty |
| `life_diversity.py` | Shannon entropy enforcement + Tanimoto deduplication — prevent your miner from rescoring the same molecule |

The default miner uses random ZINC15 sampling.
**Build your own search strategy — better algorithms find better cancer drug candidates and earn more $LIFE.**

Ideas to explore:
- Bayesian optimisation over molecular descriptors
- Reinforcement learning on Boltz2 feedback
- Genetic algorithms seeded from ChEMBL actives
- Graph neural networks trained on your accumulated scores
- Multi-target co-optimisation across the cancer target panel
