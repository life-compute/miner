"""
nova_pulse_scorer.py — Boltz2 affinity scoring for PULSE-generated molecules.

Reads nova_pulse_data.jsonl, finds rows that lack a boltz_score, scores them
through Boltz2 against the current epoch target, and appends results to
nova_boltz_scores.jsonl (keyed by smiles — safe to re-run, never overwrites).

Design decisions:
- Append-only output: nova_boltz_scores.jsonl, never rewrite nova_pulse_data.jsonl
- Boltz uses fast settings (recycling=1, sampling=25, diffusion_affinity=1) to
  keep per-molecule cost ~25s instead of ~2 min.
- Only scores rows with proxy_score > 0 AND boltz_safe SMILES (skip junk early).
- Target is read from config/config.yaml (protein_selection.small_molecule.target).
- If the MSA file is missing for the target, falls back gracefully with a warning.
- Batch size ≤ 10 molecules per Boltz call (avoids OOM on large batches).

Boltz combined score (matches validator formula):
    score = (affinity_probability_binary - affinity_pred_value) / heavy_atom_count
    boltz_mode = max  →  higher is better

Usage (standalone):
    python nova_adaptive/nova_pulse_scorer.py            # score up to 50 new rows
    python nova_adaptive/nova_pulse_scorer.py --max 100
    python nova_adaptive/nova_pulse_scorer.py --status   # how many scored vs total
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import random
import shutil
import sys
import time
import tempfile
from pathlib import Path
from typing import Optional

NOVA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NOVA_DIR))

OUTPUT_DIR      = NOVA_DIR / "output" / "nova_adaptive"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PULSE_JSONL     = OUTPUT_DIR / "nova_pulse_data.jsonl"
BOLTZ_SCORES    = OUTPUT_DIR / "nova_boltz_scores.jsonl"
CONFIG_YAML     = NOVA_DIR / "config" / "config.yaml"
MSA_DIR         = NOVA_DIR / "data" / "msa_files"
BOLTZ_TMP       = NOVA_DIR / "external_tools" / "boltz" / "boltz_scorer_tmp"

# Fast settings for scoring (noisier than validator, cheap for ML training)
FAST_RECYCLING_STEPS     = 1
FAST_SAMPLING_STEPS      = 25
FAST_DIFFUSION_SAMPLES   = 1
FAST_SAMPLING_STEPS_AFF  = 25
FAST_DIFFUSION_SAMPLES_AFF = 1

# Validator combo formula constants (from config.yaml boltz2_config)
BOLTZ_METRIC_0 = "affinity_probability_binary"
BOLTZ_METRIC_1 = "affinity_pred_value"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_target_config() -> tuple[str, Optional[list]]:
    """
    Returns (target_code, clip_interval) from config.yaml.
    clip_interval is list of [start, end] pairs or None.
    """
    try:
        import yaml
        with open(CONFIG_YAML) as f:
            cfg = yaml.safe_load(f)
        sm = cfg.get("protein_selection", {}).get("small_molecule", {})
        target = sm.get("target", "Q9UQM7")
        clip   = sm.get("clip_interval", None)
        return target, clip
    except Exception as e:
        print(f"[SCORER] Could not load config.yaml: {e}; using default Q9UQM7")
        return "Q9UQM7", None


def _get_protein_sequence(target: str, clip_interval) -> Optional[str]:
    """Read protein sequence from local MSA a3m file (first non-header line = query sequence).
    Falls back to UniProt/HF API only if the MSA file is missing.
    """
    msa_path = MSA_DIR / f"{target}.a3m"
    if msa_path.exists():
        with open(msa_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith(">"):
                    seq = line
                    if clip_interval and len(clip_interval) >= 2:
                        seq = seq[clip_interval[0]:clip_interval[1]]
                    print(f"[SCORER] Loaded sequence for {target} from local MSA ({len(seq)} aa)")
                    return seq
        print(f"[SCORER] MSA file for {target} exists but contains no sequence lines")
        return None
    # MSA absent — fall back to network fetch
    print(f"[SCORER] No local MSA for {target}, trying API fallback")
    try:
        from utils.proteins import get_sequence_from_protein_code
        return get_sequence_from_protein_code(target, clip_interval)
    except Exception as e:
        print(f"[SCORER] Could not fetch sequence for {target}: {e}")
        return None


# ---------------------------------------------------------------------------
# SMILES → Boltz score (single molecule)
# ---------------------------------------------------------------------------

def _mol_idx(smiles: str, seed: int = 68) -> int:
    h = hashlib.sha256(smiles.encode()).digest()
    return (int.from_bytes(h[:8], "little") ^ seed) % (2**31 - 1)


def _write_input_yaml(
    input_dir: Path,
    target: str,
    protein_sequence: str,
    smiles: str,
    mol_id: int,
    msa_path: str,
) -> Path:
    import yaml
    data = {
        "version": 1,
        "sequences": [
            {
                "protein": {
                    "id": "A",
                    "sequence": protein_sequence,
                    "msa": msa_path,
                }
            },
            {"ligand": {"id": "B", "smiles": smiles}},
        ],
        "properties": [{"affinity": {"binder": "B"}}],
    }
    path = input_dir / f"{mol_id}_{target}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    return path


def _read_boltz_output(output_dir: Path, mol_id: int, target: str) -> Optional[dict]:
    """Parse affinity + confidence files from Boltz output.

    Boltz writes to: <out_dir>/boltz_results_<input_dir_stem>/predictions/<mol_id>_<target>/
    For our layout (input dir named "inputs"): boltz_results_inputs/predictions/<mol_id>_<target>/

    Affinity fields parsed:
        affinity_pred_value            float  (predicted binding affinity, lower = tighter)
        affinity_probability_binary    float  (P(binder), higher = more likely to bind)
    """
    results_path = output_dir / "boltz_results_inputs" / "predictions" / f"{mol_id}_{target}"
    if not results_path.exists():
        # Distinguish: predictions dir never created (predict() crash) vs this mol's subdir missing
        predictions_root = output_dir / "boltz_results_inputs" / "predictions"
        if not predictions_root.exists():
            print(f"[SCORER]   WARN: predictions dir missing entirely — predict() likely crashed "
                  f"({output_dir / 'boltz_results_inputs'})")
        else:
            avail = list(predictions_root.iterdir())
            print(f"[SCORER]   WARN: no subdir for {mol_id}_{target}  "
                  f"(available: {[p.name for p in avail[:5]]})")
        return None

    affinity_file = results_path / f"affinity_{mol_id}_{target}.json"
    if not affinity_file.exists():
        # Affinity step was skipped (structure ran but affinity prediction failed/was skipped)
        files = [f for f in os.listdir(str(results_path))]
        print(f"[SCORER]   WARN: affinity JSON missing for {mol_id}_{target}  files={files}")
        return None

    combined = {}
    for fp in os.listdir(str(results_path)):
        if fp.startswith("affinity") or fp.startswith("confidence"):
            try:
                with open(results_path / fp) as f:
                    combined.update(json.load(f))
            except Exception as exc:
                print(f"[SCORER]   WARN: could not parse {fp}: {exc}")

    if not combined:
        return None

    # Validate required fields are present
    missing = [k for k in (BOLTZ_METRIC_0, BOLTZ_METRIC_1) if k not in combined]
    if missing:
        print(f"[SCORER]   WARN: affinity JSON for {mol_id}_{target} missing fields {missing}  "
              f"(got {list(combined.keys())})")
        return None

    return combined


def _combine_score(metrics: dict, heavy_atom_count: int) -> Optional[float]:
    """Match validator heavy_atom_normalization formula."""
    v0 = metrics.get(BOLTZ_METRIC_0)
    v1 = metrics.get(BOLTZ_METRIC_1)
    if v0 is None or v1 is None:
        return None
    if heavy_atom_count == 0:
        return None
    return (v0 - v1) / heavy_atom_count


def _score_batch_impl(
    smiles_list: list[str],
    target: str,
    protein_sequence: str,
    msa_path: str,
) -> dict[str, Optional[dict]]:
    """Shared implementation for score_batch / score_batch_detailed.

    Returns {smiles: {"boltz_score", "affinity_pred_value",
                      "affinity_probability_binary"} or None}.
    """
    from boltz.main import predict
    from utils.molecules import get_heavy_atom_count, is_boltz_safe_smiles

    # Fresh tmp dirs each batch to avoid stale output confusion
    batch_tmp = BOLTZ_TMP / f"batch_{int(time.time()*1000)}"
    in_dir  = batch_tmp / "inputs"
    out_dir = batch_tmp / "outputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    mol_ids = {}
    for smi in smiles_list:
        ok, _ = is_boltz_safe_smiles(smi)
        if not ok:
            continue
        mid = _mol_idx(smi)
        mol_ids[smi] = mid
        _write_input_yaml(in_dir, target, protein_sequence, smi, mid, msa_path)

    if not mol_ids:
        shutil.rmtree(str(batch_tmp), ignore_errors=True)
        return {s: None for s in smiles_list}

    try:
        predict(
            data=str(in_dir),
            out_dir=str(out_dir),
            recycling_steps=FAST_RECYCLING_STEPS,
            sampling_steps=FAST_SAMPLING_STEPS,
            diffusion_samples=FAST_DIFFUSION_SAMPLES,
            sampling_steps_affinity=FAST_SAMPLING_STEPS_AFF,
            diffusion_samples_affinity=FAST_DIFFUSION_SAMPLES_AFF,
            output_format="mmcif",
            seed=68,
            affinity_mw_correction=True,
            override=True,
            num_workers=0,
            no_kernels=True,   # disable cuequivariance custom CUDA kernels (CUDA 13 / driver 580 compat)
        )
    except Exception as e:
        print(f"[SCORER] Boltz predict() raised: {e}")
        shutil.rmtree(str(batch_tmp), ignore_errors=True)
        return {s: None for s in smiles_list}

    # Post-predict sanity check — catch silent failures early
    predictions_root = out_dir / "boltz_results_inputs" / "predictions"
    if not predictions_root.exists():
        print(f"[SCORER] WARN: predict() returned OK but predictions dir is missing: {predictions_root}")
        print(f"[SCORER]       This usually means predict() crashed internally without raising.")
        shutil.rmtree(str(batch_tmp), ignore_errors=True)
        return {s: None for s in smiles_list}

    n_affinity = sum(
        1 for d in predictions_root.iterdir()
        if d.is_dir() and any(f.name.startswith("affinity_") for f in d.iterdir())
    )
    print(f"[SCORER] predict() complete: {n_affinity}/{len(mol_ids)} affinity files written")

    results: dict[str, Optional[dict]] = {}
    for smi in smiles_list:
        if smi not in mol_ids:
            results[smi] = None
            continue
        mid = mol_ids[smi]
        metrics = _read_boltz_output(out_dir, mid, target)
        if metrics is None:
            results[smi] = None
            continue
        ha = get_heavy_atom_count(smi)
        results[smi] = {
            # Nova ranking statistic — UNCHANGED, bit-for-bit identical.
            "boltz_score": _combine_score(metrics, ha),
            # Raw Boltz2 affinity fields, previously discarded. Surfaced so
            # callers can derive a real physical dG instead of reusing the
            # dimensionless ranking statistic as if it were an energy.
            "affinity_pred_value": metrics.get(BOLTZ_METRIC_1),
            "affinity_probability_binary": metrics.get(BOLTZ_METRIC_0),
        }

    shutil.rmtree(str(batch_tmp), ignore_errors=True)
    return results


def score_batch(
    smiles_list: list[str],
    target: str,
    protein_sequence: str,
    msa_path: str,
) -> dict[str, Optional[float]]:
    """
    Run Boltz2 on a list of SMILES strings.
    Returns {smiles: boltz_score or None}.
    """
    detailed = _score_batch_impl(smiles_list, target, protein_sequence, msa_path)
    return {
        smi: (d["boltz_score"] if d else None) for smi, d in detailed.items()
    }


def score_batch_detailed(
    smiles_list: list[str],
    target: str,
    protein_sequence: str,
    msa_path: str,
) -> dict[str, Optional[dict]]:
    """Same Boltz2 run as score_batch, but also returns the raw affinity fields.

    {smiles: {"boltz_score":                 float,  # Nova ranking statistic
              "affinity_pred_value":         float,  # log10(IC50 / 1uM)
              "affinity_probability_binary": float}} # P(binder), [0, 1]

    boltz_score is produced by the identical code path as score_batch; this
    function only surfaces inputs that were already being computed and thrown
    away. Ranking behaviour is unaffected.
    """
    return _score_batch_impl(smiles_list, target, protein_sequence, msa_path)


# ---------------------------------------------------------------------------
# Load existing scored smiles (cache lookup)
# ---------------------------------------------------------------------------

def _load_scored_smiles(target: str) -> set[str]:
    """Return set of smiles already scored for this target."""
    scored = set()
    if BOLTZ_SCORES.exists():
        for line in BOLTZ_SCORES.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("target") == target and r.get("boltz_score") is not None:
                    scored.add(r["smiles"])
            except Exception:
                pass
    return scored


def _append_score(smiles: str, boltz_score: Optional[float], target: str, ts: float) -> None:
    row = {
        "smiles": smiles,
        "boltz_score": round(boltz_score, 6) if boltz_score is not None else None,
        "target": target,
        "ts": ts,
    }
    with BOLTZ_SCORES.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_scoring(max_rows: int = 50, batch_size: int = 5, auto_retrain: bool = True) -> int:
    """
    Score up to max_rows unscored PULSE candidates with Boltz2.
    Returns number of rows successfully scored.
    When auto_retrain=True (default), triggers nova_art.train() after scoring
    if at least one new score was added — creating a continuous improvement loop.
    """
    if not PULSE_JSONL.exists():
        print("[SCORER] nova_pulse_data.jsonl not found; run nova_pulse.py first")
        return 0

    target, clip_interval = _load_target_config()
    msa_path = str(MSA_DIR / f"{target}.a3m")
    if not os.path.exists(msa_path):
        print(f"[SCORER] MSA file missing for {target} at {msa_path}; cannot score")
        return 0

    protein_seq = _get_protein_sequence(target, clip_interval)
    if not protein_seq:
        print(f"[SCORER] Could not retrieve protein sequence for {target}")
        return 0

    scored_smiles = _load_scored_smiles(target)
    print(f"[SCORER] target={target}  already_scored={len(scored_smiles)}")

    # Load PULSE rows, filter: proxy_score > 0, smiles not yet scored
    from utils.molecules import is_boltz_safe_smiles
    candidates: list[tuple[str, float]] = []  # (smiles, proxy_score)
    for line in PULSE_JSONL.read_text().splitlines():
        try:
            r = json.loads(line)
            smi = r.get("smiles", "")
            if not smi:
                continue
            if smi in scored_smiles:
                continue
            ps = float(r.get("proxy_score", 0.0))
            if ps <= 0.0:
                continue
            ok, _ = is_boltz_safe_smiles(smi)
            if not ok:
                continue
            candidates.append((smi, ps))
        except Exception:
            pass

    # Deduplicate by smiles (PULSE may have duplicates), sort by proxy_score descending
    seen: set[str] = set()
    deduped = []
    for smi, ps in sorted(candidates, key=lambda x: -x[1]):
        if smi not in seen:
            seen.add(smi)
            deduped.append((smi, ps))

    to_score = [smi for smi, _ in deduped[:max_rows]]
    if not to_score:
        print("[SCORER] No unscored candidates found")
        return 0

    print(f"[SCORER] Scoring {len(to_score)} molecules in batches of {batch_size}")
    n_scored = 0

    for i in range(0, len(to_score), batch_size):
        batch = to_score[i : i + batch_size]
        ts = time.time()
        print(f"[SCORER] batch {i//batch_size + 1}: {len(batch)} molecules ... ", end="", flush=True)
        results = score_batch(batch, target, protein_seq, msa_path)
        t_elapsed = time.time() - ts
        ok_count = sum(1 for v in results.values() if v is not None)
        print(f"done in {t_elapsed:.1f}s  ({ok_count}/{len(batch)} scored)")
        for smi, score in results.items():
            _append_score(smi, score, target, ts)
            if score is not None:
                n_scored += 1

    print(f"[SCORER] done  n_scored={n_scored}  total_in_cache={len(scored_smiles)+n_scored}")

    # Auto-retrain ART after scoring completes (skip if nothing new was scored)
    if auto_retrain and n_scored > 0:
        print(f"[SCORER] {n_scored} new scores — triggering ART retrain ...")
        try:
            # Import here (not at top-level) so scorer works even if sklearn is absent
            from nova_adaptive.nova_art import train as art_train
            report = art_train()
            if report.get("ready"):
                print(f"[SCORER] ART retrain OK  n={report['n_rows']}  R²={report.get('r2')}")
            else:
                print(f"[SCORER] ART retrain: not ready yet — {report.get('reason')}")
        except Exception as e:
            print(f"[SCORER] ART retrain failed (non-fatal): {e}")
    elif auto_retrain and n_scored == 0:
        print("[SCORER] No new scores — ART retrain skipped")

    return n_scored


def status() -> None:
    total_pulse = 0
    pulse_proxy_ok = 0
    if PULSE_JSONL.exists():
        for line in PULSE_JSONL.read_text().splitlines():
            try:
                r = json.loads(line)
                total_pulse += 1
                if float(r.get("proxy_score", 0.0)) > 0:
                    pulse_proxy_ok += 1
            except Exception:
                pass

    total_boltz = 0
    boltz_ok = 0
    if BOLTZ_SCORES.exists():
        for line in BOLTZ_SCORES.read_text().splitlines():
            try:
                r = json.loads(line)
                total_boltz += 1
                if r.get("boltz_score") is not None:
                    boltz_ok += 1
            except Exception:
                pass

    print(f"PULSE rows:         {total_pulse}  (proxy_score>0: {pulse_proxy_ok})")
    print(f"Boltz scores:       {total_boltz}  (non-null: {boltz_ok})")
    print(f"Unscored eligible:  ~{pulse_proxy_ok - boltz_ok}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--max", type=int, default=50, dest="max_rows",
                   help="Max PULSE rows to score this run")
    p.add_argument("--batch-size", type=int, default=5,
                   help="Molecules per Boltz predict() call")
    p.add_argument("--status", action="store_true",
                   help="Print counts and exit")
    p.add_argument("--no-retrain", action="store_true",
                   help="Skip ART retrain even if new scores were added")
    args = p.parse_args()
    if args.status:
        status()
    else:
        run_scoring(max_rows=args.max_rows, batch_size=args.batch_size,
                    auto_retrain=not args.no_retrain)
