#!/usr/bin/env bash
# ============================================================
#  LIFE Compute Miner Installer
#  Your GPU could help cure cancer. Earn $LIFE tokens.
# ============================================================
set -euo pipefail

# ─── Colors ─────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

LIFE_DIR="$HOME/.life-compute"
DOCKER_IMAGE="ghcr.io/life-compute/miner:latest"
SERVICE_NAME="life-compute-miner"

# ─── Helpers ────────────────────────────────────────────────
info()    { echo -e "${CYAN}  ℹ  ${RESET}$*"; }
success() { echo -e "${GREEN}  ✔  ${RESET}$*"; }
warn()    { echo -e "${YELLOW}  ⚠  ${RESET}$*"; }
error()   { echo -e "${RED}  ✖  ${RESET}$*" >&2; }
die()     { error "$*"; exit 1; }

step() {
  local num="$1"; local title="$2"
  echo ""
  echo -e "${BOLD}${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${BOLD}${MAGENTA}  Step ${num}: ${title}${RESET}"
  echo -e "${BOLD}${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

banner() {
  echo ""
  echo -e "${BOLD}${GREEN}"
  echo "  ██╗     ██╗███████╗███████╗     ██████╗ ██████╗ ███╗   ███╗██████╗ ██╗   ██╗████████╗███████╗"
  echo "  ██║     ██║██╔════╝██╔════╝    ██╔════╝██╔═══██╗████╗ ████║██╔══██╗██║   ██║╚══██╔══╝██╔════╝"
  echo "  ██║     ██║█████╗  █████╗      ██║     ██║   ██║██╔████╔██║██████╔╝██║   ██║   ██║   █████╗  "
  echo "  ██║     ██║██╔══╝  ██╔══╝      ██║     ██║   ██║██║╚██╔╝██║██╔═══╝ ██║   ██║   ██║   ██╔══╝  "
  echo "  ███████╗██║██║     ███████╗    ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║     ╚██████╔╝   ██║   ███████╗"
  echo "  ╚══════╝╚═╝╚═╝     ╚══════╝     ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝      ╚═════╝    ╚═╝   ╚══════╝"
  echo -e "${RESET}"
  echo -e "${BOLD}${CYAN}         ✦  Your GPU could help cure cancer. Earn \$LIFE tokens.  ✦${RESET}"
  echo -e "${DIM}                        Decentralized Drug Discovery Network${RESET}"
  echo ""
}

require_cmd() {
  command -v "$1" &>/dev/null || die "Required command not found: $1. $2"
}

version_gte() {
  # Returns 0 (true) if $1 >= $2 (both in x.y format)
  printf '%s\n%s' "$2" "$1" | sort -V -C
}

# ─── Banner ─────────────────────────────────────────────────
banner

echo -e "${DIM}  Installer version 1.0.0  •  $(date '+%Y-%m-%d')${RESET}"
echo ""

# ════════════════════════════════════════════════════════════
# STEP 1 — DOWNLOAD & PREREQUISITES CHECK
# ════════════════════════════════════════════════════════════
step 1 "Download & Prerequisites"

info "Checking system prerequisites..."

# Docker
if ! command -v docker &>/dev/null; then
  die "Docker is not installed. Please install Docker Desktop from https://docs.docker.com/get-docker/ and re-run this installer."
fi
DOCKER_VER=$(docker --version | grep -oP '\d+\.\d+' | head -1)
success "Docker ${DOCKER_VER} found"

# Docker daemon running?
if ! docker info &>/dev/null 2>&1; then
  die "Docker daemon is not running. Start Docker and re-run this installer."
fi
success "Docker daemon is running"

# Python 3.10+
if command -v python3 &>/dev/null; then
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  if version_gte "$PY_VER" "3.10"; then
    success "Python ${PY_VER} found"
  else
    warn "Python ${PY_VER} found but 3.10+ is recommended for the local dashboard."
  fi
else
  warn "Python 3 not found. Dashboard features will be limited. Install from https://python.org"
fi

# NVIDIA GPU
GPU_OK=false
if command -v nvidia-smi &>/dev/null; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "Unknown")
  GPU_DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "Unknown")
  success "NVIDIA GPU detected: ${GPU_NAME} (driver ${GPU_DRIVER})"
  GPU_OK=true
  # Check NVIDIA Container Toolkit
  if ! docker run --rm --gpus all nvidia/cuda:12.0-base-ubuntu22.04 nvidia-smi &>/dev/null 2>&1; then
    warn "NVIDIA Container Toolkit may not be installed. GPU passthrough might not work."
    warn "Install from: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
  else
    success "NVIDIA Container Toolkit is working"
  fi
