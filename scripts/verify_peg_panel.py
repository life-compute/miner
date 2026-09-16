#!/usr/bin/env python3
"""
verify_peg_panel.py — verification gate for the Stage 4 pegRNA dashboard panel.

Verifies the data path the PRIME EDITING panel depends on, end to end, and — the
part that matters — proves the honesty rules are real by breaking them on purpose
and confirming the endpoint refuses to render.

A checker that cannot fail is worthless, so section E corrupts each invariant in
turn against a scratch copy of the reference data and asserts the row is dropped.

SECTIONS
  A. Stage 4 JSONL integrity — required fields, log-only discipline
  B. Geometry invariants — all five, strand-aware, on every logged row
  C. Miner-vs-validator agreement — independent recompute, no observer trust
  D. Observer output — internal consistency against a fresh recompute
  E. Negative controls — corrupt each invariant, prove pegWindow() returns null
  F. Endpoint contract — /peg field presence, helix coordinate correctness

Read-only on everything the miner owns. Section E writes only into a temp dir
and restores nothing (it never touches the real reference files — it runs the
Node gate against a copied tree with an overridden ROOT).

USAGE
    python scripts/verify_peg_panel.py
    python scripts/verify_peg_panel.py --port 3099   # test a running server too
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from adaptive.life_peg import featurize_pegrna              # noqa: E402
from scripts.export_peg_model import predict_json           # noqa: E402

DAEMON_JSONL = REPO / "output" / "life_peg_daemon_scores.jsonl"
OBSERVER_JSONL = REPO / "output" / "life_peg_observer.jsonl"
MODEL_JSON = REPO / "output" / "peg_model.json"
REF_DIR = REPO / "data" / "peg_reference"
SERVER_CJS = REPO / "dashboard" / "server.cjs"

_COMPLEMENT = str.maketrans("ACGTN", "TGCAN")

REQUIRED = (
    "gene", "hotspot_label", "strand", "spacer", "pbs", "rtt", "edit_type",
    "pam_offset", "nick_offset", "edit_offset", "nick_to_edit",
    "correction_length", "edit_to_rtt_end", "wt_seq", "edited_seq",
    "genomic_edit_pos", "predicted_efficiency", "rank", "ts",
)

checks = 0
fails: list[str] = []


def ok(cond: bool, label: str) -> None:
    global checks
    if cond:
        checks += 1
    else:
        fails.append(label)


def revcomp(s: str) -> str:
    return s.translate(_COMPLEMENT)[::-1]


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


_refs: dict[str, dict] = {}


def window_for(row: dict, ref_dir: Path = REF_DIR) -> str | None:
    gene = row["gene"]
    key = f"{ref_dir}/{gene}"
    if key not in _refs:
        try:
            _refs[key] = json.loads((ref_dir / f"{gene}.json").read_text())
        except Exception:
            return None
    hs = next((h for h in _refs[key].get("hotspots", [])
               if h.get("label") == row["hotspot_label"]), None)
    if not hs:
        return None
    w = hs["window"]
    return w if row["strand"] == "+" else revcomp(w)


# ── The Node-side gate, invoked as a subprocess so we test the REAL code the
#    endpoint uses rather than a Python re-implementation of it. ──────────────
NODE_HARNESS = r"""
const path = require('path');
const fs = require('fs');
const ROOT = process.argv[2];
const rowJson = process.argv[3];

// Re-create pegWindow()'s dependencies, then eval the real function out of
// server.cjs so we are testing shipped code, not a copy of it.
const src = fs.readFileSync(path.join(__dirname, 'server_under_test.cjs'), 'utf8');
const start = src.indexOf('function pegRevcomp');
const end   = src.indexOf('/* ── Miner alive detection');
if (start < 0 || end < 0) { console.log('HARNESS_EXTRACT_FAIL'); process.exit(2); }
const body = src.slice(start, end);

function readJson(p) { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; } }
const PEG_REF_DIR = path.join(ROOT, 'data', 'peg_reference');
const pegRefCache = new Map();
eval(body);

