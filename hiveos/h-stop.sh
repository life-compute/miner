#!/usr/bin/env bash
# HiveOS h-stop.sh — graceful shutdown of LIFE Compute miner

set -euo pipefail

GRN='\033[0;32m' RST='\033[0m'
info() { echo -e "${GRN}[life-compute]${RST} $*"; }

if command -v pm2 &>/dev/null; then
    pm2 stop  life-miner     2>/dev/null && info "life-miner stopped"     || true
    pm2 stop  life-dashboard 2>/dev/null && info "life-dashboard stopped" || true
    pm2 save  --force        2>/dev/null || true
else
    # Fallback: kill by process name
    pkill -f miner_daemon.py  2>/dev/null || true
    pkill -f "dashboard/server.cjs" 2>/dev/null || true
    info "Processes terminated"
fi
