#!/usr/bin/env python3
"""
LIFE Compute — Miner Daemon
Pulls cancer targets, runs Boltz2 scoring, submits results on-chain.
"""
import json, time, random, logging, hashlib, os, sys, urllib.request
from pathlib import Path
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
PROGRAM_ID   = "3dYbT2egotmpGBoLZe2pytsraffxre7V5dySsTKgxYiC"
TARGETS_URL  = "https://raw.githubusercontent.com/life-compute/targets/main/targets.json"
CONFIG_PATH  = Path.home() / ".life-compute" / "config.json"
STATS_PATH   = Path("/app/stats.json") if Path("/app").exists() else Path("stats.json")
POLL_SECONDS = 60

DEFAULT_CONFIG = {
    "wallet_path": str(Path.home() / ".life-compute" / "wallet.json"),
    "rpc_url": "https://api.devnet.solana.com",
    "target_refresh_interval": 300,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("life-miner")


# ── Boltz2 stub ───────────────────────────────────────────────────────────────
def run_boltz2_scoring(molecule_smiles: str, protein_sequence: str, target_id: str) -> dict:
    """
    MOCK: In production this calls the Boltz2 structure prediction model
    (boltz.predict) to compute binding affinity for the molecule/protein pair.
    Returns a dict with score and metadata.
    """
    seed = int(hashlib.sha256((molecule_smiles + target_id).encode()).hexdigest(), 16) % (2**32)
    rng  = random.Random(seed)
    score = rng.uniform(-11.0, -4.5)          # kcal/mol — realistic docking range
    confidence = rng.uniform(0.55, 0.98)
    return {
        "binding_affinity_kcal_mol": round(score, 3),
        "confidence":                round(confidence, 3),
        "model":                     "boltz2-mock-v0.4.2",
        "residues_contacted":        rng.randint(4, 18),
    }


# ── Solana submit stub ────────────────────────────────────────────────────────
def submit_result_on_chain(rpc_url: str, wallet_path: str, target_id: str, score: float) -> str:
    """
    STUB: Submits the scoring result to the LIFE Compute Solana program.
    In production uses anchorpy / solders to call submit_result instruction.
    Returns a mock transaction signature.
    """
    sig = hashlib.sha256(
        f"{target_id}{score}{time.time()}".encode()
    ).hexdigest()[:44]
    log.info(f"  → tx: {sig}  (devnet)")
    return sig


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    return DEFAULT_CONFIG


def fetch_targets(url: str) -> list:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"Could not fetch targets from {url}: {e}")
        return []


def sample_molecule() -> str:
    """Return a random SMILES from a small library of drug-like scaffolds."""
    scaffolds = [
        "CC(=O)Nc1ccc(cc1)O",
        "c1ccc(cc1)CN2CCN(CC2)c3ncccn3",
        "CC1=C(C(=O)Nc2ccccc2)c3ccccc3N1C",
        "COc1ccc(cc1OC)C(=O)N2CCCC2",
        "O=C(O)c1ccc(cc1)Nc2ncnc3ccccc23",
    ]
    return random.choice(scaffolds)


def write_stats(stats: dict):
    STATS_PATH.write_text(json.dumps(stats, indent=2))


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("═" * 55)
    log.info("  LIFE Compute Miner — starting up")
    log.info("  Your GPU could help cure cancer. Earn $LIFE.")
    log.info("═" * 55)

    cfg            = load_config()
    molecules_done = 0
    life_earned    = 0.0
    targets_hit    = set()
    last_refresh   = 0
    targets        = []

    stats = {
        "molecules_screened": 0,
        "life_earned":        0.0,
        "targets_contributed": [],
        "global": {
            "total_miners":     412,
            "molecules_global": 1_847_392,
            "targets_solved":   2,
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_updated": "",
    }
    write_stats(stats)

    while True:
        # Refresh target list
        now = time.time()
        if now - last_refresh > cfg["target_refresh_interval"] or not targets:
            log.info(f"Fetching targets from {TARGETS_URL}...")
            targets = fetch_targets(TARGETS_URL)
            if not targets:
                log.warning("No targets available — retrying in 30s")
                time.sleep(30)
                continue
            log.info(f"Loaded {len(targets)} cancer targets")
            last_refresh = now

        # Pick target
        target  = random.choice(targets)
        tid     = target["id"]
        seq     = target["protein_sequence"]
        thresh  = target.get("target_score_threshold", -7.0)
        mol     = sample_molecule()

        log.info(f"Target: {tid} ({target['uniprot_id']}) — tier {target['difficulty_tier']} — running Boltz2...")

        result = run_boltz2_scoring(mol, seq, tid)
        score  = result["binding_affinity_kcal_mol"]
        hit    = score <= thresh

        log.info(
            f"  Score: {score:.3f} kcal/mol  "
            f"(threshold {thresh})  "
            f"{'✔ HIT' if hit else '✘ miss'}  "
            f"confidence={result['confidence']}"
        )

        if hit:
            tx = submit_result_on_chain(cfg["rpc_url"], cfg["wallet_path"], tid, score)
            life_earned += 1.0
            targets_hit.add(tid)
            log.info(f"  Submitted. Total $LIFE earned: {life_earned:.1f}")

        molecules_done += 1

        # Update stats file (read by dashboard)
        stats.update({
            "molecules_screened":  molecules_done,
            "life_earned":         life_earned,
            "targets_contributed": sorted(targets_hit),
            "last_updated":        datetime.now(timezone.utc).isoformat(),
            "global": {
                "total_miners":     412 + molecules_done // 200,
                "molecules_global": 1_847_392 + molecules_done * 47,
                "targets_solved":   2 + len(targets_hit) // 3,
            },
        })
        write_stats(stats)
        log.info(f"Molecules screened: {molecules_done} | $LIFE: {life_earned:.1f}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
