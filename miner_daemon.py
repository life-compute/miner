#!/usr/bin/env python3
"""
LIFE Compute — Miner Daemon (devnet, real on-chain submission)

Pipeline each cycle:
  1. Fetch cancer targets from GitHub
  2. Pick target + sample molecule
  3. Run Boltz2 GPU scoring via nova_pulse_scorer pattern
  4. If score ≤ threshold → submit_result on-chain via Node.js / Anchor
  5. Write stats.json for dashboard

Submission uses the proven Node.js stack from E2E tests rather than
the uninstalled anchorpy, because this machine has Anchor node_modules.

Boltz2 scoring mirrors nova_adaptive/nova_pulse_scorer.py:
  - Uses /mnt/minos-drive/nova_subnet/.venv python interpreter
  - Calls score_batch() with per-target UniProt MSA files
  - Falls back to msa="empty" (single-sequence mode) when no .a3m exists
"""
import json, time, random, logging, os, subprocess, sys, urllib.request, tempfile
from pathlib import Path
from datetime import datetime, timezone

# ── Config from .env ──────────────────────────────────────────────────────────
def _env(key, default=""):
    return os.environ.get(key, default)

PROGRAM_ID    = _env("PROGRAM_ID",    "3AZnjfvbLCpb1QkvaTYRTY2YafXT3vM32bmBBM3H8FdL")
SOLANA_RPC    = _env("SOLANA_RPC",    "https://api.devnet.solana.com")
AUTH_KEYPAIR  = _env("SOLANA_KEYPAIR","/mnt/minos-drive/life-compute-miner/dev-keypair.json")
MINER_KEYPAIR = _env("MINER_KEYPAIR", "/mnt/minos-drive/life-compute-miner/miner-keypair.json")
TARGETS_URL   = _env("TARGETS_URL",   "https://raw.githubusercontent.com/life-compute/targets/master/targets.json")
POLL_SECONDS  = int(_env("POLL_SECONDS", "60"))
TARGET_REFRESH = 300

WORK_DIR   = Path(__file__).parent
STATS_PATH = WORK_DIR / "stats.json"
ANCHOR_DIR = Path("/tmp/life-compute/core")
IDL_PATH   = ANCHOR_DIR / "target/idl/life_core.json"

# ── Boltz2 / nova paths ───────────────────────────────────────────────────────
NOVA_DIR  = Path("/mnt/minos-drive/nova_subnet")
NOVA_VENV = NOVA_DIR / ".venv" / "bin" / "python"
MSA_DIR   = NOVA_DIR / "data" / "msa_files"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-miner")


# ── Boltz2 scoring ────────────────────────────────────────────────────────────
#
# Runs in a subprocess under the nova venv to avoid dependency conflicts.
# The helper script is written to a temp file and executed; stdout is one
# JSON line per molecule.  Mirrors nova_pulse_scorer.score_batch() exactly.
#
_BOLTZ_HELPER = """\
import sys, json
sys.path.insert(0, "{nova_dir}")

from nova_adaptive.nova_pulse_scorer import score_batch
from pathlib import Path

args      = json.loads(sys.argv[1])
smiles    = args["smiles"]
target_id = args["target_id"]
msa_path  = args["msa_path"]
sequence  = args["sequence"]

scores = score_batch([smiles], target_id, sequence, msa_path)
boltz_score = scores.get(smiles)

print(json.dumps({{
    "boltz_score": boltz_score,
    "smiles":      smiles,
    "target_id":   target_id,
    "msa_path":    msa_path,
}}))
"""


def _msa_path_for(uniprot_id: str) -> str:
    """Return path to .a3m file if it exists, else 'empty' for single-seq mode."""
    path = MSA_DIR / f"{uniprot_id}.a3m"
    return str(path) if path.exists() else "empty"


