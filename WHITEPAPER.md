# LIFE COMPUTE
## Decentralized Cancer Drug Discovery Network

**White Paper — Version 1.0 — August 2026**

[github.com/life-compute](https://github.com/life-compute) | lifecompute.ai

---

> *"Your GPU could help cure cancer. Earn $LIFE tokens."*

---

## Abstract

LIFE Compute is a decentralized, Solana-based network that harnesses idle GPU compute power worldwide to accelerate cancer drug discovery. Miners contribute their graphics processing units to screen billions of molecular candidates against validated cancer protein targets using Boltz2, a state-of-the-art structure-based molecular docking model. In return, miners earn **$LIFE tokens** — a fixed-supply cryptocurrency minted exclusively through real scientific work, with zero pre-mine and zero team allocation.

Every $LIFE token in existence represents a genuine contribution to cancer research. The network is designed to be accessible to anyone — a three-step setup process allows non-technical users to begin contributing within minutes. Results are stored openly on-chain and contributed to the global scientific community.

LIFE Compute addresses two urgent problems simultaneously: the computational bottleneck in early-stage drug discovery, and the lack of meaningful utility in cryptocurrency mining. By aligning economic incentives with humanitarian goals, LIFE Compute creates a self-sustaining ecosystem where mining is literally saving lives.

---

## Table of Contents

1. [The Problem](#1-the-problem)
2. [The Solution: LIFE Compute](#2-the-solution-life-compute)
3. [The $LIFE Token](#3-the-life-token)
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
6. $LIFE tokens are minted directly to the miner's wallet
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
| Total Supply | 21,000,000 $LIFE — fixed forever |
| Pre-mine | Zero |
| Team Allocation | Zero |
| Investor Allocation | Zero |
| Blockchain | Solana |
| Token Standard | SPL Token |

### 3.2 Mining Rewards

Rewards scale with the scientific difficulty of the target, incentivizing miners to focus compute on the hardest, most important problems:

| Difficulty | Reward | Description |
|-----------|--------|-------------|
| Easy | 1 $LIFE | Well-characterized binding pocket |
| Medium | 5 $LIFE | Partial structural data available |
| Hard | 25 $LIFE | Novel, poorly characterized target |
| Discovery Bonus | 100 $LIFE | Top affinity score for a target that week |

### 3.3 Why This Model Works

Every $LIFE token represents real scientific work. As the total supply is distributed over time, each token becomes a permanent record of humanity's collective contribution to cancer research. The fixed supply creates natural scarcity as harder targets are solved, while the discovery bonus creates intense competition for breakthrough findings.

Unlike proof-of-work mining where difficulty increases arbitrarily, LIFE Compute's difficulty is intrinsic — it reflects the genuine scientific challenge of finding high-affinity molecules for specific cancer proteins. The network gets harder to mine in exactly the ways that advance science.

---

## 4. Cancer Target Portfolio

LIFE Compute launches with ten validated cancer protein targets, curated from the most clinically significant and computationally tractable proteins in the oncology literature:

| Target | UniProt | Cancer Type | Significance | Tier |
|--------|---------|-------------|--------------|------|
| **TP53** | P04637 | Pan-cancer | Most mutated gene in human cancer (50% of all cases) | Hard (25 $LIFE) |
| **BRCA1** | P38398 | Breast/Ovarian | Hereditary cancer suppressor, 1 in 400 carriers | Hard (25 $LIFE) |
| **EGFR** | P00533 | Lung | Driver mutation in 15% of lung adenocarcinoma | Medium (5 $LIFE) |
| **HER2** | P04626 | Breast | Amplified in 20% of breast cancers, poor prognosis | Medium (5 $LIFE) |
| **KRAS** | P01116 | Pancreatic/Lung | Undruggable for decades, mutated in 85% pancreatic | Hard (25 $LIFE) |
| **BCL2** | P10415 | Lymphoma/Leukemia | Apoptosis regulator, validated venetoclax target | Medium (5 $LIFE) |
| **CDK4** | P11802 | Multiple | Cell cycle driver, palbociclib target | Easy (1 $LIFE) |
| **VEGFR** | P35968 | Angiogenesis | Tumor blood vessel formation, bevacizumab target | Easy (1 $LIFE) |
| **PD-L1** | Q9NZQ7 | Immunotherapy | Checkpoint inhibitor, atezolizumab/durvalumab target | Medium (5 $LIFE) |
| **MDM2** | Q00987 | Pan-cancer | p53 suppressor, amplified in 7% of all cancers | Medium (5 $LIFE) |

---

## 5. Technical Architecture

### 5.1 Solana Program (life-compute/core)

The LIFE Compute smart contract is written in Rust using the Anchor framework and deployed on Solana. The program manages six state accounts:

- `NetworkState` — global configuration, total $LIFE minted, epoch parameters
- `TargetAccount` — per-protein target data, current best score, weekly winner
- `MinerAccount` — per-miner statistics, total $LIFE earned, submission history
- `JobAccount` — active job assignments linking miners to targets
- `SubmissionAccount` — individual molecule submission with Boltz2 score
- `ValidatorAccount` — registered validator credentials and stake

Nine on-chain instructions handle the full lifecycle: `initialize`, `register_miner`, `register_validator`, `assign_job`, `submit_result`, `validate_result`, `mint_reward`, `claim_discovery_bonus`, and `update_target`.

**Program ID:** `3dYbT2egotmpGBoLZe2pytsraffxre7V5dySsTKgxYiC`

### 5.2 Miner Software (life-compute/miner)

The miner daemon is a Python application packaged as a Docker container. It handles the complete workflow autonomously:

- Pulls current target assignments from the Solana program
- Downloads protein sequences from the `life-compute/targets` database
- Samples candidate molecules from the ZINC15 drug-like subset (~10M compounds)
- Runs Boltz2 affinity prediction locally on the miner's GPU
- Submits results meeting the affinity threshold to the Solana program
- Monitors $LIFE rewards in real time via a local React dashboard at `http://localhost:3000`

### 5.3 Validation Mechanism

To prevent fake score submissions, LIFE Compute uses a two-stage validation system. When a miner submits a result, two randomly selected validators independently re-run Boltz2 on the submitted molecule-target pair. If both validators confirm the score within a 5% tolerance, the $LIFE reward is minted. Validators earn a small commission on each validation.

In Phase 1, validators are operated by the LIFE Compute foundation. Phase 2 will open validation to any party staking a minimum $LIFE amount, creating a fully decentralized verification network.

### 5.4 Infrastructure

| Component | Technology |
|-----------|-----------|
| Blockchain | Solana mainnet |
| Scoring Oracle | Boltz2 (MIT/Recursion, open weights) |
| Molecule Library | ZINC15 drug-like subset + SAVI-2020 |
| Target Database | life-compute/targets (open source) |
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
- [x] 10 cancer targets active
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

*LIFE Compute — Version 1.0 — August 2026*
