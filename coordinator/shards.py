"""
Shard manager: download lease shards, verify SHA-256, LRU disk cache (C3/C24).

C24 fix — shard format is plain .smi.gz (gzip-compressed SMILES text, NOT tar):
- Each shard archive stored under data/shards/<sha256>.smi.gz
- SHA-256 verified against the lease sha256 field
- file_name from the lease shard entry carries the correct extension
  (e.g. savi_novel_0001.smi.gz); miner uses it to cache with correct suffix
- _molecules_from_smi_gz: gzip.decompress → split lines → yield SMILES
- Backward compat: if the URL ends in .tar.gz the old tarfile path is kept
  (guarded by _is_tar_url to handle any future format change)
- LRU eviction unchanged
- Falls back gracefully if coordinator or shard CDN is unreachable
- Shard row index is preserved in molecule records for result submission
- Never uses /tmp — all files written under the miner's data folder
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

from .ua import MINER_UA

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
    return sum(
        f.stat().st_size
        for f in SHARD_DIR.iterdir()
        if f.is_file() and (f.suffix == ".gz")
    )


def _evict_lru(needed_bytes: int) -> None:
    """Remove oldest-accessed shard files until needed_bytes are free."""
    files = sorted(
        [(f.stat().st_atime, f) for f in SHARD_DIR.iterdir()
         if f.is_file() and f.suffix == ".gz"]
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


def _is_tar_url(url: str) -> bool:
    """True when the URL ends with .tar.gz — legacy format check."""
    return url.lower().endswith(".tar.gz")


def _cache_path(expected_sha256: str, url: str) -> Path:
    """
    Derive the local cache path for a shard.

    C24: shards are plain .smi.gz (not .tar.gz). We derive the extension from
    the URL so that the cache filename reflects the actual file format.
    Cached files are named <sha256><ext> — e.g. <sha256>.smi.gz.
    """
    if _is_tar_url(url):
        ext = ".tar.gz"
    else:
        ext = ".smi.gz"
    return SHARD_DIR / f"{expected_sha256}{ext}"


def _download_shard(url: str, expected_sha256: str) -> Optional[Path]:
    """
    Download a shard to the cache.
    Returns the local Path on success, None on failure.
    Verifies SHA-256 before storing; retries once on mismatch.

    C24: saves with correct extension derived from the URL
    (plain .smi.gz, not .tar.gz).
    """
    dest = _cache_path(expected_sha256, url)
    if dest.exists():
        actual = _sha256_file(dest)
        if actual == expected_sha256:
            dest.touch()   # update atime for LRU
            log.debug("[SHARD] cache hit %s", expected_sha256[:16])
            return dest
        log.warning("[SHARD] cache file corrupt — re-downloading %s", expected_sha256[:16])
        dest.unlink(missing_ok=True)

    # Remove any stale file with the wrong extension for the same hash
    for ext in (".tar.gz", ".smi.gz"):
        stale = SHARD_DIR / f"{expected_sha256}{ext}"
        if stale != dest and stale.exists():
            stale.unlink(missing_ok=True)
            log.info("[SHARD] removed stale file with wrong extension %s", stale.name)

    log.info("[SHARD] downloading shard %s …", expected_sha256[:16])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MINER_UA})
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


def _molecules_from_smi_gz(path: Path) -> Iterator[tuple[str, int]]:
    """
    Yield (smiles, row_index) from a plain gzip-compressed SMILES file.

    C24: coordinator shards are .smi.gz — plain gzip over a text file where
    each line is a SMILES string (optionally followed by a name/id after a
    space, matching the standard .smi format).  There is NO tar layer.

    Format: one SMILES per line, whitespace-separated (first token = SMILES).
    Lines starting with '#' or blank lines are skipped.
    """
    try:
        with gzip.open(path, "rb") as fh:
            content = fh.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.error("[SHARD] failed to read archive %s: %s", path, e)
        return

    row = 0
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        smiles = line.split()[0]
        if smiles:
            yield smiles, row
        row += 1


def _molecules_from_tar_gz(path: Path) -> Iterator[tuple[str, int]]:
    """
    Yield (smiles, row_index) from a shard tar.gz archive (legacy format).

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
                smiles = line.split()[0]
            if smiles:
                yield smiles, row
            row += 1
    except Exception as e:
        log.error("[SHARD] failed to read archive %s: %s", path, e)


def _molecules_from_archive(path: Path) -> Iterator[tuple[str, int]]:
    """
    Dispatch to the correct reader based on the cached file's extension.

    C24: .smi.gz → plain gzip text; .tar.gz → tar+gzip (legacy).
    """
    name = path.name
    if name.endswith(".tar.gz"):
        yield from _molecules_from_tar_gz(path)
    else:
        # .smi.gz — plain gzip (C24 standard format)
        yield from _molecules_from_smi_gz(path)


class ShardManager:
    """
    Download and iterate molecules from coordinator lease shards (C24).

    Usage:
        sm = ShardManager()
        for smiles, shard_id, row in sm.iter_molecules(lease.shards):
            ...
    Falls back to an empty iterator if all shards fail to download.

    C24: shard entries must carry download_url and sha256.  The file_name
    field (added to GET /work in C24) is informational only — the cache path
    is derived from the URL extension.
    """

    def __init__(self, shard_dir: Path = SHARD_DIR):
        self.shard_dir = shard_dir
        self.shard_dir.mkdir(parents=True, exist_ok=True)

    def iter_molecules(
        self, shards: list[dict]
    ) -> Iterator[tuple[str, int, int]]:
        """
        Yield (smiles, shard_id, row_index) from all shards in the lease.

        shard entry keys: shard_id, download_url, sha256, mol_count[, file_name]
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
