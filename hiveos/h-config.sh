#!/usr/bin/env bash
# HiveOS h-config.sh — translate flight sheet env vars into miner .env
# Called by HiveOS after the flight sheet is applied.
#
# Flight sheet fields:
#   WALLET      = Solana wallet address (miner keypair public key)
#   WORKER_NAME = rig/worker name
#   POOL        = ignored (LIFE Compute uses Solana, not a pool)

set -euo pipefail

MINER_DIR="/hive/miners/life-compute"
ENV_FILE="$MINER_DIR/.env"

[[ -d "$MINER_DIR" ]] || { echo "life-compute not installed — run install.sh first"; exit 1; }

# Write wallet + worker into .env (preserve other keys already present)
update_env() {
    local key="$1" val="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

touch "$ENV_FILE"
[[ -n "${WALLET:-}"      ]] && update_env "SOLANA_WALLET" "$WALLET"
[[ -n "${WORKER_NAME:-}" ]] && update_env "WORKER_NAME"   "$WORKER_NAME"

echo "[life-compute] Config written to $ENV_FILE"
echo "  WALLET      = ${WALLET:-<not set>}"
echo "  WORKER_NAME = ${WORKER_NAME:-<not set>}"
