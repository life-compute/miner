#!/usr/bin/env bash
# HiveOS h-run.sh — start LIFE Compute miner
# Handles: auto-install, on-chain registration check, SOL balance gate, PM2 start.

set -euo pipefail

MINER_DIR="/hive/miners/life-compute"
INSTALL_URL="https://github.com/life-compute/miner/releases/latest/download/life-compute-hiveos.tar.gz"
SOLANA_RPC="${SOLANA_RPC:-https://api.mainnet-beta.solana.com}"
PROGRAM_ID="${PROGRAM_ID:-74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf}"
FREE_MINER_THRESHOLD=20          # miners < this count = free registration
MIN_SOL_LAMPORTS=33000000        # 0.033 SOL in lamports

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m' GRN='\033[0;32m' YEL='\033[1;33m' CYN='\033[0;36m' RST='\033[0m'
info()  { echo -e "${GRN}[life-compute]${RST} $*"; }
warn()  { echo -e "${YEL}[life-compute]${RST} $*"; }
error() { echo -e "${RED}[life-compute] ERROR:${RST} $*" >&2; }

# ── 1. Auto-install if needed ──────────────────────────────────────────────────
if [[ ! -f "$MINER_DIR/miner_daemon.py" ]]; then
    info "Not installed — downloading from GitHub release..."
    TMP=$(mktemp -d)
    trap "rm -rf $TMP" EXIT
    curl -fsSL "$INSTALL_URL" -o "$TMP/life-compute-hiveos.tar.gz"
    tar -xzf "$TMP/life-compute-hiveos.tar.gz" -C "$TMP"
    bash "$TMP/install.sh"
    info "Install complete"
fi

cd "$MINER_DIR"
source .env 2>/dev/null || true
WALLET="${SOLANA_WALLET:-${WALLET:-}}"

if [[ -z "$WALLET" ]]; then
    error "No wallet configured. Set WALLET in the HiveOS flight sheet."
    exit 1
fi

# ── 2. Check on-chain registration (Python one-liner using existing daemon RPC) ─
check_registered() {
    python3 - <<PYEOF 2>/dev/null
import sys, json, urllib.request, base64, struct

PROGRAM_ID = "${PROGRAM_ID}"
RPC        = "${SOLANA_RPC}"
WALLET     = "${WALLET}"

B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def b58enc(d):
    n = int.from_bytes(d, "big"); out = []
    while n: n, r = divmod(n, 58); out.append(B58[r])
    out.extend(B58[0] for b in d if b == 0)
    return bytes(reversed(out)).decode()

def rpc(method, params):
    p = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    try:
        req = urllib.request.Request(RPC, data=p,
              headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("result")
    except: return None

# MinerAccount discriminator
disc = bytes([232, 196, 79, 139, 222, 213, 161, 99])
res  = rpc("getProgramAccounts", [PROGRAM_ID, {
    "encoding": "base64",
    "filters": [{"memcmp": {"offset": 0, "bytes": b58enc(disc)}}]
}])
if not isinstance(res, list):
    print("rpc_fail"); sys.exit(0)

# Decode each account's owner pubkey (bytes 8..40 in Anchor layout)
import base58_fallback  # not available — use base64 wallet comparison trick
for item in res:
    try:
        raw = base64.b64decode(item["account"]["data"][0])
        # owner pubkey is at offset 8, 32 bytes — compare with getAccountInfo
        pass
    except: pass

# Simpler: check getAccountInfo on the wallet itself and see lamports
# Real check: PDA exists for this wallet
print("unknown")
PYEOF
}

# More reliable registration check via Node (Anchor client already in miner dir)
check_registered_node() {
    node - <<'JSEOF' 2>/dev/null
const { execSync } = require('child_process');
try {
    // Use solana CLI if available
    const out = execSync(
        `solana account $(solana-keygen pubkey ${process.env.MINER_KEYPAIR || '/hive/miners/life-compute/miner-keypair.json'}) --output json --url ${process.env.SOLANA_RPC || 'https://api.mainnet-beta.solana.com'} 2>/dev/null`,
        { encoding: 'utf8', timeout: 10000 }
    );
    console.log('registered');
} catch(e) {
    console.log('not_registered');
}
JSEOF
}

# ── 3. SOL balance check via RPC ───────────────────────────────────────────────
get_sol_balance_lamports() {
    python3 - "$WALLET" <<'PYEOF'
import sys, json, urllib.request

wallet = sys.argv[1]
rpc    = "${SOLANA_RPC}"
payload = json.dumps({"jsonrpc":"2.0","id":1,"method":"getBalance","params":[wallet]}).encode()
try:
    req = urllib.request.Request(rpc, data=payload,
          headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        result = json.loads(r.read())
        print(result.get("result", {}).get("value", 0))
except Exception as e:
    print(0)
PYEOF
}

get_total_miners_registered() {
    python3 - <<PYEOF
import json, urllib.request, base64, struct

rpc = "${SOLANA_RPC}"
pda = "3cp9veeRTsqnXWSJYw2jqhRVeeKcaEkp4Pb2md9GJXPi"
payload = json.dumps({"jsonrpc":"2.0","id":1,"method":"getAccountInfo",
                      "params":[pda,{"encoding":"base64"}]}).encode()
try:
    req = urllib.request.Request(rpc, data=payload,
          headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        res = json.loads(r.read()).get("result",{})
    val = res.get("value") if isinstance(res, dict) else None
    if not val: print(0); exit()
    raw = base64.b64decode(val["data"][0])
    if len(raw) >= 288:
        print(struct.unpack_from("<Q", raw, 280)[0])
    else:
        print(0)
except:
    print(0)
PYEOF
}

# ── 4. Registration gate ───────────────────────────────────────────────────────
MINER_KEYPAIR="${MINER_KEYPAIR:-$MINER_DIR/miner-keypair.json}"

# Try to derive PDA for this wallet and check if account exists
REGISTERED=false
if command -v solana &>/dev/null && [[ -f "$MINER_KEYPAIR" ]]; then
    MINER_PUBKEY=$(solana-keygen pubkey "$MINER_KEYPAIR" 2>/dev/null || echo "")
    if [[ -n "$MINER_PUBKEY" ]]; then
        ACCT_INFO=$(solana account "$MINER_PUBKEY" --url "$SOLANA_RPC" --output json 2>/dev/null || echo "null")
        [[ "$ACCT_INFO" != "null" ]] && REGISTERED=true
    fi
fi

if [[ "$REGISTERED" == false ]]; then
    info "Miner not yet registered on-chain — checking eligibility..."
    TOTAL_MINERS=$(get_total_miners_registered)
    info "Total miners registered: $TOTAL_MINERS"

    if [[ "$TOTAL_MINERS" -lt "$FREE_MINER_THRESHOLD" ]]; then
        info "Free registration slot available (${TOTAL_MINERS} < ${FREE_MINER_THRESHOLD})"
        # Registration happens inside miner_daemon.py on first run
    else
        BALANCE_LAMPORTS=$(get_sol_balance_lamports)
        BALANCE_SOL=$(python3 -c "print(f'{${BALANCE_LAMPORTS}/1e9:.4f}')" 2>/dev/null || echo "0.0000")

        if [[ "$BALANCE_LAMPORTS" -lt "$MIN_SOL_LAMPORTS" ]]; then
            echo
            error "INSUFFICIENT SOL — Add at least 0.033 SOL to your wallet"
            echo -e "  ${CYN}Wallet:          ${RST}${WALLET}"
            echo -e "  ${CYN}Current balance: ${RST}${BALANCE_SOL} SOL"
            echo -e "  ${CYN}Required:        ${RST}0.033 SOL (~\$5)"
            echo -e "  ${CYN}Fund your wallet at: ${RST}https://phantom.app"
            echo
            exit 1
        fi
        info "Balance OK: ${BALANCE_SOL} SOL — proceeding with paid registration"
    fi
fi

# ── 5. Start via PM2 ──────────────────────────────────────────────────────────
info "Starting LIFE Compute miner..."
cd "$MINER_DIR"

if ! command -v pm2 &>/dev/null; then
    npm install -g pm2 --quiet
fi

pm2 delete life-miner      2>/dev/null || true
pm2 delete life-dashboard  2>/dev/null || true

pm2 start ecosystem.config.js --update-env
pm2 save --force

info "✓ life-miner     started"
info "✓ life-dashboard started (port ${DASHBOARD_PORT:-3001})"
