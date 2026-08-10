# LIFE Compute Miner

<div align="center">

```
  ██╗     ██╗███████╗███████╗     ██████╗ ██████╗ ███╗   ███╗██████╗ ██╗   ██╗████████╗███████╗
  ██║     ██║██╔════╝██╔════╝    ██╔════╝██╔═══██╗████╗ ████║██╔══██╗██║   ██║╚══██╔══╝██╔════╝
  ██║     ██║█████╗  █████╗      ██║     ██║   ██║██╔████╔██║██████╔╝██║   ██║   ██║   █████╗
  ██║     ██║██╔══╝  ██╔══╝      ██║     ██║   ██║██║╚██╔╝██║██╔═══╝ ██║   ██║   ██║   ██╔══╝
  ███████╗██║██║     ███████╗    ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║     ╚██████╔╝   ██║   ███████╗
  ╚══════╝╚═╝╚═╝     ╚══════╝     ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝      ╚═════╝    ╚═╝   ╚══════╝
```

### ✦ Your GPU could help cure cancer. Earn $LIFE tokens. ✦

*Decentralized Drug Discovery — Powered by Bittensor & Solana*

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io%2Flife--compute%2Fminer-blue)](https://ghcr.io/life-compute/miner)
[![Network](https://img.shields.io/badge/Network-Solana%20Devnet-purple)](https://explorer.solana.com)

</div>

---

## What is LIFE Compute?

LIFE Compute is a decentralized network where GPU miners run molecular docking simulations against real cancer drug targets. Results are submitted on-chain to the Solana program, verified by validators, and rewarded with $LIFE tokens. Every molecule you screen is a real contribution to drug discovery research.

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| **OS** | Linux, macOS, or Windows 10/11 | Ubuntu 22.04 recommended |
| **Docker** | 24.0+ | [Install Docker](https://docs.docker.com/get-docker/) |
| **NVIDIA GPU** | 8 GB VRAM | CPU fallback available (slower) |
| **NVIDIA drivers** | 525+ | `nvidia-smi` must work |
| **NVIDIA Container Toolkit** | Latest | [Install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) |
| **Python** | 3.10+ | For local dashboard (optional) |
| **Disk** | 20 GB free | Docker image + model weights |
| **RAM** | 16 GB | 32 GB recommended |

---

## ⚡ Three-Step Quickstart

### Linux / macOS

```bash
# Step 1 — Download & verify prerequisites
curl -fsSL https://raw.githubusercontent.com/life-compute/miner/main/install.sh | bash
```

### Windows (PowerShell, run as Administrator)

```powershell
# Step 1 — Download & verify prerequisites
irm https://raw.githubusercontent.com/life-compute/miner/main/install.ps1 | iex
```

The installer will guide you through all three steps interactively:

1. **Download** — pulls the Docker image and checks your GPU + Docker setup
2. **Connect Wallet** — paste an existing Solana address or generate a new keypair
3. **Start** — launches the miner and registers it as a system service (auto-starts on boot)

---

## Manual Setup (advanced)

```bash
# 1. Pull the image
docker pull ghcr.io/life-compute/miner:latest

# 2. Set up your config directory
mkdir -p ~/.life-compute
cat > ~/.life-compute/config.json <<EOF
{
  "wallet_path": "~/.life-compute/wallet.json",
  "rpc_url": "https://api.devnet.solana.com",
  "target_refresh_interval": 300,
  "log_level": "INFO",
  "stats_output": "~/.life-compute/stats.json"
}
EOF

# 3. Run the miner
docker run -d \
  --name life-compute-miner \
  --restart unless-stopped \
  --gpus all \
  -v ~/.life-compute:/root/.life-compute \
  -p 8765:8765 \
  ghcr.io/life-compute/miner:latest

# 4. Open the dashboard
open http://localhost:8765
```

---

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                        Your Machine                              │
│                                                                  │
│  miner_daemon.py                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  1. Fetch cancer targets  ──► life-compute/targets repo  │   │
│  │  2. Generate molecule ID  ──► random candidate ligand    │   │
│  │  3. Run Boltz2 prediction ──► GPU-accelerated docking    │   │
│  │  4. Submit result         ──► Solana program (on-chain)  │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Solana Devnet                                  │
│                                                                  │
│  Program: 3dYbT2egotmpGBoLZe2pytsraffxre7V5dySsTKgxYiC         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  submit_result(target_id, molecule_id, binding_score)    │   │
│  │         │                                                │   │
│  │         ▼                                                │   │
│  │  Validators verify ──► consensus reached                 │   │
│  │         │                                                │   │
│  │         ▼                                                │   │
│  │  $LIFE minted to your wallet ✦                          │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

**In plain English:**

1. **Your GPU scores molecules** — The miner uses Boltz2, a state-of-the-art protein structure prediction and molecular docking model, to estimate how well each candidate drug molecule binds to a cancer target protein. A strong negative binding score (ΔG in kcal/mol) means the molecule might be a good drug candidate.

2. **Results are submitted on-chain** — Each binding score is cryptographically signed with your wallet and posted to the LIFE Core Solana program. This creates a permanent, tamper-proof record of your contribution.

3. **Validators verify the work** — A network of validators independently re-run a subset of docking calculations to check that your results are honest. Bad submissions are penalised; good submissions are rewarded.

4. **$LIFE tokens are minted** — Once your submission clears validation, the Solana program mints $LIFE tokens directly to your wallet. One verified submission = one $LIFE token (rate subject to governance).

---

## Dashboard

After installation, open **http://localhost:8765** to see your live mining stats:

![Dashboard Screenshot](docs/dashboard-screenshot.png)

> *Screenshot placeholder — run the miner and visit http://localhost:8765 to see your live dashboard.*

The dashboard shows:
- **Molecules Screened** — animated counter with your all-time total
- **$LIFE Earned** — your accumulated token balance
- **Cancer Targets Contributed To** — the specific proteins you've helped screen
- **Global Network Stats** — live view of the full LIFE Compute network

---

## Configuration

Edit `~/.life-compute/config.json`:

```json
{
  "wallet_path": "~/.life-compute/wallet.json",
  "rpc_url": "https://api.devnet.solana.com",
  "target_refresh_interval": 300,
  "log_level": "INFO",
  "stats_output": "~/.life-compute/stats.json"
}
```

| Key | Default | Description |
|---|---|---|
| `wallet_path` | `~/.life-compute/wallet.json` | Path to your Solana keypair |
| `rpc_url` | Devnet | Solana RPC endpoint |
| `target_refresh_interval` | 300s | How often to refresh the target list |
| `log_level` | INFO | Logging verbosity (DEBUG, INFO, WARNING, ERROR) |
| `stats_output` | `~/.life-compute/stats.json` | Stats file read by the dashboard |

---

## Useful Commands

```bash
# View live miner logs
docker logs -f life-compute-miner

# Stop the miner
docker stop life-compute-miner

# Restart the miner
docker restart life-compute-miner

# Check GPU usage
nvidia-smi

# View your stats
cat ~/.life-compute/stats.json | python3 -m json.tool
```

---

## Building from Source

```bash
git clone https://github.com/life-compute/miner.git
cd miner

# Build Docker image
docker build -t life-compute/miner:local .

# Build dashboard
cd dashboard
npm install
npm run build
cd ..

# Run locally
docker run --gpus all -v ~/.life-compute:/root/.life-compute -p 8765:8765 life-compute/miner:local
```

---

## Architecture

```
miner/
├── install.sh          # Linux/macOS three-step installer
├── install.ps1         # Windows PowerShell installer
├── Dockerfile          # Ubuntu 22.04 + CUDA + Boltz2
├── miner_daemon.py     # Main mining daemon
├── dashboard/
│   ├── src/App.jsx     # React dashboard (biopunk dark theme)
│   ├── src/main.jsx    # Vite entry point
│   ├── index.html      # HTML shell
│   └── package.json    # npm config
├── .github/
│   └── workflows/
│       └── docker.yml  # CI: build & push ghcr.io image
└── README.md           # This file
```

---

## Security Notes

- **Your private key** (`wallet.json`) never leaves your machine — it is mounted into the container but not transmitted anywhere.
- The miner only makes outbound connections to: the Solana RPC endpoint, the targets JSON URL, and GitHub Container Registry.
- All on-chain submissions are signed locally before broadcast.

---

## License

MIT — see [LICENSE](LICENSE)

---

<div align="center">

**Join the community:** [Discord](https://discord.gg/life-compute) • [Docs](https://docs.life-compute.io) • [Twitter](https://twitter.com/life_compute_io)

*Built with ❤️ for open science. Your GPU could help cure cancer.*

</div>
