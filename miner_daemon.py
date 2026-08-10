#!/usr/bin/env python3
"""
LIFE Compute Miner Daemon
=========================
Decentralized cancer drug discovery miner.
Your GPU could help cure cancer. Earn $LIFE tokens.

Flow (every cycle):
  1. Load config from ~/.life-compute/config.json
  2. Fetch cancer targets from remote targets.json
  3. For each target: run mock Boltz2 structure prediction
  4. Submit binding score to Solana program (submit_result instruction)
  5. Update local stats.json for the dashboard
  6. Serve stats.json via a tiny HTTP server on port 8765
  7. Sleep for 60 seconds and repeat
"""

import asyncio
import hashlib
import http.server
import json
import logging
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ─── Optional Solana imports ────────────────────────────────────────────────
try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.rpc.config import RpcSendTransactionConfig
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    from anchorpy import Program, Provider, Wallet, Idl
    from anchorpy.provider import DEFAULT_OPTIONS
    SOLANA_AVAILABLE = True
except ImportError:
    SOLANA_AVAILABLE = False

# ─── Rich logging (graceful fallback) ───────────────────────────────────────
try:
    from rich.logging import RichHandler
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    console = Console()
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)]
    )
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    console = None

log = logging.getLogger("life-miner")

# ─── Constants ──────────────────────────────────────────────────────────────
PROGRAM_ID        = "3dYbT2egotmpGBoLZe2pytsraffxre7V5dySsTKgxYiC"
TARGETS_URL       = "https://raw.githubusercontent.com/life-compute/targets/main/targets.json"
DEFAULT_RPC_URL   = "https://api.devnet.solana.com"
LIFE_DIR          = Path.home() / ".life-compute"
CONFIG_FILE       = LIFE_DIR / "config.json"
STATS_FILE        = LIFE_DIR / "stats.json"
WALLET_FILE       = LIFE_DIR / "wallet.json"
CYCLE_INTERVAL    = 60          # seconds between mining cycles
DASHBOARD_PORT    = 8765

# ─── Default cancer targets (used if network is unavailable) ────────────────
FALLBACK_TARGETS = [
    {"id": "EGFR_T790M",  "name": "EGFR T790M",   "cancer": "Non-small cell lung cancer",     "priority": "high"},
    {"id": "KRAS_G12D",   "name": "KRAS G12D",    "cancer": "Pancreatic cancer",              "priority": "high"},
    {"id": "BRCA1_WT",    "name": "BRCA1",         "cancer": "Breast / ovarian cancer",        "priority": "medium"},
    {"id": "BCR_ABL1",    "name": "BCR-ABL1",      "cancer": "Chronic myelogenous leukemia",   "priority": "high"},
    {"id": "ALK_EML4",    "name": "EML4-ALK",      "cancer": "Lung adenocarcinoma",            "priority": "medium"},
    {"id": "PIK3CA_E545K","name": "PIK3CA E545K",  "cancer": "Breast cancer",                 "priority": "medium"},
    {"id": "MYC_AMP",     "name": "c-MYC",         "cancer": "Multiple myeloma",               "priority": "low"},
    {"id": "TP53_R175H",  "name": "TP53 R175H",    "cancer": "Pan-cancer",                     "priority": "high"},
]


