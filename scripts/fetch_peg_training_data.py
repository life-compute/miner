#!/usr/bin/env python3
"""
fetch_peg_training_data.py — vendor the PRIDICT2 pegRNA training library.

Downloads the published PRIDICT2 dataset (22,956 pegRNAs with MEASURED
HEK293T and K562 prime-editing efficiencies) into data/peg_training/ so the
Stage 2 model can be retrained offline and reproducibly.

Source: https://github.com/uzh-dqbm-cmi/PRIDICT2  (MIT licence)
        dataset/proc_v2/data_23k_v1.csv

Why this dataset and not a synthetic label
------------------------------------------
We have no wet-lab measurements of our own.  The only honest way to train an
efficiency predictor is on real measured efficiencies; inventing a heuristic
target and fitting to it would produce a confident number traceable to
nothing.  These are real experimental readouts from the PRIDICT2 paper.

Verifies after download:
  * row count, column presence
  * labels genuinely in [0, 1]
  * segment coordinates parse and reconstruct sequences of sane length
Refuses to write a file that fails these checks.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "data" / "peg_training"
CSV_PATH = OUT_DIR / "pridict2_23k.csv"
META_PATH = OUT_DIR / "meta.json"

URL = ("https://raw.githubusercontent.com/uzh-dqbm-cmi/PRIDICT2/main/"
       "dataset/proc_v2/data_23k_v1.csv")

REQUIRED_COLS = {
    "Correction_Type", "Correction_Length", "RTlength", "PBSlength",
    "Editing_Position", "wide_initial_target", "wide_mutated_target",
    "protospacerlocation_only_initial", "PBSlocation", "RT_initial_location",
    "seq_id", "grp_id",
    "HEKaverageedited_clamped", "K562averageedited_clamped",
}

MIN_ROWS = 20_000


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if CSV_PATH.exists():
        print(f"already present: {CSV_PATH} "
              f"({CSV_PATH.stat().st_size:,} bytes) — re-verifying")
        raw = CSV_PATH.read_bytes()
    else:
        print(f"downloading {URL}")
        req = urllib.request.Request(URL, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
        print(f"  got {len(raw):,} bytes")

    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    print(f"  rows: {len(rows):,}")

    if len(rows) < MIN_ROWS:
        print(f"FAIL: only {len(rows)} rows, expected >= {MIN_ROWS}")
        return 1

    missing = REQUIRED_COLS - set(rows[0])
    if missing:
        print(f"FAIL: missing columns: {sorted(missing)}")
        return 1
    print(f"  columns: all {len(REQUIRED_COLS)} required present")

    # Labels must be real fractions in [0, 1].
    for col in ("HEKaverageedited_clamped", "K562averageedited_clamped"):
        vals = [float(r[col]) for r in rows if r[col] not in ("", "nan", None)]
        lo, hi = min(vals), max(vals)
        mean = sum(vals) / len(vals)
        print(f"  {col:30} n={len(vals):,} min={lo:.4f} max={hi:.4f} mean={mean:.4f}")
        if not (0.0 <= lo and hi <= 1.0):
            print(f"FAIL: {col} outside [0,1]")
            return 1

    # Coordinates must parse and reconstruct sane segments.
    bad = 0
    for r in rows[:2000]:
        try:
            ps = ast.literal_eval(r["protospacerlocation_only_initial"])
            pb = ast.literal_eval(r["PBSlocation"])
            rt = ast.literal_eval(r["RT_initial_location"])
            w = r["wide_initial_target"]
            if not (len(w[ps[0]:ps[1]]) >= 19 and pb[1] > pb[0] and rt[1] > rt[0]):
                bad += 1
        except Exception:
            bad += 1
    print(f"  coordinate parse: {2000 - bad}/2000 ok")
    if bad:
        print(f"FAIL: {bad} rows have unusable coordinates")
        return 1

    if not CSV_PATH.exists():
        CSV_PATH.write_bytes(raw)
        print(f"  wrote {CSV_PATH}")

    sha = hashlib.sha256(raw).hexdigest()
    META_PATH.write_text(json.dumps({
        "source_url": URL,
        "source_repo": "uzh-dqbm-cmi/PRIDICT2",
        "licence": "MIT",
        "description": "PRIDICT2 pegRNA library with measured HEK293T / K562 "
                       "prime-editing efficiencies",
        "n_rows": len(rows),
        "sha256": sha,
        "fetched_at": time.time(),
    }, indent=1))

    print(f"\nsha256: {sha}")
    print(f"OK — {len(rows):,} real measured pegRNAs vendored to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
