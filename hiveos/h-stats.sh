#!/usr/bin/env bash
# HiveOS h-stats.sh — emit miner stats in HiveOS JSON format
# HiveOS reads stdout and expects JSON matching its stats schema.
#
# Field mapping:
#   khs      = molecules screened per hour (as "hashrate")
#   accepted = life_earned (cumulative $LIFE tokens = accepted shares)
#   rejected = 0 (failed Boltz2 runs are retried, not tracked as rejects)
#   temp     = GPU temperature from nvidia-smi
#   fan      = GPU fan speed from nvidia-smi

MINER_DIR="/hive/miners/life-compute"
STATS_FILE="$MINER_DIR/stats.json"

# ── Read miner stats ───────────────────────────────────────────────────────────
if [[ ! -f "$STATS_FILE" ]]; then
    echo '{"hs":[],"hs_units":"khs","temp":[],"fan":[],"uptime":0,"ar":[0,0,0],"algo":"boltz2"}'
    exit 0
fi

python3 - <<PYEOF
import json, subprocess, time, os, sys

stats_path = "$STATS_FILE"
try:
    with open(stats_path) as f:
        s = json.load(f)
except Exception:
    s = {}

# ── Molecules/hour (hashrate proxy) ───────────────────────────────────────────
mols = s.get("molecules_screened", 0)
started_at = s.get("started_at", "")
khs = 0.0
if started_at:
    try:
        from datetime import datetime, timezone
        t0 = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        elapsed_h = max(0.001, (datetime.now(timezone.utc) - t0).total_seconds() / 3600)
        khs = round(mols / elapsed_h / 1000, 4)  # mols/hour → khs
    except Exception:
        pass

life_earned = s.get("life_earned", 0.0)

# ── GPU stats via nvidia-smi ───────────────────────────────────────────────────
temp, fan = [], []
try:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=temperature.gpu,fan.speed",
         "--format=csv,noheader,nounits"],
        timeout=4, text=True
    ).strip()
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 2:
            try: temp.append(int(parts[0].strip()))
            except: temp.append(0)
            try:
                fan_val = parts[1].strip()
                fan.append(int(fan_val) if fan_val.isdigit() else 0)
            except: fan.append(0)
except Exception:
    pass

# ── Uptime seconds ─────────────────────────────────────────────────────────────
uptime = 0
try:
    import re
    pm2_out = subprocess.check_output(["pm2", "jlist"], timeout=5, text=True)
    procs   = json.loads(pm2_out)
    for p in procs:
        if p.get("name") == "life-miner":
            up_ms = p.get("pm2_env", {}).get("pm_uptime", 0)
            uptime = max(0, int((time.time() * 1000 - up_ms) / 1000))
            break
except Exception:
    pass

result = {
    "hs":       [khs],
    "hs_units": "khs",
    "temp":     temp or [0],
    "fan":      fan  or [0],
    "uptime":   uptime,
    "ar":       [int(life_earned * 1000), 0, 0],  # accepted, rejected, invalid
    "algo":     "boltz2",
}
print(json.dumps(result))
PYEOF
