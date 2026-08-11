#!/usr/bin/env python3
"""
Download MSA files for cancer targets using ColabFold mmseqs2 API.
Saves {UniProt_ID}.a3m to /mnt/minos-drive/life-compute-miner/data/msa_files/
"""

import os
import sys
import time
import random
import tarfile
import tempfile
import requests

HOST_URL = "https://api.colabfold.com"
OUT_DIR = "/mnt/minos-drive/life-compute-miner/data/msa_files"
HEADERS = {"User-Agent": "colabfold"}

TARGETS = {
    "TP53":   ("P04637", None),  # fetched from UniProt
    "BRCA1":  ("P38398", None),
    "EGFR":   ("P00533", None),
    "HER2":   ("P04626", None),
    "KRAS":   ("P01116", None),
    "BCL2":   ("P10415", None),
    "CDK4":   ("P11802", None),
    "VEGFR2": ("P35968", None),
    "MDM2":   ("Q00987", None),
}


def fetch_uniprot_sequence(uniprot_id: str) -> str:
    """Fetch canonical FASTA sequence from UniProt."""
    url = f"https://rest.uniprot.org/uniprotkb/{uniprot_id}.fasta"
    print(f"  Fetching sequence from UniProt: {url}")
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            lines = r.text.strip().split("\n")
            seq = "".join(lines[1:])  # skip FASTA header
            if not seq:
                raise ValueError(f"Empty sequence for {uniprot_id}")
            return seq
        except Exception as e:
            print(f"  UniProt fetch attempt {attempt+1}/5 failed: {e}")
            time.sleep(5)
    raise RuntimeError(f"Failed to fetch sequence for {uniprot_id}")


def submit(seq: str) -> dict:
    """Submit an MSA job to ColabFold."""
    query = f">101\n{seq}\n"
    for attempt in range(10):
        try:
            res = requests.post(
                f"{HOST_URL}/ticket/msa",
                data={"q": query, "mode": "env"},
                timeout=10,
                headers=HEADERS,
            )
            out = res.json()
            return out
        except Exception as e:
            print(f"  Submit attempt {attempt+1} failed: {e}")
            time.sleep(5)
    raise RuntimeError("Too many failed submit attempts")


def poll_status(job_id: str) -> dict:
    """Check job status."""
    for attempt in range(5):
        try:
            res = requests.get(
                f"{HOST_URL}/ticket/{job_id}",
                timeout=10,
                headers=HEADERS,
            )
            return res.json()
        except Exception as e:
            print(f"  Status check failed: {e}")
            time.sleep(5)
    raise RuntimeError(f"Cannot get status for job {job_id}")


def download_result(job_id: str, path: str):
    """Download result tar.gz."""
    for attempt in range(5):
        try:
            res = requests.get(
                f"{HOST_URL}/result/download/{job_id}",
                timeout=120,
                headers=HEADERS,
                stream=True,
            )
            res.raise_for_status()
            total = int(res.headers.get("content-length", 0))
            downloaded = 0
            with open(path, "wb") as f:
                for chunk in res.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total * 100
                            print(f"  Downloading... {downloaded/1024:.0f} KB / {total/1024:.0f} KB ({pct:.1f}%)", end="\r")
            print(f"  Downloaded {downloaded/1024:.0f} KB                    ")
            return
        except Exception as e:
            print(f"\n  Download attempt {attempt+1}/5 failed: {e}")
            time.sleep(10)
    raise RuntimeError(f"Failed to download result for job {job_id}")


