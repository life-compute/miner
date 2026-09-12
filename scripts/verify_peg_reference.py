#!/usr/bin/env python3
"""
Offline verifier for data/peg_reference/ (Stage 0.1).

Independently re-derives every claim in the emitted JSON without calling NCBI
and without importing the builder, so a bug in build_peg_reference.py cannot
vouch for its own output.  Offline, deterministic, no network.

Checks per hotspot entry:
  A. window[edit_offset : +3] == wt_codon              (codon readback)
  B. CODON_TABLE[wt_codon] == wt_aa                    (translation, own table)
  C. 0 <= edit_offset and edit_offset+3 <= len(window) (bounds)
  D. window is pure ACGTN, non-empty                   (alphabet)
  E. edit_offset == genomic_codon_start - window_start (offset self-consistency)
  F. a real NGG PAM exists within +-30nt of the edit   (prime-editing viability)
  G. promoter entries: codon fields are None, offset negative
Checks per gene file:
  H. index.json agrees with the per-gene file (count + labels + status)
  I. protein_len * 3 + 3 == cds_len                    (CDS/protein coherence)
Global:
  J. TP53 carries exactly the 4 requested hotspot codons 175/248/249/273
  K. all 10 expected genes present, every status OK
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REF = Path(__file__).resolve().parents[1] / "data" / "peg_reference"

# Own genetic code — deliberately re-derived, not imported from the builder.
_B = "TCAG"
_A = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
CODON = {a + b + c: _A[i] for i, (a, b, c)
         in enumerate((x, y, z) for x in _B for y in _B for z in _B)}

EXPECTED_GENES = {"TP53", "KRAS", "MYC", "EGFR", "HER2",
                  "BRCA1", "CDK4", "BCL2", "PDL1", "TERT"}
TP53_CODONS = {175, 248, 249, 273}

fails: list[str] = []
passes = 0


def check(cond: bool, label: str) -> None:
    global passes
    if cond:
        passes += 1
    else:
        fails.append(label)


def main() -> int:
    if not REF.is_dir():
        print(f"BLOCKER: {REF} does not exist")
        return 2

    index = json.loads((REF / "index.json").read_text())
    idx_genes = index["genes"]

    check(set(idx_genes) == EXPECTED_GENES,
          f"K: gene set mismatch: {set(idx_genes) ^ EXPECTED_GENES}")

    total_seen = 0
    for gene in sorted(EXPECTED_GENES):
        f = REF / f"{gene}.json"
        if not f.exists():
            fails.append(f"H: {gene}.json missing")
            continue
        rec = json.loads(f.read_text())
        entries = rec["hotspots"]
        total_seen += len(entries)

        check(rec["status"] == "OK", f"K: {gene} status={rec['status']}")
        check(idx_genes[gene]["n_hotspots"] == len(entries),
              f"H: {gene} index count {idx_genes[gene]['n_hotspots']} "
              f"!= file {len(entries)}")
        check(idx_genes[gene]["labels"] == [e["label"] for e in entries],
              f"H: {gene} index labels disagree with file")
        check(rec["protein_len"] * 3 + 3 == rec["cds_len"],
              f"I: {gene} protein_len {rec['protein_len']} incoherent with "
              f"cds_len {rec['cds_len']}")

        for e in entries:
            tag = f"{gene}/{e['label']}"
            w, off = e["window"], e["edit_offset_in_window"]

            check(bool(w) and set(w) <= set("ACGTN"), f"D: {tag} bad alphabet")
            check(0 <= off and off + 3 <= len(w), f"C: {tag} offset out of bounds")
            check(off == e["genomic_codon_start"] - e["window_start"],
                  f"E: {tag} offset != genomic_start - window_start")

            if e["hotspot_source"] == "promoter":
                check(e["codon"] is None and e["wt_codon"] is None,
                      f"G: {tag} promoter entry has codon fields set")
                check(e.get("promoter_offset_from_atg", 0) < 0,
                      f"G: {tag} promoter offset not negative")
            else:
                check(w[off:off + 3] == e["wt_codon"],
                      f"A: {tag} readback {w[off:off + 3]} != {e['wt_codon']}")
                check(CODON.get(e["wt_codon"]) == e["wt_aa"],
                      f"B: {tag} {e['wt_codon']} translates to "
                      f"{CODON.get(e['wt_codon'])}, claimed {e['wt_aa']}")

            pams = [i for i in range(max(0, off - 30), min(len(w) - 2, off + 30))
                    if w[i + 1:i + 3] == "GG"]
            check(len(pams) > 0, f"F: {tag} no NGG PAM within +-30nt")

    tp53 = json.loads((REF / "TP53.json").read_text())
    check({h["codon"] for h in tp53["hotspots"]} == TP53_CODONS,
          f"J: TP53 codons != {TP53_CODONS}")

    check(index["total_hotspots"] == total_seen,
          f"H: index total {index['total_hotspots']} != {total_seen}")

    print(f"assertions passed: {passes}")
    print(f"hotspot entries verified: {total_seen}")
    if fails:
        print(f"\nFAILURES ({len(fails)}):")
        for x in fails:
            print(f"  {x}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
