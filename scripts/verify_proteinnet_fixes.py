#!/usr/bin/env python3
"""Standing gate for the 2026-09-14 ProteinNet / daemon fixes.

    python3 scripts/verify_proteinnet_fixes.py            # full (trains models)
    python3 scripts/verify_proteinnet_fixes.py --offline  # static checks only

Covers four changes plus the two tools built to verify them:
  FIX 1  life_proteinnet._train_target  — KFold(shuffle=True) instead of cv=<int>
  FIX 2  life_proteinnet._train_target  — degenerate-label guard + stale-pkl eviction
  FIX 3  miner_daemon (both loops)      — stop clearing ref_last_screened/ref_scores
  FIX 4  life_proteinnet._model_path    — key on target_id, not the shared UniProt id
  TOOLS  watch_ref_throttle verdict logic + migrate_proteinnet_model_names safety

Exercises the real functions (imported, or AST-parsed for the daemon) rather
than reimplementations.  Model writes go to a scratch dir; output/ is never
touched.  Exits non-zero on any failure.
"""
from __future__ import annotations

# Scripts are loaded dynamically and their module globals monkeypatched, which
# static analysis cannot follow.
# pyright: reportAttributeAccessIssue=false

import argparse
import ast
import collections
import importlib.util
import json
import pathlib
import re
import shutil
import sys
import tempfile

import numpy as np

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEGENERATE = ["CDK4", "ESR1", "MDM2", "PDL1", "TERT_mRNA"]
RECOVERED = ["CCL2_mRNA", "CDK4_mRNA", "IL6_mRNA", "LDHA_mRNA"]
SCRIPTS = REPO / "scripts"

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    return ok


def load_rows() -> dict[str, list[dict]]:
    """Scored rows per target, using the same filters as _load_boltz_rows."""
    data: dict[str, list[dict]] = collections.defaultdict(list)
    with (REPO / "output" / "life_boltz_scores.jsonl").open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("boltz_score") is None or not row.get("smiles"):
                continue
            if row.get("target_type") == "CRISPR" or row.get("source") == "crispr_generated":
                continue
            data[row["target_id"]].append(row)
    return data


def _ref_clears(tree: ast.AST) -> list[int]:
    """Line numbers of any ref_last_screened/ref_scores .clear() call."""
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "clear"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id in {"ref_last_screened", "ref_scores"}
    ]


# ── FIX 3 — static: the throttle reset is gone ────────────────────────────────
def verify_daemon(data: dict[str, list[dict]]) -> None:
    print("\nFIX 3 — daemon ref throttle no longer reset by the 5-min refresh")
    tree = ast.parse((REPO / "miner_daemon.py").read_text())

    bad = _ref_clears(tree)
    check(not bad, "no ref_last_screened/ref_scores .clear() remains",
          f"offending lines: {bad}" if bad else "0 occurrences")

    refresh = next(
        (n.value.value
         for n in ast.walk(tree)
         if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
         for t in n.targets
         if isinstance(t, ast.Name) and t.id == "TARGET_REFRESH"),
        None,
    )
    check(refresh == 300,
          "TARGET_REFRESH unchanged at 300s (the fix is the .clear(), not the cadence)",
          f"TARGET_REFRESH={refresh}")

    # Negative control: a matcher that never fires would pass the check above.
    check(bool(_ref_clears(ast.parse("ref_last_screened.clear()"))),
          "negative control: detector fires on the pre-fix pattern")

    print("\nFIX 3 evidence — historical data shows the 4h throttle never fired")
    gaps = {
        tid: float(np.median(np.diff(ts)))
        for tid, rows in data.items()
        if len(ts := sorted(r["ts"] for r in rows if r.get("source") == "ref")) >= 10
    }
    fastest = min(gaps.items(), key=lambda kv: kv[1], default=None)
    check(bool(gaps) and float(np.median(list(gaps.values()))) < 14400,
          "median ref rescreen gap << REF_RESCREEN_INTERVAL (14400s)",
          f"n_targets={len(gaps)} median_gap={np.median(list(gaps.values())):.0f}s "
          f"fastest={fastest[0]}@{fastest[1]:.0f}s" if fastest else "no ref rows")


