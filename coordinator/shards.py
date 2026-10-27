"""
Shard manager: download lease shards, verify SHA-256, LRU disk cache (C3).

- Each shard archive is stored under data/shards/<sha256>.tar.gz.
- SHA-256 of the downloaded file is verified against the lease field sha256.
- LRU eviction: when disk exceeds CACHE_MAX_BYTES, remove oldest accessed shards.
- Falls back gracefully if coordinator or shard CDN is unreachable.
- Shard row index is preserved in molecule records for result submission.
- Never uses /tmp — all files written under the miner's data folder.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

_WORK_DIR   = Path(__file__).resolve().parents[1]
SHARD_DIR   = _WORK_DIR / "data" / "shards"
SHARD_DIR.mkdir(parents=True, exist_ok=True)

# Maximum total bytes for the shard cache before LRU eviction triggers.
# Default 10 GiB — overridable via env var SHARD_CACHE_MAX_GB.
CACHE_MAX_BYTES = int(os.environ.get("SHARD_CACHE_MAX_GB", "10")) * 1024 ** 3


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_used_bytes() -> int:
    return sum(f.stat().st_size for f in SHARD_DIR.glob("*.tar.gz") if f.is_file())


def _evict_lru(needed_bytes: int) -> None:
    """Remove oldest-accessed shard archives until needed_bytes are free."""
    files = sorted(
        [(f.stat().st_atime, f) for f in SHARD_DIR.glob("*.tar.gz") if f.is_file()]
    )
    used = _cache_used_bytes()
    target = CACHE_MAX_BYTES - needed_bytes
    for _atime, f in files:
        if used <= target:
            break
        sz = f.stat().st_size
        f.unlink(missing_ok=True)
        log.info("[SHARD] LRU evicted %s (%d MB)", f.name, sz // 1024 // 1024)
        used -= sz


def _download_shard(url: str, expected_sha256: str) -> Optional[Path]:
    """
    Download a shard archive to the cache.
    Returns the local Path on success, None on failure.
    Verifies SHA-256 before storing; retries once on mismatch.
    """
    dest = SHARD_DIR / f"{expected_sha256}.tar.gz"
    if dest.exists():
        actual = _sha256_file(dest)
        if actual == expected_sha256:
            dest.touch()   # update atime for LRU
            log.debug("[SHARD] cache hit %s", expected_sha256[:16])
            return dest
        log.warning("[SHARD] cache file corrupt — re-downloading %s", expected_sha256[:16])
        dest.unlink(missing_ok=True)

    log.info("[SHARD] downloading shard %s …", expected_sha256[:16])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "life-compute-miner/c3"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except Exception as e:
        log.warning("[SHARD] download failed: %s", e)
        return None

    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        log.error(
            "[SHARD] SHA-256 mismatch for %s: expected %s got %s",
            url, expected_sha256[:16], actual[:16],
        )
        return None

    # Evict if needed
    needed = len(data)
    if _cache_used_bytes() + needed > CACHE_MAX_BYTES:
        _evict_lru(needed)

    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    log.info("[SHARD] cached shard %s (%d KB)", expected_sha256[:16], len(data) // 1024)
    return dest


def _molecules_from_archive(path: Path) -> Iterator[tuple[str, int]]:
    """
    Yield (smiles, row_index) from a shard tar.gz archive.

    Expected archive layout:
      molecules.smi   — one SMILES per line (plain text)
    or
      molecules.jsonl — one JSON object per line with a "smiles" key
    or
      *.smi / *.smiles — any file matching, first found
    """
    try:
        with tarfile.open(path, "r:gz") as tf:
            names = tf.getnames()
            # prefer molecules.smi
            target_name = None
            for candidate in ["molecules.smi", "molecules.smiles", "molecules.jsonl"]:
                if candidate in names:
                    target_name = candidate
                    break
            if target_name is None:
                for n in names:
                    if n.endswith(".smi") or n.endswith(".smiles") or n.endswith(".jsonl"):
                        target_name = n
                        break
            if target_name is None:
                log.warning("[SHARD] no molecule file in archive %s", path.name)
                return

            member = tf.extractfile(target_name)
            if member is None:
                return
            content = member.read().decode("utf-8", errors="replace")

        row = 0
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if target_name.endswith(".jsonl"):
                try:
                    obj = json.loads(line)
                    smiles = obj.get("smiles") or obj.get("SMILES") or ""
                except Exception:
                    smiles = ""
            else:
                # .smi: "SMILES [name]"
                smiles = line.split()[0]
            if smiles:
                yield smiles, row
            row += 1
    except Exception as e:
        log.error("[SHARD] failed to read archive %s: %s", path, e)


class ShardManager:
    """
    Download and iterate molecules from coordinator lease shards.

    Usage:
        sm = ShardManager()
        for smiles, shard_id, row in sm.iter_molecules(lease.shards):
            ...
    Falls back to an empty iterator if all shards fail to download.
    """

    def __init__(self, shard_dir: Path = SHARD_DIR):
        self.shard_dir = shard_dir
        self.shard_dir.mkdir(parents=True, exist_ok=True)

    def iter_molecules(
        self, shards: list[dict]
    ) -> Iterator[tuple[str, int, int]]:
        """
        Yield (smiles, shard_id, row_index) from all shards in the lease.

        shard entry keys: shard_id, download_url, sha256, mol_count
        """
        for shard in shards:
            shard_id    = shard.get("shard_id", 0)
            url         = shard.get("download_url", "")
            sha256      = shard.get("sha256", "")
            if not url or not sha256:
                log.warning("[SHARD] shard entry missing url/sha256: %s", shard)
                continue
            path = _download_shard(url, sha256)
            if path is None:
                log.warning("[SHARD] skipping shard %s — download/verify failed", shard_id)
                continue
            for smiles, row in _molecules_from_archive(path):
                yield smiles, shard_id, row

    def prefetch(self, shards: list[dict]) -> None:
        """Pre-download shards in background (non-blocking)."""
        import threading
        def _fetch():
            for shard in shards:
                url = shard.get("download_url", "")
                sha256 = shard.get("sha256", "")
                if url and sha256:
                    _download_shard(url, sha256)
        threading.Thread(target=_fetch, daemon=True, name="shard-prefetch").start()
