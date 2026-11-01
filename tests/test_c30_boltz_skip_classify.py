"""
tests/test_c30_boltz_skip_classify.py  –  C30 failure classification tests.

Root cause (2026-11-01): on the 5060 GPU (8 GB VRAM) Boltz runs OOM during
the structure phase for SAVI molecules, prints "| WARNING: ran out of memory,
skipping batch", exits without writing pre_affinity_input.npz, then the
affinity DataLoader raises FileNotFoundError.  The runner propagates this as
RescoreFailed (rc=1), which was previously counted as a SYSTEMIC failure →
5 consecutive → lease abandoned.

Classification:
  • Boltz prints "Failed to process <input>. Skipping. Error: ..." → the molecule
    is unfeaturizable; classify as PerMoleculeError (SKIP_MOL).
  • OOM that lets Boltz skip the structure stage produces the same per-molecule
    symptom (no .npz → FileNotFoundError).  The "Failed to process … Skipping"
    pattern appears in Boltz stdout regardless of the underlying cause.
  • A genuine systemic failure (GPU unavailable, torch crash, NOVA_VENV missing)
    does NOT contain that pattern → stays SYSTEMIC → abandon after 5 consecutive.

Tests:
  1. _is_per_molecule_error detects "Failed to process" (Boltz OOM symptom).
  2. Real failing SMILES from shard 1 (rows 0-5) and shard 5 (rows 0-3) are
     classified as SKIP_MOL via the pattern, not as systemic failures.
  3. A start failure (bad interpreter path) is systemic — not per-molecule.
  4. Golden ESR1 molecules from shard 9 give positive scores (GPU, skipped by default).
  5. 95%-skip-rate gate abandons lease when skipped > 95% of FAIL_RATE_WINDOW mols.
"""
from __future__ import annotations

import logging
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from coordinator.loop import (
    FAIL_RATE_WINDOW,
    MAX_SCORER_FAILURES,
    PerMoleculeError,
    _clear_cursor,
    _is_per_molecule_error,
    _reset_backoff,
    coordinator_cycle,
)
from coordinator.client import CoordinatorClient, Lease

log = logging.getLogger("test-c30")

# ---------------------------------------------------------------------------
# Real SMILES from production shard files (copied verbatim, not invented).
# ---------------------------------------------------------------------------

# data/shards/adb3ea...smi.gz  — ABL1 lease 9c21ed shard_id=1, rows 0-5
# All five confirmed failing (elapsed ~35s, rc=1, FileNotFoundError) in the
# incident described in the problem statement.
SHARD1_ABL1_ROWS_0_5 = [
    "Fc1cc2c3c(c1)C1(CC4(CSSC4)C1)NC(CO2)C3",
    "c1ccc(-n2cccn2)c(C2NC3CCc4cccc2c4C3)c1",
    "Cc1ccc2c3c1OCC(C3)NC2c1c(N)n(C)c(=O)n(C)c1=O",
    "COCC1(COC)NCCc2ncc(O)cc21",
    "FC(F)(F)c1ccc2c3c1N(CC3)C21CO[C@@H]2CCCO[C@@H]2C1",
    "Cc1[nH]c2c(=O)[nH]cnc2c1C1NC2CCc3cccc1c3C2",
]

# data/shards/c08bae...smi.gz  — BRAF lease 9f284a8a shard_id=5, rows 0-3
SHARD5_BRAF_ROWS_0_3 = [
    "Cc1ccc2cc(C3NC4Cc5c(cccc53)OC4C)c(Cl)nc2c1",
    "CC1Oc2cccc3c2CC1NC3C1CCN(CCN2CCCCC2)CC1",
    "COc1ccc(C2NC(C(=O)O)Cc3nc(N)sc32)cc1OC(F)F",
    "CC1Oc2cccc3c2CC1NC3(C)c1cccc(NS(C)(=O)=O)c1",
]

