#!/usr/bin/env python3
"""
LIFE Compute — Adaptive MSA Auto-Downloader
============================================
adaptive/auto_msa.py

Public API
----------
    ensure_msa(uniprot_id, gene_name="") -> str
        Returns path to .a3m file, or "empty" if unavailable.
        Downloads synchronously if file is missing.
        Safe to call from the main miner loop.

    start_background_prefetch(targets: list[dict]) -> None
        Launches a daemon thread that downloads MSAs for upcoming targets
        in priority order (tier 1 → tier 4), 1 request/second.
        Call once at miner startup. Idempotent — ignores second call.

    prefetch_status() -> dict
        Returns {"queued": N, "downloaded": N, "failed": N, "in_progress": str|None}

Design
------
- ColabFold mmseqs2 API (same endpoint as existing download_msas.py)
- Rate-limited: 1 request/second hard floor (ColabFold ToS)
- Null-byte stripping on all a3m content
- Validation: file > 1 KB AND starts with valid a3m header (">")
- Skip if already downloaded (content-valid check, not just os.path.exists)
- Thread-safe: uses a threading.Lock for the rate-limit token bucket
- Atomic write: writes to .tmp then renames to avoid partial reads by miner
- Retries: up to 5 submit attempts, up to 30 poll cycles (2.5 min max wait)
"""

import json
import logging
import os
import re
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("life-auto-msa")

# ── Configuration ─────────────────────────────────────────────────────────────
_COLABFOLD_URL = "https://api.colabfold.com"
_UNIPROT_URL   = "https://rest.uniprot.org/uniprotkb/{uid}.fasta"
_HEADERS       = {"User-Agent": "colabfold"}

_MSA_DIR = Path(
    os.environ.get(
        "MSA_DIR",
        "/mnt/minos-drive/life-compute-miner/data/msa_files",
    )
)

_MIN_FILE_BYTES  = 1024          # 1 KB minimum valid a3m
_RATE_LIMIT_SEC  = 1.0           # minimum seconds between ColabFold API calls
_SUBMIT_RETRIES  = 5
_POLL_MAX_CYCLES = 60            # 60 × 5s = 5 min max wait per job
_POLL_INTERVAL   = 5             # seconds between status polls

# ── Rate-limit token bucket ───────────────────────────────────────────────────
_api_lock        = threading.Lock()
_last_api_call   = 0.0           # epoch time of last ColabFold API call

def _rate_limited_request(fn):
    """Wrap any ColabFold API call to enforce ≥ 1s between requests."""
    global _last_api_call
    with _api_lock:
        now = time.time()
        wait = _RATE_LIMIT_SEC - (now - _last_api_call)
        if wait > 0:
            time.sleep(wait)
        result = fn()
        _last_api_call = time.time()
    return result

# ── Background prefetch state ─────────────────────────────────────────────────
_bg_thread:   Optional[threading.Thread] = None
_bg_started   = False
_bg_lock      = threading.Lock()

_status_lock   = threading.Lock()
_status: dict  = {"queued": 0, "downloaded": 0, "failed": 0, "in_progress": None}


def _update_status(**kw):
    with _status_lock:
        _status.update(kw)


def prefetch_status() -> dict:
    with _status_lock:
        return dict(_status)


# ── a3m validation ────────────────────────────────────────────────────────────

def _valid_a3m(path: Path) -> bool:
    """Return True if path exists, > 1 KB, and starts with a FASTA '>' header."""
    try:
        if not path.exists():
            return False
        size = path.stat().st_size
        if size < _MIN_FILE_BYTES:
            return False
        first = path.read_bytes()[:32]
        return first.lstrip(b"\x00").startswith(b">")
    except OSError:
        return False


def _strip_nulls(content: str) -> str:
    return content.replace("\x00", "")


# ── UniProt sequence fetch ────────────────────────────────────────────────────

