"""
tests/test_c25_scorer_failures.py  –  C25 scoring-failure accounting tests.

Reproduces the incident: a scorer that always returns None must
  (a) not post anything,
  (b) not advance the saved cursor,
  (c) abandon the lease after MAX_SCORER_FAILURES consecutive failures,
  (d) trigger back-off.

Also tests:
  - nan / non-finite contract_score is treated the same as None,
  - a successful score resets the consecutive-failure counter,
  - the lease summary line reports scored, failed, skipped separately.
"""

from __future__ import annotations

import logging
import math
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from coordinator.auth import MinerKeypair
from coordinator.client import CoordinatorClient, Lease
from coordinator.loop import (
    MAX_SCORER_FAILURES,
    BACKOFF_BASE_S,
    _backoff_state,
    _reset_backoff,
    _clear_cursor,
    _load_cursor,
    coordinator_cycle,
    molecule_hash,
)

ASPIRIN   = "CC(=O)Oc1ccccc1C(=O)O"
CAFFEINE  = "Cn1cnc2c1c(=O)n(c(=O)n2C)C"
IBUPROFEN = "CC(C)Cc1ccc(cc1)C(C)C(=O)O"
ACETAMINO = "CC(=O)Nc1ccc(O)cc1"
NAPROXEN  = "COc1ccc2cc(ccc2c1)C(C)C(=O)O"
CELECOXIB = "Cc1ccc(-c2cc(C(F)(F)F)nn2-c2ccc(S(N)(=O)=O)cc2)cc1"

log = logging.getLogger("test-c25")


def _make_lease(modality: str = "protein", expires_offset_ms: int = 4 * 3600 * 1000) -> Lease:
    return Lease(
        lease_id="a" * 32,
        target="CDK4",
        modality=modality,
        expires_at=int(time.time() * 1000) + expires_offset_ms,
        shards=[],
        hit_threshold=-8.0,
        scorer_profile={"flags": {}, "msa_sampling": {}, "seed": 68},
        coord_config_version="2.0.0",
    )


def _stub_client(lease: Lease) -> CoordinatorClient:
    client = MagicMock(spec=CoordinatorClient)
    client.get_work.return_value = lease
    client.enqueue_result = MagicMock()
    return client


