# LIFE Compute Miner

> **Your GPU could help cure cancer. Earn $LIFE tokens.**

LIFE Compute is a decentralized network where anyone with a GPU can contribute to
cancer drug discovery. Your machine runs [Boltz2](https://github.com/jwohlwend/boltz)
structure prediction against validated cancer protein targets. Hits are submitted
on-chain to the Solana LIFE Compute program. Validators verify results and mint
$LIFE tokens to your wallet.

## How it works

```
GPU scores molecules → results submitted on-chain → validators verify → $LIFE minted
```

1. The miner pulls the current target list from [life-compute/targets](https://github.com/life-compute/targets)
2. Boltz2 predicts binding affinity for drug-like molecules against each target
3. Hits (score ≤ threshold) are submitted to the Solana program (`3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL`)
4. Validators independently verify the structure prediction
5. Your wallet receives 1 $LIFE per verified hit

## Prerequisites

| Requirement | Minimum |
|---|---|
| OS | Ubuntu 20.04+ / Windows 10+ / macOS 12+ |
| GPU | NVIDIA 8 GB VRAM (RTX 3080 or better recommended) |
| RAM | 16 GB |
| CUDA | 11.8+ |
| Docker | 24.0+ (with NVIDIA Container Toolkit) |
| Python | 3.10+ |

## Quickstart (3 commands)

```bash
# 1. Download and install
curl -sSL https://raw.githubusercontent.com/life-compute/miner/main/install.sh | bash

# 2. Connect your Solana wallet (interactive)
~/.life-compute/bin/life-compute wallet connect

# 3. Start mining
docker run -d --gpus all --name life-compute-miner \
  -v ~/.life-compute:/root/.life-compute \
  ghcr.io/life-compute/miner:latest
```

## Dashboard

Once the miner is running, open **http://localhost:3000** to see:

- Molecules screened (live counter)
- $LIFE earned
- Cancer targets you've contributed to
- Global network stats

![Dashboard](docs/dashboard-preview.png)

## Configuration

`~/.life-compute/config.json`:

```json
{
  "wallet_path": "~/.life-compute/wallet.json",
  "rpc_url": "https://api.devnet.solana.com",
  "target_refresh_interval": 300
}
```

## Windows

```powershell
irm https://raw.githubusercontent.com/life-compute/miner/main/install.ps1 | iex
```

## Architecture

```
miner_daemon.py
├── fetch_targets()          → life-compute/targets on GitHub
├── run_boltz2_scoring()     → Boltz2 GPU inference
├── submit_result_on_chain() → Solana devnet (anchorpy)
└── write_stats()            → stats.json → dashboard reads
```

## On-chain program

Program ID: `3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL`  
Network: Solana devnet (mainnet-beta at launch)  
Source: [life-compute/core](https://github.com/life-compute/core)

## License

MIT