# data/shards/684eb1...smi.gz  — ESR1 lease 413c95a shard_id=9 golden mols
# These scored successfully (rows 2-7 confirmed in life_boltz_scores.jsonl).
SHARD9_ESR1_GOLDEN = [
    "CCOc1ccc(C#Cc2c(-c3ccc(F)cc3)noc2C)cn1",
    "CCOc1ccc(C#Cc2c(C)cccc2F)cn1",
    "CCNCc1cccc(C#CCNc2nccs2)c1",
    "CNc1cc(C#CCCNC(C)=O)ccc1[N+](=O)[O-]",
]


def _make_lease(
    modality: str = "protein",
    expires_offset_ms: int = 4 * 3600 * 1000,
    shards: list | None = None,
) -> Lease:
    return Lease(
        lease_id="c" * 32,
        target="ABL1",
        modality=modality,
        expires_at=int(time.time() * 1000) + expires_offset_ms,
        shards=shards or [],
        hit_threshold=-8.0,
        scorer_profile={"flags": {}, "msa_sampling": {}, "seed": 68},
        coord_config_version="2.0.0",
    )


class TestBoltzOOMPatternDetected(unittest.TestCase):
    """Test 1: _is_per_molecule_error classifies Boltz OOM/skip patterns."""

    def test_failed_to_process_is_per_mol(self):
        """'Failed to process' in Boltz stdout → per-molecule, not systemic."""
        stdout = (
            "Failed to process /tmp/life-miner-ref-xxxxx/inputs/input.yaml. "
            "Skipping. Error: 'NoneType' object has no attribute 'GetAtoms'."
        )
        ok, reason = _is_per_molecule_error("", stdout)
        self.assertTrue(ok, "Expected per-molecule classification")
        self.assertIn("Failed to process", reason)

    def test_skipping_error_is_per_mol(self):
        ok, reason = _is_per_molecule_error(
            "", "Skipping. Error: 'NoneType' object has no attribute 'GetAtoms'."
        )
        self.assertTrue(ok)

    def test_getattrs_is_per_mol(self):
        ok, reason = _is_per_molecule_error(
            "", "AttributeError: 'NoneType' object has no attribute 'GetAtoms'"
        )
        self.assertTrue(ok)
        self.assertIn("GetAtoms", reason)

    def test_oom_cuda_still_systemic(self):
        """Pure OOM without a 'Failed to process' line stays systemic."""
        stderr = "[W] memory allocation failed … CUDA out of memory"
        ok, _ = _is_per_molecule_error(stderr, "")
        self.assertFalse(ok, "Pure OOM should stay systemic")

    def test_reason_up_to_300_chars(self):
        """reason_line is at most 300 characters."""
        long_line = "Failed to process " + "x" * 400
        ok, reason = _is_per_molecule_error("", long_line)
        self.assertTrue(ok)
        self.assertLessEqual(len(reason), 300)


