# Affinity Scale Boundary — protein `claimed_affinity` derivation changed

Verified: 2026-09-12 (devnet). Anchored to on-chain slot — see "The boundary" below.

**Read this before making any decision about historical `affinity_kcal_mol` NFT
traits, the `discoveries.json` registry, the public leaderboard, or LIFE-BRAIN
training labels derived from `claimed_affinity`.**

Protein-target affinity values changed derivation mid-life. Values on each side
of the boundary are **not comparable** and were produced by different formulas
with different units. This document records exactly where the boundary is.

---

## The boundary

| Anchor | Value |
|---|---|
| **On-chain slot (authoritative)** | **`497232432`** |
| First correct-scale protein tx | `2kxafQojGUZ48qfujSxzxV9nqVUBpaq8JmGRj5nTRCrDjzen6Bwv3yh9jb83EKYNgkqtRES7JjzduKApr2JyBksx` |
| Its `blockTime` | `1789223558` = **2026-09-12 14:32:38 UTC** |
| Miner restart (fix deployed) | 2026-09-12 **14:31:21 UTC** |
| Epoch at deploy | **9002** |
| Last OLD-formula submission attempt | 2026-09-12 14:22:53 UTC (rejected on-chain) |

**Use the slot, not the timestamp.** `ResultSubmission.submitted_slot` is stored
on-chain and is immutable and monotonic:

```
submitted_slot >= 497232432   ->  NEW derivation (real kcal/mol)
submitted_slot <  497232432   ->  OLD derivation (not an energy; see below)
```

Applies to **protein targets only**. CRISPR and mRNA were never affected — see
"Scope" below.

---

## What changed

### OLD (all protein submissions before slot 497232432)

```python
affinity = round(-boltz_score * 30.0, 3)
```

`boltz_score` is the Nova validator ranking statistic:

```
boltz_score = (affinity_probability_binary - affinity_pred_value) / heavy_atom_count
```

This subtracts `affinity_pred_value` (log10 of IC50 in µM) from
`affinity_probability_binary` (a dimensionless probability in [0,1]). The
subtraction is **dimensionally meaningless** — it mixes a log-concentration with
a probability. The `× -30.0` scale factor was arbitrary.

The result is **not a binding free energy** and has no physical units, despite
having been labelled "kcal/mol" everywhere downstream.

Observed range across 5,592 historical protein rows: **[-4.50, +6.39]**, with
**~24–36% positive** — physically impossible for a binder, and rejected on-chain
by `submit_result.rs:15` (`require!(claimed_affinity < 0.0)`). 802 such rejects
occurred over ~30 days, burning ~4.73 SOL.

### NEW (slot 497232432 onward)

```python
_RT_LN10_KCAL = 1.364247            # 0.001987204259 * 298.15 * ln(10)
affinity = round(_RT_LN10_KCAL * (affinity_pred_value - 6.0), 3)
```

Derived from Boltz-2's documented semantics (`external_tools/boltz/docs/prediction.md`):
`affinity_pred_value` is log10(IC50) with IC50 in **micromolar**. Therefore:

```
IC50_molar = 10 ** (v - 6)
dG         = RT * ln(IC50_molar) = RT*ln(10) * (v - 6)
```

This is a real binding free energy in kcal/mol at 298.15 K.

Estimated range over the same 5,592 historical rows: **[-15.32, -3.46]**,
**0.00% positive**. First 6 live values after deploy: [-8.416, -7.302].

### Worked example — same molecule, both formulas

PDL1 reference compound BMS-202 (a genuine, published PD-L1 inhibitor),
measured on GPU at seed 68:

| Quantity | Value |
|---|---|
| `affinity_probability_binary` | 0.24219919741153717 |
| `affinity_pred_value` | 0.3587188720703125 (IC50 ≈ 2.28 µM) |
| `boltz_score` (unchanged) | -0.0037586991825411397 |
| OLD affinity | **+0.113** — positive, rejected on-chain |
| NEW affinity | **-7.696 kcal/mol** |

---

## Scope — which data is affected

| Path | Formula | Affected? |
|---|---|---|
| **protein** | `-boltz_score * 30` → `RT*ln(10)*(v-6)` | **YES — this boundary** |
| mRNA | `-6.0 - 3.0 * iptm` | No. Bounded formula, already correct. Had its own separate positive-value bug fixed 2026-08-31; all positives stop that same day. |
| CRISPR | `-6.0 - 2.5 * combined` | No. Bounded formula; 15,959 rows, 100% on-chain-valid, never used the broken conversion. |

