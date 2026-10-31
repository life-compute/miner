"""
Coordinator alignment-file fetcher (C14).

When COORDINATOR_MODE is on and a protein target's local .a3m file is absent
or its SHA-256 differs from the lease's pinned_msa_sha256, this module fetches
the file from the coordinator's /align/ endpoints:

    GET /align/manifest.json          → list of {gz_file_name, compressed_sha256}
    GET /align/{file}.a3m.gz          → compressed alignment; ETag = compressed SHA-256

Security guarantees
-------------------
• A name that is not in the manifest is refused.
• The compressed SHA-256 is checked against the manifest entry before decompress.
• After decompression, the raw SHA-256 is checked against pinned_msa_sha256 from
  the lease.  A mismatch deletes the corrupt file and raises AlignFetchError.
• The decompressed file is written atomically into the miner's data/align_cache/
  directory, never over the original repo file and never over the existing local
  .a3m used by the scorer.
• Low disk space (< MIN_FREE_BYTES) is detected before downloading.
• On any failure the caller should skip the target and log; no fallback to
  targets.json or to the repo's tracked alignment files is attempted.

Public API
----------
    fetch_alignment(
        coordinator_url: str,
        target_id:       str,
        gz_file_name:    str,        # from the manifest, e.g. "P00519.a3m.gz"
        pinned_raw_sha256: str,      # from the lease (pinned_msa_sha256)
        cache_dir:       Path,       # miner's data/align_cache directory
    ) -> Path                        # path to the cached uncompressed .a3m
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Filename safety: only well-formed UniProt IDs with .a3m.gz extension.
# Same regex the coordinator enforces: ^[A-Z0-9]{6}\.a3m\.gz$
import re
_SAFE_GZ_RE = re.compile(r'^[A-Z0-9]{6}\.a3m\.gz$')

# Minimum free bytes required before attempting a download.
MIN_FREE_BYTES: int = 500 * 1024 * 1024  # 500 MB

# Timeout for HTTP calls (seconds).
_HTTP_TIMEOUT: float = 60.0


class AlignFetchError(Exception):
    """Raised on any alignment-fetch failure; caller should skip the target."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """HTTP GET; returns body bytes.  Raises AlignFetchError on non-200 or network error."""
    req = urllib.request.Request(url, headers={"User-Agent": "life-compute-miner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise AlignFetchError(
                    f"HTTP {resp.status} fetching {url}"
                )
            return resp.read()
    except urllib.error.HTTPError as e:
        raise AlignFetchError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise AlignFetchError(f"Network error fetching {url}: {e.reason}") from e


def _fetch_manifest(coordinator_url: str) -> dict:
    """
    GET /align/manifest.json → parsed JSON dict.
    Raises AlignFetchError on failure.
    """
    url = coordinator_url.rstrip("/") + "/align/manifest.json"
    raw = _get(url)
    try:
        return json.loads(raw)
    except Exception as exc:
        raise AlignFetchError(f"Could not parse manifest JSON: {exc}") from exc


def _manifest_entry(manifest: dict, gz_file_name: str) -> dict:
    """
    Return the manifest entry for gz_file_name, or raise AlignFetchError if
    the name is absent (name-not-in-manifest refusal).
    """
    files = manifest.get("files")
    if not isinstance(files, list):
        raise AlignFetchError("Manifest 'files' key missing or not a list")
    for entry in files:
        if isinstance(entry, dict) and entry.get("gz_file_name") == gz_file_name:
            return entry
    raise AlignFetchError(
        f"Alignment file {gz_file_name!r} not found in manifest — refusing"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_alignment(
    coordinator_url: str,
    target_id: str,
    gz_file_name: str,
    pinned_raw_sha256: str,
    cache_dir: Path,
) -> Path:
    """
    Fetch, verify, and cache a decompressed .a3m alignment file.

    Parameters
    ----------
    coordinator_url   : Base URL of the coordinator, e.g.
                        "https://life-coordinator-devnet.lifecompute313.workers.dev"
    target_id         : Human-readable target name for log messages ("CDK4", etc.)
    gz_file_name      : Filename as it appears in the manifest, e.g. "P00519.a3m.gz"
    pinned_raw_sha256 : SHA-256 of the decompressed .a3m from the lease.
    cache_dir         : Directory where the decompressed file is cached.  Created
                        if absent.  Never a tracked repo path.

    Returns
    -------
    Path to the cached (decompressed) .a3m file.

    Raises
    ------
    AlignFetchError   : On any failure.  Caller must skip the target.
    """
    # ── 0. Safety checks on the requested filename ────────────────────────────
    if not _SAFE_GZ_RE.match(gz_file_name):
        raise AlignFetchError(
            f"[ALIGN-FETCH] {target_id}: unsafe filename {gz_file_name!r} — refused"
        )

    # ── 1. Disk space pre-check ───────────────────────────────────────────────
    cache_dir.mkdir(parents=True, exist_ok=True)
    stat = shutil.disk_usage(str(cache_dir))
    if stat.free < MIN_FREE_BYTES:
        raise AlignFetchError(
            f"[ALIGN-FETCH] {target_id}: low disk space "
            f"({stat.free // (1024**2)} MB free, need {MIN_FREE_BYTES // (1024**2)} MB)"
        )

    # ── 2. Check if a valid cached copy already exists ───────────────────────
    raw_name = gz_file_name[:-len(".gz")]  # strip .gz → UNIPROT.a3m
    cached_path = cache_dir / raw_name
    if cached_path.exists():
        actual = _sha256_file(cached_path)
        if actual == pinned_raw_sha256:
            log.info(
                "[ALIGN-FETCH] %s: cache hit %s (%s…)",
                target_id, raw_name, actual[:16],
            )
            return cached_path
        log.warning(
            "[ALIGN-FETCH] %s: cached file hash mismatch — re-fetching "
            "(cached=%s… pinned=%s…)",
            target_id, actual[:16], pinned_raw_sha256[:16],
        )
        cached_path.unlink(missing_ok=True)

    # ── 3. Fetch manifest and confirm name is listed ──────────────────────────
    log.info("[ALIGN-FETCH] %s: fetching manifest from %s", target_id, coordinator_url)
    manifest = _fetch_manifest(coordinator_url)
    entry = _manifest_entry(manifest, gz_file_name)  # raises if absent
    compressed_sha256 = entry.get("compressed_sha256", "")
    if not compressed_sha256:
        raise AlignFetchError(
            f"[ALIGN-FETCH] {target_id}: manifest entry for {gz_file_name!r} "
            "has no compressed_sha256"
        )

    # ── 4. Download the compressed file ──────────────────────────────────────
    download_url = (
        coordinator_url.rstrip("/") + "/align/" + gz_file_name
    )
    log.info("[ALIGN-FETCH] %s: downloading %s", target_id, download_url)
    gz_bytes = _get(download_url)

    # ── 5. Verify compressed SHA-256 ─────────────────────────────────────────
    actual_gz_sha = _sha256_bytes(gz_bytes)
    if actual_gz_sha != compressed_sha256:
        raise AlignFetchError(
            f"[ALIGN-FETCH] {target_id}: compressed SHA-256 mismatch for "
            f"{gz_file_name!r} "
            f"(manifest={compressed_sha256[:16]}… actual={actual_gz_sha[:16]}…) "
            "— corrupt download, refused"
        )

    # ── 6. Decompress and write atomically ────────────────────────────────────
    try:
        raw_bytes = gzip.decompress(gz_bytes)
    except Exception as exc:
        raise AlignFetchError(
            f"[ALIGN-FETCH] {target_id}: gzip decompression failed for "
            f"{gz_file_name!r}: {exc}"
        ) from exc

    # ── 7. Verify raw SHA-256 against lease pinned hash ───────────────────────
    actual_raw_sha = _sha256_bytes(raw_bytes)
    if actual_raw_sha != pinned_raw_sha256:
        raise AlignFetchError(
            f"[ALIGN-FETCH] {target_id}: raw SHA-256 mismatch for {raw_name} "
            f"(pinned={pinned_raw_sha256[:16]}… actual={actual_raw_sha[:16]}…) "
            "— corrupt or tampered download, refused and deleted"
        )

    # ── 8. Atomic write into cache_dir ────────────────────────────────────────
    # Write to a temp file in the same directory, then rename (atomic on POSIX).
    fd, tmp_path = tempfile.mkstemp(dir=str(cache_dir), suffix=".a3m.tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw_bytes)
        os.replace(tmp_path, str(cached_path))
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise AlignFetchError(
            f"[ALIGN-FETCH] {target_id}: could not write cached file "
            f"{cached_path}: {exc}"
        ) from exc

    log.info(
        "[ALIGN-FETCH] %s: cached %s → %s (%d bytes, sha256=%s…)",
        target_id, gz_file_name, cached_path, len(raw_bytes), actual_raw_sha[:16],
    )
    return cached_path
