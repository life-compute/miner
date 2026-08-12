#!/usr/bin/env python3
"""
Download ZINC20 fragment-like compounds for life_generate.py fragment recombination.

ZINC20 2D tranche naming (http://files.docking.org/2D/):
  Letter 1 = MW bin    B=200-250 Da, C=250-300 Da
  Letter 2 = logP bin  B=-1–0, C=0–1, D=1–2, E=2–3, F=3–3.5
  Letter 3 = reactivity (any; PAINS filter applied locally)
  Letter 4 = purchasability  A=in-stock, B=in-stock (two vendors)

Strategy: scrape each tranche directory index, collect all .smi filenames,
filter to purchasability A or B (last letter), download what returns 200.
Some files redirect HTTP→HTTPS→403; those are silently skipped.

Output: data/zinc15_fragments.smi — one canonical SMILES per line, deduplicated.
Resumable via data/zinc15_download_state.json.

Usage:
    python3 data/download_zinc15_fragments.py           # all fragment tranches
    python3 data/download_zinc15_fragments.py --dry-run # discover URLs, no download
    python3 data/download_zinc15_fragments.py --status  # progress summary
    python3 data/download_zinc15_fragments.py --test    # one tranche only
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.request
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_DIR   = Path(__file__).parent
OUT_SMILES = DATA_DIR / "zinc15_fragments.smi"
OUT_LOG    = DATA_DIR / "zinc15_download.log"
STATE_FILE = DATA_DIR / "zinc15_download_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(OUT_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("zinc15-dl")

# ── Tranche selection ──────────────────────────────────────────────────────────
BASE_URL   = "http://files.docking.org/2D"
MW_BINS    = ["B", "C"]               # 200-250, 250-300 Da
LOGP_BINS  = ["B", "C", "D", "E", "F"]  # -1 to 3.5
TRANCHES   = [f"{m}{l}" for m in MW_BINS for l in LOGP_BINS]  # 10 tranches
PURCH_OK   = {"A", "B"}              # in-stock (last letter of 4-char filename)

# ── Chemistry filters (match life_generate.py) ─────────────────────────────────
BANNED_ATOMS   = {"Se", "Na", "Fe", "Zn", "B", "Si", "P"}
MIN_MW, MAX_MW = 200.0, 300.0
MAX_ROT_BONDS  = 5
MAX_HA         = 25


def _validate(smiles: str) -> bool:
    """RDKit validation — lazy, fail-open when rdkit absent."""
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
        if mol.GetNumHeavyAtoms() > MAX_HA:
            return False
        mw = Descriptors.MolWt(mol)
        if not (MIN_MW <= mw <= MAX_MW):
            return False
        return Descriptors.NumRotatableBonds(mol) <= MAX_ROT_BONDS
    except ImportError:
        return bool(smiles.strip())
    except Exception:
        return False


def _fetch(url: str, timeout: int = 30) -> str | None:
    """GET url, follow redirects; return body text or None on error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "life-compute/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"    fetch {url} → {e}")
        return None


def discover_files(tranche: str) -> list[str]:
    """Scrape tranche directory index; return .smi filenames filtered by purchasability."""
    body = _fetch(f"{BASE_URL}/{tranche}/")
    if not body:
        return []
    names = re.findall(r'href="([A-Z]{4}\.smi)"', body)
    return [n for n in names if n[-5] in PURCH_OK]   # 4th letter is [:-5+1] = [-5]


def download_file(tranche: str, fname: str, seen: set, out_fh) -> int:
    """Fetch one .smi file, filter, write new SMILES. Returns count written."""
    body = _fetch(f"{BASE_URL}/{tranche}/{fname}", timeout=60)
    if not body:
        log.info(f"    {fname}: skipped (inaccessible)")
        return 0

    n_new = n_seen = n_inv = 0
    for line in body.splitlines():
        if not line or line.startswith("smiles"):
            continue
        smi = line.split()[0]
        if smi in seen:
            n_seen += 1
            continue
        if not _validate(smi):
            n_inv += 1
            continue
        seen.add(smi)
        out_fh.write(smi + "\n")
        n_new += 1

    log.info(f"    {fname}: {n_new} new (dup={n_seen}, inv={n_inv})")
    return n_new


# ── State ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"done_files": [], "n_written": 0}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="ZINC20 fragment downloader")
    ap.add_argument("--dry-run", action="store_true", help="Discover URLs, no download")
    ap.add_argument("--status",  action="store_true", help="Progress summary")
    ap.add_argument("--test",    action="store_true", help="First tranche only")
    args = ap.parse_args()

    state     = _load_state()
    done_set  = set(state["done_files"])
    n_total   = state["n_written"]

    if args.status:
        n_out = sum(1 for _ in OUT_SMILES.open()) if OUT_SMILES.exists() else 0
        print(f"Files done   : {len(done_set)}")
        print(f"Total written: {n_total:,}")
        print(f"File lines   : {n_out:,}  →  {OUT_SMILES}")
        print(f"Log          : {OUT_LOG}")
        return

    log.info("═" * 62)
    log.info("  ZINC20 Fragment Downloader")
    log.info(f"  Tranches: {TRANCHES}  ·  purch A/B only")
    log.info(f"  Filters : MW {MIN_MW}–{MAX_MW} Da  rot≤{MAX_ROT_BONDS}  no banned atoms")
    log.info(f"  Output  : {OUT_SMILES}")
    log.info("═" * 62)

    tranches = TRANCHES[:1] if args.test else TRANCHES

    # Rebuild seen set from disk
    seen: set = set()
    if OUT_SMILES.exists():
        log.info(f"Loading existing SMILES ...")
        for line in OUT_SMILES.read_text().splitlines():
            s = line.strip()
            if s:
                seen.add(s)
        log.info(f"  {len(seen):,} already on disk")

    with OUT_SMILES.open("a") as out_fh:
        for ti, tranche in enumerate(tranches, 1):
            log.info(f"[{ti}/{len(tranches)}] Tranche {tranche} — discovering files ...")
            files = discover_files(tranche)
            log.info(f"  {len(files)} in-stock files found")

            if args.dry_run:
                for f in files:
                    print(f"  {BASE_URL}/{tranche}/{f}")
                continue

            t_start = time.time()
            tranche_new = 0
            for fname in files:
                if fname in done_set:
                    log.info(f"    {fname}: already done")
                    continue
                n = download_file(tranche, fname, seen, out_fh)
                out_fh.flush()
                n_total += n
                tranche_new += n
                done_set.add(fname)
                state["done_files"] = list(done_set)
                state["n_written"]  = n_total
                _save_state(state)
                time.sleep(0.3)   # polite to docking.org

            log.info(
                f"  Tranche {tranche} done: +{tranche_new:,} new "
                f"({time.time()-t_start:.0f}s) | total={n_total:,}"
            )

    if not args.dry_run:
        log.info("═" * 62)
        log.info(f"  Complete — {n_total:,} unique fragment SMILES in {OUT_SMILES}")
        log.info("═" * 62)


if __name__ == "__main__":
    main()
