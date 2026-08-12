"""
life_chembl.py — ChEMBL integration for Life Compute.

Downloads and caches known active compounds from ChEMBL for each cancer target,
enabling three capabilities:

  1. Seeding  — ChEMBL actives as high-quality starting points for generative methods.
  2. Validation — Tanimoto similarity check: is a miner HIT structurally novel vs
                  known drugs, or is it rediscovering an existing compound?
  3. Enrichment — drug-likeness-ranked seeds for scaffold hopping and guided mutation.

ChEMBL REST API (public, no auth):
  https://www.ebi.ac.uk/chembl/api/data/

Cache layout:
  data/chembl/{uniprot_id}_actives.json   — list of {smiles, pchembl, activity_type}

Design notes:
  • All rdkit imports are lazy (inside function bodies) — fail-open when unavailable.
  • All network calls use urllib.request only (no requests dependency).
  • Cache miss in get_chembl_actives() returns [] silently — callers tolerate empty lists.
  • download_all() is designed to run in a background daemon thread.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional

log = logging.getLogger("life-miner")

# ── Paths ──────────────────────────────────────────────────────────────────────
LIFE_DIR   = Path(__file__).resolve().parents[1]
CACHE_DIR  = LIFE_DIR / "data" / "chembl"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── ChEMBL API ────────────────────────────────────────────────────────────────
CHEMBL_API   = "https://www.ebi.ac.uk/chembl/api/data"
MIN_PCHEMBL  = 6.0    # pChEMBL ≥ 6 ↔ IC50/Ki ≤ 1000 nM
MAX_ACTIVES  = 500    # cap per target to keep cache files small
PAGE_SIZE    = 200    # ChEMBL pagination limit
CACHE_MIN    = 5      # minimum entries to consider cache valid

# ── Chemistry filter (matches life_generate.py / life_art.py) ─────────────────
BANNED_ATOMS = {"Se", "Na", "Fe", "Zn", "B", "Si", "P"}
MIN_MW, MAX_MW = 200.0, 800.0   # broader than fragment mode — drug-sized


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_json(url: str, timeout: int = 30) -> Optional[dict]:
    """GET url and return parsed JSON, or None on error."""
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": "life-compute-miner/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log.debug(f"[ChEMBL] GET {url[:80]}: {e}")
        return None


def _cache_path(uniprot_id: str) -> Path:
    return CACHE_DIR / f"{uniprot_id}_actives.json"


def _chembl_id_for(uniprot_id: str) -> Optional[str]:
    """
    Resolve UniProt accession → ChEMBL target_chembl_id via the targets endpoint.
    Returns the first single-protein target that matches, or None.
    """
    url = (f"{CHEMBL_API}/target.json?"
           f"target_components__accession={uniprot_id}&target_type=SINGLE PROTEIN")
    data = _get_json(url)
    if not data:
        return None
    targets = data.get("targets", [])
    if not targets:
        return None
    return targets[0].get("target_chembl_id")


def _smiles_valid(smiles: str) -> bool:
    """Basic SMILES validity gate — lazy rdkit, fail-open."""
    if not smiles or len(smiles) < 4:
        return False
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return False
        if any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()):
            return False
        if {a.GetSymbol() for a in mol.GetAtoms()} & BANNED_ATOMS:
            return False
        mw = Descriptors.MolWt(mol)
        return MIN_MW <= mw <= MAX_MW
    except ImportError:
        return True   # no rdkit → accept optimistically
    except Exception:
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def download_chembl_actives(uniprot_id: str) -> list:
    """
    Download active compounds for a target from ChEMBL and write to cache.

    Steps:
      1. Resolve UniProt → ChEMBL target_chembl_id
      2. Paginate /activity endpoint (pchembl_value ≥ MIN_PCHEMBL)
      3. Filter SMILES for validity, banned atoms, MW range
      4. Write cache JSON; return list of records

    Returns [] on network failure or if target not found in ChEMBL.
    Cache is written even on partial results so subsequent calls don't re-fetch.
    """
    log.info(f"[ChEMBL] Downloading actives for {uniprot_id} ...")

    chembl_id = _chembl_id_for(uniprot_id)
    if not chembl_id:
        log.warning(f"[ChEMBL] No target found for {uniprot_id}")
        _cache_path(uniprot_id).write_text(json.dumps([]))
        return []

    log.info(f"[ChEMBL] {uniprot_id} → {chembl_id}")

    records: list = []
    seen_smiles: set = set()
    offset = 0

    while len(records) < MAX_ACTIVES:
        url = (
            f"{CHEMBL_API}/activity.json?"
            f"target_chembl_id={chembl_id}"
            f"&pchembl_value__gte={MIN_PCHEMBL}"
            f"&standard_type__in=IC50,Ki,Kd,EC50"
            f"&assay_type=B"          # binding assays only
            f"&limit={PAGE_SIZE}&offset={offset}"
        )
        data = _get_json(url)
        if not data:
            break

        activities = data.get("activities", [])
        if not activities:
            break

        for act in activities:
            smi    = act.get("canonical_smiles") or ""
            pch    = act.get("pchembl_value")
            atype  = act.get("standard_type", "")
            mol_id = act.get("molecule_chembl_id", "")
            if not smi or pch is None:
                continue
            try:
                pch = float(pch)
            except (TypeError, ValueError):
                continue
            if smi in seen_smiles:
                continue
            if not _smiles_valid(smi):
                continue
            seen_smiles.add(smi)
            records.append({
                "smiles":           smi,
                "pchembl":          round(pch, 3),
                "activity_type":    atype,
                "molecule_chembl_id": mol_id,
            })
            if len(records) >= MAX_ACTIVES:
                break

        offset += PAGE_SIZE
        # If fewer results than page_size, we've reached the end
        if len(activities) < PAGE_SIZE:
            break
        time.sleep(0.3)   # polite to ChEMBL

    _cache_path(uniprot_id).write_text(json.dumps(records, indent=2))
    log.info(f"[ChEMBL] {uniprot_id}: {len(records)} actives cached")
    return records


def get_chembl_actives(uniprot_id: str) -> list:
    """
    Return cached actives for uniprot_id as a list of record dicts.
    Returns [] silently on cache miss — call download_chembl_actives() first.
    """
    path = _cache_path(uniprot_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def validate_against_chembl(smiles: str, uniprot_id: str) -> dict:
    """
    Check whether a SMILES is structurally similar to known ChEMBL actives.

    Returns:
      {
        "similarity":      float  — max Tanimoto to any known active (0 if no actives)
        "closest_smiles":  str    — SMILES of the nearest known active ("" if none)
        "is_novel":        bool   — True when similarity < 0.4
      }

    Fails open (returns is_novel=True, similarity=0) when rdkit is unavailable
    or the cache is empty.
    """
    actives = get_chembl_actives(uniprot_id)
    if not actives:
        return {"similarity": 0.0, "closest_smiles": "", "is_novel": True}

    try:
        from rdkit import Chem, DataStructs
        from rdkit.Chem import AllChem

        query_mol = Chem.MolFromSmiles(smiles)
        if query_mol is None:
            return {"similarity": 0.0, "closest_smiles": "", "is_novel": True}
        query_fp  = AllChem.GetMorganFingerprintAsBitVect(query_mol, 2, nBits=2048)

        best_sim  = 0.0
        best_smi  = ""
        for rec in actives:
            ref_smi = rec.get("smiles", "")
            ref_mol = Chem.MolFromSmiles(ref_smi)
            if ref_mol is None:
                continue
            ref_fp = AllChem.GetMorganFingerprintAsBitVect(ref_mol, 2, nBits=2048)
            sim    = DataStructs.TanimotoSimilarity(query_fp, ref_fp)
            if sim > best_sim:
                best_sim = sim
                best_smi = ref_smi

        return {
            "similarity":     round(best_sim, 4),
            "closest_smiles": best_smi,
            "is_novel":       best_sim < 0.4,
        }

    except ImportError:
        # rdkit not available — return novel optimistically
        return {"similarity": 0.0, "closest_smiles": "", "is_novel": True}
    except Exception as e:
        log.debug(f"[ChEMBL] validate_against_chembl error: {e}")
        return {"similarity": 0.0, "closest_smiles": "", "is_novel": True}


def get_chembl_seeds(uniprot_id: str, n: int = 20) -> list:
    """
    Return up to n drug-like ChEMBL actives ranked by potency × drug-likeness.

    Scoring: QED (drug-likeness [0,1]) × (pChEMBL / 9.0) normalised potency.
    Fails open → returns [] when rdkit unavailable or cache empty.
    """
    actives = get_chembl_actives(uniprot_id)
    if not actives:
        return []

    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors

        scored = []
        for rec in actives:
            smi = rec.get("smiles", "")
            mol = Chem.MolFromSmiles(smi)
            if mol is None:
                continue
            try:
                qed   = Descriptors.qed(mol)
            except Exception:
                qed   = 0.5
            pch   = float(rec.get("pchembl", 6.0))
            score = qed * min(pch / 9.0, 1.0)
            scored.append((score, smi))

        scored.sort(reverse=True)
        return [smi for _, smi in scored[:n]]

    except ImportError:
        # No rdkit — return top-n by pchembl only
        ranked = sorted(actives, key=lambda r: float(r.get("pchembl", 0)), reverse=True)
        return [r["smiles"] for r in ranked[:n] if r.get("smiles")]
    except Exception as e:
        log.debug(f"[ChEMBL] get_chembl_seeds error: {e}")
        return []


def download_all(uniprot_ids: list, force: bool = False) -> None:
    """
    Download ChEMBL actives for all targets.  Designed to run in a background
    thread — skips targets whose cache already has ≥ CACHE_MIN entries unless
    force=True.  Logs one line per target; never raises.
    """
    for uid in uniprot_ids:
        try:
            if not force:
                existing = get_chembl_actives(uid)
                if len(existing) >= CACHE_MIN:
                    log.debug(f"[ChEMBL] {uid}: cache OK ({len(existing)} entries) — skip")
                    continue
            download_chembl_actives(uid)
        except Exception as e:
            log.warning(f"[ChEMBL] download_all {uid}: {e}")
        time.sleep(0.5)   # polite between targets


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s")

    ap = argparse.ArgumentParser(description="ChEMBL data manager for Life Compute")
    ap.add_argument("--download",   metavar="UNIPROT", help="Download actives for one target")
    ap.add_argument("--all",        action="store_true", help="Download all targets from targets.json")
    ap.add_argument("--status",     action="store_true", help="Print cache summary")
    ap.add_argument("--seeds",      metavar="UNIPROT", help="Print top 5 seeds for a target")
    ap.add_argument("--validate",   nargs=2, metavar=("SMILES","UNIPROT"), help="Validate a SMILES")
    ap.add_argument("--force",      action="store_true")
    args = ap.parse_args()

    if args.status:
        files = sorted(CACHE_DIR.glob("*_actives.json"))
        print(f"ChEMBL cache: {len(files)} targets")
        for f in files:
            try:
                n = len(json.loads(f.read_text()))
                print(f"  {f.stem.replace('_actives',''):12}  {n:4} actives")
            except Exception:
                print(f"  {f.stem:12}  CORRUPT")

    elif args.download:
        download_chembl_actives(args.download)

    elif args.all:
        import urllib.request as _ur
        try:
            TARGETS_URL = "https://raw.githubusercontent.com/life-compute/targets/master/targets.json"
            with _ur.urlopen(TARGETS_URL, timeout=15) as r:
                targets = json.loads(r.read())
            uids = [t["uniprot_id"] for t in targets]
            print(f"Downloading ChEMBL actives for {len(uids)} targets ...")
            download_all(uids, force=args.force)
        except Exception as e:
            print(f"Error: {e}")

    elif args.seeds:
        seeds = get_chembl_seeds(args.seeds, n=5)
        print(f"Top seeds for {args.seeds}:")
        for s in seeds:
            print(f"  {s[:80]}")

    elif args.validate:
        smi, uid = args.validate
        result = validate_against_chembl(smi, uid)
        print(json.dumps(result, indent=2))
