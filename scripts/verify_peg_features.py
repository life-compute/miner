#!/usr/bin/env python3
"""
Stage 1 verification for adaptive/life_peg.py.

Exercises the extractor against REAL pegRNA candidates designed from the
Stage 0.1 reference data, focusing on TP53's four validated hotspot codons
(175, 248, 249, 273).  Offline, deterministic, no network.

Checks
------
  A. Candidate geometry is real and self-consistent
       - spacer sits immediately 5' of the nick in the window
       - a genuine NGG PAM exists at pam_offset
       - the RTT, reverse-complemented, actually encodes the intended edit
       - the PBS anneals to the sequence 5' of the nick
  B. Vector integrity
       - fixed length N_PEG_FEATURES for every candidate
       - all finite, all plain float, no NaN/inf
  C. Bounds
       - fractions in [0,1]; one-hot sums to exactly 1
  D. Discriminative power
       - features vary across the PBS/RTT design grid (a near-constant
         featuriser is useless downstream)
  E. Independent hand-recomputation
       - GC, Tm, homopolymer, base fractions recomputed from raw sequence by
         formulas written out separately here, then compared
  F. Determinism
       - repeated extraction is bit-identical
  G. Reference linkage
       - hotspot_exact fires at the true hotspot and not at an offset position
  H. Negative controls
       - malformed inputs return None rather than a plausible-looking vector
  I. All three edit types produce valid candidates and vectors
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adaptive.life_peg import (            # noqa: E402
    EDIT_TYPES, NICK_OFFSET_FROM_PAM, N_PEG_FEATURES, PEG_FEATURE_NAMES,
    SPACER_LEN, design_candidates, featurize_candidate, featurize_pegrna,
    feature_dict, get_hotspot, revcomp,
)

TP53_HOTSPOTS = ["R175H", "R248W/Q", "R249S", "R273H"]

fails: list[str] = []
passes = 0


def check(cond: bool, label: str) -> None:
    global passes
    if cond:
        passes += 1
    else:
        fails.append(label)


# ── Independent reimplementations (deliberately not imported) ────────────────
def ind_gc(s: str) -> float:
    return sum(1 for ch in s if ch in "GC") / len(s) if s else 0.0


def ind_tm(s: str) -> float:
    if not s:
        return 0.0
    at = sum(1 for ch in s if ch in "AT")
    gc = sum(1 for ch in s if ch in "GC")
    if len(s) < 14:
        return float(2 * at + 4 * gc)
    return 64.9 + 41.0 * (gc - 16.4) / len(s)


def ind_homopolymer(s: str) -> int:
    if not s:
        return 0
    runs, cur = [], 1
    for i in range(1, len(s)):
        if s[i] == s[i - 1]:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    return max(runs)


def main() -> int:
    print("=" * 74)
    print("STAGE 1 VERIFICATION — pegRNA feature extractor")
    print("=" * 74)
    print(f"N_PEG_FEATURES = {N_PEG_FEATURES}")
    print(f"feature names  = {len(PEG_FEATURE_NAMES)} unique: "
          f"{len(set(PEG_FEATURE_NAMES)) == len(PEG_FEATURE_NAMES)}")
    check(len(set(PEG_FEATURE_NAMES)) == len(PEG_FEATURE_NAMES),
          "duplicate feature names")

    all_vectors: list[list[float]] = []
    total_candidates = 0

    for label in TP53_HOTSPOTS:
        h = get_hotspot("TP53", label)
        if h is None:
            fails.append(f"TP53/{label} missing from Stage 0.1 reference")
            continue

        cands = design_candidates("TP53", label)
        total_candidates += len(cands)
        print(f"\n─── TP53 {label} "
              f"(codon {h['codon']}, wt {h['wt_codon']}) ───")
        print(f"  candidates designed: {len(cands)}")
        check(len(cands) > 0, f"A: TP53/{label} produced no candidates")
        if not cands:
            continue

        window = h["window"]
        edit_off = h["edit_offset_in_window"]
        n_fwd = sum(1 for c in cands if c.strand == "+")
        n_rev = sum(1 for c in cands if c.strand == "-")
        print(f"  strands: +{n_fwd}  -{n_rev}")

        # ── A. geometry on every candidate ──────────────────────────────────
        for c in cands:
            tag = f"TP53/{label} {c.strand} PBS{len(c.pbs)}/RTT{len(c.rtt)}"
            # Validate in the candidate's OWN coordinate frame.
            cw = c.window
            check(cw == (window if c.strand == "+" else revcomp(window)),
                  f"A: {tag} candidate window is not the expected strand view")

            check(cw[c.pam_offset + 1:c.pam_offset + 3] == "GG",
                  f"A: {tag} PAM offset does not point at a real GG")
            # Hardcoded 3 on purpose: comparing against NICK_OFFSET_FROM_PAM
            # would be circular and could not catch a wrong constant.  SpCas9
            # nicks between protospacer positions 17 and 18, i.e. 3nt 5' of
            # the PAM.
            check(c.nick_offset == c.pam_offset - 3,
                  f"A: {tag} nick is not 3nt 5' of the PAM")
            check(NICK_OFFSET_FROM_PAM == 3,
                  "A: NICK_OFFSET_FROM_PAM must be 3 for SpCas9")
            check(c.spacer == cw[c.nick_offset - SPACER_LEN:c.nick_offset],
                  f"A: {tag} spacer is not the real protospacer")
            check(c.pbs == revcomp(cw[c.nick_offset - len(c.pbs):c.nick_offset]),
                  f"A: {tag} PBS does not anneal 5' of the nick")

            # The RTT must genuinely encode the edit.
            templated = revcomp(c.rtt)
            local = c.edit_offset - c.nick_offset
            check(templated[:local] == cw[c.nick_offset:c.edit_offset],
                  f"A: {tag} RTT 5' homology arm does not match genomic")
            check(templated[local:local + len(c.edited_seq)] == c.edited_seq,
                  f"A: {tag} RTT does not carry the intended edit")
            check(c.nick_to_edit > 0, f"A: {tag} edit is not 3' of the nick")
            check(c.edit_to_rtt_end >= 0,
                  f"A: {tag} RTT ends before the edit completes")

            # The wt span the candidate claims must really be there.
            check(cw[c.edit_offset:c.edit_offset + len(c.wt_seq)] == c.wt_seq,
                  f"A: {tag} wt_seq does not match the window at edit_offset")
            check(c.wt_seq != c.edited_seq,
                  f"A: {tag} edited sequence is identical to wild type")

            # Reverse-strand candidates must map back to the SAME genomic
            # edit in original + strand coordinates.  This is the check that
            # would catch an off-by-one in the coordinate flip.
            if c.strand == "-":
                back_off = len(window) - (c.edit_offset + len(c.wt_seq))
                check(back_off == edit_off,
                      f"A: {tag} reverse edit maps to +{back_off}, "
                      f"expected +{edit_off}")
                check(revcomp(c.wt_seq) ==
                      window[edit_off:edit_off + len(c.wt_seq)],
                      f"A: {tag} reverse wt_seq does not revcomp back to the "
                      f"original hotspot")

        # ── B/C. vectors ────────────────────────────────────────────────────
        vecs = []
        for c in cands:
            v = featurize_candidate(c)
            tag = f"TP53/{label} PBS{len(c.pbs)}/RTT{len(c.rtt)}"
            if v is None:
                fails.append(f"B: {tag} featurizer returned None")
                continue
            vecs.append(v)
            check(len(v) == N_PEG_FEATURES, f"B: {tag} wrong vector length")
            check(all(isinstance(x, float) for x in v),
                  f"B: {tag} non-float entries")
            check(all(math.isfinite(x) for x in v),
                  f"B: {tag} NaN/inf present")

            d = feature_dict(v)
            for name in ("spacer_gc", "pbs_gc", "rtt_gc",
                         "spacer_a", "spacer_c", "spacer_g", "spacer_t",
                         "pbs_a", "pbs_c", "pbs_g", "pbs_t",
                         "rtt_a", "rtt_c", "rtt_g", "rtt_t",
                         "ext_self_comp", "spacer_ext_comp", "pbs_gc_clamp"):
                check(0.0 <= d[name] <= 1.0, f"C: {tag} {name}={d[name]} out of [0,1]")
            onehot = d["edit_substitution"] + d["edit_insertion"] + d["edit_deletion"]
            check(abs(onehot - 1.0) < 1e-12, f"C: {tag} edit one-hot sums to {onehot}")

            for seg, seq in (("spacer", c.spacer), ("pbs", c.pbs), ("rtt", c.rtt)):
                s = sum(d[f"{seg}_{b}"] for b in "acgt")
                check(abs(s - 1.0) < 1e-12, f"C: {tag} {seg} base fractions sum to {s}")

        all_vectors.extend(vecs)

        # ── E. hand-recomputation on a representative candidate ─────────────
        c = cands[len(cands) // 2]
        v = featurize_candidate(c)
        assert v is not None
        d = feature_dict(v)
        print(f"  spot-check candidate: PBS={len(c.pbs)} RTT={len(c.rtt)} "
              f"nick_to_edit={c.nick_to_edit} corr={c.correction_length}")
        for seg, seq in (("spacer", c.spacer), ("pbs", c.pbs), ("rtt", c.rtt)):
            check(abs(d[f"{seg}_gc"] - ind_gc(seq)) < 1e-12,
                  f"E: {seg}_gc {d[f'{seg}_gc']} != independent {ind_gc(seq)}")
            check(abs(d[f"{seg}_tm"] * 100.0 - ind_tm(seq)) < 1e-9,
                  f"E: {seg}_tm mismatch")
            check(abs(d[f"{seg}_homopolymer"] * 10.0 - ind_homopolymer(seq)) < 1e-12,
                  f"E: {seg}_homopolymer mismatch")
            check(abs(d[f"{seg}_len"] * 30.0 - len(seq)) < 1e-12,
                  f"E: {seg}_len mismatch")
        check(abs(d["nick_to_edit"] * 30.0 - c.nick_to_edit) < 1e-9,
              "E: nick_to_edit mismatch")
        check(abs(d["rtt_pbs_ratio"] - len(c.rtt) / len(c.pbs)) < 1e-12,
              "E: rtt_pbs_ratio mismatch")

        # ── G. reference linkage ────────────────────────────────────────────
        check(d["hotspot_exact"] == 1.0,
              f"G: TP53/{label} hotspot_exact not set at a true hotspot")
        check(d["hotspot_dist"] == 0.0,
              f"G: TP53/{label} hotspot_dist != 0 at a true hotspot")
        check(d["hotspot_is_coding"] == 1.0,
              f"G: TP53/{label} not flagged as a coding hotspot")
        check(d["pam_density"] > 0.0, f"G: TP53/{label} pam_density is zero")

        off_v = featurize_pegrna(
            c.spacer, c.pbs, c.rtt, c.edit_type,
            gene="TP53", genomic_edit_pos=c.genomic_edit_pos + 5000)
        assert off_v is not None
        off_d = feature_dict(off_v)
        check(off_d["hotspot_exact"] == 0.0,
              f"G: TP53/{label} hotspot_exact fires 5000bp away")
        check(off_d["hotspot_dist"] > 0.0,
              f"G: TP53/{label} hotspot_dist stays 0 at +5000bp")

        # ── F. determinism ──────────────────────────────────────────────────
        check(featurize_candidate(c) == v, f"F: TP53/{label} not deterministic")
        check(design_candidates("TP53", label)[0].to_dict() == cands[0].to_dict(),
              f"F: TP53/{label} candidate design not deterministic")

    # ── D. discriminative power across the whole TP53 set ───────────────────
    print(f"\n─── discriminative power ({len(all_vectors)} vectors) ───")
    check(len(all_vectors) > 20, "D: too few vectors to assess variance")
    varying = 0
    constant_names = []
    for i, name in enumerate(PEG_FEATURE_NAMES):
        col = [v[i] for v in all_vectors]
        spread = max(col) - min(col)
        if spread > 1e-9:
            varying += 1
        else:
            constant_names.append(name)
    print(f"  features varying across candidates: {varying}/{N_PEG_FEATURES}")
    if constant_names:
        print(f"  constant here: {', '.join(constant_names)}")
    check(varying >= N_PEG_FEATURES * 0.6,
          f"D: only {varying}/{N_PEG_FEATURES} features vary — weak extractor")

    distinct = len({tuple(v) for v in all_vectors})
    print(f"  distinct vectors: {distinct}/{len(all_vectors)}")
    check(distinct == len(all_vectors),
          f"D: {len(all_vectors) - distinct} duplicate vectors — "
          f"design grid not separated")

    # ── I. all three edit types ─────────────────────────────────────────────
    print("\n─── edit types ───")
    for et in EDIT_TYPES:
        new_seq = "AT" if et == "insertion" else None
        cs = design_candidates("TP53", "R175H", edit_type=et, new_seq=new_seq)
        ok = sum(1 for c in cs if featurize_candidate(c) is not None)
        print(f"  {et:13} candidates={len(cs):4}  vectors={ok}")
        check(len(cs) > 0, f"I: {et} produced no candidates")
        check(ok == len(cs), f"I: {et} some candidates failed featurisation")
        if cs:
            v = featurize_candidate(cs[0])
            assert v is not None
            d = feature_dict(v)
            check(d[f"edit_{et}"] == 1.0, f"I: {et} one-hot not set")
            check(cs[0].correction_length > 0, f"I: {et} correction_length is 0")

    # ── H. negative controls ────────────────────────────────────────────────
    print("\n─── negative controls (must return None) ───")
    bad = [
        ("empty spacer",      ("", "GGCCTT", "AACCGG", "substitution")),
        ("empty pbs",         ("A" * 20, "", "AACCGG", "substitution")),
        ("empty rtt",         ("A" * 20, "GGCCTT", "", "substitution")),
        ("N in spacer",       ("N" * 20, "GGCCTT", "AACCGG", "substitution")),
        ("lowercase junk",    ("xyz", "GGCCTT", "AACCGG", "substitution")),
        ("bad edit_type",     ("A" * 20, "GGCCTT", "AACCGG", "frameshift")),
        ("RNA U in rtt",      ("A" * 20, "GGCCTT", "AAUCGG", "substitution")),
    ]
    for name, args in bad:
        got = featurize_pegrna(*args)
        print(f"  {name:18} -> {got}")
        check(got is None, f"H: '{name}' returned a vector instead of None")

    # ── coverage across ALL 28 Stage 0.1 hotspots ───────────────────────────
    print("\n─── coverage across all Stage 0.1 hotspots ───")
    from adaptive.life_peg import list_reference_genes, load_gene_reference
    covered = empty = 0
    for g in list_reference_genes():
        rec = load_gene_reference(g)
        assert rec is not None
        for hs in rec["hotspots"]:
            cs = design_candidates(g, hs["label"])
            ok = sum(1 for c in cs if featurize_candidate(c) is not None)
            check(ok == len(cs), f"{g}/{hs['label']}: featurisation failures")
            nf = sum(1 for c in cs if c.strand == "+")
            nr = len(cs) - nf
            if cs:
                covered += 1
            else:
                empty += 1
            flag = "" if cs else "   <-- NO CANDIDATES"
            print(f"  {g:6} {hs['label']:12} n={len(cs):4} "
                  f"(+{nf:3} -{nr:3}){flag}")
    print(f"\n  hotspots with candidates: {covered}/{covered + empty}")
    check(empty == 0,
          f"coverage: {empty} hotspot(s) yielded zero candidates")

    print("\n" + "=" * 74)
    print(f"assertions passed: {passes}")
    print(f"TP53 candidates exercised: {total_candidates}")
    if fails:
        print(f"\nFAILURES ({len(fails)}):")
        for x in fails[:40]:
            print(f"  {x}")
        if len(fails) > 40:
            print(f"  ... and {len(fails) - 40} more")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