**`boltz_score` itself is unchanged and remains valid.** It is bit-for-bit
identical before and after (verified: `-0.0037586991825411397` on both sides).
It was always a legitimate *ranking* statistic and still matches the Nova
validator formula exactly (`boltz_wrapper.py::_combine_boltz_scores`). Only its
misuse as a physical energy was fixed. Anything that merely sorts/ranks by
`boltz_score` is unaffected and needs no migration.

---

## Why this matters — the deferred decision

These downstream artifacts recorded the OLD value under a physical-unit label
and **were not corrected**:

| Artifact | Location | Reversible? |
|---|---|---|
| NFT trait `affinity_kcal_mol` | `scripts/mint_discovery_nft.js:140` | **NO — minted immutably to Metaplex** |
| NFT trait `combined_affinity` | `scripts/mint_discovery_nft.js:103` | **NO** |
| `discoveries.json` registry | `scripts/mint_discovery_nft.js:387,391` | Yes (local file) |
| Dashboard `kcal/mol` label | `dashboard/src/App.jsx:1772` | Yes |
| Dashboard `AFF(kcal)` column | `dashboard/src/App.jsx:504,556` | Yes |
| Public "ΔG kcal/mol" claim | `index.html:286` | Yes |
| Public leaderboard `best_score` | `update_results_db.py:93,102,136,148` | Yes |
| LIFE-BRAIN training labels | `adaptive/life_brain_ingest.py:151,169` | Yes (retrainable) |

**The NFT traits cannot be retroactively corrected.** The registry therefore
contains two eras of differently-derived values under one trait name, split at
slot 497232432.

The open decision — deliberately deferred on 2026-09-12, not yet made:

- **Option A:** mark pre-boundary NFTs as legacy/historical (e.g. add a trait or
  publish a mapping keyed on `submitted_slot`).
- **Option B:** leave them as-is with a documented caveat (this file).

Note also `life_brain_ingest.py` ingests `claimed_affinity` from chain as a
supervised training **label**, pooled across miners and across all three
modalities. Measured label means: protein -2.67, CRISPR -8.51, mRNA -7.85 —
a ~3x scale gap that is an artifact of three different transforms, not biology.
Pre-boundary protein labels also carry survivorship bias: positive values were
rejected on-chain and never became labels at all. Any retrain should either
filter on `submitted_slot >= 497232432` or handle the two eras separately.

---

## Changes that established this boundary

| File | Change |
|---|---|
| `miner_daemon.py` | `_boltz_score_to_affinity()` removed; `_affinity_pred_value_to_dg()` added. Both call sites (~1950 worker, ~2938 main loop) updated. |
| `miner_daemon.py` | `[SUBMIT-GUARD]` added to both submission paths — refuses to send `affinity >= 0.0` on-chain. |
| `nova_adaptive/nova_pulse_scorer.py` | Additive only: body → `_score_batch_impl`; `score_batch` re-derived (signature + return type unchanged); new `score_batch_detailed()` surfaces `affinity_pred_value` / `affinity_probability_binary`. `_combine_score` untouched. |
| `life_submit.js` (core repo) | Logs `requestedTargetId` + `SLOT_COLLISION` alongside the decoded `targetId`. Unrelated to affinity; fixes a misleading log that made PDA slot collisions look like target-type misrouting. |

**Thresholds were deliberately NOT changed.** The per-target
`target_score_threshold` values in `targets.json` (-7.0 … -10.0) were always
real kcal/mol thresholds; the broken formula simply could never reach them
(0.00% hit rate on 328 deduped novel molecules). The fix restores their intended
meaning — 10.98% hit rate — rather than needing recalibration.

## Verification performed

- Ad-hoc script, 24/24 checks (no test suite exists in either repo)
- Real-GPU end-to-end: `boltz_score` bit-identical, PDL1 +0.113 → -7.696
- On-chain: tx `2kxafQoj…` slot 497232432, `err: None`, `Instruction: SubmitResult`
- Live: 0 `InvalidAffinityScore` rejects after deploy (was ~27/day)
