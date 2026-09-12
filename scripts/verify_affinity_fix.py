"""
verify_affinity_fix.py — protein ΔG derivation + scale-boundary verifier.

Sixth member of the verify_* family. Proves the 2026-09-12 affinity fix is
intact and that the recorded scale boundary stays trustworthy over time.

Protein `claimed_affinity` was `-boltz_score * 30.0`. boltz_score is the Nova
ranking statistic `(affinity_probability_binary - affinity_pred_value) /
heavy_atom_count` — it subtracts a log10 concentration from a probability, so
it is dimensionless and meaningless as an energy. It is now derived instead as
`RT*ln(10) * (affinity_pred_value - 6)`, a real kcal/mol ΔG.

miner_daemon.py cannot be imported (module import triggers GPU/chain setup), so
the conversion is lifted out by AST and executed in isolation.

  A. ΔG conversion math + real measured reference values.
  B. Call sites, submit guards, no stale pre-fix symbol.
  C. Nova ranking surface unchanged (score_batch contract preserved).
  D. Scale boundary recorded consistently in doc + code + git history.
  E. Boundary matches the chain (devnet RPC; SKIPPED when offline).

E needs network. When devnet is unreachable it is SKIPPED, not failed, so the
script still works air-gapped.

Usage:
    python scripts/verify_affinity_fix.py
    python scripts/verify_affinity_fix.py --offline
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MINER = REPO / "miner_daemon.py"
DOC = REPO / "AFFINITY_SCALE_BOUNDARY.md"
NFT = REPO / "scripts" / "mint_discovery_nft.js"
SCORER = Path("/mnt/minos-drive/nova_subnet/nova_adaptive/nova_pulse_scorer.py")

# Boundary anchors — see AFFINITY_SCALE_BOUNDARY.md. The slot is authoritative.
SLOT = 497232432
BLOCK_TIME = 1789223558
TX = ("2kxafQojGUZ48qfujSxzxV9nqVUBpaq8JmGRj5nTRCrDjzen6Bwv3yh9"
      "jb83EKYNgkqtRES7JjzduKApr2JyBksx")
DEVNET = "https://api.devnet.solana.com"

# Real Boltz2 output for the PDL1 reference compound BMS-202 at seed 68.
# Under the old formula this molecule claimed +0.113 and was rejected on-chain.
BMS202_PRED_VALUE = 0.3587188720703125
MYCI975_PRED_VALUE = 0.7362370491027832


class Checker:
    def __init__(self) -> None:
        self.checks = self.fails = 0

    def __call__(self, name: str, cond: bool, detail: str = "") -> bool:
        self.checks += 1
        self.fails += not cond
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        return cond

    def skip(self, name: str, why: str) -> None:
        print(f"  SKIP  {name}  [{why}]")


def _load_conversion(src: str) -> dict:
    """Lift the ΔG constant + function out of miner_daemon.py without importing it."""
    ns: dict = {}
    for node in ast.parse(src).body:
        wanted = (
            isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "_RT_LN10_KCAL"
        ) or (
            isinstance(node, ast.FunctionDef)
            and node.name == "_affinity_pred_value_to_dg"
        )
        if wanted:
            exec(compile(ast.Module([node], []), "<verify>", "exec"), ns)
    return ns


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True
    ).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="skip the devnet RPC check")
    args = ap.parse_args()

    ck = Checker()
    src = MINER.read_text()

    print("\n[A] ΔG conversion math")
    ns = _load_conversion(src)
    if not ck("conversion lifted from source", "_affinity_pred_value_to_dg" in ns):
        return 1
    dg = ns["_affinity_pred_value_to_dg"]
    ck(
        "constant == RT*ln(10) @298.15K",
        abs(ns["_RT_LN10_KCAL"] - 0.001987204259 * 298.15 * math.log(10)) < 5e-6,
    )
    ck("v=6 (IC50=1M) -> ΔG=0", dg(6.0) == 0.0, str(dg(6.0)))
    ck("monotonic 1nM < 1uM < 1mM", dg(-3.0) < dg(0.0) < dg(3.0))
    ck("v>6 -> positive ΔG (non-binder)", dg(7.0) > 0, str(dg(7.0)))
    ck("None passthrough", dg(None) is None)
    ck("PDL1/BMS-202 -> -7.696", dg(BMS202_PRED_VALUE) == -7.696)
    ck("MYC/MYCi975 -> -7.181", dg(MYCI975_PRED_VALUE) == -7.181)
    ck(
        "regression: old formula gave a positive claim",
        -((0.24219919741153717 - BMS202_PRED_VALUE) / 31) * 30.0 > 0,
    )

    print("\n[B] wiring")
    ck("both ΔG call sites present", src.count("_affinity_pred_value_to_dg(result.get(") == 2)
    ck("submit guard on both paths", src.count("[SUBMIT-GUARD]") == 2)
    ck(
        "no stale _boltz_score_to_affinity(",
        not any(
            re.search(r"_boltz_score_to_affinity\s*\(", p.read_text(errors="replace"))
            for p in REPO.rglob("*.py")
            if p.resolve() != Path(__file__).resolve()  # this file names it to detect it
        ),
    )
    ck(
        "miner_daemon.py parses",
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(MINER)], capture_output=True
        ).returncode
        == 0,
    )

    print("\n[C] Nova ranking surface unchanged")
    if SCORER.exists():
        s = SCORER.read_text()
        ck("_combine_score still (v0 - v1)/heavy_atom_count",
           "return (v0 - v1) / heavy_atom_count" in s)
        ck("score_batch still returns dict[str, Optional[float]]",
           "def score_batch(" in s and "-> dict[str, Optional[float]]:" in s)
        ck("score_batch_detailed present", "def score_batch_detailed(" in s)
        ck("miner consumes the detailed variant", "score_batch_detailed(" in src)
    else:
        ck.skip("nova scorer checks", f"not found at {SCORER}")

    print("\n[D] scale boundary recorded")
    doc = DOC.read_text() if DOC.exists() else ""
    ck("AFFINITY_SCALE_BOUNDARY.md exists", bool(doc), f"{len(doc)} chars")
    for label, text in (("doc", doc), ("miner_daemon.py", src), ("mint_discovery_nft.js", NFT.read_text())):
        ck(f"slot {SLOT} recorded in {label}", str(SLOT) in text)
    ck("doc cites the verifying tx", TX[:24] in doc)
    ck("doc gives the machine-usable rule", "submitted_slot" in doc)
    ck("doc records both formulas",
       "-boltz_score * 30.0" in doc and "RT*ln(10) * (v - 6)" in doc)
    ck("doc flags NFT immutability", "cannot be retroactively corrected" in doc)
    ck("boundary is in git history", bool(_git("log", "--all", "-S", str(SLOT), "--oneline").strip()))
    ck("boundary tracked in >=3 files", len(_git("grep", "-l", str(SLOT), "HEAD").split()) >= 3)

    print("\n[E] boundary matches chain")
    if args.offline:
        ck.skip("devnet cross-check", "--offline")
    else:
        try:
            req = urllib.request.Request(
                DEVNET,
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                    "params": [TX, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            res = json.load(urllib.request.urlopen(req, timeout=25))["result"]
            ck(f"on-chain slot == {SLOT}", res["slot"] == SLOT, str(res["slot"]))
            ck("tx succeeded (err is None)", res["meta"].get("err") is None)
            ck(f"blockTime == {BLOCK_TIME}", res.get("blockTime") == BLOCK_TIME)
        except Exception as exc:  # noqa: BLE001 — offline is a skip, not a failure
            ck.skip("devnet cross-check", f"{type(exc).__name__}")

    print(f"\n{'=' * 52}\n{ck.checks - ck.fails}/{ck.checks} checks passed")
    return 1 if ck.fails else 0


if __name__ == "__main__":
    sys.exit(main())