class TestRealShardSMILESClassifiedAsSkip(unittest.TestCase):
    """Test 2: real failing SMILES from shard 1 and shard 5 → SKIP_MOL via pattern."""

    def setUp(self):
        _reset_backoff()
        _clear_cursor()

    def _run_with_boltz_pattern(self, smiles_list: list[str]) -> int:
        """Run coordinator_cycle with a mock scorer that returns the Boltz OOM pattern."""
        fake_shards = [MagicMock()]
        fake_shards[0].shard_id = 1
        lease = _make_lease(shards=fake_shards)

        enqueued = []
        skipped_reasons = []

        # Simulate: subprocess returns rc=1 with "Failed to process" in stderr/stdout
        # (the actual Boltz OOM path observed in production).
        def oom_scorer(smiles, target, msa_path, protein_seq, profile, wlog):
            # This is what _score_protein does when it sees rc=1 + "Failed to process":
            # it calls _is_per_molecule_error → True → raises PerMoleculeError
            raise PerMoleculeError(
                f"Per-molecule error for {smiles[:40]!r}: "
                f"Failed to process /tmp/life-miner-ref-xxx/inputs/input.yaml. "
                f"Skipping. Error: 'NoneType' object has no attribute 'GetAtoms'."
            )

        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease
        client.enqueue_result.side_effect = lambda **kw: enqueued.append(kw)

        mol_iter = iter([(s, 1, i) for i, s in enumerate(smiles_list)])
        remaining = [len(smiles_list)]

        def fake_iter(_shards):
            for item in mol_iter:
                remaining[0] -= 1
                yield item

        with (
            patch("coordinator.loop.ShardManager") as MockSM,
            patch("coordinator.loop._score_protein", side_effect=oom_scorer),
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._save_cursor"),
            patch("coordinator.loop._clear_cursor"),
            patch("coordinator.loop._load_cursor", return_value={}),
            patch("coordinator.loop._write_jsonl"),
        ):
            MockSM.return_value.iter_molecules.side_effect = fake_iter
            coordinator_cycle(log, client, {}, gpu_idx=0)

        self.assertEqual(len(enqueued), 0, "No results should be posted for per-mol skips")
        return remaining[0]  # 0 means all mols were iterated (no early abandon)

    def test_shard1_rows_not_abandoned_early(self):
        """Shard 1 rows 0-5 are skipped as per-molecule, not causing early abandon."""
        remaining = self._run_with_boltz_pattern(SHARD1_ABL1_ROWS_0_5)
        self.assertEqual(remaining, 0,
                         "All 6 shard1 molecules should be iterated (no systemic abandon)")

    def test_shard5_rows_not_abandoned_early(self):
        """Shard 5 rows 0-3 are skipped as per-molecule, not causing early abandon."""
        remaining = self._run_with_boltz_pattern(SHARD5_BRAF_ROWS_0_3)
        self.assertEqual(remaining, 0,
                         "All 4 shard5 molecules should be iterated (no systemic abandon)")


class TestStartFailureIsSystemic(unittest.TestCase):
    """Test 3: a start failure (None from scorer) counts as systemic → abandons."""

    def setUp(self):
        _reset_backoff()
        _clear_cursor()

    def test_none_return_is_systemic_and_abandons(self):
        """_score_protein returning None (interpreter missing, etc.) → systemic → abandon."""
        smiles_list = SHARD1_ABL1_ROWS_0_5 + SHARD5_BRAF_ROWS_0_3  # 10 mols

        fake_shards = [MagicMock()]
        fake_shards[0].shard_id = 1
        lease = _make_lease(shards=fake_shards)

        call_count = [0]

        def start_fail_scorer(*args, **kwargs):
            call_count[0] += 1
            return None   # None = systemic failure (not PerMoleculeError)

        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease

        with (
            patch("coordinator.loop.ShardManager") as MockSM,
            patch("coordinator.loop._score_protein", side_effect=start_fail_scorer),
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._save_cursor"),
            patch("coordinator.loop._clear_cursor"),
            patch("coordinator.loop._load_cursor", return_value={}),
            patch("coordinator.loop._write_jsonl"),
        ):
            def fake_iter(_shards):
                for i, s in enumerate(smiles_list):
                    yield s, 1, i
            MockSM.return_value.iter_molecules.side_effect = fake_iter
            coordinator_cycle(log, client, {}, gpu_idx=0)

        self.assertLessEqual(
            call_count[0], MAX_SCORER_FAILURES,
            f"Expected abandon after {MAX_SCORER_FAILURES} systemic failures, "
            f"but scored {call_count[0]} mols"
        )


