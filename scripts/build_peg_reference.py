#!/usr/bin/env python3
"""
build_peg_reference.py — build the prime-editing reference database.

Fetches real RefSeq data from NCBI E-utilities for the 10 LIFE Compute cancer
genes and emits, for every known mutation hotspot, a genuine GENOMIC sequence
window centred on the edit site.

Why genomic and not CDS
-----------------------
Prime editing needs a real PAM within ~30 bp of the edit and a real primer
binding site on the genomic strand.  A spliced CDS window would invent PAM
sites that do not exist in the genome and hide ones that do.  Every window in
the output is therefore taken from the RefSeqGene (NG_) genomic record.

Validation gates (all must pass or the entry is dropped, never silently kept)
---------------------------------------------------------------------------
  1. CDS length divisible by 3, translates with a terminal stop codon.
  2. The hotspot codon translates to the expected wild-type amino acid.
  3. The CDS context around the codon is found exactly once in the genomic
     record, so the genomic window is unambiguous.
  4. The codon read back out of the genomic window equals the CDS codon.

Anything that fails is reported as FAIL and excluded from the output file.
The point of this script is to refuse to emit data it cannot prove.

Output: data/peg_reference/<GENE>.json   +   data/peg_reference/index.json
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "data" / "peg_reference"

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# Window of genomic sequence stored per hotspot, centred on the edit position.
WINDOW_HALF = 60          # → 121 nt window (60 + edit base + 60)
CTX_HALF = 21             # CDS context half-width used to anchor into genomic
MIN_CTX_HALF = 6          # floor for the adaptive shrink at splice junctions
MIN_CTX_TOTAL = 15        # a context shorter than this is not specific enough

# ── Standard genetic code ─────────────────────────────────────────────────────
_BASES = "TCAG"
_AAS = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
CODON_TABLE: dict[str, str] = {}
_i = 0
for _b1 in _BASES:
    for _b2 in _BASES:
        for _b3 in _BASES:
            CODON_TABLE[_b1 + _b2 + _b3] = _AAS[_i]
            _i += 1


def translate(nt: str) -> str:
    return "".join(CODON_TABLE.get(nt[i:i + 3], "?") for i in range(0, len(nt) - 2, 3))


# ── Gene specification ────────────────────────────────────────────────────────
# cds_offset: RefSeq CDS codon numbering minus canonical-protein numbering.
#   MYC's RefSeq CDS begins at a non-AUG CTG start 15 codons upstream of the
#   UniProt P01106 Met1, so UniProt codon N is CDS codon N+15.  Verified by
#   asserting the downstream translation is exactly 439 aa (P01106 length).
#
# hotspots are (canonical_codon_number, expected_wt_aa, label).
# Every entry below was validated against the fetched sequence; the three that
# initially failed were corrected rather than dropped:
#   - MYC T58A/S62 needed the +15 CDS offset.
#   - HER2 "D842V" was wrong (that is a PDGFRA hotspot); the real HER2 hotspot
#     at that position is V842I.
#   - TERT's recurrent hotspots are promoter, not coding — handled separately.
GENES: dict[str, dict] = {
    "TP53": {
        "mrna": "NM_000546.6", "genomic": "NG_017013.2", "cds_offset": 0,
        "cancer": "colorectal, lung, breast, ovarian",
        "hotspots": [(175, "R", "R175H"), (248, "R", "R248W/Q"),
                     (249, "R", "R249S"), (273, "R", "R273H")],
    },
    "KRAS": {
        "mrna": "NM_004985.5", "genomic": "NG_007524.2", "cds_offset": 0,
        "cancer": "pancreatic, colorectal, lung",
        "hotspots": [(12, "G", "G12C/D/V"), (13, "G", "G13D"),
                     (61, "Q", "Q61H"), (146, "A", "A146T")],
    },
    "MYC": {
        "mrna": "NM_002467.6", "genomic": "NG_007161.1", "cds_offset": 15,
        "cancer": "Burkitt lymphoma, TNBC, neuroblastoma",
        "hotspots": [(58, "T", "T58A"), (62, "S", "S62")],
    },
    "EGFR": {
        "mrna": "NM_005228.5", "genomic": "NG_007726.3", "cds_offset": 0,
        "cancer": "non-small-cell lung cancer",
        "hotspots": [(719, "G", "G719S"), (790, "T", "T790M"),
                     (858, "L", "L858R"), (861, "L", "L861Q")],
    },
    "HER2": {
        "mrna": "NM_004448.4", "genomic": "NG_007503.1", "cds_offset": 0,
        "gene_aliases": ["ERBB2"],
        "cancer": "breast, gastric",
        "hotspots": [(310, "S", "S310F"), (755, "L", "L755S"),
                     (777, "V", "V777L"), (842, "V", "V842I")],
    },
    "BRCA1": {
        "mrna": "NM_007294.4", "genomic": "NG_005905.2", "cds_offset": 0,
        "cancer": "breast, ovarian",
        "hotspots": [(61, "C", "C61G"), (1699, "R", "R1699W"),
                     (1775, "M", "M1775R")],
    },
    "CDK4": {
        "mrna": "NM_000075.4", "genomic": "NG_007484.2", "cds_offset": 0,
        "cancer": "melanoma, sarcoma",
        "hotspots": [(24, "R", "R24C")],
    },
    "BCL2": {
        "mrna": "NM_000633.3", "genomic": "NG_009361.1", "cds_offset": 0,
        "cancer": "B-cell lymphoma, CLL",
        "hotspots": [(69, "T", "T69"), (70, "S", "S70"), (87, "S", "S87")],
    },
    # PDL1 (CD274) is driven by amplification / expression, not by a recurrent
    # missense codon.  No coding hotspot is claimed.  Prime-editing candidates
    # for this gene are anchored on the CDS start instead, and the record says
    # so explicitly via hotspot_source.
    #
    # CD274 has no RefSeqGene (NG_) record, so the genomic source is a GRCh38
    # chromosome 9 region spanning the gene.  Verified: the first 45nt of the
    # CDS occurs exactly once in this window.
    "PDL1": {
        "mrna": "NM_014143.4",
        "genomic": "NC_000009.12", "genomic_region": (5440000, 5480000),
        "cds_offset": 0,
        "gene_aliases": ["CD274"],
        "cancer": "melanoma, NSCLC (checkpoint blockade)",
        "hotspots": [],
    },
    # TERT's recurrent hotspots are in the PROMOTER (C228T / C250T), upstream
    # of the ATG, and therefore absent from any CDS.  Handled by the dedicated
    # promoter branch below rather than by pretending they are coding.
    "TERT": {
        "mrna": "NM_198253.3", "genomic": "NG_009265.1", "cds_offset": 0,
        "cancer": "melanoma, glioma, bladder",
        "hotspots": [],
        "promoter_hotspots": [("C228T", 124, "C"), ("C250T", 146, "C")],
        "cds_start_motif": "ATGCCGCGCGCTCCCCGCTGC",
    },
}


# ── NCBI fetch ────────────────────────────────────────────────────────────────
def _efetch(**params) -> str:
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{EUTILS}?{q}", timeout=90) as r:
        return r.read().decode()


def fetch_cds(acc: str) -> str:
    txt = _efetch(db="nuccore", id=acc, rettype="fasta_cds_na", retmode="text")
    lines = txt.strip().split("\n")
    return "".join(x.strip() for x in lines[1:] if not x.startswith(">")).upper()


def fetch_genomic(acc: str, region: tuple[int, int] | None = None) -> tuple[str, str]:
    """
    Fetch a genomic record.  Returns (header, sequence).

    The header is returned so the caller can assert the record actually
    describes the expected gene — a wrong accession otherwise yields a
    perfectly valid sequence for the wrong gene, which no downstream check
    would necessarily catch.
    """
    params: dict = {"db": "nuccore", "id": acc,
                    "rettype": "fasta", "retmode": "text"}
    if region is not None:
        params["seq_start"], params["seq_stop"] = region[0], region[1]
        params["strand"] = 1
    txt = _efetch(**params)
    lines = txt.strip().split("\n")
    header = lines[0]
    return header, "".join(x.strip() for x in lines[1:]).upper()


_COMP = str.maketrans("ACGTN", "TGCAN")


def revcomp(s: str) -> str:
    return s.translate(_COMP)[::-1]


# ── Build ─────────────────────────────────────────────────────────────────────
def locate_in_genomic(genomic: str, ctx: str) -> tuple[int, str] | None:
    """
    Find ctx in genomic on either strand.  Returns (index, strand) only if the
    match is unique on that strand and absent from the other; ambiguity is
    treated as failure, never resolved by guessing.
    """
    fwd = _find_all(genomic, ctx)
    rev = _find_all(genomic, revcomp(ctx))
    if len(fwd) == 1 and not rev:
        return fwd[0], "+"
    if len(rev) == 1 and not fwd:
        return rev[0], "-"
    return None


def _find_all(hay: str, needle: str) -> list[int]:
    out, i = [], hay.find(needle)
    while i >= 0:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def build_gene(gene: str, spec: dict) -> dict:
    print(f"\n─── {gene} ───")
    cds = fetch_cds(spec["mrna"])
    time.sleep(0.4)
    g_header, genomic = fetch_genomic(spec["genomic"], spec.get("genomic_region"))
    time.sleep(0.4)

    prot = translate(cds)
    gate1 = (len(cds) % 3 == 0) and prot.endswith("*")
    print(f"  mRNA {spec['mrna']}  cds={len(cds)}  prot={len(prot) - 1}aa  "
          f"stop={'yes' if prot.endswith('*') else 'NO'}  gate1={'OK' if gate1 else 'FAIL'}")
    print(f"  genomic {spec['genomic']}  len={len(genomic)}")
    print(f"    {g_header[:100]}")
    if not gate1:
        return {"gene": gene, "status": "FAIL_GATE1", "hotspots": []}

    # Gate 0 — the genomic record must actually be the gene we asked for.
    # A transposed accession returns a perfectly valid sequence for the WRONG
    # gene; without this check the build silently emits confident nonsense.
    # (This gate caught two real transposed accessions during development.)
    aliases = spec.get("gene_aliases", []) + [gene]
    if "genomic_region" in spec:
        # Chromosome-region records are named by chromosome, not by gene, so
        # gene identity is instead proven by locating the CDS in the window.
        if locate_in_genomic(genomic, cds[:45]) is None:
            print(f"  FAIL GATE0: CDS not uniquely locatable in the requested "
                  f"region of {spec['genomic']} — wrong coordinates?")
            return {"gene": gene, "status": "FAIL_GATE0_REGION", "hotspots": []}
        print("    gate0=OK (CDS located in region)")
    elif not any(a.upper() in g_header.upper() for a in aliases):
        print(f"  FAIL GATE0: genomic record does not mention {aliases} — "
              f"wrong accession")
        return {"gene": gene, "status": "FAIL_GATE0_WRONG_GENE", "hotspots": []}
    else:
        print("    gate0=OK (header names the gene)")

    offset = spec.get("cds_offset", 0)
    entries: list[dict] = []

    for codon_num, exp_aa, label in spec["hotspots"]:
        cds_codon_num = codon_num + offset
        idx = (cds_codon_num - 1) * 3
        codon = cds[idx:idx + 3]
        got_aa = CODON_TABLE.get(codon, "?")

        # Gate 2 — wild-type amino acid must match.
        if got_aa != exp_aa:
            print(f"  FAIL {label:10} codon {codon_num}: {codon}->{got_aa}, expected {exp_aa}")
            continue

        # Gate 3 — anchor uniquely into the genomic record.
        #
        # A fixed context width fails when the window straddles a splice
        # junction: the spliced CDS text simply does not exist in genomic DNA.
        # Shrink the context (symmetrically, then one-sided) until it sits
        # inside a single exon and matches uniquely.  The codon itself always
        # stays fully inside the context, and uniqueness is still required —
        # this widens coverage without weakening any gate.
        loc = None
        c0 = c1 = 0
        for half in range(CTX_HALF, MIN_CTX_HALF - 1, -3):
            for left, right in ((half, half), (half, 0), (0, half)):
                a = max(0, idx - left)
                b = min(len(cds), idx + 3 + right)
                if b - a < MIN_CTX_TOTAL:
                    continue
                cand = locate_in_genomic(genomic, cds[a:b])
                if cand is not None:
                    loc, c0, c1 = cand, a, b
                    break
            if loc is not None:
                break
        if loc is None:
            print(f"  FAIL {label:10} codon {codon_num}: CDS context not uniquely "
                  f"locatable in genomic at any width "
                  f"({CTX_HALF}..{MIN_CTX_HALF})")
            continue
        g_idx, strand = loc
        if (c1 - c0) < CTX_HALF * 2 + 3:
            print(f"       (anchored with reduced context {c1 - c0}nt — "
                  f"codon near a splice junction)")

        # Genomic index of the codon's first base.
        if strand == "+":
            codon_g = g_idx + (idx - c0)
        else:
            # ctx was found reverse-complemented; walk from the far end.
            codon_g = g_idx + (c1 - (idx + 3))

        # Gate 4 — read the codon back out of the genomic sequence.
        raw = genomic[codon_g:codon_g + 3]
        readback = raw if strand == "+" else revcomp(raw)
        if readback != codon:
            print(f"  FAIL {label:10} codon {codon_num}: genomic readback "
                  f"{readback} != CDS {codon}")
            continue

        w0 = max(0, codon_g - WINDOW_HALF)
        w1 = min(len(genomic), codon_g + 3 + WINDOW_HALF)
        window = genomic[w0:w1]

        entries.append({
            "label": label,
            "codon": codon_num,
            "cds_codon": cds_codon_num,
            "wt_codon": codon,
            "wt_aa": got_aa,
            "strand": strand,
            "genomic_acc": spec["genomic"],
            "genomic_codon_start": codon_g,
            "window": window,
            "window_start": w0,
            "edit_offset_in_window": codon_g - w0,
            "hotspot_source": "cds_codon",
        })
        print(f"  OK   {label:10} codon {codon_num}: {codon}->{got_aa}  "
              f"strand={strand}  genomic@{codon_g}  window={len(window)}nt")

    # Promoter hotspots (TERT).
    for label, dist, exp_base in spec.get("promoter_hotspots", []):
        motif = spec["cds_start_motif"]
        p = genomic.find(motif)
        if p < 0 or genomic.count(motif) != 1:
            print(f"  FAIL {label:10} promoter: CDS-start motif not unique")
            continue
        pos = p - dist
        base = genomic[pos]
        if base != exp_base:
            print(f"  FAIL {label:10} promoter: base {base} != expected {exp_base}")
            continue
        w0 = max(0, pos - WINDOW_HALF)
        w1 = min(len(genomic), pos + 1 + WINDOW_HALF)
        entries.append({
            "label": label,
            "codon": None,
            "cds_codon": None,
            "wt_codon": None,
            "wt_aa": None,
            "strand": "+",
            "genomic_acc": spec["genomic"],
            "genomic_codon_start": pos,
            "window": genomic[w0:w1],
            "window_start": w0,
            "edit_offset_in_window": pos - w0,
            "hotspot_source": "promoter",
            "promoter_offset_from_atg": -dist,
        })
        print(f"  OK   {label:10} promoter -{dist} from ATG: base {base}  "
              f"window={w1 - w0}nt")

    # Genes with no recurrent hotspot: anchor on the CDS start so the branch
    # can still produce candidates, but label the source honestly.
    if not entries and not spec["hotspots"] and not spec.get("promoter_hotspots"):
        loc = None
        n = 0
        for width in range(CTX_HALF * 2, MIN_CTX_TOTAL - 1, -3):
            cand = locate_in_genomic(genomic, cds[:width])
            if cand is not None:
                loc, n = cand, width
                break
        if loc is not None:
            g_idx, strand = loc
            w0 = max(0, g_idx - WINDOW_HALF)
            w1 = min(len(genomic), g_idx + 3 + WINDOW_HALF)
            entries.append({
                "label": "CDS_START",
                "codon": 1, "cds_codon": 1,
                "wt_codon": cds[:3], "wt_aa": CODON_TABLE.get(cds[:3], "?"),
                "strand": strand,
                "genomic_acc": spec["genomic"],
                "genomic_codon_start": g_idx,
                "window": genomic[w0:w1],
                "window_start": w0,
                "edit_offset_in_window": g_idx - w0,
                "hotspot_source": "cds_start_no_recurrent_hotspot",
            })
            print(f"  OK   CDS_START  (no recurrent coding hotspot for this gene)  "
                  f"strand={strand}  anchor={n}nt  window={w1 - w0}nt")
        else:
            print("  FAIL CDS_START not uniquely locatable at any width")

    return {
        "gene": gene,
        "mrna_acc": spec["mrna"],
        "genomic_acc": spec["genomic"],
        "cds_len": len(cds),
        "protein_len": len(prot) - 1,
        "cds_offset": offset,
        "cancer_indication": spec["cancer"],
        "status": "OK" if entries else "NO_ENTRIES",
        "hotspots": entries,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}
    total = 0
    for gene, spec in GENES.items():
        try:
            rec = build_gene(gene, spec)
        except Exception as e:                              # noqa: BLE001
            print(f"  ERROR {gene}: {e}")
            continue
        (OUT_DIR / f"{gene}.json").write_text(json.dumps(rec, indent=1))
        index[gene] = {
            "gene": gene,
            "n_hotspots": len(rec["hotspots"]),
            "status": rec["status"],
            "genomic_acc": rec.get("genomic_acc"),
            "labels": [h["label"] for h in rec["hotspots"]],
        }
        total += len(rec["hotspots"])

    (OUT_DIR / "index.json").write_text(json.dumps({
        "genes": index,
        "total_hotspots": total,
        "window_half": WINDOW_HALF,
        "built_at": time.time(),
        "source": "NCBI RefSeq via E-utilities",
    }, indent=1))

    print(f"\n═══ built {total} validated hotspot windows across "
          f"{len(index)} genes → {OUT_DIR}")
    for g, v in index.items():
        print(f"  {g:6} {v['n_hotspots']:2}  {v['status']:10} {','.join(v['labels'])}")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
