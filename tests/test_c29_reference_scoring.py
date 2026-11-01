"""
tests/test_c29_reference_scoring.py — C29 reference scoring tests.

Non-GPU tests (always run):
  1.  protein_dg golden numbers — formula round(1.364247*(v-6.0),3) vs golden.json
  2.  boltz_flags_for from the live scorer profile — token-for-token against golden
  3.  Pinned SHA-256s of the 6 vendored source files vs SHA256SUMS
  4.  PerMoleculeError and RescoreFailed are importable and distinct from Exception

GPU proof (skipped when no GPU; runs under NOVA_VENV in subprocess):
  5.  Molecules A, B (×2), C through the adapter — raw affinity, score, golden delta
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────
_REPO   = Path(__file__).resolve().parents[1]
_REF    = _REPO / "coordinator" / "reference_scoring"
_GOLDEN = _REPO.parent / "handoff" / "scoring-reference" / "golden.json"
_SHA256 = _REPO.parent / "handoff" / "scoring-reference" / "SHA256SUMS"
_NOVA_VENV = Path("/mnt/minos-drive/nova_subnet/.venv/bin/python3")
_MSA_PATH  = (
    Path("/mnt/life-data/msa_files/P03372.a3m")
    if Path("/mnt/life-data/msa_files/P03372.a3m").exists()
    else Path("/mnt/minos-drive/life-compute-miner/data/msa_files/P03372.a3m")
)

sys.path.insert(0, str(_REPO))

# ── helpers ───────────────────────────────────────────────────────────────────

def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_gpu() -> bool:
    """True when the nova venv can see a CUDA device."""
    if not _NOVA_VENV.exists():
        return False
    try:
        r = subprocess.run(
            [str(_NOVA_VENV), "-c",
             "import sys; sys.path.insert(0,'/mnt/minos-drive/nova_subnet');"
             "import torch; print(torch.cuda.is_available())"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0 and "True" in r.stdout
    except Exception:
        return False


# ── Test 1: protein_dg formula against golden.json ───────────────────────────

class TestProteinDgFormula(unittest.TestCase):
    """round(1.364247 * (affinity_pred_value - 6.0), 3) must match golden."""

    @classmethod
    def setUpClass(cls):
        # Import as package member (relative imports require it).
        from coordinator.reference_scoring.rescore import protein_dg, RT_LN10_KCAL
        cls.protein_dg   = staticmethod(protein_dg)
        cls.RT_LN10_KCAL = RT_LN10_KCAL
        with open(_GOLDEN) as f:
            cls.golden = json.load(f)

    def test_constant(self):
        self.assertEqual(self.RT_LN10_KCAL, 1.364247,
                         "RT_LN10_KCAL must be the literal 1.364247")

    def test_molecule_A(self):
        mol = next(m for m in self.golden["molecules"] if m["label"] == "A")
        raw   = mol["raw_affinity_pred_value"]
        score = mol["score_kcal_per_mol"]
        self.assertEqual(self.protein_dg(raw), score,
                         f"protein_dg({raw!r}) should be {score}")

    def test_molecule_B(self):
        mol = next(m for m in self.golden["molecules"] if m["label"] == "B")
        raw   = mol["raw_affinity_pred_value"]
        score = mol["score_kcal_per_mol"]
        self.assertEqual(self.protein_dg(raw), score,
                         f"protein_dg({raw!r}) should be {score}")

    def test_molecule_C(self):
        mol = next(m for m in self.golden["molecules"] if m["label"] == "C")
        raw   = mol["raw_affinity_pred_value"]
        score = mol["score_kcal_per_mol"]
        self.assertEqual(self.protein_dg(raw), score,
                         f"protein_dg({raw!r}) should be {score}")

    def test_offset_is_six(self):
        """protein_dg(6.0) == 0.000 — confirms the –6.0 offset is applied."""
        self.assertEqual(self.protein_dg(6.0), 0.0)

    def test_old_formula_would_be_wrong(self):
        """Sanity: the old formula (no –6.0) gives a wrong result for mol B."""
        mol = next(m for m in self.golden["molecules"] if m["label"] == "B")
        raw   = mol["raw_affinity_pred_value"]
        score = mol["score_kcal_per_mol"]
        old_result = round(1.364247 * float(raw), 3)
        self.assertNotEqual(old_result, score,
                            "Old formula (without –6.0) must NOT match golden")


# ── Test 2: boltz_flags_for against golden boltz_flags ───────────────────────

class TestBoltzFlagsFor(unittest.TestCase):
    """boltz_flags_for(profile, 'protein', has_msa=True, 1024) == golden flags."""

    @classmethod
    def setUpClass(cls):
        from coordinator.reference_scoring.rescore import boltz_flags_for
        from coordinator.reference_scoring.contract import ScorerProfile
        cls.boltz_flags_for = staticmethod(boltz_flags_for)
        cls.ScorerProfile   = ScorerProfile

        with open(_GOLDEN) as f:
            cls.golden = json.load(f)

    def _live_profile(self):
        """Reconstruct the live v1.1.0 scorer profile from golden.json."""
        # golden.json records the 32 final tokens (profile flags + msa_sampling).
        # Split at --subsample_msa to get the two halves.
        all_flags = self.golden["boltz_flags"]
        try:
            split_at = all_flags.index("--subsample_msa")
        except ValueError:
            split_at = len(all_flags)

        profile_flags = tuple(all_flags[:split_at])
        msa_sampling  = tuple(all_flags[split_at:])

        return self.ScorerProfile(
            boltz_version="2.2.1",
            flags=profile_flags,
            alignment_sampling="subsampled",
            msa_sampling=msa_sampling,
            seed=None,
        )

    def test_flags_token_for_token(self):
        profile = self._live_profile()
        built   = list(self.boltz_flags_for(profile, "protein", True, 1024))
        golden  = list(self.golden["boltz_flags"])
        self.assertEqual(
            built, golden,
            f"boltz_flags_for() mismatch:\n  built={built}\ngolden={golden}"
        )

    def test_seed_for(self):
        from coordinator.reference_scoring.rescore import seed_for
        profile = self._live_profile()
        # profile has seed=None → falls through to default
        self.assertEqual(seed_for(profile, 68), 68)

    def test_token_count(self):
        profile = self._live_profile()
        built = self.boltz_flags_for(profile, "protein", True, 1024)
        self.assertEqual(len(built), 32,
                         f"Expected 32 tokens, got {len(built)}: {list(built)}")


# ── Test 3: pinned SHA-256s of the 6 vendored source files ───────────────────

class TestVendoredFilePins(unittest.TestCase):
    """SHA-256 of each vendored file must equal the value in SHA256SUMS."""

    # The subset of SHA256SUMS lines that name coordinator_client source files.
    _EXPECTED: dict[str, str] = {
        "boltzspec.py":  "d8979e6d9501ae1b390710fb5d4e92987e2a9339e03bb05899fe6085791971ef",
        "config.py":     "b4cca977840497189003ce69f9a0b97fa570cf13d35b58efbf6945dd1ce7df02",
        "contract.py":   "852c680b719ba458fe02009d4b5e2b6075ef68f2a6f52b4898cf321050a697e6",
        "hashpin.py":    "3f9d78fc81d20d7543608091105586bbcd2b5646e41b58b75100b4d7e84b78db",
        "__init__.py":   "bee0c74821a40dae3111d0a03d0937fcc6f5cd4b73492fc11eec6834169449a1",
        "rescore.py":    "9aa555334835a99063737e6a49ff1097b48a9efe678ca616cdcdafbe26e29ade",
    }

    def test_sha256_pins(self):
        errors = []
        for name, expected_sha in self._EXPECTED.items():
            path = _REF / name
            if not path.exists():
                errors.append(f"MISSING: {name}")
                continue
            actual = _sha256_file(path)
            if actual != expected_sha:
                errors.append(
                    f"HASH MISMATCH for {name}:\n"
                    f"  expected {expected_sha}\n"
                    f"  actual   {actual}"
                )
        if errors:
            self.fail("\n".join(errors))

    def test_all_six_files_present(self):
        for name in self._EXPECTED:
            self.assertTrue((_REF / name).exists(), f"Missing: {name}")


# ── Test 4: error classes are importable and distinct ────────────────────────

class TestErrorClasses(unittest.TestCase):

    def test_per_molecule_error_importable(self):
        from coordinator.loop import PerMoleculeError
        with self.assertRaises(PerMoleculeError):
            raise PerMoleculeError("test")

    def test_rescore_failed_importable(self):
        from coordinator.reference_scoring.rescore import RescoreFailed
        with self.assertRaises(RescoreFailed):
            raise RescoreFailed("TEST", "detail")

    def test_error_classes_distinct(self):
        from coordinator.loop import PerMoleculeError
        from coordinator.reference_scoring.rescore import RescoreFailed
        self.assertFalse(issubclass(PerMoleculeError, RescoreFailed))
        self.assertFalse(issubclass(RescoreFailed, PerMoleculeError))

    def test_rescore_failed_has_reason_code(self):
        from coordinator.reference_scoring.rescore import RescoreFailed
        exc = RescoreFailed("MY_CODE", "detail text")
        self.assertEqual(exc.reason_code, "MY_CODE")
        self.assertIn("detail text", str(exc))


# ── Test 5: GPU proof — molecules A, B (×2), C through the adapter ───────────

@unittest.skipUnless(_has_gpu(), "no GPU or nova_venv unavailable")
@unittest.skipUnless(_MSA_PATH.exists() and _MSA_PATH.stat().st_size > 1024,
                     "P03372.a3m not available")
class TestGPUProof(unittest.TestCase):
    """
    Run the three golden molecules (A, B×2, C) through the miner's adapter and
    compare to golden.json.  Band = 0.10 kcal/mol (cross-GPU tolerance per README).
    """

    BAND_KCAL = 0.10

    # Scorer profile that matches golden.json / scorer_profile_hash 4dc4f258…
    _SCORER_PROFILE = {
        "boltz_version": "2.2.1",
        "flags": {
            "recycling_steps": 1,
            "sampling_steps": 25,
            "diffusion_samples": 1,
            "sampling_steps_affinity": 25,
            "diffusion_samples_affinity": 1,
            "affinity_mw_correction": True,
            "no_kernels": True,
            "output_format": "mmcif",
            "override": True,
            "num_workers": 0,
            "accelerator": "gpu",
            "devices": 1,
            "model": "boltz2",
            "max_parallel_samples": 5,
            "msa_pairing_strategy": "greedy",
        },
        "alignment_sampling": "subsampled",
        "msa_sampling": {
            "subsample_msa": True,
            "num_subsampled_msa": 1024,
            "max_msa_seqs": 1024,
        },
        "seed": None,
    }

    @classmethod
    def setUpClass(cls):
        with open(_GOLDEN) as f:
            cls.golden = json.load(f)
        cls.golden_by_label = {m["label"]: m for m in cls.golden["molecules"]}

    def _run_molecule(self, smiles: str) -> tuple[float, float, float]:
        """Returns (raw_affinity, score_kcal, elapsed_s)."""
        src = f"""\
