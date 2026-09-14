#!/usr/bin/env python3
"""One-shot migration: rename ProteinNet model files from UniProt to target_id.

    python3 scripts/migrate_proteinnet_model_names.py --dry-run   # default
    python3 scripts/migrate_proteinnet_model_names.py --apply

Background: _model_path() used to key on uniprot_id, so the small-molecule,
mRNA and CRISPR modalities of one gene (e.g. P11802 → CDK4 / CDK4_mRNA /
CDK4_CRISPR) all wrote the same <UNIPROT>_model.pkl and clobbered each other.
It now keys on target_id, which orphans every existing file.

Three cases:
  RENAME  exactly one *ready* target claims the accession → attribution is certain
  DELETE  two or more ready targets share it → the pkl is whichever trained last,
          so it cannot be attributed; drop it and let train_all() rebuild
  SKIP    no report entry
Deleting is safe: a missing pkl only means the target retrains on next cycle.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
MODEL_DIR = REPO / "output" / "protein_models"
REPORT = MODEL_DIR / "proteinnet_report.json"
SUFFIX = "_model.pkl"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the migration")
    args = ap.parse_args()

    if not REPORT.exists():
        print(f"no report at {REPORT} — nothing to migrate")
        return 0

    models = json.loads(REPORT.read_text()).get("models", {})
    by_uid: dict[str, list[str]] = collections.defaultdict(list)
    for tid, meta in models.items():
        by_uid[meta.get("uniprot_id", tid)].append(tid)

    renames: list[tuple[pathlib.Path, pathlib.Path]] = []
    deletes: list[tuple[pathlib.Path, list[str]]] = []
    skips: list[pathlib.Path] = []

    for pkl in sorted(MODEL_DIR.glob(f"*{SUFFIX}")):
        key = pkl.name[: -len(SUFFIX)]
        claimants = by_uid.get(key, [])
        if key in models:          # already target_id-keyed
            continue
        ready = [t for t in claimants if models[t].get("status") == "ready"]
        if not claimants:
            skips.append(pkl)
        elif len(ready) > 1:
            deletes.append((pkl, ready))
        else:
            owner = (ready or claimants)[0]
            renames.append((pkl, pkl.with_name(f"{owner}{SUFFIX}")))

    print(f"{'APPLY' if args.apply else 'DRY-RUN'}  {MODEL_DIR}\n")
    print(f"RENAME ({len(renames)}) — single ready claimant, attribution certain")
    for src, dst in renames:
        print(f"   {src.name:28s} -> {dst.name}")
    print(f"\nDELETE ({len(deletes)}) — shared by multiple ready targets, unattributable")
    for pkl, ready in deletes:
        print(f"   {pkl.name:28s}    claimed by {ready} (will retrain)")
    if skips:
        print(f"\nSKIP ({len(skips)}) — no report entry")
        for pkl in skips:
            print(f"   {pkl.name}")

    if not args.apply:
        print("\nre-run with --apply to perform the migration")
        return 0

    for src, dst in renames:
        if dst.exists():
            print(f"  ! {dst.name} already exists — leaving {src.name} in place")
            continue
        src.rename(dst)
    for pkl, _ in deletes:
        pkl.unlink(missing_ok=True)
    print(f"\nmigrated: {len(renames)} renamed, {len(deletes)} deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
