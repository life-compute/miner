#!/usr/bin/env python3
"""Watch life_boltz_scores.jsonl for the FIX 3 discriminating event.

    python3 scripts/watch_ref_throttle.py --timeout 12000

FIX 3 stopped the 5-min target refresh from clearing `ref_last_screened`, which
had made `ref_due` permanently True.  The ONLY observable that distinguishes
fixed from broken is:

    a target that HAS a reference compound, on its SECOND+ visit within
    REF_RESCREEN_INTERVAL (4 h), is served a NON-ref molecule.

A first visit legitimately screens the reference either way (the dict starts
empty), and a target with no reference compound always falls through to
_pick_molecule — neither is evidence.  With ~60 targets at ~136 s/score a full
rotation is ~2.3 h, so this cannot be observed in a short window.

Exit 0 = PROVEN (saw a ref-equipped target served non-ref on a repeat visit)
Exit 1 = REFUTED (a ref-equipped target was re-screened as ref within 4 h)
Exit 2 = INCONCLUSIVE (timed out before any target was revisited)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
JSONL = REPO / "output" / "life_boltz_scores.jsonl"
REF_RESCREEN_INTERVAL = 4 * 3600.0
REF_SOURCES = {"ref", "reference"}


def ref_targets() -> set[str]:
    """Targets that have a reference compound, per the daemon's own log line."""
    try:
        out = subprocess.run(
            ["pm2", "logs", "life-miner", "--lines", "3000", "--nostream"],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception:
        return set()
    hits = re.findall(r"Reference compounds loaded: \d+ \(([^)]*)\)", out)
    return {t.strip() for t in hits[-1].split(",")} if hits else set()


def rows_after(ts: float) -> list[dict]:
    out = []
    with JSONL.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("ts", 0) > ts and r.get("source") != "crispr_generated":
                out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", type=float, default=time.time(),
                    help="epoch to start watching from (default: now)")
    ap.add_argument("--timeout", type=float, default=12000.0,
                    help="seconds to watch (default 12000 ≈ 3.3 h, > one rotation)")
    ap.add_argument("--poll", type=float, default=60.0)
    args = ap.parse_args()

    have_ref = ref_targets()
    if not have_ref:
        print("could not read the reference-compound list from pm2 logs")
        return 2
    print(f"watching {JSONL.name} from ts={args.since:.0f} for <= {args.timeout:.0f}s")
    print(f"{len(have_ref)} targets have a reference compound\n")

    last_ref_screen: dict[str, float] = {}
    seen: set[tuple[str, float]] = set()
    deadline = time.time() + args.timeout

    while time.time() < deadline:
        for r in rows_after(args.since):
            tid, src, ts = r.get("target_id", ""), r.get("source", ""), r.get("ts", 0.0)
            if (tid, ts) in seen:
                continue
            seen.add((tid, ts))
            if tid not in have_ref:
                continue                      # no reference compound → not evidence

            prior = last_ref_screen.get(tid)
            if src in REF_SOURCES:
                if prior is not None and ts - prior < REF_RESCREEN_INTERVAL:
                    print(f"REFUTED  {tid} re-screened as ref after "
                          f"{ts - prior:.0f}s (< {REF_RESCREEN_INTERVAL:.0f}s)")
                    return 1
                last_ref_screen[tid] = ts
                print(f"  [ref ]  {tid:14s} first screen this window")
            elif prior is not None and ts - prior < REF_RESCREEN_INTERVAL:
                print(f"\nPROVEN   {tid} served '{src}' on revisit "
                      f"{ts - prior:.0f}s after its ref screen "
                      f"(throttle held; pre-fix this was impossible)")
                return 0
            else:
                print(f"  [{src[:4]}]  {tid:14s} (no prior ref screen this window)")
        time.sleep(args.poll)

    print(f"\nINCONCLUSIVE  no ref-equipped target was revisited within "
          f"{args.timeout:.0f}s ({len(last_ref_screen)} first-screens seen)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