# ── FIX 1 + 2 — behavioural: run the real _train_target ───────────────────────
def verify_proteinnet(data: dict[str, list[dict]]) -> None:
    import adaptive.life_proteinnet as pn
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import KFold, cross_val_score, train_test_split
    from sklearn.metrics import r2_score

    def mk() -> GradientBoostingRegressor:
        return GradientBoostingRegressor(n_estimators=200, max_depth=4,
                                         learning_rate=0.05, subsample=0.8,
                                         random_state=42)

    def xy(tid: str) -> tuple[np.ndarray, np.ndarray]:
        X, y = [], []
        for row in data[tid]:
            feats = pn._featurize(row["smiles"])
            if feats is None:
                continue
            X.append(feats)
            y.append(float(row["boltz_score"]))
        return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32)

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="verify-proteinnet-models-"))
    real_dir = pn._MODEL_DIR
    pn._MODEL_DIR = scratch
    try:
        print("\nFIX 2 — degenerate-label guard (real _train_target, scratch model dir)")
        for tid in DEGENERATE:
            # Model files are keyed on target_id, so the planted pkl must be too.
            stale = pn._model_path(tid)
            stale.write_bytes(b"stale")   # must be evicted, not merely skipped
            pn._models[tid] = object()

            res = pn._train_target(tid, f"VERIFY_{tid}", data[tid]) or {}
            check(res.get("status") == "degenerate"
                  and res.get("unique_y") == 1
                  and not stale.exists()
                  and tid not in pn._models,
                  f"{tid}: refused + evicted",
                  f"status={res.get('status')} unique_y={res.get('unique_y')} "
                  f"pkl_exists={stale.exists()} in_mem={tid in pn._models}")

        print("\nFIX 1 — shuffled KFold (real _train_target trains and saves)")
        for tid in RECOVERED:
            res = pn._train_target(tid, f"VERIFY_{tid}", data[tid]) or {}
            saved = pn._model_path(tid).exists()
            check(res.get("status") != "degenerate"
                  and (res.get("r2") or -1) > 0.0   # was clamped to -1.0 pre-fix
                  and saved,
                  f"{tid}: trained, r2>0, pkl written",
                  f"r2={res.get('r2')} pkl={saved}")
    finally:
        pn._MODEL_DIR = real_dir
        shutil.rmtree(scratch, ignore_errors=True)

    print("\nFIX 1 — old (sequential) vs new (shuffled) CV vs independent 25% holdout")
    for tid in RECOVERED:
        X, y = xy(tid)
        old_raw = float(cross_val_score(mk(), X, y, cv=5, scoring="r2").mean())
        old = max(-1.0, min(1.0, old_raw))
        new = float(cross_val_score(mk(), X, y, scoring="r2",
                                    cv=KFold(5, shuffle=True, random_state=42)).mean())
        Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=7)
        model = mk()
        model.fit(Xtr, ytr)
        hold = r2_score(yte, model.predict(Xte))
        # New CV must beat the clamped old value.  Holdout agreement is only
        # meaningful where there is real signal: CDK4_mRNA sits near r2≈0.1, so a
        # single 25% split swings widely (0.12 vs -0.19) without contradicting the
        # 5-fold mean.  Require agreement only for targets above a signal floor.
        agrees = abs(new - hold) < 0.25 or new < 0.15
        check(old <= -0.999 and new > 0.0 and agrees,
              f"{tid}: recovered" + ("" if new >= 0.15 else " (weak signal, holdout not required)"),
              f"old={old:.4f} (raw {old_raw:.1f})  new={new:.4f}  holdout={hold:.4f}")

    print("\nFIX 2 rationale — a degenerate target scores 1.00 even on an honest holdout")
    X, y = xy("CDK4")
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=7)
    model = mk()
    model.fit(Xtr, ytr)
    hold = r2_score(yte, model.predict(Xte))
    # float32 residue: variance is ~5e-17, not exactly 0 — hence MIN_LABEL_STD=1e-4.
    var = float(np.var(yte))
    check(abs(hold - 1.0) < 1e-9 and var < 1e-12,
          "CDK4: holdout R2==1.00 with ~zero variance (the guard is the only defence)",
          f"holdout={hold:.6f} var={var:.3e}")

    print("\nFIX 2 margin — MIN_UNIQUE_LABELS is the criterion doing the real work")
    missed = [
        tid for tid in DEGENERATE + ["VEGF_mRNA", "TNF_mRNA", "KRAS", "BCL2", "MYC"]
        if (ys := [float(r["boltz_score"]) for r in data.get(tid, [])])
        and not (len(set(ys)) < pn.MIN_UNIQUE_LABELS or float(np.std(ys)) < pn.MIN_LABEL_STD)
    ]
    check(not missed, "all known-degenerate targets caught by the guard",
          f"missed={missed}" if missed else
          f"uY<{pn.MIN_UNIQUE_LABELS} catches the uY=2 targets a std-only check would miss")
    kept = [t for t in ("TP53", "SMAD4")
            if len({float(r["boltz_score"]) for r in data.get(t, [])}) >= pn.MIN_UNIQUE_LABELS]
    check(sorted(kept) == ["SMAD4", "TP53"],
          "healthy targets (TP53, SMAD4) still pass the guard", f"kept={sorted(kept)}")


