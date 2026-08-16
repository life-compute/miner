#!/usr/bin/env bash
# LIFE Compute — HiveOS auto-install script
# Installs everything needed to run the miner on a fresh HiveOS rig.
#
# What this does:
#   1. Install Docker (if not present)
#   2. Install Node.js 18 LTS (if not present)
#   3. Install PM2 globally
#   4. Clone/extract miner into /hive/miners/life-compute/
#   5. Install Python dependencies
#   6. Pull Boltz2 Docker image (background)
#   7. Write default .env

set -euo pipefail

MINER_DIR="/hive/miners/life-compute"
REPO_URL="https://github.com/life-compute/miner"
SOLANA_RPC="${SOLANA_RPC:-https://api.mainnet-beta.solana.com}"
PROGRAM_ID="${PROGRAM_ID:-74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf}"

RED='\033[0;31m' GRN='\033[0;32m' YEL='\033[1;33m' RST='\033[0m'
info()  { echo -e "${GRN}[install]${RST} $*"; }
warn()  { echo -e "${YEL}[install]${RST} $*"; }
step()  { echo -e "\n${GRN}══ $* ══${RST}"; }

step "LIFE Compute — HiveOS Installer"
echo "  Miner dir : $MINER_DIR"
echo "  Repo      : $REPO_URL"
echo

# ── 1. Docker ─────────────────────────────────────────────────────────────────
step "Checking Docker"
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker --now || true
else
    info "Docker $(docker --version | cut -d' ' -f3 | tr -d ',') — OK"
fi

# ── 2. Node.js 18 ─────────────────────────────────────────────────────────────
step "Checking Node.js"
NODE_OK=false
if command -v node &>/dev/null; then
    NODE_VER=$(node --version 2>/dev/null | sed 's/v//' | cut -d. -f1)
    [[ "${NODE_VER:-0}" -ge 18 ]] && NODE_OK=true
fi

if [[ "$NODE_OK" == false ]]; then
    info "Installing Node.js 18 LTS..."
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
    apt-get install -y nodejs
else
    info "Node.js $(node --version) — OK"
fi

# ── 3. PM2 ────────────────────────────────────────────────────────────────────
step "Checking PM2"
if ! command -v pm2 &>/dev/null; then
    info "Installing PM2..."
    npm install -g pm2 --quiet
    pm2 startup systemd -u root --hp /root || true
else
    info "PM2 $(pm2 --version) — OK"
fi

# ── 4. Miner installation ─────────────────────────────────────────────────────
step "Installing LIFE Compute miner"
mkdir -p "$MINER_DIR"

# If we're already unpacked alongside install.sh (tar.gz extraction), copy in place
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/../miner_daemon.py" ]]; then
    info "Copying from extracted archive..."
    rsync -a --exclude='.git' --exclude='hiveos' "$SCRIPT_DIR/../" "$MINER_DIR/"
else
    info "Cloning from GitHub..."
    if [[ -d "$MINER_DIR/.git" ]]; then
        git -C "$MINER_DIR" pull --ff-only
    else
        git clone --depth 1 "$REPO_URL" "$MINER_DIR"
    fi
fi

# ── 5. Python deps ────────────────────────────────────────────────────────────
step "Python dependencies"
if command -v pip3 &>/dev/null; then
    REQS="$MINER_DIR/requirements.txt"
    if [[ -f "$REQS" ]]; then
        pip3 install -q -r "$REQS" || warn "Some Python deps failed (non-fatal)"
    else
        pip3 install -q requests rdkit-pypi 2>/dev/null || true
    fi
fi

# ── 6. Node deps for dashboard ────────────────────────────────────────────────
step "Dashboard dependencies"
if [[ -f "$MINER_DIR/dashboard/package.json" ]]; then
    cd "$MINER_DIR/dashboard"
    npm install --include=dev --quiet
    npm run build --quiet
    cd "$MINER_DIR"
fi

# ── 7. Default .env ───────────────────────────────────────────────────────────
step "Writing default .env"
ENV_FILE="$MINER_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<ENVEOF
# LIFE Compute miner configuration
# Edit SOLANA_WALLET and MINER_KEYPAIR for your setup.
SOLANA_RPC=${SOLANA_RPC}
PROGRAM_ID=${PROGRAM_ID}
POLL_SECONDS=60
DASHBOARD_PORT=3001
MINER_KEYPAIR=${MINER_DIR}/miner-keypair.json
SOLANA_KEYPAIR=${MINER_DIR}/dev-keypair.json
ENVEOF
    info "Default .env written — update MINER_KEYPAIR path if needed"
else
    info ".env already exists — skipped"
fi

# ── 8. Boltz2 Docker image (background pull) ──────────────────────────────────
step "Boltz2 Docker image"
BOLTZ_IMAGE="ghcr.io/jwohlwend/boltz:latest"
if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${BOLTZ_IMAGE}$"; then
    info "Boltz2 image already present"
else
    info "Pulling Boltz2 in background (this takes a few minutes)..."
    nohup docker pull "$BOLTZ_IMAGE" > /tmp/boltz_pull.log 2>&1 &
    info "Pull running in background — check: tail -f /tmp/boltz_pull.log"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo -e "${GRN}══ LIFE Compute installed to ${MINER_DIR} ══${RST}"
echo
echo "  Next steps:"
echo "  1. Add your Solana keypair: $MINER_DIR/miner-keypair.json"
echo "  2. Run: pm2 start $MINER_DIR/ecosystem.config.js --update-env"
echo "  3. Dashboard: http://localhost:3001"
echo
