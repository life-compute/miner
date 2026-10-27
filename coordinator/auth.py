"""
Coordinator authentication helpers (C3).

Implements the X-Auth signing scheme described in API_CONTRACT.md §1:
  canonical message = METHOD\nPATH\nbody_sha256_hex\nnonce\ntimestamp
  signed with Ed25519; signature hex-encoded (64 bytes → 128 hex chars).

Key source (real key, never changed here):
  - Primary: MINER_KEYPAIR env var or /mnt/minos-drive/life-compute-miner/miner-keypair.json
    This is a Solana keypair JSON: [priv_0…priv_31, pub_0…pub_31] (64 ints).
  - The miner-keypair.json public key is the wallet sent in X-Auth.
  - In tests, a throwaway key is generated via Ed25519PrivateKey.generate().
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Solana base58 alphabet
_B58_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_MAP = {ch: i for i, ch in enumerate(_B58_ALPHA)}

_WORKER_NAME_RE = re.compile(r'^[A-Za-z0-9-]{1,32}$')


def b58encode(data: bytes) -> str:
    """Encode bytes as Solana-compatible base58."""
    num = int.from_bytes(data, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58_ALPHA[rem] + out
    # leading zero bytes
    for b in data:
        if b != 0:
            break
        out = "1" + out
    return out


def b58decode(s: str) -> bytes:
    """Decode Solana-compatible base58 to bytes."""
    num = 0
    for ch in s:
        num = num * 58 + _B58_MAP[ch]
    n_bytes = (num.bit_length() + 7) // 8
    result = num.to_bytes(n_bytes, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result


def validate_worker_name(name: Optional[str]) -> bool:
    """Return True if name is None (omitted) or matches [A-Za-z0-9-]{1,32}."""
    if name is None:
        return True
    return bool(_WORKER_NAME_RE.match(name))


class MinerKeypair:
    """Holds an Ed25519 keypair and exposes signing + wallet address."""

    def __init__(self, private_key: "Ed25519PrivateKey"):
        self._priv = private_key
        pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.wallet: str = b58encode(pub_bytes)
        self._pub_bytes = pub_bytes

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_solana_json(cls, path: str | Path) -> "MinerKeypair":
        """
        Load from a Solana keypair JSON file.
        Format: [b0, b1, …, b63]  — first 32 bytes = private key, next 32 = public key.
        This is the standard output of `solana-keygen new`.
        """
        with open(path) as fh:
            raw = json.load(fh)
        priv_bytes = bytes(raw[:32])
        priv = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        return cls(priv)

    @classmethod
    def generate(cls) -> "MinerKeypair":
        """Generate a throwaway keypair (tests only)."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_default(cls) -> "MinerKeypair":
        """
        Load the miner's real signing key.

        Precedence:
          1. MINER_KEYPAIR env var (path to JSON)
          2. /mnt/minos-drive/life-compute-miner/miner-keypair.json

        The miner-keypair.json is the wallet whose public key is G8wkwTUC4nyQ…
        (the legitimate primary miner wallet as documented in memory).
        It is NEVER printed, rotated, or changed here.
        """
        path = os.environ.get(
            "MINER_KEYPAIR",
            "/mnt/minos-drive/life-compute-miner/miner-keypair.json",
        )
        return cls.from_solana_json(path)

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def sign_request(
        self,
        method: str,
        path: str,
        body_bytes: bytes,
        worker_name: Optional[str] = None,
    ) -> dict:
        """
        Build a signed X-Auth dict ready to JSON-encode in the request header.

        Returns dict with keys: wallet, nonce, timestamp, signature, [worker_name].
        """
        nonce = secrets.token_hex(16)       # 32 lowercase hex chars
        timestamp = int(time.time())
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        msg = f"{method}\n{path}\n{body_hash}\n{nonce}\n{timestamp}"
        sig_bytes = self._priv.sign(msg.encode())
        result = {
            "wallet": self.wallet,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": sig_bytes.hex(),
        }
        if worker_name is not None:
            result["worker_name"] = worker_name
        return result
