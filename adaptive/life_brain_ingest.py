"""
life_brain_ingest.py — Network-wide Solana ingestion for LIFE-BRAIN.

Reads ALL confirmed ResultSubmission accounts from program 74RHjg1z…,
filters to status == Confirmed (u8 = 2), deduplicates by submission pubkey,
and appends rows to output/life_brain_dataset.jsonl.

Byte layout (Anchor-serialised ResultSubmission):
  0–7    discriminator
  8–39   miner Pubkey  (32 bytes)
  40–41  target_id u16 LE
  42–49  epoch u64 LE
  50–561 smiles [u8; 512]
  562–563 smiles_len u16 LE
  564–567 claimed_affinity f32 LE
  568–575 submitted_slot i64 LE
  576    status u8  (0=Pending, 1=Validating, 2=Confirmed, 3=Rejected)
  577    validation_count u8
  578–581 validation_score_sum f32 LE
  582–741 validator_list [Pubkey; 5]  (5 × 32 = 160 bytes)
  742    reward_minted bool
  743    confirmed_count u8
  744–903 confirming_validator_list [Pubkey; 5]  (5 × 32 = 160 bytes)
  904    confirming_validator_count u8
  905    bump u8
  Total min length: 906 bytes

ResultStatus enum (from life_core.json IDL):
  0 = Pending   1 = Validating   2 = Confirmed   3 = Rejected

We only ingest status == 2 (Confirmed).

CPU-only: network I/O and JSON parsing only; no torch, no GPU.
"""
from __future__ import annotations

import base64
import json
import logging
import struct
import time
import threading
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("life-brain")

# ── Constants ─────────────────────────────────────────────────────────────────
PROGRAM_ID  = "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf"

# ResultSubmission discriminator: sha256("account:ResultSubmission")[:8]
_DISC_RESULT = bytes([0xd6, 0x73, 0xa5, 0x67, 0x43, 0xd3, 0x2f, 0x58])

# Byte offsets (all LE)
_OFF_MINER_PK       = 8    # 32 bytes → pubkey
_OFF_TARGET_ID      = 40   # u16
_OFF_EPOCH          = 42   # u64
_OFF_SMILES         = 50   # [u8; 512]
_OFF_SMILES_LEN     = 562  # u16
_OFF_AFFINITY       = 564  # f32
_OFF_SUBMITTED_SLOT = 568  # i64
_OFF_STATUS         = 576  # u8 — ResultStatus enum
_MIN_LEN            = 906  # minimum valid account bytes

_STATUS_CONFIRMED   = 2    # ResultStatus::Confirmed

POLL_INTERVAL_S     = 300  # 5 minutes
RPC_TIMEOUT_S       = 30

# ── Paths ─────────────────────────────────────────────────────────────────────
_LIFE_DIR     = Path(__file__).resolve().parents[1]
_OUTPUT_DIR   = _LIFE_DIR / "output"
_DATASET_PATH = _OUTPUT_DIR / "life_brain_dataset.jsonl"
_STATE_PATH   = _OUTPUT_DIR / "life_brain_ingest_state.json"

# ── B58 helpers (no external deps) ───────────────────────────────────────────
_B58_ALPHA = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58enc(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out: list[int] = []
    while n:
        n, r = divmod(n, 58)
        out.append(_B58_ALPHA[r])
    out.extend(_B58_ALPHA[0] for b in data if b == 0)
    return bytes(reversed(out)).decode()


# ── RPC helper ────────────────────────────────────────────────────────────────

def _rpc(solana_rpc: str, method: str, params: list):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }).encode()
    try:
        req = urllib.request.Request(
            solana_rpc, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=RPC_TIMEOUT_S) as r:
            return json.loads(r.read()).get("result")
    except Exception as e:
        log.debug(f"[INGEST] RPC {method}: {e}")
        return None


# ── Modality resolver ─────────────────────────────────────────────────────────

def _modality(target_id: int, seq: str) -> str:
    """Primary: target_id range. Fallback: ACGT/len-20 heuristic."""
    if 0 <= target_id <= 1999:
        return "protein"
    if 2000 <= target_id <= 2029:
        return "mrna"
    if 3000 <= target_id <= 3009:
        return "crispr"
    # Fallback
    s = seq.upper().strip()
    if len(s) == 20 and all(c in "ACGT" for c in s):
        return "crispr"
    return "protein"


# ── Account parsing ───────────────────────────────────────────────────────────

