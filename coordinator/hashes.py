"""
Coordinator hash verification for protein targets (C3).

Before scoring a protein target, the miner must verify:
  1. The local receptor file SHA-256 matches pinned_receptor_sha256 from the lease.
  2. The local MSA (.a3m) file SHA-256 matches pinned_msa_sha256 from the lease.

If either hash is non-null and does not match, the target is refused:
  log it and ask for other work.

Hashes in the lease come from GET /work → scorer_profile fields.
A null hash means "not yet pinned" — the validator fails closed; the miner
should log a warning and skip.

CRITICAL: the protein_sequence field from targets.json MUST NOT be used as
the Boltz input sequence.  The alignment file first line is the authoritative
sequence source (it is what Boltz is fed and what the pinned hashes cover).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class PinnedHashError(Exception):
    """Raised when a required pinned hash does not match the local file."""


def verify_protein_target(
    target_id: str,
    receptor_path: Optional[Path],
    msa_path: Optional[Path],
    pinned_receptor_sha256: Optional[str],
    pinned_msa_sha256: Optional[str],
) -> None:
    """
    Verify local receptor and MSA files against pinned hashes from the lease.

    Raises PinnedHashError if:
      - pinned hash is non-null AND local file does not exist or hash mismatches.

    A null pinned hash causes a WARNING log but does not raise (validator fails
    closed on its own; miner treats null as "unverifiable, proceed with caution").
    """
    if pinned_receptor_sha256 is not None:
        if receptor_path is None or not receptor_path.exists():
            raise PinnedHashError(
                f"[HASH-CHECK] {target_id}: receptor file missing at {receptor_path}"
            )
        actual = _sha256_file(receptor_path)
        if actual != pinned_receptor_sha256:
            raise PinnedHashError(
                f"[HASH-CHECK] {target_id}: receptor SHA-256 mismatch "
                f"(pinned={pinned_receptor_sha256[:16]}… actual={actual[:16]}…)"
            )
        log.info("[HASH-CHECK] %s receptor OK (%s…)", target_id, actual[:16])
    else:
        log.warning(
            "[HASH-CHECK] %s: pinned_receptor_sha256 is null — not yet pinned; proceeding",
            target_id,
        )

    if pinned_msa_sha256 is not None:
        if msa_path is None or not msa_path.exists():
            raise PinnedHashError(
                f"[HASH-CHECK] {target_id}: MSA file missing at {msa_path}"
            )
        actual = _sha256_file(msa_path)
        if actual != pinned_msa_sha256:
            raise PinnedHashError(
                f"[HASH-CHECK] {target_id}: MSA SHA-256 mismatch "
                f"(pinned={pinned_msa_sha256[:16]}… actual={actual[:16]}…)"
            )
        log.info("[HASH-CHECK] %s MSA OK (%s…)", target_id, actual[:16])
    else:
        log.warning(
            "[HASH-CHECK] %s: pinned_msa_sha256 is null — not yet pinned; proceeding",
            target_id,
        )