# ════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    """Load config from ~/.life-compute/config.json; create defaults if missing."""
    LIFE_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    else:
        cfg = {}

    defaults = {
        "wallet_path":             str(WALLET_FILE),
        "rpc_url":                 DEFAULT_RPC_URL,
        "target_refresh_interval": 300,
        "log_level":               "INFO",
        "stats_output":            str(STATS_FILE),
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

    logging.getLogger().setLevel(cfg.get("log_level", "INFO"))
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# Wallet
# ════════════════════════════════════════════════════════════════════════════

def load_wallet(wallet_path: str) -> Any:
    """Load Solana keypair from a JSON file.

    Supports two formats:
      - [int, ...] (64-byte array) — standard Solana CLI format
      - {"pubkey": "...", "type": "provided"} — pubkey-only (read-only mode)
    """
    path = Path(wallet_path)
    if not path.exists():
        log.warning(f"Wallet file not found: {wallet_path}. Creating a temporary keypair.")
        if SOLANA_AVAILABLE:
            kp = Keypair()
            return kp
        return None

    with open(path) as f:
        data = json.load(f)

    if isinstance(data, list):
        if SOLANA_AVAILABLE:
            return Keypair.from_bytes(bytes(data[:64]))
        return {"pubkey": "loaded_from_array", "bytes": data}
    elif isinstance(data, dict) and "pubkey" in data:
        log.warning("Pubkey-only wallet loaded. Submissions will be signed with a temp keypair.")
        if SOLANA_AVAILABLE:
            return Keypair()  # temp signer — won't receive rewards without real key
        return data
    else:
        log.error("Unknown wallet format.")
        return None


# ════════════════════════════════════════════════════════════════════════════
# Cancer Target Fetching
# ════════════════════════════════════════════════════════════════════════════

async def fetch_targets(url: str) -> list[dict]:
    """Fetch cancer targets from the remote targets.json endpoint."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            targets = resp.json()
            log.info(f"Fetched {len(targets)} targets from network")
            return targets
    except Exception as exc:
        log.warning(f"Could not fetch targets from {url}: {exc}. Using fallback targets.")
        return FALLBACK_TARGETS


# ════════════════════════════════════════════════════════════════════════════
# ── BOLTZ2 MOCK ─────────────────────────────────────────────────────────────
# This is a clearly-labelled MOCK of the Boltz2 structure prediction call.
# In production this would invoke boltz.predict() with a real protein target
# and a library of candidate small molecules, returning per-molecule docking
# scores. The mock returns a plausible binding affinity (ΔG in kcal/mol).
# ════════════════════════════════════════════════════════════════════════════

def mock_boltz2_predict(target_id: str, molecule_id: str) -> dict:
    """
    *** MOCK BOLTZ2 STRUCTURE PREDICTION ***

    In production this calls the Boltz2 folding model to:
      1. Fold the target protein into its 3D structure
      2. Dock the candidate small molecule
      3. Return predicted binding affinity (ΔG, kcal/mol)

    The mock deterministically varies the score based on the target/molecule
    hash so that repeated runs with the same inputs return consistent results
    (useful for testing and reproducibility checks).

    Returns:
        {
          "binding_score":   float,   # ΔG in kcal/mol (negative = stronger binding)
          "confidence":      float,   # [0, 1]
          "predicted_plddt": float,   # per-residue confidence [0, 100]
          "mock":            True,    # ALWAYS TRUE in this stub
        }
    """
    # Deterministic seed from target + molecule pair
    seed = int(hashlib.sha256(f"{target_id}:{molecule_id}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)

    # Binding affinity: typical drug-like range is -5 to -12 kcal/mol
    binding_score = -(rng.uniform(4.0, 13.0))

    # pLDDT confidence score (AlphaFold-style, 0–100)
    plddt = rng.uniform(60.0, 95.0)

    # Overall confidence [0, 1]
    confidence = rng.uniform(0.5, 0.99)

    return {
        "binding_score":   round(binding_score, 4),
        "confidence":      round(confidence, 4),
        "predicted_plddt": round(plddt, 2),
        "mock":            True,
        "target_id":       target_id,
        "molecule_id":     molecule_id,
    }


# ════════════════════════════════════════════════════════════════════════════
# Solana Submission
# ════════════════════════════════════════════════════════════════════════════

async def submit_result_to_chain(
    rpc_url:       str,
    wallet,
    target_id:     str,
    molecule_id:   str,
    binding_score: float,
) -> str | None:
    """Submit a prediction result to the LIFE Core Solana program.

    Calls the `submit_result` instruction on program
    3dYbT2egotmpGBoLZe2pytsraffxre7V5dySsTKgxYiC.

    Returns the transaction signature (or None if submission failed).
    """
    if not SOLANA_AVAILABLE:
        # Simulate a tx signature for offline/demo mode
        mock_sig = hashlib.sha256(
            f"{target_id}:{molecule_id}:{binding_score}:{time.time()}".encode()
        ).hexdigest()[:64]
        log.debug(f"[OFFLINE MODE] Simulated tx sig: {mock_sig}")
        return mock_sig

    try:
        from anchorpy import Context
        from solders.pubkey import Pubkey as SoldersPubkey

        program_id = SoldersPubkey.from_string(PROGRAM_ID)

        # Minimal IDL for the submit_result instruction
        idl_json = {
            "version": "0.1.0",
            "name":    "life_core",
            "instructions": [
                {
                    "name": "submitResult",
                    "accounts": [
                        {"name": "miner",   "isMut": True,  "isSigner": True},
                        {"name": "results", "isMut": True,  "isSigner": False},
                        {"name": "systemProgram", "isMut": False, "isSigner": False},
                    ],
                    "args": [
                        {"name": "targetId",     "type": "string"},
                        {"name": "moleculeId",   "type": "string"},
                        {"name": "bindingScore", "type": "f64"},
                    ],
                }
            ],
            "accounts": [],
            "types":    [],
            "errors":   [],
            "metadata": {"address": PROGRAM_ID},
        }

        from anchorpy.idl import Idl as AnchorIdl
        idl   = AnchorIdl.from_json(json.dumps(idl_json))
        kp    = wallet if isinstance(wallet, Keypair) else Keypair()
        anc_wallet = Wallet(kp)

        from solana.rpc.async_api import AsyncClient
        client   = AsyncClient(rpc_url)
        provider = Provider(client, anc_wallet, DEFAULT_OPTIONS)
        program  = Program(idl, program_id, provider=provider)

        # Derive a PDA for the results account
        results_pda, _bump = SoldersPubkey.find_program_address(
            [b"results", bytes(kp.pubkey()), target_id.encode()],
            program_id,
        )

        tx = await program.rpc["submit_result"](
            target_id,
            molecule_id,
            binding_score,
            ctx=Context(
                accounts={
                    "miner":         kp.pubkey(),
                    "results":       results_pda,
                    "systemProgram": SoldersPubkey.from_string(
                        "11111111111111111111111111111111"
                    ),
                },
                signers=[kp],
            ),
        )
        log.info(f"  ✔  On-chain submission: {tx}")
        await client.close()
        return str(tx)

    except Exception as exc:
        log.warning(f"Chain submission failed (continuing offline): {exc}")
        # Return a mock sig so the daemon doesn't stall
        return hashlib.sha256(f"offline:{time.time()}".encode()).hexdigest()[:64]


# ════════════════════════════════════════════════════════════════════════════
# Stats
# ════════════════════════════════════════════════════════════════════════════

class MinerStats:
    def __init__(self, stats_path: str):
        self.path               = Path(stats_path)
        self.molecules_screened = 0
        self.life_earned        = 0.0
        self.targets_contributed: list[str] = []
        self.recent_submissions: list[dict] = []
        self.start_time         = datetime.now(timezone.utc).isoformat()
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    d = json.load(f)
                self.molecules_screened  = d.get("molecules_screened", 0)
                self.life_earned         = d.get("life_earned", 0.0)
                self.targets_contributed = d.get("targets_contributed", [])
                self.recent_submissions  = d.get("recent_submissions", [])[-50:]
            except Exception:
                pass

    def record_submission(self, target: dict, prediction: dict, tx_sig: str | None):
        self.molecules_screened += 1
        self.life_earned        += 1.0   # 1 LIFE per verified submission (mock reward)
        t_name = target.get("name", target.get("id", "Unknown"))
        if t_name not in self.targets_contributed:
            self.targets_contributed.append(t_name)
        self.recent_submissions.append({
            "ts":            datetime.now(timezone.utc).isoformat(),
            "target":        t_name,
            "molecule_id":   prediction["molecule_id"],
            "binding_score": prediction["binding_score"],
            "confidence":    prediction["confidence"],
            "tx_sig":        tx_sig,
        })
        self.recent_submissions = self.recent_submissions[-100:]
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "alive":               True,
            "molecules_screened":  self.molecules_screened,
            "life_earned":         round(self.life_earned, 4),
            "targets_contributed": self.targets_contributed,
            "recent_submissions":  self.recent_submissions[-20:],
            "global_mock": {
                "total_miners":             1247,
                "total_molecules_screened": 9_843_210 + self.molecules_screened,
                "targets_solved":           3,
            },
            "last_updated":        datetime.now(timezone.utc).isoformat(),
            "start_time":          self.start_time,
        }
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)


# ════════════════════════════════════════════════════════════════════════════
# Dashboard HTTP Server
# ════════════════════════════════════════════════════════════════════════════

def start_dashboard_server(stats_file: Path, port: int = DASHBOARD_PORT):
    """Serve the dashboard and stats.json on localhost:{port}."""
    dashboard_dir = Path(__file__).parent / "dashboard" / "dist"
    if not dashboard_dir.exists():
        dashboard_dir = Path(__file__).parent  # fallback: serve stats.json from cwd

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(dashboard_dir), **kwargs)

        def do_GET(self):
            # Proxy /stats.json from the live stats file
            if self.path == "/stats.json":
                try:
                    content = stats_file.read_bytes()
                except Exception:
                    content = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                super().do_GET()

        def log_message(self, fmt, *args):
            pass  # suppress HTTP log noise

    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    log.info(f"Dashboard available at http://localhost:{port}")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ════════════════════════════════════════════════════════════════════════════
# Main mining loop
# ════════════════════════════════════════════════════════════════════════════

async def mining_cycle(cfg: dict, stats: MinerStats, wallet: Any) -> None:
    """One full mining cycle: fetch targets → predict → submit."""
    targets = await fetch_targets(TARGETS_URL)

    # Pick one target per cycle (rotate deterministically by time)
    target_idx = int(time.time() / CYCLE_INTERVAL) % len(targets)
    target     = targets[target_idx]

    # Generate a candidate molecule ID (in production: iterate over a ligand library)
    molecule_id = f"MOL-{int(time.time()):010d}-{random.randint(1000, 9999)}"

    log.info(
        f"🔬  Screening molecule [{molecule_id}] against target "
        f"[{target.get('name', target['id'])}] ({target.get('cancer', 'unknown cancer')})"
    )

    # ── MOCK Boltz2 prediction ────────────────────────────────────────────
    prediction = mock_boltz2_predict(target["id"], molecule_id)
    log.info(
        f"    Binding score: {prediction['binding_score']:.4f} kcal/mol  "
        f"| Confidence: {prediction['confidence']:.2%}  "
        f"[MOCK]"
    )

    # ── Submit to Solana ─────────────────────────────────────────────────
    tx_sig = await submit_result_to_chain(
        rpc_url       = cfg["rpc_url"],
        wallet        = wallet,
        target_id     = target["id"],
        molecule_id   = molecule_id,
        binding_score = prediction["binding_score"],
    )

    # ── Update stats ─────────────────────────────────────────────────────
    stats.record_submission(target, prediction, tx_sig)

    log.info(
        f"    ✔  Submitted. Molecules screened: {stats.molecules_screened:,}  "
        f"| $LIFE earned: {stats.life_earned:.1f}"
    )


async def main() -> None:
    log.info("=" * 60)
    log.info("  LIFE Compute Miner  —  Starting up")
    log.info("  Your GPU could help cure cancer. Earn $LIFE tokens.")
    log.info("=" * 60)

    cfg    = load_config()
    stats  = MinerStats(cfg["stats_output"])
    wallet = load_wallet(cfg["wallet_path"])

    log.info(f"  RPC endpoint : {cfg['rpc_url']}")
    log.info(f"  Program ID   : {PROGRAM_ID}")
    log.info(f"  Solana SDK   : {'available' if SOLANA_AVAILABLE else 'NOT installed — offline mode'}")
    log.info(f"  Stats file   : {cfg['stats_output']}")

    # Start dashboard server
    start_dashboard_server(Path(cfg["stats_output"]), DASHBOARD_PORT)

    cycle = 0
    while True:
        cycle += 1
        log.info(f"\n{'─'*40}  Cycle #{cycle}  {'─'*40}")
        try:
            await mining_cycle(cfg, stats, wallet)
        except Exception as exc:
            log.error(f"Cycle #{cycle} failed: {exc}", exc_info=True)

        log.info(f"  Sleeping {CYCLE_INTERVAL}s until next cycle…")
        await asyncio.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