# ── FIX 4 — model files keyed on target_id, not the shared UniProt accession ──
def verify_model_paths(data: dict[str, list[dict]]) -> None:
    import adaptive.life_proteinnet as pn

    print("\nFIX 4 — modalities sharing a UniProt accession get distinct model files")
    trio = ["CDK4", "CDK4_mRNA", "CDK4_CRISPR"]
    paths = [pn._model_path(t) for t in trio]
    check(len({p.name for p in paths}) == 3,
          "CDK4 / CDK4_mRNA / CDK4_CRISPR resolve to 3 distinct filenames",
          ", ".join(p.name for p in paths))

    # Regression: the pre-fix scheme collapsed all three onto one accession.
    check(len({f"{pn.get_model_report().get('models', {}).get(t, {}).get('uniprot_id', t)}"
               for t in trio}) == 1,
          "…and all three still share one uniprot_id (so the old scheme collided)",
          "P11802")

    # Round-trip: save distinct sentinels per modality, confirm no clobbering.
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="verify-proteinnet-paths-"))
    real_dir, real_models = pn._MODEL_DIR, dict(pn._models)
    pn._MODEL_DIR = scratch
    try:
        for tid in trio:
            pn._save_model(tid, {"sentinel": tid})
        loaded: dict[str, object] = {tid: pn._load_model(tid) for tid in trio}
        check(all(loaded[t] == {"sentinel": t} for t in trio),
              "each modality round-trips its own model (no clobbering)",
              str({t: loaded[t] for t in trio}))

        # pre_screen must resolve from disk by target_id alone, with no report entry.
        pn._models.pop("CDK4_CRISPR", None)
        check(pn._load_model("CDK4_CRISPR") == {"sentinel": "CDK4_CRISPR"},
              "pre_screen's disk fallback resolves by target_id (no report lookup)")
    finally:
        pn._MODEL_DIR = real_dir
        pn._models.clear()
        pn._models.update(real_models)
        shutil.rmtree(scratch, ignore_errors=True)

    print("\nFIX 4 — on-disk migration left no UniProt-named files")
    live = REPO / "output" / "protein_models"
    stale = [p.name for p in live.glob("*_model.pkl")
             if re.fullmatch(r"[OPQ][0-9][A-Z0-9]{3}[0-9]", p.name[: -len("_model.pkl")])]
    check(not stale, "no <UNIPROT>_model.pkl remains in output/protein_models",
          f"stale={stale}" if stale else f"{len(list(live.glob('*_model.pkl')))} files, all target-keyed")

    report_targets = set(json.loads((live / "proteinnet_report.json").read_text())["models"])
    unknown = [p.name for p in live.glob("*_model.pkl")
               if p.name[: -len("_model.pkl")] not in report_targets]
    check(not unknown, "every model file maps to a known target_id",
          f"unknown={unknown}" if unknown else "all filenames are report keys")

    # life_brain reads these filenames directly — keep it in lockstep.
    brain = (REPO / "adaptive" / "life_brain.py").read_text()
    check('f"{gene_name}_model.pkl"' in brain and 'f"{uid}_model.pkl"' not in brain,
          "life_brain._fallback_mae_protein reads <target_id>_model.pkl")