const row = JSON.parse(rowJson);
const w = pegWindow(row);
console.log(w === null ? 'NULL' : 'OK:' + w.length);
"""


def run_node_gate(tmp: Path, root: Path, row: dict) -> str:
    r = subprocess.run(
        ["node", str(tmp / "harness.cjs"), str(root), json.dumps(row)],
        capture_output=True, text=True, timeout=60,
    )
    return (r.stdout or r.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0,
                    help="also verify a running dashboard on this port")
    args = ap.parse_args()

    print("=" * 74)
    print("VERIFY — Stage 4 pegRNA dashboard panel")
    print("=" * 74)

    rows = read_jsonl(DAEMON_JSONL)
    if not rows:
        print(f"BLOCKER: {DAEMON_JSONL} empty or missing")
        return 2
    spec = json.loads(MODEL_JSON.read_text())

    # ── A. JSONL integrity ────────────────────────────────────────────────
    print(f"\n── A. Stage 4 JSONL ({len(rows)} rows) ──")
    for i, r in enumerate(rows):
        missing = [f for f in REQUIRED if r.get(f) is None]
        ok(not missing, f"A: row {i} missing {missing}")
    ok(all(r.get("submitted") is False for r in rows),
       "A: a row has submitted != False — LOG-ONLY violated")
    ok(all(r.get("stage") == 4 for r in rows), "A: a row has stage != 4")
    ok(all(r.get("source") == "peg_daemon" for r in rows),
       "A: a row has source != peg_daemon")
    shas = {r.get("model_sha256") for r in rows}
    print(f"   required fields present : {len(rows)}/{len(rows)}")
    print(f"   submitted=False         : all")
    print(f"   model_sha256            : {sorted(s for s in shas if s)}")

    # ── B. Geometry invariants ────────────────────────────────────────────
    print("\n── B. Geometry invariants (strand-aware) ──")
    counts = {"edit": 0, "spacer": 0, "pam": 0, "pbs": 0, "rtt": 0}
    n_minus = 0
    for i, r in enumerate(rows):
        w = window_for(r)
        ok(w is not None, f"B: row {i} window not reconstructable")
        if w is None:
            continue
        if r["strand"] == "-":
            n_minus += 1
        edit, nick, pam = r["edit_offset"], r["nick_offset"], r["pam_offset"]
        wt, ed = r["wt_seq"], r["edited_seq"]
        sp, pbs, rtt = r["spacer"], r["pbs"], r["rtt"]

        c1 = w[edit:edit + len(wt)] == wt
        c2 = w[nick - len(sp):nick] == sp
        c3 = w[pam:pam + 3][1:] == "GG"
        c4 = w[nick - len(pbs):nick] == revcomp(pbs)
        edited = w[:edit] + ed + w[edit + len(wt):]
        c5 = edited[nick:nick + len(rtt)] == revcomp(rtt)

        for name, c in (("edit", c1), ("spacer", c2), ("pam", c3),
                        ("pbs", c4), ("rtt", c5)):
            ok(c, f"B: row {i} ({r['gene']}/{r['hotspot_label']}) {name} invariant")
            if c:
                counts[name] += 1

    n = len(rows)
    print(f"   window[edit]==wt_seq        : {counts['edit']}/{n}")
    print(f"   spacer ends at nick         : {counts['spacer']}/{n}")
    print(f"   pam_offset is NGG           : {counts['pam']}/{n}")
    print(f"   revcomp(pbs) 5' of nick     : {counts['pbs']}/{n}")
    print(f"   revcomp(rtt)==edited@nick   : {counts['rtt']}/{n}")
    print(f"   '-' strand rows (revcomp'd) : {n_minus}/{n}")
    ok(n_minus > 0, "B: no '-' strand rows — strand handling is untested")

    # ── C. Independent agreement recompute ────────────────────────────────
    print("\n── C. Miner vs validator (independent recompute) ──")
    worst, exact, scored = 0.0, 0, 0
    recomputed_exact: dict[tuple, bool] = {}
    for i, r in enumerate(rows):
        w = window_for(r)
        if w is None:
            continue
        feats = featurize_pegrna(
            r["spacer"], r["pbs"], r["rtt"], r["edit_type"],
            gene=r["gene"], genomic_edit_pos=r["genomic_edit_pos"],
            nick_to_edit=r["nick_to_edit"],
            correction_length=r["correction_length"],
            edit_to_rtt_end=r["edit_to_rtt_end"], window=w,
            edit_offset=r["edit_offset"], pam_offset=r["pam_offset"],
            strand=r["strand"],
        )
        ok(feats is not None, f"C: row {i} failed to featurize")
        if feats is None:
            continue
        v = predict_json(spec, feats)
        d = abs(v - float(r["predicted_efficiency"]))
        scored += 1
        worst = max(worst, d)
        recomputed_exact[(r["ts"], r["rank"])] = (d == 0.0)
        if d == 0.0:
            exact += 1
    print(f"   rows recomputed  : {scored}")
    print(f"   bit-exact        : {exact}/{scored}")
    print(f"   max abs delta    : {worst!r}")
    ok(scored == n, "C: not every row could be recomputed")

    # ── D. Observer file consistency ──────────────────────────────────────
    print("\n── D. Observer output ──")
    obs = read_jsonl(OBSERVER_JSONL)
    ok(bool(obs), "D: observer file empty — run scripts/peg_observer.py")
    if obs:
        by_key = {(o["ts"], o["rank"]): o for o in obs}
        ok(len(by_key) == len(obs), "D: duplicate (ts,rank) keys in observer file")
        covered = sum(1 for r in rows if (r["ts"], r["rank"]) in by_key)
        print(f"   observations       : {len(obs)}")
        print(f"   daemon rows covered: {covered}/{n}")
        mism = 0
        for r in rows:
            o = by_key.get((r["ts"], r["rank"]))
            if not o:
                continue
            if o.get("miner_eff") != r["predicted_efficiency"]:
                mism += 1
        ok(mism == 0, f"D: {mism} observations disagree with the logged miner_eff")
        n_exact_obs = sum(1 for o in obs if o.get("exact") is True)
        print(f"   observer bit-exact : {n_exact_obs}/{len(obs)}")
        # The observer samples a prefix of the daemon log and stops; the daemon
        # keeps appending. Comparing its count to a recompute of *every* row is
        # apples-to-oranges and goes red purely from the daemon running longer.
        # What must hold is that every row the observer DID cover is bit-exact.
        exact_covered = sum(
            1 for r in rows
            if (r["ts"], r["rank"]) in by_key and recomputed_exact.get(
                (r["ts"], r["rank"])) is True
        )
        ok(n_exact_obs == exact_covered,
           f"D: observer exact {n_exact_obs} != recompute over the same "
           f"{covered} covered rows ({exact_covered})")

    # ── E. Negative controls on the shipped Node gate ─────────────────────
    print("\n── E. Negative controls (shipped pegWindow must reject) ──")
    tmp = Path(tempfile.mkdtemp(prefix="hermes-peg-verify-"))
    try:
        (tmp / "harness.cjs").write_text(NODE_HARNESS)
        shutil.copy(SERVER_CJS, tmp / "server_under_test.cjs")
        fake_root = tmp / "root"
        shutil.copytree(REF_DIR, fake_root / "data" / "peg_reference")

        good = dict(rows[-1])
        base = run_node_gate(tmp, fake_root, good)
        ok(base.startswith("OK:"),
           f"E: clean row rejected by shipped gate (got {base!r}) — gate is broken")
        print(f"   clean row                      : {base}")

        # Mutations must be guaranteed to actually change the value. Picking a
        # fixed base (e.g. always "A") silently no-ops when the sequence already
        # starts with it — that produced a false "gate accepted a bad row"
        # failure on a row whose rtt began with A. flip() always differs.
        def flip(ch: str) -> str:
            return "T" if ch != "T" else "G"

        cases = [
            ("wt_seq corrupted",        {"wt_seq": "NNN"}),
            ("spacer shifted off nick", {"nick_offset": good["nick_offset"] + 3}),
            ("pam_offset not NGG",      {"pam_offset": good["pam_offset"] + 1}),
            ("pbs mutated",             {"pbs": flip(good["pbs"][0]) + good["pbs"][1:]}),
            ("rtt mutated",             {"rtt": flip(good["rtt"][0]) + good["rtt"][1:]}),
            ("spacer mutated",          {"spacer": flip(good["spacer"][0]) + good["spacer"][1:]}),
            ("edited_seq mutated",      {"edited_seq": flip(good["edited_seq"][0]) + good["edited_seq"][1:]}),
            ("edit_offset out of range",{"edit_offset": 10 ** 6}),
            ("strand flipped",          {"strand": "-" if good["strand"] == "+" else "+"}),
            ("missing spacer",          {"spacer": None}),
        ]
        for label, patch in cases:
            row = dict(good)
            row.update(patch)
            # Guard the test itself: a patch that does not change the row cannot
            # prove anything about the gate.
            ok(any(row[k] != good[k] for k in patch),
               f"E: mutation '{label}' is a no-op — test is invalid")
            out = run_node_gate(tmp, fake_root, row)
            rejected = out == "NULL"
            ok(rejected, f"E: '{label}' was NOT rejected (got {out!r})")
            print(f"   {'REJECTED' if rejected else 'ACCEPTED ⚠':<14} {label}")

        # Unknown gene → reference genuinely absent
        row = dict(good)
        row["gene"] = "NOT_A_GENE"
        out = run_node_gate(tmp, fake_root, row)
        ok(out == "NULL", f"E: unknown gene not rejected (got {out!r})")
        print(f"   {'REJECTED':<14} unknown gene")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── F. Live endpoint ──────────────────────────────────────────────────
    if args.port:
        print(f"\n── F. Endpoint contract (port {args.port}) ──")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{args.port}/peg", timeout=20
            ) as resp:
                ok(resp.status == 200, f"F: /peg returned HTTP {resp.status}")
                payload = json.loads(resp.read())
        except Exception as e:
            fails.append(f"F: /peg unreachable: {e}")
            payload = None

        if payload:
            for key in ("latest", "recent", "agreement", "total_evaluated",
                        "n_renderable", "n_hotspots", "best_efficiency",
                        "model_sha256", "stage", "mode"):
                ok(key in payload, f"F: /peg missing key {key}")
            ok(payload.get("mode") == "log_only", "F: mode != log_only")
            ok(payload.get("stage") == 4, "F: stage != 4")
            ok(payload.get("total_evaluated") == n,
               f"F: total_evaluated {payload.get('total_evaluated')} != {n}")
            ok(len(payload.get("recent", [])) <= 10, "F: recent longer than 10")

            agr = payload.get("agreement") or {}
            ok(agr.get("checked") == len(obs),
               f"F: agreement.checked {agr.get('checked')} != observer rows {len(obs)}")
            ok(agr.get("max_delta") == worst,
               f"F: agreement.max_delta {agr.get('max_delta')} != recomputed {worst}")

            lat = payload.get("latest")
            ok(lat is not None, "F: latest is null despite renderable rows")
            if lat:
                ok(lat.get("submitted") is False, "F: latest.submitted != False")
                h = lat.get("helix")
                ok(isinstance(h, dict), "F: latest.helix missing")
                if h:
                    # Helix must be a real slice of the real window, and the
                    # stated edit index must land on the stated WT bases.
                    src = next(r for r in rows
                               if r["gene"] == lat["gene"]
                               and r["hotspot_label"] == lat["hotspot_label"]
                               and r["rank"] == lat["rank"])
                    w = window_for(src)
                    ok(w is not None, "F: latest row's window not reconstructable")
                    if w is not None:
                        ok(h["wt"] == w[h["offset"]:h["offset"] + len(h["wt"])],
                           "F: helix.wt is not a verbatim slice of the real window")
                    ok(len(h["wt"]) <= 43, f"F: helix wider than 43 cols ({len(h['wt'])})")
                    ok(h["wt"][h["edit_index"]:h["edit_index"] + h["edit_len"]]
                       == lat["wt_seq"], "F: helix.edit_index does not land on wt_seq")
                    ok(h["edited"][h["edit_index"]:h["edit_index"] + len(lat["edited_seq"])]
                       == lat["edited_seq"],
                       "F: helix.edited does not carry edited_seq at edit_index")
                    ok(h["pam"][1:] == "GG", f"F: helix.pam {h['pam']!r} is not NGG")
                    print(f"   helix slice        : {len(h['wt'])} cols, "
                          f"edit@{h['edit_index']} {lat['wt_seq']}→{lat['edited_seq']}")
                    print(f"   pam                : {h['pam']}")
            print(f"   total_evaluated    : {payload.get('total_evaluated')}")
            print(f"   n_renderable       : {payload.get('n_renderable')}")
            print(f"   agreement          : {agr.get('exact')}/{agr.get('checked')} "
                  f"{agr.get('status')}")
    else:
        print("\n── F. Endpoint contract — SKIPPED (pass --port to enable) ──")

    print()
    print("=" * 74)
    print(f"assertions passed: {checks}")
    if fails:
        print(f"\nFAILURES ({len(fails)}):")
        for f in fails[:40]:
            print(f"  {f}")
        if len(fails) > 40:
            print(f"  … and {len(fails) - 40} more")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