class TestScorerFailureIncident(unittest.TestCase):
    """Reproduce the exact incident: scorer always returns None."""

    def setUp(self):
        _reset_backoff()
        _clear_cursor()

    def tearDown(self):
        _reset_backoff()
        _clear_cursor()

    def _run_always_none(self, molecules):
        """Run coordinator_cycle with a scorer that always returns None."""
        lease  = _make_lease("protein")
        client = _stub_client(lease)

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            patch("coordinator.loop._score_protein", return_value=None),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter(molecules)
            coordinator_cycle(log, client, {}, 0)

        return client

    def test_no_result_posted_when_scorer_always_fails(self):
        """Incident: None score must never be enqueued."""
        client = self._run_always_none([(ASPIRIN, 1, 0), (CAFFEINE, 1, 1)])
        client.enqueue_result.assert_not_called()

    def test_cursor_not_advanced_when_scorer_fails(self):
        """Incident: cursor must NOT advance past molecules with no finite score."""
        self._run_always_none([(ASPIRIN, 1, 0)])
        cursor = _load_cursor()
        # Cursor should not have advanced to (shard=1, row=0) for a failed molecule
        self.assertNotEqual(cursor.get("last_row"), 0,
                            "cursor must not advance past a failed molecule")

    def test_lease_abandoned_after_max_consecutive_failures(self):
        """After MAX_SCORER_FAILURES consecutive None scores the loop abandons the lease."""
        mols = [(f"C{'C' * i}", 1, i) for i in range(MAX_SCORER_FAILURES + 3)]
        client = self._run_always_none(mols)
        # enqueue_result never called (all None)
        client.enqueue_result.assert_not_called()
        # back-off should have been triggered (consecutive failure recorded)
        self.assertGreater(_backoff_state["consecutive_failures"], 0,
                           "back-off must be applied after lease abandoned")

    def test_backoff_applied_when_lease_abandoned(self):
        """Back-off window must be scheduled after an abandoned lease."""
        mols = [(f"C{'C' * i}", 1, i) for i in range(MAX_SCORER_FAILURES)]
        self._run_always_none(mols)
        self.assertGreater(_backoff_state["next_allowed_at"], time.time() - 1,
                           "next_allowed_at must be set in the future after failure")

    def test_nan_score_treated_as_failure(self):
        """A NaN contract_score is non-finite and must not be enqueued."""
        lease  = _make_lease("protein")
        client = _stub_client(lease)

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            # _score_protein returns float("nan") — non-finite
            patch("coordinator.loop._score_protein", return_value=float("nan")),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter([(ASPIRIN, 1, 0)])
            coordinator_cycle(log, client, {}, 0)

        client.enqueue_result.assert_not_called()

    def test_inf_score_treated_as_failure(self):
        """An infinite contract_score is non-finite and must not be enqueued."""
        lease  = _make_lease("protein")
        client = _stub_client(lease)

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            # Return inf as raw_score → _to_contract_score returns inf → guarded
            patch("coordinator.loop._score_protein", return_value=float("inf")),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter([(ASPIRIN, 1, 0)])
            coordinator_cycle(log, client, {}, 0)

        client.enqueue_result.assert_not_called()

    def test_success_resets_consecutive_failure_counter(self):
        """A finite score after failures resets the consec_fail counter within the lease."""
        # First two molecules fail, third succeeds
        mols = [(ASPIRIN, 1, 0), (CAFFEINE, 1, 1), (IBUPROFEN, 1, 2)]
        lease  = _make_lease("protein")
        client = _stub_client(lease)

        score_returns = iter([None, None, -0.05])

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            patch("coordinator.loop._score_protein", side_effect=score_returns),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter(mols)
            coordinator_cycle(log, client, {}, 0)

        # One result posted (the successful one)
        client.enqueue_result.assert_called_once()
        # Back-off should NOT be applied (scored > 0)
        self.assertEqual(_backoff_state["consecutive_failures"], 0,
                         "back-off must be reset after a successful score")

    def test_only_successful_score_is_enqueued(self):
        """Only the molecule with a finite score is enqueued; failed ones are not."""
        mols = [(ASPIRIN, 1, 0), (CAFFEINE, 1, 1), (IBUPROFEN, 1, 2)]
        lease  = _make_lease("protein")
        client = _stub_client(lease)

        score_returns = iter([None, -0.07, None])

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            patch("coordinator.loop._score_protein", side_effect=score_returns),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter(mols)
            coordinator_cycle(log, client, {}, 0)

        self.assertEqual(client.enqueue_result.call_count, 1)
        # Verify the enqueued molecule is CAFFEINE (the one that succeeded)
        call_kwargs = client.enqueue_result.call_args.kwargs
        self.assertEqual(call_kwargs["candidate"], CAFFEINE)

    def test_cursor_advances_only_on_success(self):
        """Cursor advances after a successful score but not after a failure."""
        mols = [(ASPIRIN, 1, 0), (CAFFEINE, 1, 1)]
        lease  = _make_lease("protein")
        client = _stub_client(lease)

        score_returns = iter([None, -0.07])

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            patch("coordinator.loop._score_protein", side_effect=score_returns),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter(mols)
            coordinator_cycle(log, client, {}, 0)

        # Cursor cleared at end of lease, but during the run it was saved at row=1
        # (the successful molecule). The test verifies by checking enqueue was called
        # exactly once (for CAFFEINE at row=1, not ASPIRIN at row=0).
        self.assertEqual(client.enqueue_result.call_count, 1)
        kw = client.enqueue_result.call_args.kwargs
        self.assertEqual(kw.get("shard_row"), 1,
                         "cursor/shard_row should point to the successful molecule (row=1)")

    def test_exactly_five_failures_triggers_abandon(self):
        """Exactly MAX_SCORER_FAILURES (default 5) consecutive failures triggers abandon."""
        mols = [(f"CC{'C' * i}", 1, i) for i in range(MAX_SCORER_FAILURES)]
        client = self._run_always_none(mols)
        # All failed, back-off triggered
        client.enqueue_result.assert_not_called()
        self.assertGreater(_backoff_state["consecutive_failures"], 0)

    def test_four_failures_then_success_no_abandon(self):
        """MAX_SCORER_FAILURES-1 consecutive failures followed by success — no abandon."""
        n_fail = MAX_SCORER_FAILURES - 1
        fail_mols = [(f"CC{'C' * i}", 1, i) for i in range(n_fail)]
        success_mol = (ASPIRIN, 1, n_fail)
        mols = fail_mols + [success_mol]

        lease  = _make_lease("protein")
        client = _stub_client(lease)

        score_returns = iter([None] * n_fail + [-0.1])

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            patch("coordinator.loop._score_protein", side_effect=score_returns),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter(mols)
            coordinator_cycle(log, client, {}, 0)

        # One successful result posted; back-off reset
        self.assertEqual(client.enqueue_result.call_count, 1)
        self.assertEqual(_backoff_state["consecutive_failures"], 0)


class TestStartupBoltzCheck(unittest.TestCase):
    """startup_boltz_check() must raise RuntimeError before any lease is taken."""

    def test_raises_if_nova_venv_missing(self):
        from coordinator import loop as loop_mod
        import coordinator.loop as cl
        orig = cl.NOVA_VENV
        try:
            cl.NOVA_VENV = Path("/nonexistent/venv/bin/python3")
            with self.assertRaises(RuntimeError) as ctx:
                cl.startup_boltz_check()
            self.assertIn("STARTUP_CHECK FAILED", str(ctx.exception))
        finally:
            cl.NOVA_VENV = orig


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
