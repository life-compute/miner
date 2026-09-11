# LIFE COMPUTE
## Decentralized Cancer Drug Discovery Network

**White Paper — Version 1.1 — August 2026**

[github.com/life-compute](https://github.com/life-compute) | lifecompute.ai

---

> *"Your GPU could help cure cancer. Earn $LIFE tokens."*

---

## Abstract

LIFE Compute is a decentralized, Solana-based network that harnesses idle GPU compute power worldwide to accelerate cancer drug discovery. Miners contribute their graphics processing units to screen billions of molecular candidates against **30** validated cancer protein targets using Boltz2, a state-of-the-art structure-based molecular docking model. In return, miners earn **$LIFE tokens** — a fixed-supply cryptocurrency minted exclusively through real scientific work, with zero pre-mine and zero team allocation.

Every $LIFE token in existence represents a genuine contribution to cancer research. The network is designed to be accessible to anyone — a three-step setup process allows non-technical users to begin contributing within minutes. Results are stored openly on-chain and contributed to the global scientific community.

LIFE Compute addresses two urgent problems simultaneously: the computational bottleneck in early-stage drug discovery, and the lack of meaningful utility in cryptocurrency mining. By aligning economic incentives with humanitarian goals, LIFE Compute creates a self-sustaining ecosystem where mining is literally saving lives.

---

## Table of Contents

1. [The Problem](#1-the-problem)
2. [The Solution: LIFE Compute](#2-the-solution-life-compute)
3. [The $LIFE Token](#3-the-life-token)
   - 3.1 Token Economics
   - 3.2 Mining Rewards
   - 3.3 Per-Target Maturity Taper
   - 3.4 Why This Model Works
4. [Cancer Target Portfolio](#4-cancer-target-portfolio)
5. [Technical Architecture](#5-technical-architecture)
6. [Getting Started — Three Steps](#6-getting-started--three-steps)
7. [Roadmap](#7-roadmap)
8. [Open Source Commitment](#8-open-source-commitment)
9. [Conclusion](#9-conclusion)

---

## 1. The Problem

### 1.1 Cancer Remains the World's Greatest Medical Challenge

Cancer kills over 10 million people annually — nearly 1 in 6 deaths worldwide. Despite decades of research and billions in funding, the drug discovery pipeline remains critically bottlenecked at the earliest stage: identifying which of the billions of possible drug-like molecules might bind to a cancer-causing protein.

The SAVI-2020 library alone contains 1.75 billion synthesizable molecules. Screening even a fraction of this space against a single protein target using traditional computational methods requires resources available only to the largest pharmaceutical companies and research institutions. Most promising molecules are never screened at all.

### 1.2 GPU Mining Has No Real-World Utility

Billions of dollars worth of GPU compute is spent annually on cryptocurrency mining that produces nothing of scientific or social value — solving arbitrary mathematical puzzles whose only purpose is securing a ledger. This represents one of the largest misallocations of computational resources in human history.

Meanwhile, cancer researchers at universities and hospitals worldwide lack the compute they need to run the molecular screening pipelines that could identify the next breakthrough drug. The resources exist — they are simply pointed at the wrong problem.

### 1.3 Drug Discovery Infrastructure is Centralized and Inaccessible

Current computational drug discovery platforms cost tens of thousands of dollars per year and require specialized expertise to operate. The scientific community has no mechanism to mobilize the global pool of consumer GPU hardware toward drug discovery.

---

## 2. The Solution: LIFE Compute

LIFE Compute solves both problems with a single, elegant mechanism: reward GPU owners in $LIFE tokens for running real molecular docking calculations against validated cancer targets. The more useful work a miner contributes, the more $LIFE they earn. The harder the target, the greater the reward.

### 2.1 How It Works

1. Miners download the LIFE Compute miner software in three steps
2. The software connects to the Solana blockchain and pulls the current cancer target assignment
3. Boltz2 runs locally on the miner's GPU, scoring molecule candidates against the target protein
4. High-affinity molecule candidates are submitted to the Solana program
5. Validators verify the Boltz2 scores are genuine
6. $LIFE tokens are minted directly to the miner's wallet — a flat amount set by target difficulty
7. Results are stored publicly on-chain and in the `life-compute/targets` repository

### 2.2 Scientific Validity

LIFE Compute uses **Boltz2** — developed jointly by MIT and Recursion Pharmaceuticals — as its scoring oracle. Boltz2 is a structure-based molecular docking and co-folding model that predicts how small molecules interact with protein targets using 3D structural information, providing physics-level accuracy in affinity prediction.

This is the same technology used by professional computational chemists and validated against experimental binding data across thousands of protein-ligand pairs. Molecules flagged as hits by the network are genuinely worth pursuing in laboratory validation.

---

## 3. The $LIFE Token

$LIFE is a **proof-of-useful-work** cryptocurrency. It cannot be purchased in an ICO, pre-mined by the team, or allocated to investors. The only way to obtain $LIFE is to contribute genuine GPU compute to cancer drug discovery.

### 3.1 Token Economics

| Property | Value |
|----------|-------|
| Total Supply | **Uncapped** — a live figure: total minted minus total burned |
| Emission | Flat per-tier rewards; no halving, no schedule |
| Pre-mine | Zero |
| Team Allocation | Zero |
| Investor Allocation | Zero |
| Blockchain | Solana |
| Token Standard | SPL Token (freely transferable) |

> There is no ceiling on $LIFE. There is only the work. Every token in existence is proof that a real computation happened — a real GPU, a real target, a real independently-verified result. Supply does not stop at a number. It stops when there is nothing left to discover.

**Total supply is defined, at every moment, as total minted minus total burned.**
It is a live, running figure — not a historical total and not a target. It rises
only when real work is verified and mints new $LIFE through the program's
official reward-payout path, at a flat rate with no time-based reduction. There
is no fixed cap and no artificial ceiling.

> $LIFE can only ever be created by real, verified work — running inference against real cancer targets, independently confirmed by a second machine. There is no other way for new $LIFE to come into existence. Once earned, it can be freely held or traded — but no one can ever buy their way into being the one who discovered it.

$LIFE is earn-only **at the source**. The mint authority is a program-derived
address; the only instruction that can create new tokens is the reward payout
triggered by an independently confirmed result. Once earned, $LIFE is an
ordinary SPL token: no transfer hook, no burn-on-transfer, no restriction on
trading. A miner may hold or sell freely on any Solana DEX. A non-miner can only
ever obtain $LIFE by buying it from someone who actually earned it — never by
earning it without doing the work, and never through any other minting path. The
market redistributes already-earned tokens; it never creates new supply.

### 3.2 Mining Rewards

Rewards scale with the scientific difficulty of the target, incentivizing miners to focus compute on the hardest, most important problems. Rates are **flat** — they do not decrease over time or with cumulative supply:

| Difficulty | Reward | Description |
|-----------|--------|-------------|
| Easy | 0.3 $LIFE | Well-characterized binding pocket |
| Medium | 0.7 $LIFE | Partial structural data available |
| Hard | 0.9 $LIFE | Novel, poorly characterized target |
| mRNA silencing | 0.9 $LIFE | Always Hard tier |
| CRISPR gRNA | 0.252 $LIFE | Knockout targets (CPU-scored) |
| Reference compound | 0.108 $LIFE | Known-binder control |
| Discovery Bonus | 100 $LIFE | Top affinity score for a target that week |

These rates were set from production measurement: at realistic multi-miner scale
(~2,000 miners submitting three results per epoch) the earlier 25/5/1 scale
emitted roughly 157,500 $LIFE per epoch — an unsustainable rate. The Hard tier
was reduced to 0.9 $LIFE and every other tier scaled to preserve relativities.
CRISPR is held below Hard because it is CPU-scored and must not out-price full
GPU inference work.

### 3.3 Per-Target Maturity Taper

There is no halving. The only reduction applied to a reward is a **per-target
maturity taper**, and it is not a monetary schedule — it reflects diminishing
scientific return. The thousandth confirmed hit on a target narrows the search
far less than the first, so the reward tapers with how well-explored that
specific target already is:

| Verified Hits on Target | Reward Multiplier |
|------------------------|-------------------|
| 0 – 99 hits | 100% of tier reward |
| 100 – 999 hits | 75% of tier reward |
| 1,000+ hits | 50% of tier reward |

**Example:** A Hard target (0.9 $LIFE) with 150 verified hits:
> 0.9 × 0.75 = **0.675 $LIFE per confirmed hit**

Early miners on fresh targets earn the most. As a target is well-explored,
rewards taper naturally — mirroring how scientific value concentrates at the
frontier. This taper is a property of the science, not of the token.

### 3.4 Why This Model Works

Every $LIFE token represents real scientific work. Each token is a permanent
record of a computation that actually ran and was independently confirmed.
Because supply is uncapped, the token's meaning does not come from scarcity —
it comes from provenance. There is no number at which discovery is declared
finished.

> This does not mean $LIFE cures disease. It means $LIFE only exists where real computational science — narrowing the search for what might — has genuinely happened.

Unlike proof-of-work mining where difficulty increases arbitrarily, LIFE Compute's difficulty is intrinsic — it reflects the genuine scientific challenge of finding high-affinity molecules for specific cancer proteins. The network gets harder to mine in exactly the ways that advance science.

This design means early miners on fresh targets earn the most, creating a competitive incentive to discover binding molecules for newly added cancer proteins. As a target is well-explored and the network matures, rewards taper naturally — mirroring how scientific value concentrates at the frontier.

---

## 4. Cancer Target Portfolio

LIFE Compute launches with **30** validated cancer protein targets, curated from the most clinically significant and computationally tractable proteins in the oncology literature. Targets are organized into three reward tiers based on structural complexity, mutation heterogeneity, and the difficulty of finding high-affinity candidates.

### Hard Tier — 0.9 $LIFE per hit

Targets with novel or poorly characterized binding pockets, high mutation heterogeneity, or historically deemed "undruggable." These represent the frontier of computational drug discovery.

| Target | UniProt | Cancer Type | Significance |
|--------|---------|-------------|--------------|
| **TP53** | P04637 | Pan-cancer | Most mutated gene in human cancer (50% of all cases) |
| **BRCA1** | P38398 | Breast/Ovarian | Hereditary cancer suppressor, 1 in 400 carriers |
| **KRAS** | P01116 | Pancreatic/Lung | Undruggable for decades, mutated in 85% of pancreatic cancers |
| **BRAF** | P15056 | Melanoma/Colorectal | V600E driver in 50% of melanoma; vemurafenib target |
| **PTEN** | P60484 | Glioblastoma/Prostate | Tumor suppressor lost in 30% of glioblastomas |
| **MYC** | P01106 | Pan-cancer | Proto-oncogene amplified across diverse cancers; master transcription factor |
| **STAT3** | P40763 | AML/Lymphoma | Transcription factor oncogenic in 70% of solid tumors |
| **IDH1** | O75874 | Glioma/AML | Mutated in 70–80% of low-grade gliomas; enasidenib/ivosidenib target |
| **FLT3** | P36888 | AML | FLT3-ITD mutations in 25–30% of AML; midostaurin/gilteritinib target |
| **SMAD4** | Q13485 | Pancreatic/Colorectal | TGF-β pathway mediator; lost in 55% of pancreatic cancers |
| **APC** | P25054 | Colorectal | Gatekeeper tumor suppressor; mutated in 80% of colorectal cancers |

### Medium Tier — 0.7 $LIFE per hit

Targets with partial structural data and validated drug candidates, offering meaningful scientific value with tractable binding pockets.

| Target | UniProt | Cancer Type | Significance |
|--------|---------|-------------|--------------|
| **EGFR** | P00533 | Lung | Driver mutation in 15% of lung adenocarcinoma |
| **HER2** | P04626 | Breast | Amplified in 20% of breast cancers, poor prognosis |
| **BCL2** | P10415 | Lymphoma/Leukemia | Apoptosis regulator; validated venetoclax target |
| **PD-L1** | Q9NZQ7 | Immunotherapy | Checkpoint inhibitor; atezolizumab/durvalumab target |
| **MDM2** | Q00987 | Pan-cancer | p53 suppressor, amplified in 7% of all cancers |
| **PIK3CA** | P42336 | Breast/Colorectal | Most commonly mutated PI3K isoform; alpelisib target |
| **MTOR** | P42345 | Kidney/Breast | Central growth/metabolism node; everolimus target |
| **FGFR1** | P11362 | Bladder/Lung | FGFR1 amplification in 20% of squamous lung cancer |
| **RET** | P07949 | Thyroid/Lung | RET fusions in papillary thyroid cancer and NSCLC; selpercatinib target |
| **AR** | P10275 | Prostate | Androgen receptor; enzalutamide/abiraterone target |
| **PARP1** | P09874 | Breast/Ovarian | DNA repair enzyme; olaparib/niraparib target in BRCA-mutated cancers |
| **JAK2** | O60674 | MPN/AML | JAK2 V617F in 95% of polycythemia vera; ruxolitinib target |
| **ESR1** | P03372 | Breast | Estrogen receptor; endocrine-resistant breast cancer driver |
| **HDAC1** | Q13547 | Lymphoma/Solid Tumors | Histone deacetylase; epigenetic regulator; vorinostat/romidepsin target |
| **HDAC2** | Q92769 | Hematologic | Class I HDAC; pan-HDAC inhibitor target in hematologic cancers |
| **ABL1** | P00519 | CML | BCR-ABL fusion in 95% of CML; imatinib/dasatinib target |

### Easy Tier — 0.3 $LIFE per hit

Well-characterized targets with known binding pockets and approved small-molecule drugs. Ideal for new miners calibrating their setup.

| Target | UniProt | Cancer Type | Significance |
|--------|---------|-------------|--------------|
| **CDK4** | P11802 | Multiple | Cell cycle driver; palbociclib target |
| **VEGFR** | P35968 | Angiogenesis | Tumor blood vessel formation; bevacizumab target |
| **NTRK1** | Q16288 | Pan-cancer | TRK fusion oncogene; larotrectinib/entrectinib target |

---

## 5. Technical Architecture

### 5.1 Solana Program (life-compute/core)

The LIFE Compute smart contract is written in Rust using the Anchor framework and deployed on Solana. The program manages six state accounts:

- `NetworkConfig` — global configuration, total $LIFE minted (live running figure), epoch parameters
- `TargetAccount` — per-protein target data, current best score, weekly winner, confirmed hit count
- `MinerAccount` — per-miner statistics, total $LIFE earned, submission history
- `JobAccount` — active job assignments linking miners to targets
- `SubmissionAccount` — individual molecule submission with Boltz2 score
- `ValidatorAccount` — registered validator credentials and stake

Nine on-chain instructions handle the full lifecycle: `initialize`, `register_miner`, `register_validator`, `assign_job`, `submit_result`, `validate_result`, `mint_reward`, `claim_discovery_bonus`, and `update_target`.

The `mint_reward` instruction computes the payout automatically: reward = flat_tier_reward × hit_count_multiplier (see Section 3.3). There is no supply cap and no halving — the only reduction is the per-target maturity taper.

**Program ID:** `3dYbT2egotmpGBoLZe2pytsraffxre7V5dySsTKgxYiC`

### 5.2 Miner Software (life-compute/miner)

The miner daemon is a Python application packaged as a Docker container. It handles the complete workflow autonomously:

- Pulls current target assignments from the Solana program
- Downloads protein sequences from the `life-compute/targets` database
- Samples candidate molecules from the ZINC15 drug-like subset (~10M compounds)
- Runs Boltz2 affinity prediction locally on the miner's GPU
- Submits results meeting the affinity threshold to the Solana program
- Monitors $LIFE rewards in real time via a local React dashboard at `http://localhost:3000`

### 5.3 Validator Node (life-compute/validator)

Validator nodes are operated from the [`life-compute/validator`](https://github.com/life-compute/validator) repository and form the network's decentralized verification layer. When a miner submits a result, three validators are randomly selected from the registered pool. A **2-of-3 consensus** is required to confirm a score — meaning at least two of the three validators must independently reproduce the Boltz2 affinity prediction within a 5% tolerance before a $LIFE reward is minted.

Each validator re-runs **Boltz2** locally on the submitted molecule–target pair, using the same open-weight model version pinned in the `NetworkConfig` account. This ensures deterministic re-scoring: given identical inputs and model weights, Boltz2 produces numerically consistent outputs that can be verified without access to the original miner's hardware.

Validators earn a small commission on each validation they participate in. In Phase 1, validators are operated by the LIFE Compute foundation. Phase 2 will open validation to any party staking a minimum $LIFE amount, creating a fully decentralized verification network.

### 5.4 Infrastructure

| Component | Technology |
|-----------|-----------|
| Blockchain | Solana mainnet |
| Scoring Oracle | Boltz2 (MIT/Recursion, open weights) |
| Molecule Library | ZINC15 drug-like subset + SAVI-2020 |
| Target Database | life-compute/targets (open source) |
| Validator Software | life-compute/validator (open source) |
| Miner Software | Docker (Ubuntu + Windows) |
| Epoch Length | 24 hours |

---

## 6. Getting Started — Three Steps

LIFE Compute is designed to be accessible to anyone with a compatible GPU. The entire setup takes under five minutes:

### Step 1 — Download

```bash
curl -sSL https://raw.githubusercontent.com/life-compute/miner/main/install.sh | bash
```

### Step 2 — Connect your Solana wallet

```bash
~/.life-compute/bin/life-compute wallet connect
```

### Step 3 — Start mining

```bash
docker run -d --gpus all --name life-compute-miner \
  -v ~/.life-compute:/root/.life-compute \
  ghcr.io/life-compute/miner:latest
```

The miner dashboard opens at **http://localhost:3000** showing real-time statistics: molecules screened, $LIFE earned, cancer targets contributed to, and global network stats.

### Minimum Hardware Requirements

| Requirement | Minimum |
|-------------|---------|
| GPU | NVIDIA RTX 3060 or newer, 8GB+ VRAM |
| RAM | 16GB system RAM |
| OS | Ubuntu 20.04+ or Windows 10/11 |
| Software | Docker |
| Internet | 100 Mbps |
| Wallet | Any Solana-compatible wallet (Phantom, Solflare) |

---

## 7. Roadmap

### Phase 1 — Foundation (Q3 2026)
- [x] Solana program written and compiled
- [x] Miner software released for Ubuntu + Windows
- [x] 30 cancer targets active
- [x] GitHub repositories published
- [ ] Mainnet deployment
- [ ] lifecompute.ai launch
- [ ] Foundation-operated validators live

### Phase 2 — Decentralization (Q4 2026)
- [ ] Open validator registration with $LIFE staking
- [ ] Governance: miners vote on new cancer targets
- [ ] TREAT-1 integration for monoamine transporter targets
- [ ] TREAT-2 integration for HDAC targets
- [ ] Generative molecule sampling (REINVENT integration)
- [ ] Windows installer with GUI

### Phase 3 — Scientific Integration (2027)
- [ ] Partnership with academic cancer research institutions
- [ ] Top hits validated in wet lab experiments
- [ ] Results published in peer-reviewed journals
- [ ] Expansion to 50+ cancer targets
- [ ] Cross-chain bridges for wider accessibility

---

## 8. Open Source Commitment

All LIFE Compute code is open source and publicly available on GitHub under the MIT License:

| Repository | Contents |
|-----------|----------|
| [life-compute/core](https://github.com/life-compute/core) | Solana smart contracts, $LIFE token |
| [life-compute/miner](https://github.com/life-compute/miner) | Miner software, dashboard, installer |
| [life-compute/validator](https://github.com/life-compute/validator) | Validator node software, consensus logic |
| [life-compute/targets](https://github.com/life-compute/targets) | Cancer target database |

All molecular screening results submitted to the network are stored publicly on-chain and in the targets repository. Any researcher, institution, or pharmaceutical company may access and build upon these results freely. **LIFE Compute does not assert intellectual property claims over discovered molecules — findings belong to humanity.**

---

## 9. Conclusion

LIFE Compute represents a fundamental reorientation of cryptocurrency mining — from arbitrary computation toward humanity's most urgent medical challenges. By creating a direct economic incentive for GPU owners to contribute to cancer drug discovery, LIFE Compute mobilizes a previously untapped pool of computational resources for science.

The mathematics are compelling: millions of consumer GPUs worldwide, coordinated toward a single purpose, represent a computational force orders of magnitude larger than any single research institution or pharmaceutical company can deploy. LIFE Compute makes this coordination possible through economic incentives that are simultaneously fair, transparent, and scientifically meaningful.

Every $LIFE token mined is a permanent record of one person's GPU working toward a cure. Every molecule screened is one more data point in humanity's fight against cancer. Every miner is a researcher.

---

> **Join the network. Your GPU could help cure cancer.**
>
> [github.com/life-compute](https://github.com/life-compute) | lifecompute.ai

---

*LIFE Compute — Version 1.1 — August 2026*
