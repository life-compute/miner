#!/usr/bin/env python3
"""
peg_observer.py — miner-vs-validator agreement observer for Stage 4 pegRNA rows.

WHAT THIS IS
------------
Stage 4 (`_peg_loop` in miner_daemon.py) logs one JSONL row per top-N pegRNA
candidate, each carrying the miner's `predicted_efficiency` as produced by
`output/peg_model.pkl` through sklearn.

Stage 3 established a second, independent evaluation path: `output/peg_model.json`
walked by a pure-stdlib tree evaluator (`scripts/export_peg_model.py::predict_json`,
`json` + `struct` only, no sklearn / numpy / pickle).  `output/peg_versions.json`
names that as "the recommended Stage 3 validator path".

This script runs the validator path over the miner's logged rows and records
whether the two agree.  That is a REAL agreement signal available today — it
requires no confirm/reject verdict and no Stage 5 tolerance.

WHAT THIS IS NOT
----------------
Not a scorer.  It contains no model, no formula, no threshold, and no notion of
HIT/MISS.  It calls the existing featurizer and the existing exported evaluator
and subtracts two numbers.  A HIT/MISS verdict would require a tolerance that
Stage 5 has not set, so none is invented here: the observer reports the delta
and whether it is exactly zero, nothing more.

READ-ONLY W.R.T. STAGE 4
------------------------
Reads   output/life_peg_daemon_scores.jsonl   (never writes it)
Reads   output/peg_model.json
Reads   data/peg_reference/<GENE>.json
Writes  output/life_peg_observer.jsonl        (its own file, append-only)

Nothing here imports from miner_daemon, touches on-chain state, or affects
earnings.  Safe to run at any time, including while the daemon is writing.

STRAND FRAME — the trap this script exists to get right
-------------------------------------------------------
Candidate offsets (`pam_offset`, `nick_offset`, `edit_offset`) index the strand
the candidate was DESIGNED on, not the stored reference window.  For `-` strand
rows that frame is the reverse complement of `window` in the Stage 0.1 JSON.
Indexing the `+` window for a `-` row silently reads the wrong bases — 98 of
the first 245 rows — and the featurizer then produces a different vector and a
different prediction, which would look like validator disagreement when it is
really an observer bug.  `_window_for()` handles this and `_invariants_hold()`
proves it per row before any recompute is attempted.

RESUME
------
Keyed on (ts, rank, model_sha256).  Re-running is safe and cheap: already-observed
rows are skipped, so the 30-minute cron only pays for genuinely new candidates.

USAGE
-----
    python scripts/peg_observer.py              # observe any new rows
    python scripts/peg_observer.py --rebuild    # discard and re-observe everything
    python scripts/peg_observer.py --status     # summary only, writes nothing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from adaptive.life_peg import featurize_pegrna              # noqa: E402
from scripts.export_peg_model import predict_json           # noqa: E402

DAEMON_JSONL = REPO / "output" / "life_peg_daemon_scores.jsonl"
OBSERVER_JSONL = REPO / "output" / "life_peg_observer.jsonl"
MODEL_JSON = REPO / "output" / "peg_model.json"
REF_DIR = REPO / "data" / "peg_reference"

_COMPLEMENT = str.maketrans("ACGTN", "TGCAN")

# Fields a row must carry to be observable at all.  A row missing any of these
# is skipped rather than defaulted — a defaulted feature produces a confident
# number traceable to nothing, which is the failure mode this whole branch was
# built to avoid.
REQUIRED_FIELDS = (
    "gene", "hotspot_label", "strand", "spacer", "pbs", "rtt", "edit_type",
    "pam_offset", "nick_offset", "edit_offset", "nick_to_edit",
    "correction_length", "edit_to_rtt_end", "wt_seq", "edited_seq",
    "genomic_edit_pos", "predicted_efficiency", "rank", "ts",
)


def revcomp(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]


_ref_cache: dict[str, Optional[dict]] = {}


def _load_ref(gene: str) -> Optional[dict]:
    if gene not in _ref_cache:
        path = REF_DIR / f"{gene}.json"
        try:
            _ref_cache[gene] = json.loads(path.read_text())
        except Exception:
            _ref_cache[gene] = None
    return _ref_cache[gene]


def _window_for(row: dict) -> Optional[str]:
    """
    The genomic window in the candidate's own design frame.

    `-` strand candidates were designed against the reverse complement of the
    stored Stage 0.1 window; all their offsets index that frame.
    """
    rec = _load_ref(row["gene"])
    if not rec:
        return None
    hs = next((h for h in rec.get("hotspots", [])
               if h.get("label") == row["hotspot_label"]), None)
    if not hs or not hs.get("window"):
        return None
    window = hs["window"]
    return window if row.get("strand") == "+" else revcomp(window)


def _invariants_hold(row: dict, window: str) -> tuple[bool, str]:
    """
    Prove the row's geometry reconstructs against the reference before trusting
    it.  Returns (ok, reason).  These are the same five invariants the panel
    gates its DNA rendering on, checked here so a bad row never reaches the
    recompute and never reaches the UI.
    """
    spacer, pbs, rtt = row["spacer"], row["pbs"], row["rtt"]
    nick, edit, pam = row["nick_offset"], row["edit_offset"], row["pam_offset"]
    wt, edited = row["wt_seq"], row["edited_seq"]

    if not (0 <= nick <= len(window) and 0 <= edit < len(window)):
        return False, "offsets out of window bounds"
    if window[edit:edit + len(wt)] != wt:
        return False, "window[edit_offset] != wt_seq"
    if nick - len(spacer) < 0 or window[nick - len(spacer):nick] != spacer:
        return False, "spacer does not end at nick_offset"
    if window[pam:pam + 3][1:] != "GG":
        return False, "pam_offset is not an NGG PAM"
    if window[nick - len(pbs):nick] != revcomp(pbs):
        return False, "revcomp(pbs) != window 5' of nick"

    edited_window = window[:edit] + edited + window[edit + len(wt):]
    if edited_window[nick:nick + len(rtt)] != revcomp(rtt):
        return False, "revcomp(rtt) != edited window at nick"

    return True, ""


def observe_row(row: dict, spec: dict) -> Optional[dict]:
    """
    Recompute one row's efficiency via the validator path.

    Returns an observation dict, or None when the row cannot be honestly
    observed (missing fields, unreconstructable window, failed invariants,
    or a feature vector the featurizer rejects).
    """
    missing = [f for f in REQUIRED_FIELDS if row.get(f) is None]
    if missing:
        return None

    window = _window_for(row)
    if window is None:
        return None

    ok, reason = _invariants_hold(row, window)
    if not ok:
        return {
            "ts": row["ts"],
            "rank": row["rank"],
            "gene": row["gene"],
            "hotspot_label": row["hotspot_label"],
            "model_sha256": row.get("model_sha256"),
            "miner_eff": row["predicted_efficiency"],
            "validator_eff": None,
            "delta": None,
            "exact": False,
            "status": "INVARIANT_FAIL",
            "reason": reason,
            "observed_at": time.time(),
        }

    feats = featurize_pegrna(
        row["spacer"], row["pbs"], row["rtt"], row["edit_type"],
        gene=row["gene"],
        genomic_edit_pos=row["genomic_edit_pos"],
        nick_to_edit=row["nick_to_edit"],
        correction_length=row["correction_length"],
        edit_to_rtt_end=row["edit_to_rtt_end"],
        window=window,
        edit_offset=row["edit_offset"],
        pam_offset=row["pam_offset"],
        strand=row["strand"],
    )
    if feats is None:
        return {
            "ts": row["ts"],
            "rank": row["rank"],
            "gene": row["gene"],
            "hotspot_label": row["hotspot_label"],
            "model_sha256": row.get("model_sha256"),
            "miner_eff": row["predicted_efficiency"],
            "validator_eff": None,
            "delta": None,
            "exact": False,
            "status": "FEATURIZE_FAIL",
            "reason": "featurize_pegrna returned None",
            "observed_at": time.time(),
        }

    validator_eff = predict_json(spec, feats)
    miner_eff = float(row["predicted_efficiency"])
    delta = abs(validator_eff - miner_eff)

    return {
        "ts": row["ts"],
        "rank": row["rank"],
        "gene": row["gene"],
        "hotspot_label": row["hotspot_label"],
        "model_sha256": row.get("model_sha256"),
        "miner_eff": miner_eff,
        "validator_eff": validator_eff,
        "delta": delta,
        "exact": delta == 0.0,
        "status": "EXACT" if delta == 0.0 else "DIVERGENT",
        "reason": "",
        "observed_at": time.time(),
    }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # A partially-flushed final line while the daemon is mid-write.
            continue
    return out


def _key(row: dict) -> tuple:
    return (row.get("ts"), row.get("rank"), row.get("model_sha256"))


def summarise(observations: list[dict]) -> dict:
    scored = [o for o in observations if o.get("delta") is not None]
    n_exact = sum(1 for o in scored if o.get("exact"))
    deltas = [o["delta"] for o in scored]
    failed = [o for o in observations if o.get("delta") is None]
    if not scored:
        status = "PENDING"
    elif n_exact == len(scored):
        status = "EXACT"
    else:
        status = "DIVERGENT"
    return {
        "checked": len(scored),
        "exact": n_exact,
        "divergent": len(scored) - n_exact,
        "unobservable": len(failed),
        "max_delta": max(deltas) if deltas else None,
        "status": status,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                    help="discard existing observations and re-observe all rows")
    ap.add_argument("--status", action="store_true",
                    help="print a summary and exit without writing")
    args = ap.parse_args()

    if not DAEMON_JSONL.exists():
        print(f"BLOCKER: {DAEMON_JSONL} not found — Stage 4 has not logged yet")
        return 1
    if not MODEL_JSON.exists():
        print(f"BLOCKER: {MODEL_JSON} not found — run scripts/export_peg_model.py")
        return 1

    rows = _read_jsonl(DAEMON_JSONL)
    existing = [] if args.rebuild else _read_jsonl(OBSERVER_JSONL)

    if args.status:
        s = summarise(existing)
        print(f"daemon rows      : {len(rows):,}")
        print(f"observed         : {len(existing):,}")
        print(f"checked          : {s['checked']:,}")
        print(f"bit-exact        : {s['exact']:,}")
        print(f"divergent        : {s['divergent']:,}")
        print(f"unobservable     : {s['unobservable']:,}")
        print(f"max abs delta    : {s['max_delta']!r}")
        print(f"agreement status : {s['status']}")
        return 0

    spec = json.loads(MODEL_JSON.read_text())
    seen = {_key(o) for o in existing}

    new: list[dict] = []
    skipped_unobservable = 0
    for row in rows:
        if _key(row) in seen:
            continue
        obs = observe_row(row, spec)
        if obs is None:
            skipped_unobservable += 1
            continue
        new.append(obs)
        seen.add(_key(obs))

    if args.rebuild:
        OBSERVER_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with OBSERVER_JSONL.open("w") as fh:
            for o in new:
                fh.write(json.dumps(o) + "\n")
    elif new:
        OBSERVER_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with OBSERVER_JSONL.open("a") as fh:
            for o in new:
                fh.write(json.dumps(o) + "\n")

    all_obs = new if args.rebuild else existing + new
    s = summarise(all_obs)

    print(f"daemon rows            : {len(rows):,}")
    print(f"newly observed         : {len(new):,}")
    print(f"skipped (no fields/ref): {skipped_unobservable:,}")
    print(f"total observations     : {len(all_obs):,}")
    print(f"  checked              : {s['checked']:,}")
    print(f"  bit-exact            : {s['exact']:,}")
    print(f"  divergent            : {s['divergent']:,}")
    print(f"  unobservable         : {s['unobservable']:,}")
    print(f"  max abs delta        : {s['max_delta']!r}")
    print(f"agreement status       : {s['status']}")

    if s["status"] == "DIVERGENT":
        print()
        print("NOTE: miner and validator paths disagree on at least one row.")
        print("      This is reported, not suppressed. Inspect with:")
        print("      grep DIVERGENT output/life_peg_observer.jsonl | head")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