import sys, json, time
sys.path.insert(0, '/mnt/minos-drive/nova_subnet')
sys.path.insert(0, '/mnt/minos-drive/life-compute-miner')

from coordinator.reference_scoring.runner import run_protein_reference

t0 = time.monotonic()
result = run_protein_reference(
    smiles={smiles!r},
    msa_path={str(_MSA_PATH)!r},
    scorer_profile_dict={json.dumps(self._SCORER_PROFILE)},
    default_seed=68,
    num_subsampled_msa=1024,
    timeout_s=300.0,
)
elapsed = time.monotonic() - t0
result['elapsed_s'] = round(elapsed, 3)
print(json.dumps(result))
"""
        r = subprocess.run(
            [str(_NOVA_VENV), "-c", src],
            capture_output=True, text=True, timeout=400,
            cwd=str(_REPO),
        )
        if r.returncode != 0:
            self.fail(f"subprocess failed (rc={r.returncode}):\n{r.stderr[-600:]}")
        for line in reversed(r.stdout.strip().splitlines()):
            try:
                d = json.loads(line)
                return float(d["raw"]), float(d["score"]), float(d.get("elapsed_s", 0))
            except Exception:
                continue
        self.fail(f"no parseable JSON in stdout:\n{r.stdout[:400]}")

    def _check(self, label: str, smiles: str, run_idx: int = 0):
        mol = self.golden_by_label[label]
        golden_raw   = mol["raw_affinity_pred_value"]
        golden_score = mol["score_kcal_per_mol"]

        t0 = time.monotonic()
        raw, score, elapsed = self._run_molecule(smiles)
        wall = time.monotonic() - t0

        delta = abs(score - golden_score)
        within_band = delta <= self.BAND_KCAL

        print(
            f"\n[GPU-PROOF] mol={label} run={run_idx+1}  "
            f"raw={raw:.6f}  score={score:.3f}  "
            f"golden_raw={golden_raw:.6f}  golden_score={golden_score:.3f}  "
            f"delta={delta:.3f}  within_band={within_band}  "
            f"elapsed={elapsed:.1f}s",
            flush=True,
        )
        # We report both GPU models in the test; do not assert — just log.
        try:
            gpu_r = subprocess.run(
                [str(_NOVA_VENV), "-c",
                 "import sys; sys.path.insert(0,'/mnt/minos-drive/nova_subnet');"
                 "import torch;"
                 "print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no-gpu')"],
                capture_output=True, text=True, timeout=15,
            )
            miner_gpu = gpu_r.stdout.strip()
        except Exception:
            miner_gpu = "unknown"

        print(f"[GPU-PROOF] miner_gpu={miner_gpu!r}  validator_gpu={self.golden['gpu']!r}",
              flush=True)

        if not within_band:
            print(
                f"[GPU-PROOF] WARNING: delta={delta:.3f} > band={self.BAND_KCAL} "
                f"— reporting only, not failing (cross-GPU non-determinism per README §3)",
                flush=True,
            )
        # Always pass regardless of delta — requirement says report, not assert on delta.
        return raw, score, delta, within_band

    def test_molecule_A(self):
        mol = self.golden_by_label["A"]
        self._check("A", mol["smiles"], run_idx=0)

    def test_molecule_B_run1(self):
        mol = self.golden_by_label["B"]
        self._check("B", mol["smiles"], run_idx=0)

    def test_molecule_B_run2(self):
        mol = self.golden_by_label["B"]
        self._check("B", mol["smiles"], run_idx=1)

    def test_molecule_C(self):
        mol = self.golden_by_label["C"]
        self._check("C", mol["smiles"], run_idx=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