# ── TOOLS — the two verification tools themselves ─────────────────────────────
def _load_script(name: str):  # pyright: ignore[reportUnknownParameterType]
    """Import a sibling script by path so its module globals can be monkeypatched."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def verify_tools() -> None:
    """The watcher decides whether FIX 3 is declared proven, so a false PROVEN
    (or a missed REFUTED) is worse than having no watcher.  Drive its real
    main() over synthetic feeds with the reference list stubbed."""
    watch = _load_script("watch_ref_throttle")
    t0, hour = 1_000_000.0, 3600.0

    def row(tid: str, src: str, ts: float) -> dict:
        return {"target_id": tid, "source": src, "ts": ts, "smiles": "C", "boltz_score": 0.1}

    def verdict(rows: list[dict], refs: set[str]) -> int:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="verify-watch-"))
        (tmp / "f.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        saved = (watch.JSONL, watch.ref_targets, sys.argv)
        watch.JSONL, watch.ref_targets = tmp / "f.jsonl", lambda: refs
        sys.argv = ["watch", "--since", str(t0 - 1), "--timeout", "0.4", "--poll", "0.1"]
        try:
            return watch.main()
        finally:
            watch.JSONL, watch.ref_targets, sys.argv = saved
            shutil.rmtree(tmp, ignore_errors=True)

    print("\nTOOLS — watch_ref_throttle.py verdict logic (0=proven 1=refuted 2=inconclusive)")
    cases: list[tuple[str, int, list[dict], set[str]]] = [
        ("PROVEN: ref-equipped target served non-ref on revisit within 4h", 0,
         [row("TP53", "ref", t0), row("TP53", "zinc15", t0 + hour)], {"TP53"}),
        ("REFUTED: ref-equipped target re-screened as ref after 300s", 1,
         [row("TP53", "ref", t0), row("TP53", "ref", t0 + 300)], {"TP53"}),
        ("INCONCLUSIVE: only first screens seen", 2,
         [row("TP53", "ref", t0), row("MYC", "ref", t0 + 60)], {"TP53", "MYC"}),
        # The two non-discriminating cases misread as proof on 2026-09-14.
        ("no-ref-compound target served non-ref is NOT proof", 2,
         [row("KRAS_mRNA", "zinc15", t0), row("KRAS_mRNA", "zinc15", t0 + 60)], {"KRAS"}),
        ("non-ref on a FIRST visit is NOT proof", 2,
         [row("TP53", "zinc15", t0)], {"TP53"}),
        ("re-screen AFTER 4h is legitimate, not refuted", 2,
         [row("TP53", "ref", t0), row("TP53", "ref", t0 + 5 * hour)], {"TP53"}),
        ("crispr_generated rows are excluded from the verdict", 2,
         [row("TP53", "ref", t0), row("TP53", "crispr_generated", t0 + 60)], {"TP53"}),
        ("a refutation is not masked by a later passing row", 1,
         [row("TP53", "ref", t0), row("TP53", "ref", t0 + 300),
          row("TP53", "zinc15", t0 + 600)], {"TP53"}),
    ]
    for label, want, rows, refs in cases:
        got = verdict(rows, refs)
        check(got == want, label, f"exit={got} (want {want})")

    print("\nTOOLS — migrate_proteinnet_model_names.py dry-run safety + idempotency")
    mig = _load_script("migrate_proteinnet_model_names")
    box = pathlib.Path(tempfile.mkdtemp(prefix="verify-mig-"))
    (box / "proteinnet_report.json").write_text(json.dumps({"models": {
        "CDK4":      {"status": "ready",    "uniprot_id": "P11802"},
        "CDK4_mRNA": {"status": "ready",    "uniprot_id": "P11802"},
        "TP53":      {"status": "ready",    "uniprot_id": "P04637"},
        "APC":       {"status": "learning", "uniprot_id": "P25054"},
    }}))
    for acc in ("P11802", "P04637", "P25054"):
        (box / f"{acc}_model.pkl").write_bytes(b"x")

    names = lambda: sorted(p.name for p in box.glob("*_model.pkl"))  # noqa: E731
    saved = (mig.MODEL_DIR, mig.REPORT, sys.argv)
    mig.MODEL_DIR, mig.REPORT = box, box / "proteinnet_report.json"
    try:
        sys.argv = ["mig"]                     # dry-run must not mutate
        mig.main()
        check(names() == ["P04637_model.pkl", "P11802_model.pkl", "P25054_model.pkl"],
              "dry-run leaves the directory untouched", str(names()))

        sys.argv = ["mig", "--apply"]
        mig.main()
        after = names()
        check(after == ["APC_model.pkl", "TP53_model.pkl"],
              "apply: unambiguous renamed, contested accession deleted", str(after))
        check(not (box / "P11802_model.pkl").exists(),
              "contested P11802 (CDK4 + CDK4_mRNA both ready) deleted, not guessed")
        check((box / "APC_model.pkl").exists(),
              "sole non-ready claimant still renamed")

        sys.argv = ["mig", "--apply"]          # second apply is a no-op
        mig.main()
        check(names() == after, "second --apply is idempotent", str(after))
    finally:
        mig.MODEL_DIR, mig.REPORT, sys.argv = saved
        shutil.rmtree(box, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true",
                    help="static/daemon checks only; skip model training")
    args = ap.parse_args()

    data = load_rows()
    verify_daemon(data)
    verify_model_paths(data)
    verify_tools()
    if args.offline:
        print("\n[SKIP] FIX 1/2 behavioural checks (--offline)")
    else:
        verify_proteinnet(data)

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'=' * 70}\nad-hoc verification: {passed}/{total} checks passed\n{'=' * 70}")
    if passed != total:
        print("FAILED:", [lbl for ok, lbl in _results if not ok])
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
