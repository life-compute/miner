# LIFE Compute Miner

## Your GPU could help cure cancer. Earn $LIFE tokens.

LIFE Compute is a decentralized network where anyone with a NVIDIA GPU can contribute to
cancer drug discovery. Your machine runs [Boltz2](https://github.com/jwohlwend/boltz)
GPU structure prediction against validated cancer protein targets. Hits are submitted
on-chain to the Solana LIFE Compute program. Validators verify results and mint
$LIFE tokens to your wallet.

---

## Start mining in 3 commands

```bash
# 1. Download and install
curl -sSL https://raw.githubusercontent.com/life-compute/miner/main/install.sh | bash

# 2. Connect your Solana wallet (interactive — generates one if needed)
~/.life-compute/bin/life-compute wallet connect

# 3. Start mining
docker run -d --gpus all --name life-compute-miner \
  -v ~/.life-compute:/root/.life-compute \
  ghcr.io/life-compute/miner:latest
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/life-compute/miner/main/install.ps1 | iex
```

---

## Minimum hardware requirements

| Requirement | Minimum |
|---|---|
| GPU | NVIDIA 8 GB VRAM (RTX 3080 / A4000 or better recommended) |
| RAM | 16 GB system RAM |
| OS | Ubuntu 20.04+ / Windows 10+ |
| CUDA | 11.8+ |
| Docker | 24.0+ with [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) |
| Wallet | Solana wallet (generated automatically if you don't have one) |

---

## What is LIFE Compute?

```
GPU scores molecules → results submitted on-chain → validators verify → $LIFE minted
```

1. The miner pulls the current target list from **[life-compute/targets](https://github.com/life-compute/targets)** — 20 validated cancer proteins (TP53, BRCA1, EGFR, HER2, KRAS, BCL2, CDK4, VEGFR2, PDL1, MDM2, MET, FGFR1, PIK3CA, mTOR, PTEN, BRAF, AR, STAT3, RET, CDK6)
2. Boltz2 GPU inference predicts binding affinity for drug-like molecules against each target
3. Hits (predicted binding score ≤ threshold) are submitted to the Solana program on-chain
4. Validators independently verify the structure prediction
5. Your wallet receives $LIFE per verified hit (a flat amount set by target difficulty — no halving, no supply cap)

Read the full vision in **[WHITEPAPER.md](https://github.com/life-compute/miner/blob/main/WHITEPAPER.md)**.

---

## $LIFE Tokenomics

> There is no ceiling on $LIFE. There is only the work. Every token in existence is proof that a real computation happened — a real GPU, a real target, a real independently-verified result. Supply does not stop at a number. It stops when there is nothing left to discover.

**Total supply is a live figure, not a target.** At every moment it is defined as
*total minted minus total burned*. It rises only when real work is verified and
mints new $LIFE through the program's official reward-payout path. There is no
fixed cap, no artificial ceiling, and no halving schedule.

> $LIFE can only ever be created by real, verified work — running inference against real cancer targets, independently confirmed by a second machine. There is no other way for new $LIFE to come into existence. Once earned, it can be freely held or traded — but no one can ever buy their way into being the one who discovered it.

### Rewards per verified hit

Flat rates. No halving, no time-based reduction, no supply-based reduction.

| Difficulty | Reward | Description |
|---|---|---|
| Easy | **0.3 $LIFE** | Well-characterized binding pocket |
| Medium | **0.7 $LIFE** | Partial structural data available |
| Hard | **0.9 $LIFE** | Novel, poorly characterized target |
| mRNA silencing | **0.9 $LIFE** | Always Hard tier |
| CRISPR gRNA | **0.252 $LIFE** | Knockout targets (CPU-scored) |
| Reference compound | **0.108 $LIFE** | Known-binder control |
| Discovery bonus | **100 $LIFE** | Top affinity for a target that week |

### Per-target maturity taper

The one reduction that remains is **not** a monetary schedule — it reflects
diminishing scientific return. The thousandth confirmed hit on a target teaches
less than the first, so reward tapers with how well-explored that target is:

| Verified hits on target | Multiplier |
|---|---|
| 0 – 99 | 100% of tier reward |
| 100 – 999 | 75% of tier reward |
| 1,000+ | 50% of tier reward |

Example: a Hard target with 150 confirmed hits pays `0.9 × 0.75 = 0.675 $LIFE`.

$LIFE is a standard SPL token. There is no transfer hook, no burn-on-transfer,
and no restriction on trading once earned — a miner who earned it can sell or
trade it on any Solana DEX. The market redistributes already-earned tokens; it
never creates new supply.

> This does not mean $LIFE cures disease. It means $LIFE only exists where real computational science — narrowing the search for what might — has genuinely happened.

---

## Repositories

| Repo | Description |
|---|---|
| **[life-compute/miner](https://github.com/life-compute/miner)** | This repo — Python daemon, installer, React dashboard |
| **[life-compute/core](https://github.com/life-compute/core)** | Solana on-chain program (Anchor) |
| **[life-compute/targets](https://github.com/life-compute/targets)** | Cancer protein target database |

**On-chain program ID (Solana devnet):** `3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL`

---

## Dashboard

Once the miner is running, open **http://localhost:3001** to see:

- Molecules screened (live counter)
- $LIFE earned
- Cancer targets you've contributed to
- Current binding score

---

## Configuration

`~/.life-compute/config.json`:

```json
{
  "wallet_path": "~/.life-compute/wallet.json",
  "rpc_url": "https://api.devnet.solana.com",
  "target_refresh_interval": 300
}
```

---

## Architecture

```
miner_daemon.py
├── fetch_targets()          → life-compute/targets on GitHub
├── run_boltz2_scoring()     → Boltz2 GPU inference (real, not mock)
├── submit_result_on_chain() → Solana devnet via Node.js + Anchor
└── write_stats()            → stats.json → dashboard reads on :3001
```

---

## On-chain program

Program ID: `3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL`  
Network: Solana devnet (mainnet-beta at launch)  
Source: [life-compute/core](https://github.com/life-compute/core)

---

## License

MIT