def get_a3m_from_tar(tar_path: str) -> str:
    """Extract and concatenate uniref.a3m + env a3m from the ColabFold tar.gz."""
    with tarfile.open(tar_path, "r:gz") as tf:
        members = tf.getnames()
        print(f"  Tar contents: {members}")

        # Get uniref hits
        uniref_content = ""
        env_content = ""

        for member in members:
            if member.endswith("uniref.a3m"):
                f = tf.extractfile(member)
                if f:
                    uniref_content = f.read().decode("utf-8")
            elif "bfd" in member and member.endswith(".a3m"):
                f = tf.extractfile(member)
                if f:
                    env_content = f.read().decode("utf-8")

        if not uniref_content:
            raise RuntimeError("uniref.a3m not found in tar")

        # Concatenate: uniref first, then env hits
        combined = uniref_content
        if env_content:
            # Append env lines, skipping the query header block (first entry ">101\n...")
            env_lines = env_content.split("\n")
            # Skip past the first record (the query itself, marked as >101)
            skip = True
            env_extra = []
            for line in env_lines:
                if line.startswith(">") and line.strip() != ">101":
                    skip = False
                if not skip:
                    env_extra.append(line)
            if env_extra:
                combined = combined.rstrip("\n") + "\n" + "\n".join(env_extra)

        return combined


def download_msa_for_target(name: str, uniprot_id: str) -> bool:
    """Full pipeline: fetch seq → submit → poll → download → extract → save."""
    out_path = os.path.join(OUT_DIR, f"{uniprot_id}.a3m")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print(f"[{name}/{uniprot_id}] Already exists ({os.path.getsize(out_path)//1024} KB), skipping.")
        return True

    print(f"\n{'='*60}")
    print(f"[{name}/{uniprot_id}] Starting MSA download")
    print(f"{'='*60}")

    # 1. Fetch sequence
    seq = fetch_uniprot_sequence(uniprot_id)
    print(f"  Sequence length: {len(seq)} aa")

    # 2. Submit job
    print(f"  Submitting to ColabFold mmseqs2 API...")
    out = submit(seq)

    # Handle rate limiting / unknown
    while out.get("status") in ("UNKNOWN", "RATELIMIT"):
        sleep_time = 10 + random.randint(0, 10)
        print(f"  Status: {out.get('status')} — sleeping {sleep_time}s...")
        time.sleep(sleep_time)
        out = submit(seq)

    if out.get("status") == "ERROR":
        print(f"  ERROR from API: {out}")
        return False

    job_id = out["id"]
    print(f"  Job ID: {job_id}  Status: {out.get('status')}")

    # 3. Poll until COMPLETE
    elapsed = 0
    while out.get("status") in ("RUNNING", "PENDING", "UNKNOWN"):
        sleep_time = 5 + random.randint(0, 5)
        time.sleep(sleep_time)
        elapsed += sleep_time
        out = poll_status(job_id)
        print(f"  [{elapsed:4d}s] Status: {out.get('status')}")

    if out.get("status") != "COMPLETE":
        print(f"  Job ended with status: {out.get('status')}  Full response: {out}")
        return False

    print(f"  Job COMPLETE after ~{elapsed}s")

    # 4. Download tar.gz to temp file
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        download_result(job_id, tmp_path)

        # 5. Extract and merge a3m
        print(f"  Extracting a3m files...")
        a3m_content = get_a3m_from_tar(tmp_path)

        # 6. Save
        with open(out_path, "w") as f:
            f.write(a3m_content)

        lines = a3m_content.count("\n")
        seqs = a3m_content.count(">")
        size_kb = os.path.getsize(out_path) // 1024
        print(f"  Saved: {out_path}")
        print(f"  Size: {size_kb} KB | Sequences: {seqs} | Lines: {lines}")
        return True

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"ColabFold MSA Downloader")
    print(f"Output: {OUT_DIR}")
    print(f"Targets: {len(TARGETS)}")

    results = {}
    for name, (uniprot_id, _) in TARGETS.items():
        try:
            ok = download_msa_for_target(name, uniprot_id)
            results[name] = "OK" if ok else "FAILED"
        except Exception as e:
            print(f"\n[{name}/{uniprot_id}] Exception: {e}")
            results[name] = f"ERROR: {e}"

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, status in results.items():
        uniprot_id = TARGETS[name][0]
        print(f"  {name:10s} ({uniprot_id}): {status}")

    failed = [n for n, s in results.items() if s != "OK"]
    if failed:
        print(f"\nFailed: {failed}")
        sys.exit(1)
    else:
        print(f"\nAll {len(TARGETS)} MSAs downloaded successfully.")


if __name__ == "__main__":
    main()
