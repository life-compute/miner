"""
tests/test_c24_backoff.py  –  C24 coordinator back-off tests.

When all shards fail (scored=0) the miner must back off exponentially rather
than immediately requesting another lease (which would lock 4 more shards).
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

from coordinator.auth import MinerKeypair
from coordinator.client import CoordinatorClient, Lease
from coordinator.loop import (
    BACKOFF_BASE_S,
    BACKOFF_MAX_CONSECUTIVE,
    BACKOFF_MAX_S,
    _backoff_state,
    _backoff_exceeded,
    _record_failure,
    _record_success,
    _reset_backoff,
    _wait_for_backoff,
    coordinator_cycle,
    MAX_CANDIDATE_CHARS,
    _clear_cursor,
)

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
log = logging.getLogger("test-backoff")


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


class TestBackoffState(unittest.TestCase):
    """Unit tests for the back-off helper functions."""

    def setUp(self):
        _reset_backoff()

    def test_initial_state_is_zero_failures(self):
        self.assertEqual(_backoff_state["consecutive_failures"], 0)
        self.assertEqual(_backoff_state["next_allowed_at"], 0.0)

    def test_record_failure_increments_counter(self):
        _record_failure(log)
        self.assertEqual(_backoff_state["consecutive_failures"], 1)

    def test_record_failure_second_time_doubles_wait(self):
        _record_failure(log)
        wait1 = _backoff_state["next_allowed_at"] - time.time()
        _record_failure(log)
        wait2 = _backoff_state["next_allowed_at"] - time.time()
        # Second wait should be roughly 2× the first (within 2s tolerance)
        self.assertAlmostEqual(wait2, wait1 * 2, delta=2.0)

    def test_record_failure_caps_at_max_s(self):
        # Simulate many failures to hit the cap
        for _ in range(20):
            _record_failure(log)
        remaining = _backoff_state["next_allowed_at"] - time.time()
        self.assertLessEqual(remaining, BACKOFF_MAX_S + 1.0)

    def test_record_success_resets_counter(self):
        _record_failure(log)
        _record_failure(log)
        _record_success()
        self.assertEqual(_backoff_state["consecutive_failures"], 0)
        self.assertEqual(_backoff_state["next_allowed_at"], 0.0)

    def test_backoff_exceeded_false_below_max(self):
        for _ in range(BACKOFF_MAX_CONSECUTIVE - 1):
            _record_failure(log)
        self.assertFalse(_backoff_exceeded(log))

    def test_backoff_exceeded_true_at_max(self):
        for _ in range(BACKOFF_MAX_CONSECUTIVE):
            _record_failure(log)
        self.assertTrue(_backoff_exceeded(log))

    def test_wait_for_backoff_returns_immediately_when_no_backoff(self):
        t0 = time.time()
        _wait_for_backoff(log)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.1, "should return immediately when no backoff set")

    def test_wait_for_backoff_sleeps_until_allowed(self):
        # Set next_allowed_at 0.2s in the future
        _backoff_state["next_allowed_at"] = time.time() + 0.2
        t0 = time.time()
        _wait_for_backoff(log)
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 0.18, "should have waited ~0.2s")
        self.assertLess(elapsed, 1.0, "should not wait longer than expected")

    def test_reset_backoff_clears_state(self):
        _record_failure(log)
        _record_failure(log)
        _reset_backoff()
        self.assertEqual(_backoff_state["consecutive_failures"], 0)
        self.assertEqual(_backoff_state["next_allowed_at"], 0.0)


class TestBackoffInCycle(unittest.TestCase):
    """coordinator_cycle() integration: back-off fires when scored=0."""

    def setUp(self):
        _reset_backoff()
        _clear_cursor()

    def tearDown(self):
        _reset_backoff()
        _clear_cursor()

    def _run_cycle_zero_score(self) -> None:
        """Run coordinator_cycle with shards that yield 0 molecules (shard failure)."""
        lease = _make_lease("protein")
        client = _stub_client(lease)

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),   # don't actually sleep in tests
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            # ShardManager yields nothing → scored stays 0
            MockSM.return_value.iter_molecules.return_value = iter([])
            coordinator_cycle(log, client, {}, 0)

    def test_single_zero_score_increments_failures(self):
        self._run_cycle_zero_score()
        self.assertEqual(_backoff_state["consecutive_failures"], 1)

    def test_zero_score_sets_next_allowed_at_in_future(self):
        self._run_cycle_zero_score()
        self.assertGreater(_backoff_state["next_allowed_at"], time.time())

    def test_successful_cycle_resets_backoff(self):
        # First: two failed cycles (back-off wait is mocked so no actual sleep)
        self._run_cycle_zero_score()
        self._run_cycle_zero_score()
        self.assertEqual(_backoff_state["consecutive_failures"], 2)

        # Then: one successful cycle (mock wait again so no sleep)
        lease = _make_lease("protein")
        client = _stub_client(lease)

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._wait_for_backoff"),
            patch("coordinator.loop._score_protein", return_value=-0.05),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter([(ASPIRIN, 1, 0)])
            coordinator_cycle(log, client, {}, 0)

        self.assertEqual(_backoff_state["consecutive_failures"], 0,
                         "success should reset back-off to 0")
        self.assertAlmostEqual(_backoff_state["next_allowed_at"], 0.0, delta=0.01)

    def test_backoff_max_consecutive_stops_get_work(self):
        """After BACKOFF_MAX_CONSECUTIVE failures coordinator_cycle returns early."""
        # Pre-set state to max
        _backoff_state["consecutive_failures"] = BACKOFF_MAX_CONSECUTIVE
        _backoff_state["next_allowed_at"] = 0.0  # no sleep needed

        get_work_called = []

        class _FakeClient:
            def get_work(self):
                get_work_called.append(1)
                return _make_lease("protein")
            def enqueue_result(self, **kw):
                pass

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter([])
            coordinator_cycle(log, _FakeClient(), {}, 0)

        # get_work must NOT be called when back-off is exceeded
        self.assertEqual(get_work_called, [], "get_work must not be called when back-off exceeded")

    def test_backoff_wait_is_respected_before_next_get_work(self):
        """
        When next_allowed_at is in the future and consecutive_failures < max,
        coordinator_cycle waits before calling get_work.
        """
        # Set a very short wait
        _backoff_state["consecutive_failures"] = 1
        _backoff_state["next_allowed_at"] = time.time() + 0.15

        lease = _make_lease("protein")
        client = _stub_client(lease)

        t0 = time.time()

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQ")),
            patch("coordinator.loop._score_protein", return_value=-0.05),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = iter([(ASPIRIN, 1, 0)])
            coordinator_cycle(log, client, {}, 0)

        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 0.12, "should have waited for back-off window")

    def test_backoff_base_s_doubles_per_failure(self):
        """Verify exponential: failure 1 → BACKOFF_BASE_S, failure 2 → 2×, failure 3 → 4×."""
        original_base = BACKOFF_BASE_S
        import coordinator.loop as loop_mod
        orig = loop_mod.BACKOFF_BASE_S
        loop_mod.BACKOFF_BASE_S = 1.0  # use 1s base for fast test
        try:
            _record_failure(log)
            w1 = _backoff_state["next_allowed_at"] - time.time()
            _record_failure(log)
            w2 = _backoff_state["next_allowed_at"] - time.time()
            _record_failure(log)
            w3 = _backoff_state["next_allowed_at"] - time.time()
        finally:
            loop_mod.BACKOFF_BASE_S = orig

        self.assertAlmostEqual(w1, 1.0, delta=0.1)
        self.assertAlmostEqual(w2, 2.0, delta=0.2)
        self.assertAlmostEqual(w3, 4.0, delta=0.3)

    def test_env_configurable_max_consecutive(self):
        """BACKOFF_MAX_CONSECUTIVE is read from env (test defaults)."""
        # BACKOFF_MAX_CONSECUTIVE was set at import time from env; default is 10.
        self.assertIsInstance(BACKOFF_MAX_CONSECUTIVE, int)
        self.assertGreater(BACKOFF_MAX_CONSECUTIVE, 0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