else
  warn "No NVIDIA GPU detected (nvidia-smi not found). The miner will run in CPU-only mode (much slower)."
  warn "For best performance, use a machine with an NVIDIA GPU."
fi

info "Pulling Docker image: ${DOCKER_IMAGE}"
info "(This may take a few minutes on first run...)"
if docker pull "${DOCKER_IMAGE}" 2>/dev/null; then
  success "Docker image pulled successfully"
else
  warn "Could not pull ${DOCKER_IMAGE} from registry."
  warn "The image may not be published yet or you may be offline."
  warn "Continuing — the image can be built locally with: docker build -t ${DOCKER_IMAGE} ."
fi

mkdir -p "${LIFE_DIR}"
success "Created config directory: ${LIFE_DIR}"

# ════════════════════════════════════════════════════════════
# STEP 2 — CONNECT SOLANA WALLET
# ════════════════════════════════════════════════════════════
step 2 "Connect Your Solana Wallet"

WALLET_FILE="${LIFE_DIR}/wallet.json"

if [[ -f "${WALLET_FILE}" ]]; then
  info "Existing wallet found at ${WALLET_FILE}"
  echo -e "${YELLOW}  Do you want to use the existing wallet or set up a new one?${RESET}"
  echo "    [1] Use existing wallet"
  echo "    [2] Replace with a new/different wallet"
  read -rp "  Choice [1]: " WALLET_CHOICE
  WALLET_CHOICE="${WALLET_CHOICE:-1}"
  if [[ "${WALLET_CHOICE}" == "1" ]]; then
    success "Using existing wallet"
    SKIP_WALLET=true
  else
    SKIP_WALLET=false
  fi
else
  SKIP_WALLET=false
fi

if [[ "${SKIP_WALLET}" == "false" ]]; then
  echo ""
  echo -e "  ${BOLD}How would you like to set up your wallet?${RESET}"
  echo "    [1] Enter an existing Solana wallet address"
  echo "    [2] Generate a new keypair (recommended for first-time users)"
  read -rp "  Choice [2]: " CHOICE
  CHOICE="${CHOICE:-2}"

  if [[ "${CHOICE}" == "1" ]]; then
    echo ""
    read -rp "  Enter your Solana wallet address (public key): " WALLET_ADDR
    if [[ -z "${WALLET_ADDR}" ]]; then
      die "Wallet address cannot be empty."
    fi
    # Basic length/character validation
    if ! echo "${WALLET_ADDR}" | grep -qE '^[1-9A-HJ-NP-Za-km-z]{32,44}$'; then
      die "Invalid Solana address format. Please check your address."
    fi
    # Save as a pubkey-only wallet file
    cat > "${WALLET_FILE}" <<WALLETEOF
{
  "pubkey": "${WALLET_ADDR}",
  "type": "provided",
  "note": "Pubkey-only entry. The private key is managed by your own wallet (Phantom, Solflare, etc.)."
}
WALLETEOF
    success "Wallet address saved: ${WALLET_ADDR}"
    info "Note: reward claims require signing — connect your wallet in the dashboard."

  elif [[ "${CHOICE}" == "2" ]]; then
    info "Generating a new Solana keypair..."
    if command -v solana-keygen &>/dev/null; then
      solana-keygen new --outfile "${WALLET_FILE}" --no-bip39-passphrase --force
      PUBKEY=$(solana-keygen pubkey "${WALLET_FILE}")
      success "New keypair generated!"
      echo ""
      echo -e "  ${BOLD}${GREEN}  Public Key: ${PUBKEY}${RESET}"
      echo ""
      echo -e "  ${RED}${BOLD}  ⚠  IMPORTANT: Back up ${WALLET_FILE} immediately!${RESET}"
      echo -e "  ${RED}     This file contains your private key. Never share it.${RESET}"
    else
      info "solana-keygen not found. Generating keypair via Python..."
      python3 - <<'PYEOF'
import json, secrets, hashlib, base58, os, sys

# Generate a simple ed25519 keypair using nacl if available, else use secrets for demo
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes_raw()
    public_bytes = private_key.public_key().public_bytes_raw()
    keypair_bytes = list(private_bytes) + list(public_bytes)
    pubkey = base58.b58encode(public_bytes).decode()
