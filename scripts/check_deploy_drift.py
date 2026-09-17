#!/usr/bin/env python3
"""check_deploy_drift.py — is the code that's RUNNING the code that's COMMITTED?

Three independent drift classes, each of which has bitten this project:

  1. WORKTREE   file on disk != committed HEAD        (uncommitted edit)
  2. PROCESS    file mtime    >  process start time   (edited since restart)
  3. REMOTE     local HEAD    not pushed to origin    (survives /tmp wipe?)

Class 3 matters because ANCHOR_DIR is /tmp/life-compute/core: the JOB_MISMATCH
guard in life_submit.js is the live fix for an active on-chain bug, and /tmp can
be wiped without warning. Committed-but-unpushed there is NOT durable.

Exit 0 = no drift. Exit 1 = drift (prints what and how to fix).
Silent-clean by design, so it is safe to run from cron.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MINER_REPO = Path("/mnt/minos-drive/life-compute-miner")
CORE_REPO = Path("/tmp/life-compute/core")

# (repo, path-relative-to-repo, pm2 process that executes it)
WATCH: list[tuple[Path, str, str | None]] = [
    (CORE_REPO,  "life_submit.js",               "life-miner"),
    (MINER_REPO, "miner_daemon.py",              "life-miner"),
    (MINER_REPO, "adaptive/life_brain.py",       "life-brain"),
    (MINER_REPO, "adaptive/life_brain_ingest.py", "life-brain"),
    (MINER_REPO, "adaptive/life_brain_model.py", "life-brain"),
]


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(("git", "-C", str(repo)) + args,
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def _pm2_start_times() -> dict[str, float]:
    """name -> unix epoch seconds when the current instance started."""
    r = subprocess.run(("pm2", "jlist"), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {}
    try:  # pm_uptime is MILLISECONDS, not seconds
        return {p["name"]: p["pm2_env"]["pm_uptime"] / 1000.0
                for p in json.loads(r.stdout)}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def main() -> int:
    starts = _pm2_start_times()
    drift: list[str] = []
    restart: set[str] = set()
    unpushed: set[Path] = set()

    for repo, rel, proc in WATCH:
        path = repo / rel
        if not path.exists():
            drift.append(f"MISSING   {path} — expected file is gone "
                         f"({'/tmp wiped?' if repo == CORE_REPO else 'bad checkout?'})")
            continue

        # 1. worktree vs HEAD
        on_disk = _git(repo, "hash-object", str(path))
        committed = _git(repo, "rev-parse", f"HEAD:{rel}")
        if not committed:
            drift.append(f"UNTRACKED {rel} — not in {repo.name} HEAD")
        elif on_disk and on_disk != committed:
            drift.append(f"WORKTREE  {rel} — on-disk differs from HEAD "
                         f"({on_disk[:8]} vs {committed[:8]})")

        # 2. edited since the process that runs it last started
        if proc and proc in starts and path.stat().st_mtime > starts[proc]:
            age = (path.stat().st_mtime - starts[proc]) / 60.0
            drift.append(f"PROCESS   {rel} — modified {age:.0f} min AFTER "
                         f"{proc} started; {proc} is running stale code")
            restart.add(proc)

    # 3. local commits not on the remote
    for repo in {r for r, _, _ in WATCH}:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if branch and _git(repo, "log", "--oneline", f"origin/{branch}..HEAD"):
            unpushed.add(repo)
            drift.append(f"REMOTE    {repo.name} — HEAD not pushed to "
                         f"origin/{branch}"
                         + (" (⚠ lives in /tmp — a wipe reverts the fix)"
                            if repo == CORE_REPO else ""))

    if not drift:
        return 0

    print("DEPLOY DRIFT — running code is not committed code\n")
    for d in drift:
        print("  " + d)
    print("\nfix:")
    for p in sorted(restart):
        print(f"  pm2 restart {p}")
    for repo in sorted(unpushed, key=str):
        print(f"  git -C {repo} push")
    return 1


if __name__ == "__main__":
    sys.exit(main())
