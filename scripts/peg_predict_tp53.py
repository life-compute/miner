#!/usr/bin/env python3
"""
peg_predict_tp53.py — Stage 2 log-only prediction run.

Designs real pegRNA candidates for TP53's validated hotspot codons (175, 248,
249, 273) from the Stage 0.1 reference data, featurises them with Stage 1, and
records the Stage 2 model's predicted editing efficiency for each.

LOG-ONLY.  Nothing is submitted on-chain, no reward is computed, no production
code path is touched.  Output is appended to output/life_peg_scores.jsonl for
later calibration - the same discipline used for the CRISPR pre-screen gate and
the CDK4/MYC threshold work: gather real distributions first, pick thresholds
from data second.

Usage:
    python scripts/peg_predict_tp53.py
    python scripts/peg_predict_tp53.py --all-genes
    python scripts/peg_predict_tp53.py --top 5 --no-write
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from adaptive.life_peg import (                                  # noqa: E402
    design_candidates, get_hotspot, list_reference_genes, load_gene_reference,
)
from adaptive import life_peg_model as lpm                       # noqa: E402

OUT_PATH = REPO / "output" / "life_peg_scores.jsonl"
TP53_HOTSPOTS = ["R175H", "R248W/Q", "R249S", "R273H"]


def run_hotspot(gene: str, label: str, top: int, write: bool) -> dict | None:
    h = get_hotspot(gene, label)
    if h is None:
        print(f"  {gene}/{label}: not in Stage 0.1 reference — skipped")
        return None

    t0 = time.time()
    cands = design_candidates(gene, label)
    ranked = lpm.rank_candidates(cands)
    elapsed = time.time() - t0

    if not ranked:
        print(f"  {gene}/{label}: {len(cands)} candidates, 0 scored "
              f"(model missing?)")
        return None

    scores = [s for _, s in ranked]
    print(f"\n─── {gene} {label} "
          f"(codon {h['codon']}, wt {h['wt_codon']}→{h['wt_aa']}) ───")
    print(f"  candidates={len(cands)}  scored={len(ranked)}  [{elapsed:.1f}s]")
    print(f"  predicted efficiency: max={max(scores):.4f} "
          f"mean={statistics.mean(scores):.4f} "
          f"median={statistics.median(scores):.4f} min={min(scores):.4f}")
    print(f"  top {top}:")
    print(f"    {'rank':>4} {'eff':>7} {'strand':>6} {'PBS':>4} {'RTT':>4} "
          f"{'n2e':>4}  spacer")
    for i, (c, s) in enumerate(ranked[:top], 1):
        print(f"    {i:>4} {s:>7.4f} {c.strand:>6} {len(c.pbs):>4} "
              f"{len(c.rtt):>4} {c.nick_to_edit:>4}  {c.spacer}")

    if write:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PATH.open("a") as fh:
            for c, s in ranked:
                fh.write(json.dumps({
                    "ts": time.time(),
                    "gene": gene,
                    "hotspot": label,
                    "codon": h["codon"],
                    "spacer": c.spacer,
                    "pbs": c.pbs,
                    "rtt": c.rtt,
                    "edit_type": c.edit_type,
                    "strand": c.strand,
                    "pbs_len": len(c.pbs),
                    "rtt_len": len(c.rtt),
                    "nick_to_edit": c.nick_to_edit,
                    "correction_length": c.correction_length,
                    "edit_to_rtt_end": c.edit_to_rtt_end,
                    "wt_seq": c.wt_seq,
                    "edited_seq": c.edited_seq,
                    "genomic_edit_pos": c.genomic_edit_pos,
                    "predicted_efficiency": round(s, 6),
                    "mode": "log_only",
                    "stage": 2,
                }) + "\n")

    return {"gene": gene, "hotspot": label, "n": len(ranked),
            "max": max(scores), "mean": statistics.mean(scores),
            "min": min(scores)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--all-genes", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    print("=" * 74)
    print("STAGE 2 — pegRNA efficiency prediction (LOG-ONLY, no submission)")
    print("=" * 74)

    if not lpm.is_trained():
        print("BLOCKER: no trained model at "
              f"{lpm.MODEL_PATH}\n  run: python scripts/train_peg_model.py")
        return 2

    rep = lpm.get_model_report()
    m = rep.get("metrics", {})
    print(f"model     : {lpm.MODEL_PATH.name}")
    print(f"trained on: {rep.get('n_rows', '?'):,} real pegRNAs "
          f"({rep.get('n_loci', '?'):,} loci) — {rep.get('label', '?')}")
    print(f"grouped R2: {m.get('grouped_r2')}  "
          f"Spearman: {m.get('grouped_spearman')}")
    print(f"caveat    : {rep.get('caveat', 'n/a')}")

    write = not args.no_write
    targets: list[tuple[str, str]]
    if args.all_genes:
        targets = []
        for g in list_reference_genes():
            rec = load_gene_reference(g)
            targets += [(g, x["label"]) for x in (rec["hotspots"] if rec else [])]
    else:
        targets = [("TP53", lab) for lab in TP53_HOTSPOTS]

    results = [r for g, lab in targets if (r := run_hotspot(g, lab, args.top, write))]

    print("\n" + "=" * 74)
    print(f"{'gene/hotspot':22} {'n':>6} {'max':>8} {'mean':>8} {'min':>8}")
    for r in results:
        print(f"{r['gene'] + '/' + r['hotspot']:22} {r['n']:>6} "
              f"{r['max']:>8.4f} {r['mean']:>8.4f} {r['min']:>8.4f}")
    total = sum(r["n"] for r in results)
    print(f"\n{total:,} predictions across {len(results)} hotspots")
    if write:
        print(f"appended → {OUT_PATH}")
    else:
        print("--no-write: nothing logged")
    print("\nLOG-ONLY: no on-chain submission, no reward logic, "
          "no threshold applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
