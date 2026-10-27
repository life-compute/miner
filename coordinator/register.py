"""
Coordinator registration command (C3).

Usage:
    python -m coordinator.register

Steps:
  1. Print the URLs of the Terms of Use, Privacy Notice, and Rewards and
     Digital Assets Notice.
  2. Prompt the user to type 'yes' to accept.
  3. Send the existing on-chain register_miner transaction via life_submit.js.
  4. Register with the coordinator (POST /register) sending terms_version.

Requirement notes:
  - The miner needs a small one-time SOL balance on the fee-payer wallet
    (dev-keypair.json / AUTH_KEYPAIR) for the on-chain fee and stake.
  - This command never changes any keypair file.
  - The keypair used to sign the coordinator request is miner-keypair.json
    (MINER_KEYPAIR env var); same keypair used for all coordinator requests.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from .auth import MinerKeypair
from .client import CoordinatorClient, COORD_URL_DEFAULT, _signed_headers, _http

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Terms document URLs
# ---------------------------------------------------------------------------
TERMS_OF_USE_URL = (
    "https://lifecompute.ai/terms-of-use"
)
PRIVACY_NOTICE_URL = (
    "https://lifecompute.ai/privacy"
)
REWARDS_NOTICE_URL = (
    "https://lifecompute.ai/rewards-and-digital-assets-notice"
)

# The terms version the coordinator accepts.  Must match known_terms_versions
# in the coordinator config (currently "1.0.0" or "1.1.0").
TERMS_VERSION = "1.0.0"

# On-chain registration script location (Anchor JS client).
ANCHOR_DIR = Path(os.environ.get("ANCHOR_DIR", "/tmp/life-compute/core"))
REGISTER_SCRIPT = ANCHOR_DIR / "life_register.js"
# Fallback: use life_submit.js with a register action if life_register.js is absent.
SUBMIT_SCRIPT = ANCHOR_DIR / "life_submit.js"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORK_DIR = Path(__file__).resolve().parents[1]
_NODE     = os.environ.get("NODE_BIN", "node")
_NOVA_VENV = Path("/mnt/minos-drive/nova_subnet/.venv/bin/python")


def _onchain_register(auth_keypair_path: str, rpc: str) -> bool:
    """
    Send the on-chain register_miner transaction via the Anchor JS client.
    Returns True on success.
    """
    script = REGISTER_SCRIPT if REGISTER_SCRIPT.exists() else SUBMIT_SCRIPT
    if not script.exists():
        log.error("[REGISTER] Anchor registration script not found at %s", script)
        return False

    args = json.dumps({
        "action": "register_miner",
        "keypair": auth_keypair_path,
        "rpc": rpc,
    })
    try:
        r = subprocess.run(
            [_NODE, str(script), args],
            capture_output=True, text=True, timeout=120,
            cwd=str(ANCHOR_DIR),
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode == 0:
            log.info("[REGISTER] on-chain tx: %s", out[:200])
            return True
        if "already" in out.lower() or "already" in err.lower():
            log.info("[REGISTER] miner already registered on-chain")
            return True
        log.error("[REGISTER] on-chain registration failed:\n  stdout: %s\n  stderr: %s",
                  out[:500], err[:500])
        return False
    except Exception as e:
        log.error("[REGISTER] on-chain call failed: %s", e)
        return False


def _coordinator_register(
    keypair: MinerKeypair,
    coordinator_url: str,
    terms_version: str,
    worker_name: str | None,
) -> bool:
    """POST /register to the coordinator. Returns True on success."""
    path = "/register"
    body_dict = {"terms_version": terms_version}
    body_bytes = json.dumps(body_dict, separators=(",", ":")).encode()
    x_auth = _signed_headers(keypair, "POST", path, body_bytes, worker_name)
    try:
        status, data = _http(coordinator_url + path, "POST", body_bytes, x_auth)
        if status == 200:
            log.info("[REGISTER] coordinator registration OK: %s", data)
            return True
        log.error("[REGISTER] coordinator /register → %d: %s", status, data.get("error", ""))
        return False
    except Exception as e:
        log.error("[REGISTER] coordinator /register network error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------

def register_command(
    coordinator_url: str = COORD_URL_DEFAULT,
    worker_name: str | None = None,
    terms_version: str = TERMS_VERSION,
    dry_run: bool = False,
) -> bool:
    """
    Full registration flow: ToU prompt → on-chain tx → coordinator /register.
    Returns True if both steps succeed.
    Prints the three legal document URLs and requires the user to type 'yes'.
    """
    print()
    print("=" * 70)
    print("  LIFE Compute — Miner Registration")
    print("=" * 70)
    print()
    print("Before registering, you must accept the following documents:")
    print()
    print(f"  Terms of Use:                     {TERMS_OF_USE_URL}")
    print(f"  Privacy Notice:                   {PRIVACY_NOTICE_URL}")
    print(f"  Rewards & Digital Assets Notice:  {REWARDS_NOTICE_URL}")
    print()
    print("Type  yes  to accept all three documents and proceed, or anything")
    print("else to cancel:")
    print()
    try:
        answer = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer != "yes":
        print("Registration cancelled.")
        return False

    print()
    print(f"Accepted terms_version={terms_version}.")
    print()

    auth_keypair_path = os.environ.get(
        "SOLANA_KEYPAIR",
        "/mnt/minos-drive/life-compute-miner/dev-keypair.json",
    )
    rpc = os.environ.get("SOLANA_RPC", "https://api.devnet.solana.com")

    if dry_run:
        print("[DRY-RUN] Would run on-chain register_miner.")
        print("[DRY-RUN] Would POST /register to coordinator.")
        return True

    # Step 1: on-chain
    print("Step 1/2: Sending on-chain register_miner transaction …")
    if not _onchain_register(auth_keypair_path, rpc):
        print("ERROR: On-chain registration failed. "
              "Ensure the fee-payer wallet (dev-keypair.json) has SOL for fees and stake.")
        return False
    print("  On-chain registration OK.")
    print()

    # Step 2: coordinator
    print("Step 2/2: Registering with coordinator …")
    keypair = MinerKeypair.load_default()
    if not _coordinator_register(keypair, coordinator_url.rstrip("/"), terms_version, worker_name):
        print("ERROR: Coordinator registration failed. "
              "Check coordinator URL and your wallet's on-chain status.")
        return False

    print("  Coordinator registration OK.")
    print()
    print(f"Registration complete. Wallet: {keypair.wallet}")
    return True


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="Register miner with LIFE Compute coordinator")
    p.add_argument("--coordinator-url", default=COORD_URL_DEFAULT)
    p.add_argument("--worker-name", default=None)
    p.add_argument("--terms-version", default=TERMS_VERSION)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    ok = register_command(
        coordinator_url=args.coordinator_url,
        worker_name=args.worker_name,
        terms_version=args.terms_version,
        dry_run=args.dry_run,
    )
    sys.exit(0 if ok else 1)
