"""
tests/test_c5s_ed25519_nonce.py — C5s: Ed25519 R-uniqueness and known-answer tests
                                  for the miner coordinator auth module.

Root-cause context
──────────────────
export_c5b_bundle.ts (coordinator repo) called ed.sign(msg, privKey) with a sha512Sync
hook set to @noble/hashes/sha512, which is a single-argument function.  noble/ed25519 v2
calls sha512Sync(prefix, msg) with two arguments; the single-arg wrapper ignored the
second argument, computing SHA-512(prefix only).  This made the nonce r = SHA-512(prefix)
message-independent, so R = [r]G was identical across all messages for the same key.

This file is in the miner repo because the miner's coordinator/auth.py also produces
Ed25519 signatures (MinerKeypair.sign_request).  The analysis is:

  coordinator/auth.py → cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey.sign()
  Nonce source: RFC 8032 deterministic derivation built into the library (OpenSSL backend).
  Status: CORRECT — not affected by the noble/ed25519 hook bug.

These tests verify:
  1. R-uniqueness: two different messages signed with the same key give different R values.
  2. Known-answer: three self-consistent test vectors (produced by the same library).
     All three seeds are deterministic (repeated-byte patterns; no RNG).
"""

from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_key(seed: bytes) -> Ed25519PrivateKey:
    """Create a throwaway Ed25519 private key from a 32-byte seed."""
    assert len(seed) == 32, "seed must be 32 bytes"
    return Ed25519PrivateKey.from_private_bytes(seed)


def _pub_hex(priv: Ed25519PrivateKey) -> str:
    pub: Ed25519PublicKey = priv.public_key()
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _r_from_sig(sig: bytes) -> bytes:
    """Return the R component (first 32 bytes) of an Ed25519 signature."""
    assert len(sig) == 64
    return sig[:32]


# ── Known-answer vectors ───────────────────────────────────────────────────────
# Produced by Python cryptography 42.x (OpenSSL backend).
# Seeds are deterministic (repeated-byte patterns).  No RNG involved.
# Cross-checked against noble/ed25519 v2 signAsync (Node.js) — results agree.

KA_VECTORS = [
    {
        # seed = 0x9a × 32, msg = b''
        "seed": bytes([0x9A] * 32),
        "msg": b"",
        "pub": "1bee74bb8f513f1b5357bc477c886ad96949d04740ccd13a22f912edf00dfe27",
        "sig": (
            "5c413d8709c659367608e3f7893484067a6d02c1b91f1b170dab10b75624d46"
            "6fa8628fb834e587b637172e0e50aef328caca2af82fefe33451cdc4d0fa99003"
        ),
    },
    {
        # seed = 0x01 × 32, msg = b'\x72'
        "seed": bytes([0x01] * 32),
        "msg": bytes.fromhex("72"),
        "pub": "8a88e3dd7409f195fd52db2d3cba5d72ca6709bf1d94121bf3748801b40f6f5c",
        "sig": (
            "3c69e8568428383ad54de260f341ab46bcfede3490ceeb232d527106830b12c6"
            "d161e21b8cebaedc88761ad2e48e8ccb3e502967fdbca7a48fbdffb08ab98f04"
        ),
    },
    {
        # seed = 0xc5 × 32, msg = b'\xaf\x82'
        "seed": bytes([0xC5] * 32),
        "msg": bytes.fromhex("af82"),
        "pub": "2db797636e323ed576e9b0e03e27cb92171a896b63e9186f0d4a43b9c7d382d1",
        "sig": (
            "93ea6de6fd090316dc5c40ebaea45fd2f6ff5e343471192607d5feb0118bdcdf"
            "ef70023452b6e9c9de4a806d788c9ecc00e2460417ca86209ce23a1cdf68500c"
        ),
    },
]


# ── 1. R-uniqueness ────────────────────────────────────────────────────────────


def test_r_uniqueness_different_messages():
    """Two different messages signed with the same key must produce distinct R values."""
    seed = hashlib.sha256(b"c5s-miner-test-key").digest()
    priv = _make_key(seed)

    msg1 = b"life-compute message ONE"
    msg2 = b"life-compute message TWO"

    sig1 = priv.sign(msg1)
    sig2 = priv.sign(msg2)

    R1 = _r_from_sig(sig1)
    R2 = _r_from_sig(sig2)

    assert R1 != R2, (
        "R values must differ for different messages — "
        "identical R leaks the private key."
    )


def test_r_uniqueness_different_paths():
    """R-uniqueness for the auth.py request-signing pattern (method+path+body)."""
    seed = hashlib.sha256(b"c5s-miner-request-key").digest()
    priv = _make_key(seed)

    def _sign_request(method: str, path: str, body: bytes) -> bytes:
        body_hash = hashlib.sha256(body).hexdigest()
        nonce = "fixed-nonce-for-test"
        timestamp = 1_000_000
        msg = f"{method}\n{path}\n{body_hash}\n{nonce}\n{timestamp}".encode()
        return priv.sign(msg)

    sig1 = _sign_request("GET", "/work/alpha", b"")
    sig2 = _sign_request("GET", "/work/beta",  b"")

    R1 = _r_from_sig(sig1)
    R2 = _r_from_sig(sig2)

    assert R1 != R2


# ── 2. Known-answer tests ──────────────────────────────────────────────────────


@pytest.mark.parametrize("vec", KA_VECTORS)
def test_known_answer_public_key(vec):
    """Public key derived from seed must match expected value."""
    priv = _make_key(vec["seed"])
    assert _pub_hex(priv) == vec["pub"]


@pytest.mark.parametrize("vec", KA_VECTORS)
def test_known_answer_signature(vec):
    """Signature over known message must match expected value."""
    priv = _make_key(vec["seed"])
    sig = priv.sign(vec["msg"])
    assert sig.hex() == vec["sig"]


@pytest.mark.parametrize("vec", KA_VECTORS)
def test_known_answer_verify(vec):
    """The known signature must verify correctly against the known public key."""
    priv = _make_key(vec["seed"])
    pub: Ed25519PublicKey = priv.public_key()
    sig = bytes.fromhex(vec["sig"])
    # verifyAsync raises InvalidSignature on failure
    pub.verify(sig, vec["msg"])  # must not raise
