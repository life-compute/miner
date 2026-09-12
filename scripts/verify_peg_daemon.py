"""
verify_peg_daemon.py — Stage 4 daemon-wiring verifier.

Fifth member of the verify_peg_* family. The others prove the pegRNA branch is
correct; this one proves wiring it into miner_daemon.py did not disturb the
protein / mRNA / CRISPR paths that actually earn.

miner_daemon.py cannot be imported (module import triggers GPU/chain setup), so
structural claims are proven by AST over the source and behavioural claims by
re-executing the row-building logic against the real Stage 1/2 modules.

  A. _peg_loop exists; every pre-existing loop survives.
  B. Row builder yields valid, ordered rows from real candidates.
  C. Output isolation + no reward/submission surface reachable.
  D. Both kill switches present.
  E. Insertion-only vs the pre-Stage-4 baseline (difflib, not git --numstat).
  F. Pre-existing modality function bodies byte-identical.

E and F need a git baseline. Pass --baseline <rev> to override the default
(the commit that introduced Stage 4, resolved via git log). When the baseline
cannot be resolved E/F are SKIPPED, not failed, so the script still works in a
tarball export with no git history.

Usage:
    python scripts/verify_peg_daemon.py
    python scripts/verify_peg_daemon.py --baseline HEAD~1
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DAEMON = REPO / "miner_daemon.py"
EXPECTED_INSERTIONS = 137
STAGE4_SUBJECT = "Stage 4 pegRNA"

# Reward / submission / confirm surface that must be unreachable from _peg_loop.
BANNED = (
    "submit_on_chain", "AUTH_KEYPAIR", "_pending_pdas", "_confirmed_state",
    "REWARD_", "_maybe_mint_discovery_nft", "mint_", "_crispr_stats",
    "stats[", "_dedup", "SubmissionMemory", "tier_reward", "_rpc(",
    "epoch_submissions",
)
# Loops that predate Stage 4 and must survive untouched.
PREEXISTING = ("_crispr_loop", "_confirm_poller", "gpu_worker")

fails: list[str] = []
checks = 0


def check(cond: bool, msg: str) -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(msg)


def fn_src(src: str, tree: ast.AST, name: str) -> str | None:
    n = next((x for x in ast.walk(tree)
              if isinstance(x, ast.FunctionDef) and x.name == name), None)
    return ast.get_source_segment(src, n) if n else None


def git(*args: str) -> str:
    try:
        r = subprocess.run(("git", *args), capture_output=True, text=True,
                           cwd=REPO, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def resolve_baseline(explicit: str | None) -> str | None:
    """Parent of the Stage 4 commit, or None when history is unavailable."""
    if explicit:
        return explicit if git("rev-parse", "--verify", explicit).strip() else None
    line = git("log", "--format=%H %s", "--max-count=50")
    for entry in line.splitlines():
        sha, _, subject = entry.partition(" ")
        if subject.startswith(STAGE4_SUBJECT):
            return f"{sha}~1"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", help="git rev to diff against (default: "
                                       "parent of the Stage 4 commit)")
    args = ap.parse_args()

    src = DAEMON.read_text()
    tree = ast.parse(src)

    # ── A. structure ─────────────────────────────────────────────────────
    peg = fn_src(src, tree, "_peg_loop")
    check(peg is not None, "A: _peg_loop missing")
    if peg is None:
        print("BLOCKER: _peg_loop not found — Stage 4 not applied")
        return 1
    for name in PREEXISTING:
        check(fn_src(src, tree, name) is not None, f"A: {name} vanished")
    for marker in ('name="peg-rna"', 'name="crispr-grna"',
                   'name="confirm-poller"'):
        check(marker in src, f"A: thread launch {marker} missing")
    print(f"  A. structure      : _peg_loop ({len(peg.splitlines())} lines); "
          f"{len(PREEXISTING)} pre-existing loops intact")

    # ── B. row builder against real candidates ───────────────────────────
    from adaptive.life_peg import (design_candidates, list_reference_genes,
                                   load_gene_reference)
    from adaptive.life_peg_model import MODEL_PATH, rank_candidates

    gene = list_reference_genes()[0]
    label = (load_gene_reference(gene) or {})["hotspots"][0]["label"]
    cands = design_candidates(gene, label)
    ranked = rank_candidates(cands, top=5)
    check(bool(ranked), "B: nothing scored")

    sha = hashlib.sha256(Path(MODEL_PATH).read_bytes()).hexdigest()[:16]
    rows = [{**c.to_dict(), "ts": 1.0, "gene": gene, "hotspot_label": label,
             "predicted_efficiency": s, "rank": i, "n_candidates": len(cands),
             "model_sha256": sha, "source": "peg_daemon", "stage": 4,
             "submitted": False}
            for i, (c, s) in enumerate(ranked, start=1)]

    with tempfile.TemporaryDirectory(prefix="verify-peg-daemon-") as td:
        out = Path(td) / "life_peg_daemon_scores.jsonl"
        with out.open("a") as fh:            # append mode, as the daemon uses
            fh.writelines(json.dumps(r) + "\n" for r in rows)
        back = [json.loads(ln) for ln in out.read_text().splitlines()]

    need = {"gene", "hotspot_label", "spacer", "pbs", "rtt", "edit_type",
            "strand", "predicted_efficiency", "rank", "model_sha256",
            "source", "submitted"}
    check(len(back) == len(rows), "B: round-trip row count mismatch")
    for r in back:
        check(need <= set(r), f"B: row missing {need - set(r)}")
        check(r["submitted"] is False, "B: submitted flag not False")
        check(0.0 <= r["predicted_efficiency"] <= 1.0, "B: score out of range")
        check(r["source"] == "peg_daemon", "B: wrong source tag")
    effs = [r["predicted_efficiency"] for r in back]
    check(effs == sorted(effs, reverse=True), "B: not ranked best-first")
    print(f"  B. row builder    : {len(back)} rows, {len(need)} fields, "
          f"best eff={effs[0]:.4f}, ordered")

    # ── C. isolation ─────────────────────────────────────────────────────
    check("PEG_SCORES_JSONL" in peg, "C: not using dedicated constant")
    check("life_peg_daemon_scores" in src, "C: dedicated filename absent")
    check("life_boltz_scores" not in peg, "C: writes to dashboard feed")
    check('"life_peg_scores.jsonl"' not in peg, "C: collides with Stage 2 dump")
    for b in BANNED:
        check(b not in peg, f"C: banned symbol {b} reachable in _peg_loop")
    print(f"  C. isolation      : dedicated JSONL; {len(BANNED)}/{len(BANNED)} "
          "reward/submit symbols absent")

    # ── D. kill switches ─────────────────────────────────────────────────
    check("if _PEG_AVAILABLE and PEG_ENABLED:" in src, "D: compound guard")
    check("elif not _PEG_AVAILABLE:" in src, "D: unavailable branch")
    check('os.getenv("PEG_ENABLED", "1") == "1"' in src, "D: env switch")
    print("  D. kill switches  : _PEG_AVAILABLE guard + PEG_ENABLED=0")

    # ── E/F. insertion-only + untouched modality logic ───────────────────
    baseline = resolve_baseline(args.baseline)
    base = git("show", f"{baseline}:miner_daemon.py") if baseline else ""
    if not base:
        print("  E. vs baseline    : SKIPPED (no git history / baseline "
              "unresolved)")
        print("  F. modality logic : SKIPPED (same reason)")
    else:
        b_lines, n_lines = base.splitlines(), src.splitlines()
        ops = difflib.SequenceMatcher(None, b_lines, n_lines).get_opcodes()
        removed = sum(i2 - i1 for t, i1, i2, _, _ in ops
                      if t in ("delete", "replace"))
        inserted = sum(j2 - j1 for t, _, _, j1, j2 in ops if t == "insert")
        check(removed == 0, f"E: {removed} pre-existing lines changed/removed")
        check(all(t in ("equal", "insert") for t, *_ in ops),
              "E: diff contains a non-insert operation")
        check(inserted == EXPECTED_INSERTIONS,
              f"E: expected {EXPECTED_INSERTIONS} insertions, got {inserted}")
        print(f"  E. vs {baseline:<12}: {len(b_lines)} → {len(n_lines)} lines, "
              f"+{inserted}/-{removed} (insertion-only)")

        b_tree = ast.parse(base)
        for name in PREEXISTING:
            check(fn_src(base, b_tree, name) == fn_src(src, tree, name),
                  f"F: {name} body changed")
        print(f"  F. modality logic : {', '.join(PREEXISTING)} byte-identical")

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
