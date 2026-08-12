#!/usr/bin/env python3
"""
Download MSA files for 10 new cancer targets using ColabFold mmseqs2 API.
Strips null bytes, verifies >1KB with valid a3m header.
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
OUT_DIR  = "/mnt/minos-drive/life-compute-miner/data/msa_files"
HEADERS  = {"User-Agent": "colabfold"}

# 10 new targets to download
NEW_TARGETS = {
    "BRAF":   "P15056",
    "PTEN":   "P60484",
    "MYC":    "P01106",
    "STAT3":  "P40763",
    "PIK3CA": "P42336",
    "MTOR":   "P42345",
    "FGFR1":  "P11362",
    "RET":    "P07949",
    "AR":     "P10275",
    "NTRK1":  "Q16288",
}


def fetch_uniprot_sequence(uniprot_id: str) -> str:
    url = f"https://rest.uniprot.org/uniprotkb/{uniprot_id}.fasta"
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            lines = r.text.strip().split("\n")
            seq = "".join(l for l in lines if not l.startswith(">"))
            if not seq:
                raise ValueError("Empty sequence")
            return seq
        except Exception as e:
            print(f"  UniProt fetch attempt {attempt+1}/5 failed: {e}")
            time.sleep(5)
    raise RuntimeError(f"Failed to fetch sequence for {uniprot_id}")


def submit(seq: str) -> dict:
    query = f">101\n{seq}\n"
    for attempt in range(10):
        try:
            res = requests.post(
                f"{HOST_URL}/ticket/msa",
                data={"q": query, "mode": "env"},
                timeout=15,
                headers=HEADERS,
            )
            return res.json()
        except Exception as e:
            print(f"  Submit attempt {attempt+1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Too many failed submit attempts")


def poll_status(job_id: str) -> dict:
    for attempt in range(5):
        try:
            res = requests.get(f"{HOST_URL}/ticket/{job_id}", timeout=10, headers=HEADERS)
            return res.json()
        except Exception as e:
            print(f"  Status check failed: {e}")
            time.sleep(5)
    raise RuntimeError(f"Cannot get status for job {job_id}")


def download_result(job_id: str, path: str):
    for attempt in range(5):
        try:
            res = requests.get(
                f"{HOST_URL}/result/download/{job_id}",
                timeout=180,
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
    with tarfile.open(tar_path, "r:gz") as tf:
        members = tf.getnames()
        print(f"  Tar contents: {members}")
        uniref_content = ""
        env_content = ""
        for member in members:
            if member.endswith("uniref.a3m"):
                f = tf.extractfile(member)
                if f:
                    uniref_content = f.read().decode("utf-8", errors="replace")
            elif "bfd" in member and member.endswith(".a3m"):
                f = tf.extractfile(member)
                if f:
                    env_content = f.read().decode("utf-8", errors="replace")

        if not uniref_content:
            raise RuntimeError("uniref.a3m not found in tar")

        combined = uniref_content
        if env_content:
            env_lines = env_content.split("\n")
            skip = True
            env_extra = []
            for line in env_lines:
                if line.startswith(">") and line.strip() != ">101":
                    skip = False
                if not skip:
                    env_extra.append(line)
            if env_extra:
                combined = combined.rstrip("\n") + "\n" + "\n".join(env_extra)

        # Strip null bytes
        combined = combined.replace("\x00", "")
        return combined


def verify_a3m(path: str) -> bool:
    """Verify file is >1KB with a valid a3m header."""
    size = os.path.getsize(path)
    if size < 1024:
        print(f"  FAIL: file too small ({size} bytes)")
        return False
    with open(path) as f:
        first_line = f.readline().strip()
    if not first_line.startswith(">"):
        print(f"  FAIL: no valid a3m header (first line: {first_line[:50]!r})")
        return False
    print(f"  Verified: {size//1024} KB, header='{first_line[:40]}'")
    return True


def download_msa(name: str, uniprot_id: str) -> bool:
    out_path = os.path.join(OUT_DIR, f"{uniprot_id}.a3m")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print(f"[{name}/{uniprot_id}] Already exists ({os.path.getsize(out_path)//1024} KB), skipping.")
        return True

    print(f"\n{'='*60}")
    print(f"[{name}/{uniprot_id}] Starting MSA download")
    print(f"{'='*60}")

    seq = fetch_uniprot_sequence(uniprot_id)
    print(f"  Sequence: {len(seq)} aa")

    print("  Submitting to ColabFold mmseqs2 API...")
    out = submit(seq)

    retries = 0
    while out.get("status") in ("UNKNOWN", "RATELIMIT") and retries < 20:
        sleep_time = 15 + random.randint(0, 15)
        print(f"  Status: {out.get('status')} — sleeping {sleep_time}s...")
        time.sleep(sleep_time)
        out = submit(seq)
        retries += 1

    if out.get("status") == "ERROR":
        print(f"  ERROR from API: {out}")
        return False

    job_id = out["id"]
    print(f"  Job ID: {job_id}  Status: {out.get('status')}")

    elapsed = 0
    while out.get("status") in ("RUNNING", "PENDING", "UNKNOWN"):
        sleep_time = 10 + random.randint(0, 10)
        time.sleep(sleep_time)
        elapsed += sleep_time
        out = poll_status(job_id)
        print(f"  [{elapsed:4d}s] Status: {out.get('status')}")

    if out.get("status") != "COMPLETE":
        print(f"  Job ended with status: {out.get('status')}")
        return False

    print(f"  Job COMPLETE after ~{elapsed}s")

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        download_result(job_id, tmp_path)
        print("  Extracting a3m files...")
        a3m_content = get_a3m_from_tar(tmp_path)

        with open(out_path, "w") as f:
            f.write(a3m_content)

        if not verify_a3m(out_path):
            return False

        seqs = a3m_content.count(">")
        print(f"  Saved: {out_path}")
        print(f"  Sequences: {seqs}")
        return True

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"ColabFold MSA Downloader — 10 new cancer targets")
    print(f"Output: {OUT_DIR}")

    results = {}
    for name, uniprot_id in NEW_TARGETS.items():
        try:
            ok = download_msa(name, uniprot_id)
            results[name] = "OK" if ok else "FAILED"
        except Exception as e:
            print(f"\n[{name}/{uniprot_id}] Exception: {e}")
            import traceback; traceback.print_exc()
            results[name] = f"ERROR: {e}"

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    ok_count = 0
    for name, status in results.items():
        uid = NEW_TARGETS[name]
        path = os.path.join(OUT_DIR, f"{uid}.a3m")
        size = f"{os.path.getsize(path)//1024} KB" if os.path.exists(path) else "missing"
        print(f"  {name:10s} ({uid}): {status}  [{size}]")
        if status == "OK":
            ok_count += 1

    failed = [n for n, s in results.items() if s != "OK"]
    if failed:
        print(f"\nFailed: {failed}")
        sys.exit(1)
    else:
        print(f"\nAll {len(NEW_TARGETS)} MSAs downloaded and verified.")


if __name__ == "__main__":
    main()