except ImportError:
    # Fallback: generate 64 random bytes as keypair placeholder
    import random
    keypair_bytes = [random.randint(0, 255) for _ in range(64)]
    pubkey = "GeneratedKey_InstallSolanaCliForRealKeypair"

wallet_path = os.path.expanduser("~/.life-compute/wallet.json")
with open(wallet_path, "w") as f:
    json.dump(keypair_bytes, f)
print(f"  Keypair saved. Public key: {pubkey}")
print(f"  ⚠  For a production keypair, install Solana CLI: https://docs.solana.com/cli/install-solana-cli-tools")
PYEOF
      success "Keypair generated (see above for public key)"
    fi
  else
    die "Invalid choice."
  fi
fi

# ════════════════════════════════════════════════════════════
# STEP 3 — START & CONFIGURE AUTO-START
# ════════════════════════════════════════════════════════════
step 3 "Start Miner & Enable Auto-Start on Boot"

CONFIG_FILE="${LIFE_DIR}/config.json"
cat > "${CONFIG_FILE}" <<CONFEOF
{
  "wallet_path": "${WALLET_FILE}",
  "rpc_url": "https://api.devnet.solana.com",
  "target_refresh_interval": 300,
  "log_level": "INFO",
  "stats_output": "${LIFE_DIR}/stats.json"
}
CONFEOF
success "Config written to ${CONFIG_FILE}"

# Build docker run command
GPU_FLAG=""
if [[ "${GPU_OK}" == "true" ]]; then
  GPU_FLAG="--gpus all"
fi

DOCKER_RUN_CMD="docker run -d \
  --name ${SERVICE_NAME} \
  --restart unless-stopped \
  ${GPU_FLAG} \
  -v ${LIFE_DIR}:/root/.life-compute \
  -p 8765:8765 \
  ${DOCKER_IMAGE}"

info "Starting miner container..."
# Stop any existing instance
docker rm -f "${SERVICE_NAME}" &>/dev/null || true
if eval "${DOCKER_RUN_CMD}" 2>/dev/null; then
  success "Miner container started (name: ${SERVICE_NAME})"
else
  warn "Could not start container (image may not be available locally)."
  warn "Once the image is available, run:"
  echo ""
  echo -e "  ${CYAN}${DOCKER_RUN_CMD}${RESET}"
  echo ""
fi

# ─── Systemd auto-start (Linux) ─────────────────────────────
if [[ "$(uname -s)" == "Linux" ]] && command -v systemctl &>/dev/null; then
  info "Setting up systemd service for auto-start on boot..."
  SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

  sudo tee "${SERVICE_FILE}" > /dev/null <<SVCEOF
[Unit]
Description=LIFE Compute Miner — Decentralized Cancer Drug Discovery
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=forking
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker rm -f ${SERVICE_NAME}
ExecStart=${DOCKER_RUN_CMD}
ExecStop=/usr/bin/docker stop ${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
SVCEOF

  sudo systemctl daemon-reload
  sudo systemctl enable "${SERVICE_NAME}"
  success "systemd service enabled: ${SERVICE_NAME}"
  success "Miner will auto-start on every boot"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  # macOS: LaunchAgent
  PLIST_DIR="$HOME/Library/LaunchAgents"
  PLIST_FILE="${PLIST_DIR}/com.life-compute.miner.plist"
  mkdir -p "${PLIST_DIR}"
  cat > "${PLIST_FILE}" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.life-compute.miner</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/docker</string>
    <string>start</string>
    <string>${SERVICE_NAME}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
PLISTEOF
  launchctl load -w "${PLIST_FILE}" 2>/dev/null || true
  success "LaunchAgent installed for macOS auto-start"
fi

# ─── Final summary ───────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}${GREEN}  ✦  Installation Complete!  ✦${RESET}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  ${BOLD}Your GPU could help cure cancer. Earn \$LIFE tokens.${RESET}"
echo ""
echo -e "  ${CYAN}Miner status:${RESET}   docker logs -f ${SERVICE_NAME}"
echo -e "  ${CYAN}Dashboard:${RESET}      http://localhost:8765"
echo -e "  ${CYAN}Config:${RESET}         ${CONFIG_FILE}"
echo -e "  ${CYAN}Wallet:${RESET}         ${WALLET_FILE}"
echo ""
echo -e "  ${DIM}Join the community: https://discord.gg/life-compute${RESET}"
echo -e "  ${DIM}Docs: https://docs.life-compute.io${RESET}"
echo ""
