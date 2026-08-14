#!/usr/bin/env python3
"""
Download MSA files for all 2,000 LIFE Compute cancer targets from ColabFold API.

Usage:
    python scripts/download_all_msas.py [--targets PATH] [--out-dir DIR] [--dry-run]

Defaults:
    --targets  /tmp/life-compute/targets/targets_2000.json
    --out-dir  /mnt/life-data/msa_files/

Behaviour:
    - Reads UniProt IDs from targets JSON
    - Submits each sequence to ColabFold mmseqs2 API, polls until COMPLETE
    - Extracts and combines uniref + bfd a3m from result tar
    - Strips null bytes, verifies > 1 KB with valid a3m header
    - Saves as {UniProt_ID}.a3m; skips if already exists and > 1 KB
    - Rate-limits to 1 submit/sec (ColabFold guideline)
    - Logs failures to {out-dir}/failed_msas.txt for retry
    - Fully resumable: restart at any time, completed files are skipped
"""

import argparse
import json
import os
import random
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import requests

# Ensure output is flushed immediately when piped (e.g. PM2 / tee)
os.environ.setdefault("PYTHONUNBUFFERED", "1")

HOST_URL = "https://api.colabfold.com"
HEADERS = {"User-Agent": "colabfold"}
MIN_SIZE = 1024  # bytes — anything smaller is considered invalid


# ── API helpers ──────────────────────────────────────────────────────────────

def fetch_sequence(uniprot_id: str) -> str:
    url = f"https://rest.uniprot.org/uniprotkb/{uniprot_id}.fasta"
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            lines = r.text.strip().splitlines()
            seq = "".join(l for l in lines if not l.startswith(">"))
            if not seq:
                raise ValueError("empty sequence")
            return seq
        except Exception as e:
            last_exc = e
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"UniProt fetch failed for {uniprot_id}: {last_exc}") from last_exc