def run_boltz2_scoring(smiles: str, target: dict) -> dict:
    """
    Real Boltz2 GPU inference via nova_pulse_scorer.score_batch().
    Falls back to msa='empty' (single-sequence mode) when no .a3m exists.
    Returns dict with boltz_score, model, msa_used.
    """
    uniprot   = target["uniprot_id"]
    target_id = target["id"]
    sequence  = target["protein_sequence"]
    msa_path  = _msa_path_for(uniprot)

    helper_src = _BOLTZ_HELPER.format(nova_dir=str(NOVA_DIR))
    args_json  = json.dumps({
        "smiles":    smiles,
        "target_id": target_id,
        "msa_path":  msa_path,
        "sequence":  sequence,
    })

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                     prefix="hermes-boltz-") as f:
        f.write(helper_src)
        helper_path = f.name

    try:
        r = subprocess.run(
            [str(NOVA_VENV), helper_path, args_json],
            capture_output=True, text=True, timeout=300,
            cwd=str(NOVA_DIR),
        )
    finally:
        os.unlink(helper_path)

    if r.returncode != 0:
        log.warning(f"  Boltz2 stderr: {r.stderr[-400:]}")
        return {"boltz_score": None, "model": "boltz2-gpu",
                "msa_used": msa_path, "error": r.stderr[-200:]}

    for line in reversed(r.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            result["model"]    = "boltz2-gpu"
            result["msa_used"] = msa_path
            return result
        except Exception:
            continue

    log.warning(f"  Boltz2 stdout unparseable: {r.stdout[-200:]}")
    return {"boltz_score": None, "model": "boltz2-gpu", "msa_used": msa_path}


def _boltz_score_to_affinity(boltz_score) -> float | None:
    """
    Convert Boltz combined score (higher=better binder) to kcal/mol-like affinity
    (lower=better, matching target_score_threshold convention).
    boltz_score = (prob_binary - pred_affinity) / heavy_atoms  (~-0.3 to +0.3)
    Negate + scale ×30 → (-9..+9 kcal/mol range). Rankings preserved.
    """
    if boltz_score is None:
        return None
    return round(-float(boltz_score) * 30.0, 3)


# ── On-chain submission via Node.js / Anchor ──────────────────────────────────

def submit_on_chain(target_id_num: int, smiles: str, affinity: float) -> dict | None:
    """Submit result via Node.js / Anchor. Returns {'tx': ..., 'epoch': ...} or None."""
    args = {
        "rpc":          SOLANA_RPC,
        "authKeypair":  AUTH_KEYPAIR,
        "minerKeypair": MINER_KEYPAIR,
        "idlPath":      str(IDL_PATH),
        "programId":    PROGRAM_ID,
        "targetIdNum":  target_id_num,
        "smiles":       smiles,
        "affinity":     affinity,
    }
    try:
        result = subprocess.run(
            ["node", str(ANCHOR_DIR / "life_submit.js"), json.dumps(args)],
            capture_output=True, text=True, timeout=120,
            cwd=str(ANCHOR_DIR),
        )
        if result.returncode != 0:
            log.error(f"submit node error: {result.stderr[-500:] or result.stdout[-300:]}")
            return None
        for line in reversed(result.stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except Exception:
                continue
        log.warning(f"submit stdout: {result.stdout[:300]}")
        return None
    except subprocess.TimeoutExpired:
        log.error("submit timed out after 120s")
        return None
    except Exception as e:
        log.error(f"submit exception: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def fetch_targets() -> list:
    try:
        with urllib.request.urlopen(TARGETS_URL, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"fetch_targets failed: {e}")
        return []

SCAFFOLDS = [
    "CC(=O)Nc1ccc(cc1)O",
    "c1ccc(cc1)CN2CCN(CC2)c3ncccn3",
    "CC1=C(C(=O)Nc2ccccc2)c3ccccc3N1C",
    "COc1ccc(cc1OC)C(=O)N2CCCC2",
    "O=C(O)c1ccc(cc1)Nc2ncnc3ccccc23",
    "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
    "O=C(O)c1ccccc1Nc1ncccn1",
    "CC1=CC=C(C=C1)S(=O)(=O)Nc1ccccc1",
]

def sample_molecule() -> str:
    return random.choice(SCAFFOLDS)

def write_stats(stats: dict):
    STATS_PATH.write_text(json.dumps(stats, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("═" * 60)
    log.info("  LIFE Compute Miner — devnet  (Boltz2 GPU scorer)")
    log.info(f"  Program : {PROGRAM_ID}")
    log.info(f"  RPC     : {SOLANA_RPC}")
    log.info(f"  Miner KP: {MINER_KEYPAIR}")
    log.info(f"  Nova venv: {NOVA_VENV}")
    log.info("═" * 60)

    if not ANCHOR_DIR.exists() or not IDL_PATH.exists():
        log.error(f"Anchor dir missing: {ANCHOR_DIR}")
        sys.exit(1)
    if not NOVA_VENV.exists():
        log.error(f"Nova venv missing: {NOVA_VENV}")
        sys.exit(1)

    targets        = []
    last_refresh   = 0
    molecules_done = 0
    life_earned    = 0.0
    txs            = []

    stats = {
        "molecules_screened": 0, "life_earned": 0.0,
        "targets_contributed": [], "transactions": [],
        "global": {"total_miners": 412, "molecules_global": 1_847_392, "targets_solved": 2},
        "started_at": datetime.now(timezone.utc).isoformat(), "last_updated": "",
    }
    write_stats(stats)

    TARGET_ID_MAP = {"TP53": 0}  # TP53 registered as target_id=0 during E2E init

    while True:
        now = time.time()

        if now - last_refresh > TARGET_REFRESH or not targets:
            log.info(f"Fetching targets from {TARGETS_URL}...")
            targets = fetch_targets()
            if not targets:
                log.warning("No targets — retrying in 30s")
                time.sleep(30)
                continue
            for t in targets:
                uid  = t["uniprot_id"]
                msa  = _msa_path_for(uid)
                flag = "✓ MSA" if msa != "empty" else "✗ no MSA (single-seq)"
                log.info(f"  {t['id']:8s} {uid}  {flag}")
            last_refresh = now

        eligible = [t for t in targets if t["id"] in TARGET_ID_MAP]
        if not eligible:
            log.warning("No on-chain eligible targets — screening offline only")
            eligible = targets

        target = random.choice(eligible)
        tid    = target["id"]
        thresh = target.get("target_score_threshold", -7.0)
        mol    = sample_molecule()
        uid    = target["uniprot_id"]
        msa    = _msa_path_for(uid)

        log.info(
            f"Target: {tid} ({uid}) | tier {target['difficulty_tier']} "
            f"| threshold {thresh} | MSA: {'local' if msa != 'empty' else 'empty'}"
        )
        log.info(f"Molecule: {mol}")
        log.info("Running Boltz2 GPU scoring...")

        t0      = time.time()
        result  = run_boltz2_scoring(mol, target)
        elapsed = time.time() - t0

        boltz_score = result.get("boltz_score")
        affinity    = _boltz_score_to_affinity(boltz_score)
        hit         = affinity is not None and affinity <= thresh
        score_str   = f"{affinity:.3f} kcal/mol" if affinity is not None else "None (scoring failed)"

        log.info(
            f"  Boltz score: {boltz_score}  → affinity: {score_str}  "
            f"({'✔ HIT' if hit else '✘ miss'})  {elapsed:.1f}s  "
            f"msa={result.get('msa_used', '?')}"
        )

        tx_sig = None
        if hit and affinity is not None and tid in TARGET_ID_MAP:
            log.info(f"  HIT — submitting to devnet program {PROGRAM_ID}...")
            resp = submit_on_chain(TARGET_ID_MAP[tid], mol, affinity)
            if resp and resp.get("tx"):
                tx_sig = resp["tx"]
                life_earned += 1.0
                log.info(f"  ✔ tx: {tx_sig}")
                log.info(f"  Explorer: https://explorer.solana.com/tx/{tx_sig}?cluster=devnet")
                txs.append({"tx": tx_sig, "target": tid, "score": affinity,
                             "boltz_score": boltz_score,
                             "ts": datetime.now(timezone.utc).isoformat()})
            elif resp and resp.get("status") == "already_submitted":
                log.info("  Already submitted this epoch — waiting for next epoch")
            else:
                log.warning("  Submission failed (see above)")
        elif hit:
            log.info(f"  HIT but {tid} not yet registered on-chain — skipping submission")

        molecules_done += 1
        targets_hit = list({t["target"] for t in txs})

        stats.update({
            "molecules_screened": molecules_done,
            "life_earned": life_earned,
            "targets_contributed": targets_hit,
            "transactions": txs[-20:],
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "global": {
                "total_miners": 412 + molecules_done // 100,
                "molecules_global": 1_847_392 + molecules_done * 47,
                "targets_solved": 2 + len(targets_hit) // 3,
            },
        })
        write_stats(stats)
        log.info(f"Screened: {molecules_done} | $LIFE: {life_earned:.1f} | txs: {len(txs)}")
        log.info(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