def _parse_account(raw: bytes, pubkey_b58: str) -> Optional[dict]:
    """
    Parse a ResultSubmission account byte-blob.

    Returns a dict or None if the account is not Confirmed or is malformed.
    The returned dict is one row for life_brain_dataset.jsonl.
    """
    if len(raw) < _MIN_LEN:
        return None
    if raw[:8] != _DISC_RESULT:
        return None

    status = raw[_OFF_STATUS]
    if status != _STATUS_CONFIRMED:
        return None   # Pending / Validating / Rejected — discard

    try:
        miner_pk   = _b58enc(raw[_OFF_MINER_PK : _OFF_MINER_PK + 32])
        target_id  = struct.unpack_from("<H",  raw, _OFF_TARGET_ID)[0]   # u16
        epoch      = struct.unpack_from("<Q",  raw, _OFF_EPOCH)[0]        # u64
        smiles_len = struct.unpack_from("<H",  raw, _OFF_SMILES_LEN)[0]  # u16
        smiles_len = min(smiles_len, 512)
        sequence   = raw[_OFF_SMILES : _OFF_SMILES + smiles_len].decode("utf-8", errors="replace").rstrip("\x00")
        affinity   = struct.unpack_from("<f",  raw, _OFF_AFFINITY)[0]    # f32
        slot       = struct.unpack_from("<q",  raw, _OFF_SUBMITTED_SLOT)[0]  # i64
    except struct.error as e:
        log.debug(f"[INGEST] parse failed ({pubkey_b58[:8]}…): {e}")
        return None

    if not sequence:
        return None

    modality = _modality(target_id, sequence)

    return {
        "pubkey":      pubkey_b58,
        "miner_wallet": miner_pk,
        "target_id":   target_id,
        "epoch":       epoch,
        "sequence":    sequence,
        "modality":    modality,
        "label":       affinity,     # claimed_affinity on a Confirmed account
        "submitted_slot": slot,
        "ts":          time.time(),
    }


# ── Deduplication state ───────────────────────────────────────────────────────

def _load_seen() -> set[str]:
    """Load set of already-ingested pubkeys from state file."""
    if not _STATE_PATH.exists():
        return set()
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return set(state.get("seen_pubkeys", []))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"seen_pubkeys": sorted(seen), "last_saved": time.time()}),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"[INGEST] save_seen failed: {e}")


# ── Main poll ─────────────────────────────────────────────────────────────────

def poll_once(solana_rpc: str) -> int:
    """
    Fetch all ResultSubmission PDAs, parse Confirmed ones, append new rows.
    Returns count of newly ingested rows.
    """
    seen = _load_seen()
    new_count = 0

    # getProgramAccounts with discriminator memcmp filter
    disc_b58 = _b58enc(_DISC_RESULT)
    result = _rpc(solana_rpc, "getProgramAccounts", [
        PROGRAM_ID,
        {
            "encoding": "base64",
            "filters": [{"memcmp": {"offset": 0, "bytes": disc_b58}}],
        },
    ])

    if not isinstance(result, list):
        log.warning("[INGEST] getProgramAccounts returned no list — RPC error or empty program?")
        return 0

    new_rows: list[dict] = []

    for item in result:
        try:
            pubkey = item["pubkey"]
            if pubkey in seen:
                continue
            raw_b64 = item["account"]["data"][0]
            raw     = base64.b64decode(raw_b64)
        except (KeyError, TypeError, Exception):
            continue

        row = _parse_account(raw, pubkey)
        if row is None:
            continue   # not Confirmed or malformed

        new_rows.append(row)
        seen.add(pubkey)
        new_count += 1

    if new_rows:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with _DATASET_PATH.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row) + "\n")
        _save_seen(seen)
        log.info(f"[INGEST] +{new_count} confirmed rows ingested "
                 f"(total seen={len(seen)})")
    else:
        log.debug(f"[INGEST] Poll complete — no new confirmed rows "
                  f"(checked {len(result)} PDAs, seen={len(seen)})")

    return new_count


# ── Background thread ─────────────────────────────────────────────────────────

_stop_event = threading.Event()
_ingest_thread: Optional[threading.Thread] = None
_new_rows_since_last_check = 0   # read by life_brain_runner for retrain trigger
_new_rows_lock = threading.Lock()


def start_background_ingest(solana_rpc: str) -> None:
    """
    Start the continuous ingestion background thread.
    Polls every POLL_INTERVAL_S seconds (default 300 = 5 min).
    Safe to call multiple times — no-op if already running.
    """
    global _ingest_thread

    if _ingest_thread is not None and _ingest_thread.is_alive():
        log.debug("[INGEST] Background thread already running.")
        return

    _stop_event.clear()

    def _loop():
        global _new_rows_since_last_check
        log.info(f"[INGEST] Background thread started — polling every {POLL_INTERVAL_S}s")
        while not _stop_event.is_set():
            try:
                n = poll_once(solana_rpc)
                with _new_rows_lock:
                    _new_rows_since_last_check += n
            except Exception as e:
                log.error(f"[INGEST] poll_once raised unexpectedly: {e}", exc_info=True)
            _stop_event.wait(POLL_INTERVAL_S)
        log.info("[INGEST] Background thread stopped.")

    _ingest_thread = threading.Thread(target=_loop, name="life-brain-ingest", daemon=True)
    _ingest_thread.start()


def stop_background_ingest() -> None:
    _stop_event.set()
    if _ingest_thread is not None:
        _ingest_thread.join(timeout=5)


def consume_new_row_count() -> int:
    """
    Return and reset the counter of rows ingested since last call.
    Used by life_brain_runner to decide when to trigger a retrain.
    """
    global _new_rows_since_last_check
    with _new_rows_lock:
        n = _new_rows_since_last_check
        _new_rows_since_last_check = 0
    return n
