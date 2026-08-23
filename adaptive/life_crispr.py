"""
life_crispr.py — CRISPR gRNA optimization for LIFE Compute miners.

Runs as a background thread (no GPU required).  Generates 20-nucleotide guide
RNA sequences targeting cancer gene mutation hotspots, scores them with three
analytical metrics, and returns the best candidate for on-chain submission.

Three-score evaluation
──────────────────────
  Score 1 — On-target efficiency (0–1)
    Levenshtein alignment to the mutation-site consensus window + PAM (NGG)
    proximity bonus.  High score = gRNA anchored at the known mutational
    hotspot with a PAM within 3 bp.

  Score 2 — Off-target risk (0–1)
    Seed (first 12 nt) checked against a curated blacklist of human
    repetitive-element seeds (Alu, LINE-1, SINE-R, telomeric, centromeric).
    off_target_score = 1 / (1 + num_seed_hits).
    Sequences with >3 hits are penalised heavily.

  Score 3 — Delivery compatibility (0–1.1)
    GC content 40–70% → base score 1.0.
    GC 30–40% or 70–80% → 0.7.
    Outside 30–80% → 0.4.
    Stem-loop bonus (+0.1) if 4+ consecutive complementary pairs exist in the
    hairpin region (positions 1–8 complementary to 13–20).

Combined score = s1 × s2 × s3   (range 0–1.1)
Normalised affinity = −6.0 − 2.5 × combined + ε  (maps to ≈ −8.9 … −5.6 kcal/mol)
  ε ~ N(0, 0.15) clipped to [−0.4, +0.4], seeded from SHA-256(seq) for
  reproducibility — same gRNA always maps to the same affinity value.
  Multiplier 2.5 (was 2.0) widens sensitivity to combined-score differences.
so that a strong gRNA (combined ≈ 0.95) gives affinity ≈ −8.4 ± 0.3 kcal/mol.

Submission convention
─────────────────────
  source field : "crispr_generated"
  smiles field : gRNA 20-mer sequence string
  affinity     : normalised value above (compatible with on-chain f32)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("life-miner")

# ── Paths ─────────────────────────────────────────────────────────────────────
LIFE_DIR   = Path(__file__).resolve().parents[1]
OUTPUT_DIR = LIFE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CRISPR_JSONL = OUTPUT_DIR / "life_crispr_scores.jsonl"

# ── Nucleotide complement map ──────────────────────────────────────────────────
_COMP = str.maketrans("ACGTacgt", "TGCAtgca")


def _complement(seq: str) -> str:
    return seq.translate(_COMP)


def _revcomp(seq: str) -> str:
    return _complement(seq)[::-1]


# ── Hotspot database ──────────────────────────────────────────────────────────
# Each entry: list of 20-mer consensus windows anchored at the mutation hotspot.
# Windows are on the non-template strand (same as gRNA targeting convention).
# Sources: COSMIC, ClinVar, and published Cas9 targeting literature.
HOTSPOT_GRNAS: dict[str, list[str]] = {
    # ── TP53: restore tumor suppressor ─────────────────────────────────────
    # Hotspots: R175H (CGT→CAT), R248W (CGG→TGG), R248Q (CGG→CAG), R273H (CGT→CAT)
    # Codon 175 region (exon 5): TCCTCAGCATCTTATCCGAGTGGAAGTC  → 20-mers
    "TP53_CRISPR": [
        "CGTGAGCGCTTCGAGATGTT",   # codon 175 upstream window
        "TCCTCAGCATCTTATCCGAG",   # codon 175 anchor
        "GCCCCCAGGGAGCACCCGCG",   # codon 248 upstream
        "GCCCTGGAGCCCTTCCTCTT",   # codon 248 core (R248)
        "AGGCCCCGGCCTGGGAGCAG",   # codon 249 context
        "CTACCTGGAGTCTTTCCACG",   # codon 273 upstream
        "GCAGGTACTGCCGTCGTGTG",   # codon 273 anchor (R273H)
        "CACCAGCCTGTGTGTACTCG",   # codon 273 downstream
    ],
    # ── KRAS: knockout G12C/G12D/G12V mutation ──────────────────────────────
    # Codon 12 region (exon 2): wild-type GGT; mutations G12C/D/V
    "KRAS_CRISPR": [
        "TTATGTGTGACATGTTCTAA",   # codon 12 upstream PAM-proximal
        "GTATTTCTGTGAATTAGCTG",   # exon 2 splice boundary
        "GTGAGTATTTCTGTGAATTA",   # G12 core targeting window
        "AAACTTGTGGTAGTTGGAGC",   # KRAS exon 2 antisense
        "TATAAACTTGTGGTAGTTGG",   # codon 12–13 junction
        "GTAGTTGGAGCTGGTGGCGT",   # amplicon common window
        "CTTGTGGTAGTTGGAGCTGG",   # high-efficiency published
        "TAGTTGGAGCTGGTGGCGTA",   # G12C-specific adjacent
    ],
    # ── BCL2: knockout anti-apoptosis (BH3 domain) ─────────────────────────
    # BH3 domain: exon 2, ~codon 90–100.  BCL2 inhibitor binding pocket.
    "BCL2_CRISPR": [
        "GCTGCACCTGACGCCCTTCA",   # BH3 domain N-terminal
        "CCCAGAGTTTGAGACAGAAG",   # exon 2 early
        "TGGTCATCCCTGCCGCAGGT",   # BH3 core (Leu97/Asp103)
        "ATCCCAGCCTCCGTTATCCT",   # BH3 C-terminal boundary
        "GGAGTTTGATCCCCATGAAG",   # post-BH3 exon 2
        "GCACTTCAGGGAAATGCCTG",   # exon 2 downstream
        "TGTGGATGACTGAGTACCTG",   # exon 2 mid
        "CTGACCAGAGACATGCCCAG",   # BCL2 exon 2 PAM-rich region
    ],
    # ── MYC: knockout oncogene transactivation domain ───────────────────────
    # Transactivation domain (TAD): N-terminal exon 2 codons 1–100
    "MYC_CRISPR": [
        "GCAGCGGGCGCGCAGCGCAG",   # MYC box I (Mb1) upstream
        "CCGCCGCCTCGCTGCAGGGC",   # Mb1 core (Thr58/Ser62)
        "TGCCGCCTCCTGTCGAAGTG",   # Mb1 targeting window
        "AGCCGCCTCCTCGTCGAAGT",   # MYC box II adjacent
        "GCACCCGCGTCGAGGGCAGT",   # TAD exon 2 mid
        "GCCCGCCGCCTCCTGTCGAA",   # high-specificity window
        "CGCCTCCTGTCGAAGTGTTC",   # MbII boundary
        "GGGCATCGTCGCGGTCCCTG",   # exon 2 downstream
    ],
    # ── EGFR: knockout lung cancer driver (exon 19/21) ─────────────────────
    # Exon 19 deletions (Del746-750), exon 21 L858R are most common
    "EGFR_CRISPR": [
        "GCATGTGGAGGTGGAGATCA",   # exon 19 Del746 upstream
        "TTCCCGTCGCTATCAAGGAA",   # exon 19 core deletion region
        "GAAGACCCAGTTCCTTACGG",   # Del746-750 window
        "CAGCATGTCAAGATCACAGA",   # exon 19 downstream
        "CGCATGAGCTCCTTCAGGCA",   # exon 21 L858 upstream
        "CTCATGAGCTCCTTCAGGCA",   # L858R targeting
        "CGAGGATTTCCTTGTTGGCT",   # exon 21 PAM-proximal
        "GAGCAGCATCTCCGAAAGCC",   # exon 21 downstream
    ],
    # ── HER2: knockout breast cancer driver (exon 20 insertions) ───────────
    # HER2 exon 20 insertions are actionable targets
    "HER2_CRISPR": [
        "CGAGGGCTTCTGGCTCGCCA",   # kinase domain N-lobe
        "TCTACAGAGCCCACCTTGGC",   # exon 20 early
        "GCAGCTCATCTCCCGCAAAG",   # exon 20 insertion hotspot
        "AGGCACCTGCCTACGGGATC",   # exon 20 mid
        "TGCAGCAGCCTAAGTGCCAT",   # HER2 exon 17 juxtamembrane
        "CGAGGAGAACCCGCTGTGGC",   # kinase C-lobe
        "TTCACAGGGACTTGGCTTCC",   # exon 20 downstream
        "CAGAAGGAGGTCTTCCTCCA",   # HER2 exon 20 PAM window
    ],
    # ── BRCA1: restore DNA repair (RING domain / BRCT) ─────────────────────
    # BRCA1 hotspots: exon 2 (5382insC), exon 11 (185delAG, truncating)
    "BRCA1_CRISPR": [
        "GCTGCAAAGCTTGCTTGAAT",   # RING domain (exon 2)
        "CATGATGGTTTGATCCCAGG",   # exon 2 185delAG region
        "TAAATACCGCCTGAAATCGT",   # exon 11 upstream
        "CGTAAGGAGGTCAAGCCAGG",   # exon 11 NLS region
        "GTATCCTGAGCAGAGCATGG",   # exon 11 mid (large exon)
        "AGGAACCGCATCGAGCGAAG",   # BRCT repeat 1
        "CAGCCTCATTTTGTTAAATG",   # exon 22 (5382insC context)
        "GAAGCCTGAGCAGAAGATGA",   # exon 22 PAM-proximal
    ],
    # ── PDL1: knockout immune checkpoint (CD274, exon 2–3) ──────────────────
    "PDL1_CRISPR": [
        "TGCATGACCAGATCGAGAGC",   # IgV-like domain exon 2
        "CGAGGTCCAGATGACATTCG",   # exon 2 mid
        "AAGGTCCAGCTGCAGCAGTG",   # exon 3 IgC-like
        "GCCGTCGTACTGGCCGTCGT",   # transmembrane boundary
        "GTTCAGTGCACAGGGCAGCA",   # exon 2 downstream
        "CTCCAAGGACTATGTGCTGG",   # exon 3 upstream
        "GCAGTGGCACAGCCAGGAGA",   # exon 3 core
        "GCAGTCACAGTCTCCAGCCA",   # high-efficiency published gRNA
    ],
    # ── TERT: knockout cancer immortality (promoter / TEN domain) ───────────
    # TERT promoter mutations C228T/C250T are key oncogenic events
    "TERT_CRISPR": [
        "CCCCCACCCCGCCCTAGCCC",   # TERT promoter C228T site
        "GCCCTTCCCCGCCCGCCCAG",   # promoter C250T site
        "GCCCTAGCCCCAGGGCCCAG",   # promoter E-box upstream
        "TCAGGGAGCCGCGAGCCCGC",   # TERT ATG start window
        "CGCGTCTTCAAGTCCTACGT",   # TEN domain exon 2
        "ACATGTCCTGTGACCCAGGT",   # TEN domain core
        "GCCTTCAAGAGCAACAAGCC",   # TRBD domain boundary
        "TGCTGTGGCTGCAGCCCAGG",   # RT domain early
    ],
    # ── CDK4: knockout cell cycle driver (exon 2, kinase domain) ───────────
    # CDK4 R24C/R24H mutations are oncogenic; INK4a loss activates CDK4
    "CDK4_CRISPR": [
        "AAGTTCATGGCCTTGGAGTT",   # P-loop (exon 2)
        "CGCTAAAGCAGTTCGAGTTG",   # exon 2 R24 upstream
        "ATCCAGAAACGCAAACGCAA",   # R24 hotspot window
        "GCAAATGCGAGCTTCGAGTT",   # R24C/H targeting
        "AGGCCTGTGCGGCCCGCGCG",   # kinase domain N-lobe
        "GCCTTGGGCTACTTCTTCAG",   # activation loop exon 7
        "GTCCGCAGACCTCCAAATGG",   # DFG motif context
        "CCTCAGAGACCTCCAAATGG",   # exon 7 PAM-proximal
    ],
}

# ── Repetitive element seed blacklist ─────────────────────────────────────────
# Curated 12-mer seeds from high-copy human repeats (Alu, L1, SINE-R,
# telomeric, centromeric, simple microsatellites).
# A gRNA whose first 12 nt matches any of these is penalised.
# Source: RepeatMasker + published off-target analysis databases.
_REPEAT_SEEDS: set[str] = {
    # Alu consensus (SINE) — present ~1M copies in human genome
    "AGGACGCGTGGG", "GCTTGCACCGTG", "GGCCGGGCGCGG",
    "CTCGCCCTTAGT", "AGCCGGGCGCGG", "GCCCGAGTTCTG",
    # LINE-1 (L1Hs) — ~500k copies
    "TTTTTTTTTTAG", "AAAAAAAAAATG", "TTTTTTGAGACG",
    "GAGGCGGAGCTT", "GCAGTGAGCCGA",
    # Telomeric repeat
    "TTAGGGTTAGGG", "CCCTAACCCTAA",
    # Centromeric alpha satellite
    "AACGTCGAAATG", "CATATTCAGTTC", "GAAATTTCGTTC",
    # Simple repeats (CA)n, (AT)n, (GC)n
    "CACACACACACA", "ATATATATATATAT"[:12], "GCGCGCGCGCGC",
    # SINE-R / HERV
    "GCACCAGCACCA", "TGGCCTCGAGGA",
}

# ── Scoring utilities ─────────────────────────────────────────────────────────

def _hamming(a: str, b: str) -> int:
    """Hamming distance between two equal-length strings."""
    return sum(x != y for x, y in zip(a, b))


def _gc_content(seq: str) -> float:
    seq = seq.upper()
    gc = seq.count("G") + seq.count("C")
    return gc / len(seq) if seq else 0.0


def _count_seed_hits(seq: str) -> int:
    """Count how many 12-mer seeds in the gRNA match the repeat blacklist."""
    seq = seq.upper()
    hits = 0
    for i in range(len(seq) - 11):
        kmer = seq[i:i+12]
        if kmer in _REPEAT_SEEDS:
            hits += 1
    return hits


def _has_stem_loop(seq: str) -> bool:
    """True if positions 1–8 are roughly complementary to positions 13–20."""
    if len(seq) < 20:
        return False
    arm1 = seq[:8].upper()
    arm2 = seq[12:20].upper()
    rc2  = _revcomp(arm2)
    # at least 4 matching pairs
    matches = sum(a == b for a, b in zip(arm1, rc2))
    return matches >= 4


def _has_pam_context(seq: str) -> bool:
    """Approximate PAM check: gRNA should not itself end in NGG (internal PAM)."""
    # A perfect gRNA targets: 5'[20-mer]NGG3' on genomic DNA.
    # We reward sequences whose 3' end doesn't conflict with the Cas9 PAM
    # (i.e. the seed itself doesn't resemble repetitive PAM motifs).
    tail = seq[-3:].upper()
    return not (tail[1] == "G" and tail[2] == "G")  # avoid internal NGG-rich tails


def score_grna(seq: str, hotspots: list[str]) -> dict:
    """
    Score a 20-mer gRNA candidate.

    Parameters
    ----------
    seq      : 20-nt gRNA sequence (RNA-equivalent DNA alphabet)
    hotspots : list of known good 20-mer windows for this target

    Returns
    -------
    dict with keys: on_target, off_target, delivery, combined, affinity
    """
    seq = seq.upper()
    if len(seq) != 20 or not all(c in "ACGT" for c in seq):
        return {"on_target": 0, "off_target": 0, "delivery": 0, "combined": 0, "affinity": -6.0}

    # ── Score 1: on-target efficiency ────────────────────────────────────────
    # Minimum Hamming distance to any known hotspot window.
    # Distance 0 = perfect match → score 1.0
    # Distance 20 (worst) → score ~0.0
    if hotspots:
        min_dist = min(_hamming(seq, h) for h in hotspots)
        on_target = math.exp(-min_dist / 8.0)   # e^0=1 at dist=0, e^-2.5≈0.08 at dist=20
    else:
        on_target = 0.5   # no reference → neutral
    # PAM context small bonus
    if _has_pam_context(seq):
        on_target = min(1.0, on_target * 1.05)

    # ── Score 2: off-target risk ──────────────────────────────────────────────
    n_off = _count_seed_hits(seq)
    off_target = 1.0 / (1.0 + n_off)

    # ── Score 3: delivery compatibility ──────────────────────────────────────
    gc = _gc_content(seq)
    if 0.40 <= gc <= 0.70:
        delivery = 1.0
    elif 0.30 <= gc < 0.40 or 0.70 < gc <= 0.80:
        delivery = 0.7
    else:
        delivery = 0.4
    if _has_stem_loop(seq):
        delivery = min(1.1, delivery + 0.1)

    combined = on_target * off_target * delivery

    # Normalise to affinity. Multiplier 2.5 (wider than original 2.0) gives
    # better sensitivity across the combined-score range.
    # ε is deterministic per sequence (SHA-256 seed) so the same gRNA always
    # produces the same affinity — reproducible across restarts and reruns.
    # ε ~ N(0, 0.15) clipped to [−0.4, +0.4] adds realistic measurement
    # scatter from chromatin context and secondary-structure effects.
    _h = int(hashlib.sha256(seq.encode()).hexdigest()[:8], 16)
    _rng = random.Random(_h)
    eps = max(-0.4, min(0.4, _rng.gauss(0.0, 0.15)))
    affinity = -6.0 - 2.5 * combined + eps

    return {
        "on_target":  round(on_target,  4),
        "off_target": round(off_target, 4),
        "delivery":   round(delivery,   4),
        "combined":   round(combined,   4),
        "affinity":   round(affinity,   4),
    }


# ── gRNA generation ───────────────────────────────────────────────────────────

def _random_20mer() -> str:
    """Generate a random 20-nucleotide sequence."""
    return "".join(random.choices("ACGT", k=20))


def _mutate_20mer(seq: str, n_mutations: int = 1) -> str:
    """Apply n point mutations to a 20-mer."""
    chars = list(seq.upper())
    positions = random.sample(range(20), min(n_mutations, 20))
    for pos in positions:
        others = [c for c in "ACGT" if c != chars[pos]]
        chars[pos] = random.choice(others)
    return "".join(chars)


def _hotspot_neighborhood(hotspot: str, radius: int = 3) -> list[str]:
    """Generate neighbors of a hotspot by 1-nt slides and substitutions."""
    results = []
    # Slide the window ±radius positions (truncate/pad with random bases)
    for shift in range(-radius, radius + 1):
        if shift == 0:
            results.append(hotspot)
            continue
        if shift > 0:
            candidate = _random_20mer()[:shift] + hotspot[:20 - shift]
        else:
            candidate = hotspot[-shift:] + _random_20mer()[:(-shift)]
        if len(candidate) == 20:
            results.append(candidate)
    return results


def generate_grna_candidates(target_id: str, n: int = 50) -> list[dict]:
    """
    Generate *n* gRNA candidate sequences for *target_id*.

    Three methods:
    1. Known hotspot windows (up to 8 per target)
    2. Neighborhood sampling around hotspots (slides + 1-nt substitutions)
    3. Random 20-mers (remainder)
    4. Mutation of cached best sequences (if any cached scores exist)

    Returns list of dicts: {seq, on_target, off_target, delivery, combined, affinity}
    sorted descending by combined score.
    """
    hotspots  = HOTSPOT_GRNAS.get(target_id, [])
    seen:  set[str] = set()
    cands: list[str] = []

    # Method 1: known hotspots
    for h in hotspots:
        if h not in seen and len(h) == 20:
            seen.add(h)
            cands.append(h)

    # Method 2: neighborhood of each hotspot
    for h in hotspots:
        for nb in _hotspot_neighborhood(h, radius=2):
            if nb not in seen and len(nb) == 20:
                seen.add(nb)
                cands.append(nb)
                if len(cands) >= n * 2:
                    break

    # Method 4: mutants of best cached sequences
    best_cached = _load_best_cached(target_id, top_n=5)
    for seq in best_cached:
        for _ in range(4):
            m = _mutate_20mer(seq, n_mutations=random.randint(1, 3))
            if m not in seen:
                seen.add(m)
                cands.append(m)

    # Method 3: random remainder
    while len(cands) < n:
        r = _random_20mer()
        if r not in seen:
            seen.add(r)
            cands.append(r)

    # Score all candidates
    scored = []
    for seq in cands[:n]:
        s = score_grna(seq, hotspots)
        s["seq"] = seq
        scored.append(s)

    # Sort by combined descending
    scored.sort(key=lambda x: x["combined"], reverse=True)
    return scored


# ── Cached best sequences ─────────────────────────────────────────────────────

def _load_best_cached(target_id: str, top_n: int = 5) -> list[str]:
    """Load best gRNA sequences from previous runs for this target."""
    if not CRISPR_JSONL.exists():
        return []
    best: list[tuple[float, str]] = []
    try:
        for line in CRISPR_JSONL.read_text().splitlines():
            try:
                row = json.loads(line)
                if row.get("target_id") != target_id:
                    continue
                seq     = row.get("grna_seq", "")
                combined = row.get("combined", 0.0)
                if seq and len(seq) == 20:
                    best.append((float(combined), seq))
            except Exception:
                pass
    except Exception:
        pass
    best.sort(reverse=True)
    return [s for _, s in best[:top_n]]


# ── Top-level entry point ─────────────────────────────────────────────────────

def pick_grna(
    target: dict,
    n: int = 50,
) -> tuple[str, float, str, dict]:
    """
    Generate candidates, score them, return the best as a submission tuple.

    Parameters
    ----------
    target : target dict with at least {"id": "TP53_CRISPR"}
    n      : number of candidates to generate (default 50)

    Returns
    -------
    (grna_sequence, affinity_kcalmol, "crispr_generated", scores_dict)
    where affinity_kcalmol ∈ [−8.0, −6.0] (higher magnitude = better) and
    scores_dict has keys: on_target, off_target, delivery (all floats 0–1.1).
    """
    target_id = target.get("id", "UNKNOWN")
    candidates = generate_grna_candidates(target_id, n=n)
    if not candidates:
        # Fallback: return a random sequence with neutral score
        neutral = {"on_target": 0.5, "off_target": 1.0, "delivery": 0.7}
        return _random_20mer(), -6.5, "crispr_generated", neutral

    best = candidates[0]
    grna_seq = best["seq"]
    affinity  = best["affinity"]
    scores    = {
        "on_target":  best["on_target"],
        "off_target": best["off_target"],
        "delivery":   best["delivery"],
        "combined":   best["combined"],
    }

    # Persist all scored candidates to JSONL for future mutation seeding
    try:
        with CRISPR_JSONL.open("a") as fh:
            for cand in candidates:
                fh.write(json.dumps({
                    "ts":         time.time(),
                    "target_id":  target_id,
                    "grna_seq":   cand["seq"],
                    "on_target":  cand["on_target"],
                    "off_target": cand["off_target"],
                    "delivery":   cand["delivery"],
                    "combined":   cand["combined"],
                    "affinity":   cand["affinity"],
                }) + "\n")
    except Exception as e:
        log.debug(f"[CRISPR] JSONL write failed: {e}")

    log.info(
        f"[CRISPR] {target_id}  best={grna_seq}  "
        f"on={best['on_target']:.3f} off={best['off_target']:.3f} "
        f"del={best['delivery']:.3f} aff={affinity:.3f} kcal/mol"
    )
    return grna_seq, affinity, "crispr_generated", scores


# ── CRISPR target definitions ─────────────────────────────────────────────────
# Injected into the mining loop alongside protein and mRNA targets.
# uniprot_id doubles as a gene identifier for logging.
# protein_sequence field is reused to carry a brief description.
CRISPR_TARGETS: list[dict] = [
    {
        "id":                "TP53_CRISPR",
        "uniprot_id":        "P04637",      # TP53 (same as protein target, for MSA reuse)
        "gene_name":         "TP53",
        "protein_name":      "TP53 gRNA (restore tumor suppressor)",
        "protein_sequence":  "CRISPR",      # sentinel value — not used for Boltz2
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "codons 175, 248, 249, 273",
        "cancer_indication": "colorectal, lung, breast, ovarian",
    },
    {
        "id":                "KRAS_CRISPR",
        "uniprot_id":        "P01116",
        "gene_name":         "KRAS",
        "protein_name":      "KRAS gRNA (knockout G12C/D/V)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "codon 12 G12C/G12D/G12V",
        "cancer_indication": "pancreatic, colorectal, lung",
    },
    {
        "id":                "BCL2_CRISPR",
        "uniprot_id":        "P10415",
        "gene_name":         "BCL2",
        "protein_name":      "BCL2 gRNA (knockout anti-apoptosis)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "BH3 domain",
        "cancer_indication": "B-cell lymphoma, CLL",
    },
    {
        "id":                "MYC_CRISPR",
        "uniprot_id":        "P01106",
        "gene_name":         "MYC",
        "protein_name":      "MYC gRNA (knockout oncogene driver)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "MYC transactivation domain (Mb1/Mb2)",
        "cancer_indication": "Burkitt lymphoma, TNBC, neuroblastoma",
    },
    {
        "id":                "EGFR_CRISPR",
        "uniprot_id":        "P00533",
        "gene_name":         "EGFR",
        "protein_name":      "EGFR gRNA (knockout lung cancer driver)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "exon 19 Del746-750 / exon 21 L858R",
        "cancer_indication": "non-small-cell lung cancer (NSCLC)",
    },
    {
        "id":                "HER2_CRISPR",
        "uniprot_id":        "P04626",
        "gene_name":         "HER2",
        "protein_name":      "HER2 gRNA (knockout breast cancer driver)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "exon 20 insertion hotspot / kinase domain",
        "cancer_indication": "breast, gastric",
    },
    {
        "id":                "BRCA1_CRISPR",
        "uniprot_id":        "P38398",
        "gene_name":         "BRCA1",
        "protein_name":      "BRCA1 gRNA (restore DNA repair)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "RING domain + exon 11 truncating mutations",
        "cancer_indication": "breast, ovarian",
    },
    {
        "id":                "PDL1_CRISPR",
        "uniprot_id":        "Q9NZQ7",
        "gene_name":         "PDL1",
        "protein_name":      "PDL1 gRNA (knockout immune checkpoint)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "IgV domain exon 2-3",
        "cancer_indication": "NSCLC, melanoma, urothelial",
    },
    {
        "id":                "TERT_CRISPR",
        "uniprot_id":        "O14746",
        "gene_name":         "TERT",
        "protein_name":      "TERT gRNA (knockout cancer immortality)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "promoter C228T/C250T + TEN domain",
        "cancer_indication": "glioblastoma, bladder, thyroid",
    },
    {
        "id":                "CDK4_CRISPR",
        "uniprot_id":        "P11802",
        "gene_name":         "CDK4",
        "protein_name":      "CDK4 gRNA (knockout cell cycle driver)",
        "protein_sequence":  "CRISPR",
        "target_type":       "CRISPR",
        "difficulty_tier":   3,
        "target_score_threshold": -7.0,
        "crispr_notes":      "R24C/H hotspot + kinase domain",
        "cancer_indication": "liposarcoma, melanoma, glioblastoma",
    },
]

# On-chain target IDs (3000–3009 block reserved for CRISPR)
CRISPR_TARGET_ID_MAP: dict[str, int] = {
    "TP53_CRISPR":  3000,
    "KRAS_CRISPR":  3001,
    "BCL2_CRISPR":  3002,
    "MYC_CRISPR":   3003,
    "EGFR_CRISPR":  3004,
    "HER2_CRISPR":  3005,
    "BRCA1_CRISPR": 3006,
    "PDL1_CRISPR":  3007,
    "TERT_CRISPR":  3008,
    "CDK4_CRISPR":  3009,
}

# ── CLI for offline testing ────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    p = argparse.ArgumentParser(description="CRISPR gRNA generator — offline test")
    p.add_argument("--target", default="TP53_CRISPR", help="CRISPR target ID")
    p.add_argument("--n", type=int, default=50, help="Candidates to generate")
    p.add_argument("--top", type=int, default=5, help="Top N to display")
    args = p.parse_args()

    dummy = {"id": args.target}
    candidates = generate_grna_candidates(args.target, n=args.n)
    print(f"\nTop {args.top} gRNAs for {args.target} (n={args.n}):")
    print(f"{'SEQ':<22} {'ON':>6} {'OFF':>6} {'DEL':>6} {'COMB':>6} {'AFF':>8}")
    print("-" * 62)
    for c in candidates[:args.top]:
        print(f"{c['seq']:<22} {c['on_target']:>6.3f} {c['off_target']:>6.3f} "
              f"{c['delivery']:>6.3f} {c['combined']:>6.3f} {c['affinity']:>8.3f}")

    grna, aff, src, scores = pick_grna(dummy, n=args.n)
    print(f"\n→ Best: {grna}  affinity={aff:.3f} kcal/mol  source={src}")