def submit_job(seq: str) -> dict:
    query = f">query\n{seq}\n"
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(10):
        try:
            r = requests.post(
                f"{HOST_URL}/ticket/msa",
                data={"q": query, "mode": "env"},
                timeout=20,
                headers=HEADERS,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            if attempt < 9:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Submit failed: {last_exc}") from last_exc


def poll_job(job_id: str) -> dict:
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(5):
        try:
            r = requests.get(f"{HOST_URL}/ticket/{job_id}", timeout=15, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            if attempt < 4:
                time.sleep(5)
    raise RuntimeError(f"Poll failed for {job_id}: {last_exc}") from last_exc


def download_tar(job_id: str, dest: str) -> None:
    for attempt in range(5):
        try:
            r = requests.get(
                f"{HOST_URL}/result/download/{job_id}",
                timeout=300,
                headers=HEADERS,
                stream=True,
            )
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            return
        except Exception as e:
            if attempt < 4:
                time.sleep(10 * (attempt + 1))
            else:
                raise RuntimeError(f"Download failed for {job_id}: {e}") from e


def extract_a3m(tar_path: str) -> str:
    """Combine uniref + bfd/env a3m from ColabFold result tar."""
    with tarfile.open(tar_path, "r:gz") as tf:
        names = tf.getnames()
        uniref = ""
        env = ""
        for name in names:
            if name.endswith("uniref.a3m"):
                m = tf.extractfile(name)
                if m:
                    uniref = m.read().decode("utf-8", errors="replace")
            elif ("bfd" in name or "env" in name) and name.endswith(".a3m"):
                m = tf.extractfile(name)
                if m:
                    env = m.read().decode("utf-8", errors="replace")

    if not uniref:
        raise RuntimeError("uniref.a3m not found in tar")

    combined = uniref.rstrip("\n")
    if env:
        # Append env sequences (skip the query header line)
        extra = [l for i, l in enumerate(env.splitlines())
                 if not (i == 0 and l.startswith(">"))]
        if extra:
            combined += "\n" + "\n".join(extra)

    return combined.replace("\x00", "")  # strip null bytes


# ── Core download ─────────────────────────────────────────────────────────────

def download_msa(uniprot_id: str, out_path: Path, dry_run: bool = False) -> bool:
    """Submit, poll, download, extract, verify. Returns True on success."""
    if dry_run:
        print(f"  [dry-run] would fetch {uniprot_id}")
        return True

    seq = fetch_sequence(uniprot_id)

    # Submit with RATELIMIT backoff
    retries = 0
    result = submit_job(seq)
    while result.get("status") in ("RATELIMIT", "UNKNOWN") and retries < 30:
        wait = 30 + random.randint(0, 15)
        print(f"  [{uniprot_id}] {result.get('status')} — backing off {wait}s")
        time.sleep(wait)
        result = submit_job(seq)
        retries += 1

    if result.get("status") == "ERROR":
        raise RuntimeError(f"API error on submit: {result}")

    job_id = result["id"]

    # Poll until COMPLETE
    elapsed = 0
    while result.get("status") in ("RUNNING", "PENDING", "UNKNOWN"):
        wait = 10 + random.randint(0, 5)
        time.sleep(wait)
        elapsed += wait
        result = poll_job(job_id)

    if result.get("status") != "COMPLETE":
        raise RuntimeError(f"Job {job_id} ended with status {result.get('status')}")

    # Download tar → extract a3m → write file
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        download_tar(job_id, tmp_path)
        a3m = extract_a3m(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    out_path.write_text(a3m, encoding="utf-8")

    size = out_path.stat().st_size
    if size < MIN_SIZE:
        out_path.unlink()
        raise RuntimeError(f"Output too small ({size} bytes)")

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download MSAs for all LIFE targets")
    parser.add_argument("--targets", default="/tmp/life-compute/targets/targets_2000.json")
    parser.add_argument("--out-dir", default="/mnt/life-data/msa_files")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    targets_path = Path(args.targets)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    failed_log = out_dir / "failed_msas.txt"

    targets = json.loads(targets_path.read_text())
    total = len(targets)

    # Deduplicate by UniProt ID (keep first occurrence)
    seen = {}
    for t in targets:
        uid = t["uniprot_id"]
        if uid not in seen:
            seen[uid] = t["id"]
    unique = list(seen.items())  # [(uniprot_id, gene_id), ...]

    print(f"LIFE Compute — ColabFold MSA Downloader")
    print(f"Targets file : {targets_path}  ({total} entries, {len(unique)} unique UniProt IDs)")
    print(f"Output dir   : {out_dir}")
    if args.dry_run:
        print("Mode         : DRY-RUN")
    print()

    # Count already done
    already = sum(1 for uid, _ in unique
                  if (out_dir / f"{uid}.a3m").exists()
                  and (out_dir / f"{uid}.a3m").stat().st_size >= MIN_SIZE)
    print(f"Already complete: {already}/{len(unique)}")
    print()

    downloaded = already
    failed_ids = []
    last_submit = 0.0

    for i, (uniprot_id, gene_id) in enumerate(unique):
        out_path = out_dir / f"{uniprot_id}.a3m"

        # Skip if already done
        if out_path.exists() and out_path.stat().st_size >= MIN_SIZE:
            continue

        # Rate limit: 1 submit/sec (skip in dry-run)
        if not args.dry_run:
            elapsed_since = time.time() - last_submit
            if elapsed_since < 1.0:
                time.sleep(1.0 - elapsed_since)

        try:
            download_msa(uniprot_id, out_path, dry_run=args.dry_run)
            last_submit = time.time()
            downloaded += 1
            size_mb = out_path.stat().st_size / 1_048_576 if out_path.exists() else 0.0
            print(f"Downloaded {downloaded}/{len(unique)}  {gene_id}/{uniprot_id}.a3m  ({size_mb:.1f} MB)", flush=True)
        except Exception as e:
            last_submit = time.time()
            msg = str(e)
            print(f"FAILED  [{i+1}/{len(unique)}]  {gene_id}/{uniprot_id}: {msg}", flush=True)
            failed_ids.append((uniprot_id, gene_id, msg))
            # Append to failed log immediately (resumable retry)
            with failed_log.open("a") as fh:
                fh.write(f"{uniprot_id}\t{gene_id}\t{msg}\n")

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("COMPLETE")
    print(f"  Downloaded : {downloaded}")
    print(f"  Failed     : {len(failed_ids)}")
    if failed_ids:
        print(f"  Failures logged to: {failed_log}")
        print()
        for uid, gid, err in failed_ids:
            print(f"    {gid}/{uid}: {err[:80]}")
        sys.exit(1)
    else:
        print(f"  All {downloaded} MSAs verified ✓")


if __name__ == "__main__":
    main()