def _fetch_sequence(uniprot_id: str, retries: int = 5) -> str:
    url = _UNIPROT_URL.format(uid=uniprot_id)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/plain"})
            with urllib.request.urlopen(req, timeout=20) as r:
                lines = r.read().decode("utf-8").strip().splitlines()
            seq = "".join(l for l in lines if not l.startswith(">"))
            if not seq:
                raise ValueError("empty sequence")
            return seq
        except Exception as e:
            if attempt == retries - 1:
                raise RuntimeError(f"UniProt fetch failed for {uniprot_id}: {e}") from e
            time.sleep(2 ** attempt)
    return ""   # unreachable


# ── ColabFold API: submit / poll / download ───────────────────────────────────

def _cf_post(endpoint: str, data: dict, timeout: int = 20) -> dict:
    def _do():
        encoded = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            f"{_COLABFOLD_URL}/{endpoint}",
            data=encoded,
            headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    return _rate_limited_request(_do)


def _cf_get(endpoint: str, timeout: int = 20) -> dict:
    def _do():
        req = urllib.request.Request(
            f"{_COLABFOLD_URL}/{endpoint}",
            headers=_HEADERS,
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    return _rate_limited_request(_do)


def _submit_job(sequence: str) -> str:
    """Submit MSA job; return job_id. Handles RATELIMIT with exponential backoff."""
    query = f">query\n{sequence}\n"
    for attempt in range(_SUBMIT_RETRIES):
        resp = _cf_post("ticket/msa", {"q": query, "mode": "env"})
        status = resp.get("status", "")
        if status == "RATELIMIT":
            wait = 10 * (2 ** attempt)
            log.warning(f"  ColabFold RATELIMIT — waiting {wait}s")
            time.sleep(wait)
            continue
        if status == "ERROR":
            raise RuntimeError(f"ColabFold API error: {resp}")
        job_id = resp.get("id") or resp.get("job_id")
        if not job_id:
            raise RuntimeError(f"No job_id in response: {resp}")
        return job_id
    raise RuntimeError("ColabFold submission failed after all retries")


def _poll_job(job_id: str) -> bool:
    """Poll until COMPLETE. Returns True on success, False on error/timeout."""
    for cycle in range(_POLL_MAX_CYCLES):
        resp = _cf_get(f"ticket/{job_id}")
        status = resp.get("status", "UNKNOWN")
        if status == "COMPLETE":
            return True
        if status in ("ERROR", "FAILED"):
            log.warning(f"  Job {job_id} ended with status={status}")
            return False
        # RUNNING / PENDING / UNKNOWN — keep waiting
        log.debug(f"  [{cycle * _POLL_INTERVAL:4d}s] job={job_id} status={status}")
        time.sleep(_POLL_INTERVAL)
    log.warning(f"  Job {job_id} timed out after {_POLL_MAX_CYCLES * _POLL_INTERVAL}s")
    return False


def _download_result(job_id: str, tmp_path: Path) -> None:
    """Stream-download the result tar.gz to tmp_path."""
    def _do():
        req = urllib.request.Request(
            f"{_COLABFOLD_URL}/result/download/{job_id}",
            headers=_HEADERS,
        )
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp_path, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    _rate_limited_request(_do)


def _extract_a3m(tar_path: Path) -> str:
    """Extract and merge uniref.a3m + env a3m from ColabFold tar.gz."""
    with tarfile.open(tar_path, "r:gz") as tf:
        members = tf.getnames()
        uniref = env = ""
        for m in members:
            if m.endswith("uniref.a3m"):
                f = tf.extractfile(m)
                if f:
                    uniref = f.read().decode("utf-8", errors="replace")
            elif "bfd" in m and m.endswith(".a3m"):
                f = tf.extractfile(m)
                if f:
                    env = f.read().decode("utf-8", errors="replace")

        if not uniref:
            raise RuntimeError("uniref.a3m missing from ColabFold result tar")

        combined = uniref
        if env:
            # Append env hits, skipping the query record (">query" or ">101")
            skip = True
            extra = []
            for line in env.splitlines():
                if line.startswith(">") and not re.match(r"^>(query|101)\s*$", line.strip()):
                    skip = False
                if not skip:
                    extra.append(line)
            if extra:
                combined = combined.rstrip("\n") + "\n" + "\n".join(extra)

        return _strip_nulls(combined)


# ── Core download function ────────────────────────────────────────────────────

def _download_msa(uniprot_id: str, gene_name: str = "") -> bool:
    """
    Full pipeline for one UniProt ID:
      1. Fetch sequence from UniProt
      2. Submit MSA job to ColabFold
      3. Poll until complete
      4. Download + extract tar.gz
      5. Validate > 1KB + valid a3m header
      6. Atomic write (tmp → rename)

    Returns True on success, False on any failure.
    """
    label = gene_name or uniprot_id
    out_path = _MSA_DIR / f"{uniprot_id}.a3m"

    if _valid_a3m(out_path):
        log.debug(f"[auto_msa] {label}: already valid ({out_path.stat().st_size // 1024} KB)")
        return True

    log.info(f"[auto_msa] {label} ({uniprot_id}): starting download")
    _MSA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1 — sequence
        seq = _fetch_sequence(uniprot_id)
        log.info(f"[auto_msa] {label}: sequence {len(seq)} aa")

        # Step 2 — submit
        job_id = _submit_job(seq)
        log.info(f"[auto_msa] {label}: submitted job={job_id}")

        # Step 3 — poll
        if not _poll_job(job_id):
            log.warning(f"[auto_msa] {label}: job failed/timed out")
            return False

        # Step 4 — download to temp file
        tmp = Path(tempfile.mktemp(suffix=".tar.gz", dir=_MSA_DIR))
        try:
            _download_result(job_id, tmp)
            a3m = _extract_a3m(tmp)
        finally:
            tmp.unlink(missing_ok=True)

        # Step 5 — validate
        if len(a3m) < _MIN_FILE_BYTES:
            log.warning(f"[auto_msa] {label}: result too small ({len(a3m)} bytes)")
            return False
        if not a3m.lstrip("\x00").startswith(">"):
            log.warning(f"[auto_msa] {label}: invalid a3m header")
            return False

        # Step 6 — atomic write
        tmp_out = out_path.with_suffix(".a3m.tmp")
        tmp_out.write_text(a3m)
        tmp_out.rename(out_path)

        size_kb = out_path.stat().st_size // 1024
        seqs    = a3m.count(">")
        log.info(f"[auto_msa] {label}: saved {size_kb} KB ({seqs} sequences)")
        return True

    except Exception as e:
        log.warning(f"[auto_msa] {label}: exception — {e}")
        return False


# ── Public: synchronous ensure_msa ───────────────────────────────────────────

def ensure_msa(uniprot_id: str, gene_name: str = "") -> str:
    """
    Return path to .a3m file for uniprot_id, downloading if necessary.
    Returns "empty" if download fails or validation fails.
    Safe to call from the main miner loop for the current target.
    """
    out_path = _MSA_DIR / f"{uniprot_id}.a3m"
    if _valid_a3m(out_path):
        return str(out_path)
    ok = _download_msa(uniprot_id, gene_name)
    return str(out_path) if ok and _valid_a3m(out_path) else "empty"


# ── Public: background prefetch ───────────────────────────────────────────────

def start_background_prefetch(targets: list[dict]) -> None:
    """
    Launch a daemon thread that downloads MSAs for all targets in priority order:
      tier 1 → tier 2 → tier 3 → tier 4, skipping already-valid files.
    Rate-limited to 1 ColabFold request/second.
    Idempotent — safe to call multiple times (only one thread starts).
    """
    global _bg_thread, _bg_started

    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True

    # Sort: tier 1 first, then alphabetically within tier
    sorted_targets = sorted(
        targets,
        key=lambda t: (t.get("difficulty_tier", 9), t.get("id", "")),
    )

    # Filter to only those missing a valid a3m
    pending = [
        t for t in sorted_targets
        if not _valid_a3m(_MSA_DIR / f"{t['uniprot_id']}.a3m")
    ]
    log.info(f"[auto_msa] Background prefetch: {len(pending)} MSAs to download "
             f"({len(sorted_targets) - len(pending)} already cached)")

    _update_status(queued=len(pending), downloaded=0, failed=0, in_progress=None)

    def _worker():
        downloaded = 0
        failed     = 0
        for t in pending:
            uid   = t["uniprot_id"]
            gene  = t.get("gene_name") or t.get("id", uid)
            _update_status(in_progress=f"{gene} ({uid})")
            ok = _download_msa(uid, gene)
            if ok:
                downloaded += 1
                _update_status(downloaded=downloaded, queued=len(pending) - downloaded - failed)
            else:
                failed += 1
                _update_status(failed=failed, queued=len(pending) - downloaded - failed)
        _update_status(in_progress=None)
        log.info(f"[auto_msa] Prefetch complete: {downloaded} downloaded, {failed} failed")

    _bg_thread = threading.Thread(target=_worker, daemon=True, name="auto-msa-prefetch")
    _bg_thread.start()
    log.info(f"[auto_msa] Background prefetch thread started ({len(pending)} pending)")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="LIFE Compute — MSA auto-downloader\n"
                    "Downloads ColabFold MSAs for targets in targets_2000.json or targets.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--targets", default=None,
        help="Path to targets JSON (default: targets_2000.json then targets.json)")
    parser.add_argument("--uid", default=None,
        help="Download a single UniProt ID immediately and exit")
    parser.add_argument("--status", action="store_true",
        help="Show which MSAs are cached and exit")
    parser.add_argument("--tier-max", type=int, default=4,
        help="Only download tiers ≤ N (default: 4 = all)")
    parser.add_argument("--limit", type=int, default=0,
        help="Stop after N downloads (0 = no limit)")
    args = parser.parse_args()

    # ── status mode ──────────────────────────────────────────────────────────
    if args.status:
        files = sorted(_MSA_DIR.glob("*.a3m"))
        valid = [f for f in files if _valid_a3m(f)]
        print(f"MSA cache: {_MSA_DIR}")
        print(f"  Files:     {len(files)}")
        print(f"  Valid:     {len(valid)}")
        print(f"  Corrupt:   {len(files) - len(valid)}")
        if valid:
            sizes = [f.stat().st_size // 1024 for f in valid]
            print(f"  Size range: {min(sizes)}–{max(sizes)} KB  avg {sum(sizes)//len(sizes)} KB")
        raise SystemExit(0)

    # ── single UID mode ───────────────────────────────────────────────────────
    if args.uid:
        path = ensure_msa(args.uid)
        print(f"Result: {path}")
        raise SystemExit(0 if path != "empty" else 1)

    # ── batch mode: load targets file ─────────────────────────────────────────
    targets_path = None
    if args.targets:
        targets_path = Path(args.targets)
    else:
        for candidate in [
            Path(__file__).parent.parent / "data" / "targets_2000.json",
            Path("/tmp/life-compute/targets/targets_2000.json"),
            Path("/tmp/targets_2000.json"),
            Path(__file__).parent.parent / "data" / "targets.json",
        ]:
            if candidate.exists():
                targets_path = candidate
                break

    if not targets_path or not targets_path.exists():
        print("ERROR: no targets file found. Use --targets /path/to/targets.json")
        raise SystemExit(1)

    print(f"Loading targets from {targets_path}")
    targets = json.loads(targets_path.read_text())
    if args.tier_max < 4:
        targets = [t for t in targets if t.get("difficulty_tier", 4) <= args.tier_max]
    print(f"  {len(targets)} targets (tier ≤ {args.tier_max})")

    # sort tier → name
    targets.sort(key=lambda t: (t.get("difficulty_tier", 9), t.get("id", "")))

    downloaded = 0
    skipped    = 0
    failed     = 0

    for t in targets:
        uid  = t["uniprot_id"]
        gene = t.get("gene_name") or t.get("id", uid)
        out  = _MSA_DIR / f"{uid}.a3m"

        if _valid_a3m(out):
            skipped += 1
            continue

        ok = _download_msa(uid, gene)
        if ok:
            downloaded += 1
        else:
            failed += 1

        if args.limit and downloaded >= args.limit:
            print(f"--limit {args.limit} reached, stopping.")
            break

    print(f"\nDone: {downloaded} downloaded, {skipped} skipped, {failed} failed")
    raise SystemExit(0 if failed == 0 else 1)
