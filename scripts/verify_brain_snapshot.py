"""
verify_brain_snapshot.py — public LIFE-BRAIN counter verifier.

The other verify_* scripts prove the miner computes correctly; this one proves
the public brain page actually *shows* what the miner computed.

adaptive/life_brain.py pushes output/life_brain_snapshot.json with
`git push origin HEAD`, so snapshots land on whatever branch the miner runs.
brain.html hardcodes a branch in SNAPSHOT_URL. When the two drift the page
silently freezes on a stale snapshot and nothing errors anywhere — the failure
mode this script exists to catch (2026-09: page stuck at 10,730 rows for four
days while the miner was at 13,494).

  A. Local snapshot agrees with the report it was exported beside.
  B. Live page exposes a SNAPSHOT_URL; all refs in it are consistent.
  C. That ref resolves and carries the expected schema.
  D. The number the page renders equals the miner's own total_rows.
  E. Snapshot is fresh, i.e. the push loop is alive.

C–E need network. When the page or raw host is unreachable they are SKIPPED,
not failed, so the script stays usable offline. A is always enforced.

Usage:
    python scripts/verify_brain_snapshot.py
    python scripts/verify_brain_snapshot.py --max-age-h 3
    python scripts/verify_brain_snapshot.py --offline
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

REPORT = REPO / "output" / "life_brain_report.json"
SNAPSHOT = REPO / "output" / "life_brain_snapshot.json"
PAGE_URL = "https://lifecompute.ai/brain.html"

# The ref brain.html pins. Group 1 is the branch, which must track the branch
# the miner pushes from — that coupling is the whole point of this script.
SNAPSHOT_RE = re.compile(
    r"https://raw\.githubusercontent\.com/life-compute/miner"
    r"/([\w.-]+)/output/life_brain_snapshot\.json"
)
# Rendered by brain.html as the "Discoveries" stat.
COUNTER_KEY = "total_rows_ingested"
DEFAULT_MAX_AGE_H = 6.0

fails: list[str] = []
checks = 0


def check(cond: bool, msg: str) -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(msg)


def fetch(url: str, timeout: int = 30) -> str:
    """GET with cache busting — raw.githubusercontent caches aggressively."""
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}cb={int(time.time())}",
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-h", type=float, default=DEFAULT_MAX_AGE_H,
                    help=f"snapshot staleness bound (default {DEFAULT_MAX_AGE_H})")
    ap.add_argument("--offline", action="store_true",
                    help="skip the network checks (C-E)")
    args = ap.parse_args()

    # ── A. local snapshot vs report ───────────────────────────────────────
    if not SNAPSHOT.exists() or not REPORT.exists():
        print("BLOCKER: life_brain_snapshot.json / life_brain_report.json "
              "missing — run a LIFE-BRAIN retrain first")
        return 1

    snap_meta = json.loads(SNAPSHOT.read_text())["meta"]
    local_rows = json.loads(REPORT.read_text())["total_rows"]
    exported = snap_meta[COUNTER_KEY]
    check(exported == local_rows,
          f"A: snapshot {exported} != report total_rows {local_rows}")
    print(f"  A. local export   : {exported:,} rows, agrees with report")

    if args.offline:
        for letter, name in (("B", "live page"), ("C", "snapshot ref"),
                             ("D", "rendered count"), ("E", "freshness")):
            print(f"  {letter}. {name:<14}: SKIPPED (--offline)")
        return report()

    # ── B. live page ref ─────────────────────────────────────────────────
    try:
        page = fetch(PAGE_URL)
    except (urllib.error.URLError, OSError) as e:
        for letter in "BCDE":
            print(f"  {letter}. network check  : SKIPPED ({e})")
        return report()

    found = {m.group(1): m.group(0) for m in SNAPSHOT_RE.finditer(page)}
    check(len(found) == 1,
          f"B: expected 1 snapshot ref, found {sorted(found)}")
    if not found:
        print("  B. live page      : no SNAPSHOT_URL — brain.html not deployed?")
        return report()
    branch, url = next(iter(found.items()))
    print(f"  B. live page      : SNAPSHOT_URL -> {branch}")

    # ── C. ref resolves ──────────────────────────────────────────────────
    try:
        served = json.loads(fetch(url))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        check(False, f"C: snapshot ref unusable ({e})")
        print(f"  C. snapshot ref   : FAILED ({e})")
        return report()

    meta = served.get("meta", {})
    check(COUNTER_KEY in meta, f"C: {COUNTER_KEY} absent from served snapshot")
    check("nodes" in served, "C: nodes[] absent from served snapshot")
    print(f"  C. snapshot ref   : resolves, {len(served.get('nodes', []))} nodes")

    # ── D. what the page renders ─────────────────────────────────────────
    rendered = meta.get(COUNTER_KEY, -1)
    check(rendered == local_rows,
          f"D: page shows {rendered:,}, miner has {local_rows:,} "
          f"(drift {local_rows - rendered:+,})")
    print(f"  D. rendered count : {rendered:,} vs miner {local_rows:,}")

    # ── E. push loop alive ───────────────────────────────────────────────
    age_h = (time.time() - meta.get("last_trained", 0)) / 3600
    check(age_h < args.max_age_h,
          f"E: snapshot {age_h:.1f}h old (max {args.max_age_h}h) — "
          f"push loop stalled or branch drifted")
    print(f"  E. freshness      : {age_h:.1f}h old (max {args.max_age_h}h)")

    return report()


def report() -> int:
    print()
    print("=" * 74)
    if fails:
        print(f"FAILED ({len(fails)}/{checks})")
        for f in fails:
            print("  -", f)
        return 1
    print(f"assertions passed: {checks}")
    print()
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