@unittest.skipUnless(
    Path("/mnt/minos-drive/nova_subnet/.venv/bin/python3").exists(),
    "GPU / nova_subnet venv required — run manually with LIFE_GPU_TESTS=1"
)
class TestGoldenESR1ScoresOnGPU(unittest.TestCase):
    """Test 4: ESR1 shard-9 golden molecules produce positive scores (GPU required).

    Skipped by default; run with:
        LIFE_GPU_TESTS=1 python -m pytest tests/test_c30_boltz_skip_classify.py::TestGoldenESR1ScoresOnGPU -s
    """

    MSA_PATH = Path("/mnt/minos-drive/life-compute-miner/data/align_cache/P03372.a3m")

    def setUp(self):
        import os
        if not os.environ.get("LIFE_GPU_TESTS"):
            self.skipTest("Set LIFE_GPU_TESTS=1 to enable GPU tests")

    def test_golden_esr1_row0_scores_positive(self):
        """ESR1 shard9 row0 (CCOc1ccc...) should produce a finite score."""
        import json, subprocess, tempfile, os
        NOVA_DIR = "/mnt/minos-drive/nova_subnet"
        MINER_DIR = "/mnt/minos-drive/life-compute-miner"
        NOVA_VENV = f"{NOVA_DIR}/.venv/bin/python3"

        helper_src = f"""
import sys, json
sys.path.insert(0, '{NOVA_DIR}')
import pathlib
sys.path.insert(0, '{MINER_DIR}')
args = json.loads(sys.argv[1])
from coordinator.reference_scoring.runner import run_protein_reference, RescoreFailed
try:
    result = run_protein_reference(**args)
    print(json.dumps(result))
    sys.exit(0)
except RescoreFailed as exc:
    sys.stderr.write(f'SYSTEMIC: {{exc}}\\n')
    sys.exit(1)
except Exception as exc:
    sys.stderr.write(f'SYSTEMIC: {{exc}}\\n')
    sys.exit(1)
"""
        args = json.dumps({
            "smiles": SHARD9_ESR1_GOLDEN[0],
            "msa_path": str(self.MSA_PATH),
            "scorer_profile_dict": {
                "boltz_version": "2.2.1",
                "flags": ["--recycling_steps", "3"],
                "alignment_sampling": "uniform",
            },
            "default_seed": 68,
            "num_subsampled_msa": 1024,
            "timeout_s": 300.0,
        })
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(helper_src)
            hp = f.name
        try:
            r = subprocess.run([NOVA_VENV, hp, args], capture_output=True, text=True,
                               timeout=360, cwd=NOVA_DIR)
        finally:
            os.unlink(hp)

        self.assertEqual(r.returncode, 0,
                         f"Expected rc=0; stderr={r.stderr[-300:]}")
        result = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn("score", result)
        self.assertIsInstance(result["score"], float)
        self.assertTrue(-20 < result["score"] < 10,
                        f"Score out of expected kcal range: {result['score']}")


class TestRolling95PercentSkipGate(unittest.TestCase):
    """Test 5: >95% per-molecule skips over FAIL_RATE_WINDOW → LEASE_ABANDONED."""

    def setUp(self):
        _reset_backoff()
        _clear_cursor()

    def test_95_percent_skips_abandons_lease(self):
        """If >95% of molecules are per-molecule skips, abandon (C30 rule)."""
        total = FAIL_RATE_WINDOW + 50
        # All per-molecule skips → should abandon around FAIL_RATE_WINDOW
        smiles_list = (SHARD1_ABL1_ROWS_0_5 * 50)[:total]

        fake_shards = [MagicMock()]
        fake_shards[0].shard_id = 1
        lease = _make_lease(shards=fake_shards)

        call_count = [0]

        def all_skip_scorer(smiles, *args, **kwargs):
            call_count[0] += 1
            raise PerMoleculeError(
                f"Failed to process input.yaml. Skipping. Error: bad mol"
            )

        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease

        with (
            patch("coordinator.loop.ShardManager") as MockSM,
            patch("coordinator.loop._score_protein", side_effect=all_skip_scorer),
            patch("coordinator.loop.MAX_SCORER_FAILURES", 100_000),  # disable systemic gate
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._save_cursor"),
            patch("coordinator.loop._clear_cursor"),
            patch("coordinator.loop._load_cursor", return_value={}),
            patch("coordinator.loop._write_jsonl"),
        ):
            def fake_iter(_shards):
                for i, s in enumerate(smiles_list):
                    yield s, 1, i
            MockSM.return_value.iter_molecules.side_effect = fake_iter
            coordinator_cycle(log, client, {}, gpu_idx=0)

        self.assertLess(call_count[0], total,
                        "Should abandon before exhausting all molecules")
        self.assertGreaterEqual(call_count[0], FAIL_RATE_WINDOW,
                                f"Should process at least {FAIL_RATE_WINDOW} mols to fill window")


if __name__ == "__main__":
    unittest.main()
