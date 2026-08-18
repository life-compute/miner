#!/usr/bin/env python3
"""Download MSA files from ColabFold API for a batch of UniProt IDs."""

import urllib.request
import urllib.parse
import tarfile
import time
import json
import os
import io

MSA_DIR = "/mnt/minos-drive/life-compute-miner/data/msa_files"
UNIPROT_IDS = ["O75874", "P36888", "Q13485", "P25054", "P09874",
               "O60674", "P03372", "Q13547", "Q92769", "P00519"]

COLABFOLD_BASE = "https://api.colabfold.com"
UNIPROT_BASE = "https://rest.uniprot.org/uniprotkb"
MIN_SIZE = 1000  # bytes


def check_existing(uid):
    path = os.path.join(MSA_DIR, f"{uid}.a3m")
    if os.path.exists(path) and os.path.getsize(path) > MIN_SIZE:
        return True
    return False


def fetch_uniprot_fasta(uid):
    url = f"{UNIPROT_BASE}/{uid}.fasta"
    req = urllib.request.Request(url, headers={"User-Agent": "colabfold"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        fasta = resp.read().decode("utf-8")
    # Extract sequence (skip header lines starting with >)
    lines = fasta.strip().split("\n")
    seq_lines = [l for l in lines if not l.startswith(">")]
    sequence = "".join(seq_lines)
    return sequence


def submit_msa_job(sequence):
    url = f"{COLABFOLD_BASE}/ticket/msa"
    query = f">101\n{sequence}\n"
    data = urllib.parse.urlencode({"q": query, "mode": "env"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "colabfold", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result


def poll_job(job_id, max_wait=600):
    url = f"{COLABFOLD_BASE}/ticket/{job_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "colabfold"})
    elapsed = 0
    while elapsed < max_wait:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_data = json.loads(resp.read().decode("utf-8"))
        status = status_data.get("status", "")
        print(f"  [poll] status={status} (elapsed={elapsed}s)")
        if status == "COMPLETE":
            return True
        if status in ("ERROR", "FAILED"):
            print(f"  [error] Job failed with status: {status}")
            return False
        time.sleep(10)
        elapsed += 10
    print(f"  [timeout] Job did not complete within {max_wait}s")
    return False


def download_and_extract(job_id, uid):
    url = f"{COLABFOLD_BASE}/result/download/{job_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "colabfold"})
    print(f"  [download] Fetching tar.gz from {url}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        tar_data = resp.read()
    print(f"  [download] Got {len(tar_data)} bytes")

    # Extract from tar.gz in memory
    tar_buf = io.BytesIO(tar_data)
    uniref_content = None
    bfd_content = None

    with tarfile.open(fileobj=tar_buf, mode="r:gz") as tar:
        members = tar.getnames()
        print(f"  [extract] Files in archive: {members}")
        for member in tar.getmembers():
            name = member.name
            if "uniref.a3m" in name:
                f = tar.extractfile(member)
                if f:
                    uniref_content = f.read().decode("utf-8", errors="replace")
                    print(f"  [extract] Got uniref.a3m ({len(uniref_content)} chars)")
            elif "bfd.mgnify30.metaeuk30.smag30.a3m" in name:
                f = tar.extractfile(member)
                if f:
                    bfd_content = f.read().decode("utf-8", errors="replace")
                    print(f"  [extract] Got bfd.a3m ({len(bfd_content)} chars)")

    if uniref_content is None:
        print(f"  [error] uniref.a3m not found in archive")
        return False

    # Merge: uniref first, then bfd skipping first header line (starts with >101)
    merged = uniref_content
    if bfd_content:
        bfd_lines = bfd_content.split("\n")
        # Skip first line if it starts with '>'
        if bfd_lines and bfd_lines[0].startswith(">"):
            bfd_lines = bfd_lines[1:]
        merged += "\n" + "\n".join(bfd_lines)

    # Write to file
    out_path = os.path.join(MSA_DIR, f"{uid}.a3m")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(merged)
    print(f"  [write] Wrote {len(merged)} chars to {out_path}")

    # Strip null bytes
    with open(out_path, "rb") as f:
        raw = f.read()
    cleaned = raw.replace(b"\x00", b"")
    if len(cleaned) != len(raw):
        print(f"  [strip] Removed {len(raw) - len(cleaned)} null bytes")
        with open(out_path, "wb") as f:
            f.write(cleaned)

    final_size = os.path.getsize(out_path)
    print(f"  [done] Final size: {final_size} bytes")
    return final_size > MIN_SIZE


def process_uid(uid):
    print(f"\n{'='*60}")
    print(f"Processing: {uid}")
    print(f"{'='*60}")

    if check_existing(uid):
        size = os.path.getsize(os.path.join(MSA_DIR, f"{uid}.a3m"))
        print(f"  [skip] Already exists ({size} bytes), skipping.")
        return "skipped"

    # Step 1: Fetch sequence from UniProt
    print(f"  [uniprot] Fetching sequence for {uid}...")
    try:
        sequence = fetch_uniprot_fasta(uid)
        print(f"  [uniprot] Got sequence of length {len(sequence)}")
    except Exception as e:
        print(f"  [error] Failed to fetch UniProt sequence: {e}")
        return "failed"

    # Step 2: Submit MSA job
    print(f"  [colabfold] Submitting MSA job...")
    try:
        job_info = submit_msa_job(sequence)
        print(f"  [colabfold] Job response: {job_info}")
        job_id = job_info.get("id")
        if not job_id:
            print(f"  [error] No job ID in response: {job_info}")
            return "failed"
        print(f"  [colabfold] Job ID: {job_id}")
    except Exception as e:
        print(f"  [error] Failed to submit MSA job: {e}")
        return "failed"

    # Step 3: Poll until complete
    print(f"  [poll] Waiting for job to complete...")
    try:
        completed = poll_job(job_id)
        if not completed:
            return "failed"
    except Exception as e:
        print(f"  [error] Polling failed: {e}")
        return "failed"

    # Step 4-7: Download, extract, merge, write, strip
    try:
        success = download_and_extract(job_id, uid)
        return "success" if success else "failed"
    except Exception as e:
        print(f"  [error] Download/extract failed: {e}")
        return "failed"


def main():
    os.makedirs(MSA_DIR, exist_ok=True)

    results = {}
    for uid in UNIPROT_IDS:
        status = process_uid(uid)
        results[uid] = status

    print(f"\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")
    succeeded = [uid for uid, s in results.items() if s == "success"]
    skipped = [uid for uid, s in results.items() if s == "skipped"]
    failed = [uid for uid, s in results.items() if s == "failed"]

    print(f"Succeeded ({len(succeeded)}): {', '.join(succeeded) if succeeded else 'none'}")
    print(f"Skipped ({len(skipped)}): {', '.join(skipped) if skipped else 'none'}")
    print(f"Failed ({len(failed)}): {', '.join(failed) if failed else 'none'}")


if __name__ == "__main__":
    main()
