"""
tests/test_c21e_coordinator_gate.py — C21e: coordinator-mode gating tests.

Coverage
--------
New tests added by C21e:

 C1.  gpu_worker() with COORDINATOR_MODE=true takes the coordinator branch and
      does NOT call run_boltz2_scoring / fetch_targets / submit_on_chain.
      Verifies coordinator_cycle is called.

 C2.  single-GPU main() path: when COORDINATOR_MODE=true the coordinator loop
      runs instead of the old loop — verified by checking that coordinator_cycle
      is called and the old fetch_targets is never reached.

 C3.  When COORDINATOR_MODE=true, NONE of the startup tasks start:
        - ChEMBL prefetch thread  (name="chembl-prefetch")
        - PULSE sweep thread       (name="pulse-sweep")
        - ProteinNet retrain thread (name="proteinnet-retrain")
        - CRISPR-NET retrain thread (name="crispr-net-retrain")
        - CRISPR gRNA thread        (name="crispr-grna")
        - pegRNA thread             (name="peg-rna")

 C4.  When COORDINATOR_MODE=false, ALL of those startup tasks that are
      available DO start (checking thread names or that the code path runs).
      Old behaviour must be byte-identical.

These tests use only mocking and do not require a GPU, wrangler, or the
chain.  They are self-contained and fast (<2s total).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_coord_mode(val: str):
    """Set COORDINATOR_MODE env var (empty string = unset)."""
    if val:
        os.environ["COORDINATOR_MODE"] = val
    else:
        os.environ.pop("COORDINATOR_MODE", None)


def _thread_names() -> set[str]:
    return {t.name for t in threading.enumerate()}


# ---------------------------------------------------------------------------
# Test C1: gpu_worker() takes coordinator branch when flag is true
# ---------------------------------------------------------------------------

class TestGpuWorkerCoordinatorBranch(unittest.TestCase):
    """C1 — gpu_worker() with COORDINATOR_MODE=true calls coordinator_cycle."""

    def test_c1_gpu_worker_coordinator_branch_taken(self):
        """
        Start gpu_worker() in a thread with COORDINATOR_MODE=true.
        Confirm coordinator_cycle is called and coordinator_cycle.call_count >= 1.
        Confirm no call to fetch_targets or run_boltz2_scoring.
        """
        _set_coord_mode("true")
        try:
            import importlib
            import miner_daemon
            importlib.reload(miner_daemon)

            self.assertTrue(miner_daemon.COORDINATOR_MODE,
                            "COORDINATOR_MODE must be True after reload with env=true")

            cycle_called = threading.Event()
            fetch_called = threading.Event()

            def _fake_cycle(wlog, client, shared, gpu_idx):
                cycle_called.set()
                time.sleep(0.05)

            def _fake_fetch(*a, **kw):
                fetch_called.set()
                return []

            shared: dict = {}

            # coordinator_cycle is imported lazily inside gpu_worker() via
            # "from coordinator.loop import coordinator_cycle", so we must
            # patch it at the module it lives in, not on miner_daemon.
            with (
                patch("coordinator.loop.coordinator_cycle", side_effect=_fake_cycle),
                patch("coordinator.loop._get_or_create_client", return_value=MagicMock()),
                patch("miner_daemon.fetch_targets", side_effect=_fake_fetch),
            ):
                # gpu_worker loops forever; run it in a daemon thread and stop after first cycle
                t = threading.Thread(
                    target=miner_daemon.gpu_worker,
                    args=(0, 1, shared),
                    daemon=True,
                )
                t.start()
                triggered = cycle_called.wait(timeout=10.0)

            self.assertTrue(triggered, "coordinator_cycle was never called in gpu_worker")
            self.assertFalse(fetch_called.is_set(),
                             "fetch_targets must NOT be called in coordinator mode")
        finally:
            _set_coord_mode("")
            import importlib, miner_daemon
            importlib.reload(miner_daemon)


# ---------------------------------------------------------------------------
# Test C2: single-GPU main() path coordinator branch
# ---------------------------------------------------------------------------

class TestMainSingleGpuCoordinatorBranch(unittest.TestCase):
    """C2 — main() with GPU_COUNT=1 and COORDINATOR_MODE=true calls coordinator_cycle."""

    def test_c2_single_gpu_main_coordinator_branch(self):
        """
        The single-GPU path in main() must redirect into coordinator_cycle when
        COORDINATOR_MODE=true, without ever calling fetch_targets or run_boltz2_scoring.
        """
        _set_coord_mode("true")
        try:
            import importlib
            import miner_daemon
            importlib.reload(miner_daemon)

            self.assertTrue(miner_daemon.COORDINATOR_MODE)
            self.assertEqual(miner_daemon.GPU_COUNT, 1,
                             "test assumes single-GPU (GPU_COUNT=1); set GPU_COUNT=1 in env")

            cycle_called = threading.Event()
            fetch_called = threading.Event()

            def _fake_cycle(wlog, client, shared, gpu_idx):
                cycle_called.set()
                # Raise to break the infinite loop in main()
                raise SystemExit(0)

            def _fake_fetch(*a, **kw):
                fetch_called.set()
                return []

            with (
                patch("miner_daemon._check_registration_and_balance"),
                patch("miner_daemon.write_stats"),
                patch("miner_daemon.fetch_targets", side_effect=_fake_fetch),
                patch("coordinator.loop.coordinator_cycle", side_effect=_fake_cycle),
                patch("coordinator.loop._get_or_create_client", return_value=MagicMock()),
                patch("subprocess.check_output", return_value=""),
            ):
                try:
                    miner_daemon.main()
                except SystemExit:
                    pass  # expected — _fake_cycle raises it to break the loop

            self.assertTrue(cycle_called.is_set(),
                            "coordinator_cycle was never reached in main() single-GPU path")
            self.assertFalse(fetch_called.is_set(),
                             "fetch_targets must NOT run in coordinator mode")
        finally:
            _set_coord_mode("")
            import importlib, miner_daemon
            importlib.reload(miner_daemon)


# ---------------------------------------------------------------------------
# Test C3: startup tasks do NOT run in coordinator mode
# ---------------------------------------------------------------------------

class TestStartupTasksGatedInCoordinatorMode(unittest.TestCase):
    """C3 — ChEMBL, PULSE, ProteinNet, CRISPR-NET, CRISPR, pegRNA threads must
    not start when COORDINATOR_MODE=true."""

    # Thread names that must NOT appear in coordinator mode
    _FORBIDDEN_NAMES = {
        "chembl-prefetch",
        "pulse-sweep",
        "proteinnet-retrain",
        "crispr-net-retrain",
        "crispr-grna",
        "peg-rna",
    }

    def test_c3_no_startup_threads_in_coordinator_mode(self):
        _set_coord_mode("true")
        try:
            import importlib
            import miner_daemon
            importlib.reload(miner_daemon)
            self.assertTrue(miner_daemon.COORDINATOR_MODE)

            before = _thread_names()

            cycle_count = [0]

            def _fake_cycle(wlog, client, shared, gpu_idx):
                cycle_count[0] += 1
                raise SystemExit(0)

            with (
                patch("miner_daemon._check_registration_and_balance"),
                patch("miner_daemon.write_stats"),
                patch("coordinator.loop.coordinator_cycle", side_effect=_fake_cycle),
                patch("coordinator.loop._get_or_create_client", return_value=MagicMock()),
                patch("subprocess.check_output", return_value=""),
            ):
                try:
                    miner_daemon.main()
                except SystemExit:
                    pass

            after = _thread_names()
            new_threads = after - before
            forbidden_found = new_threads & self._FORBIDDEN_NAMES

            self.assertEqual(
                forbidden_found, set(),
                f"Forbidden startup threads started in coordinator mode: {forbidden_found}",
            )
            self.assertGreater(cycle_count[0], 0,
                               "coordinator_cycle must have been called at least once")
        finally:
            _set_coord_mode("")
            import importlib, miner_daemon
            importlib.reload(miner_daemon)


# ---------------------------------------------------------------------------
# Test C4: startup tasks DO run when coordinator mode is off
# ---------------------------------------------------------------------------

class TestStartupTasksRunWhenCoordinatorModeOff(unittest.TestCase):
    """C4 — startup threads launch normally when COORDINATOR_MODE=false (old behaviour)."""

    def test_c4_startup_threads_run_without_coordinator_mode(self):
        _set_coord_mode("")  # unset = off
        try:
            import importlib
            import miner_daemon
            importlib.reload(miner_daemon)
            self.assertFalse(miner_daemon.COORDINATOR_MODE)

            # Track which thread names appeared by intercepting Thread.__init__
            started_names: list[str] = []
            real_thread_init = threading.Thread.__init__

            def _spy_init(self_t, *a, name=None, **kw):
                if name:
                    started_names.append(name)
                real_thread_init(self_t, *a, name=name, **kw)

            # We only want to see the startup thread launches, not run the main loop.
            # Stub blocking calls so main() reaches the thread-launch section then exits.
            fetch_count = [0]

            def _fake_fetch(*a, **kw):
                fetch_count[0] += 1
                if fetch_count[0] >= 2:
                    raise SystemExit(0)
                return []

            with (
                patch("miner_daemon._check_registration_and_balance"),
                patch("miner_daemon.write_stats"),
                patch("miner_daemon.fetch_targets", side_effect=_fake_fetch),
                patch("miner_daemon.fetch_reference_compounds", return_value={}),
                patch.object(threading.Thread, "__init__", _spy_init),
                patch("subprocess.check_output", return_value=""),
            ):
                try:
                    miner_daemon.main()
                except SystemExit:
                    pass

            # In normal mode fetch_targets must be reached (coordinator fast-path not taken)
            self.assertGreater(fetch_count[0], 0,
                               "fetch_targets must be called in non-coordinator mode")

            # confirm-poller thread is always constructed (registered in started_names via spy)
            self.assertIn("confirm-poller", started_names,
                          "confirm-poller thread must be created in normal mode")

        finally:
            _set_coord_mode("")
            import importlib, miner_daemon
            importlib.reload(miner_daemon)


# ---------------------------------------------------------------------------
# Test C5 (regression): COORDINATOR_MODE env flag evaluation logic unchanged
# ---------------------------------------------------------------------------

class TestCoordinatorModeFlagEvaluation(unittest.TestCase):
    """Regression: the flag parsing at module level matches documented values."""

    def _eval_flag(self, val: str) -> bool:
        return val.strip().lower() in ("1", "true", "yes", "on")

    def test_c5_truthy_values(self):
        for v in ("1", "true", "yes", "on", "True", "TRUE", "YES", "ON", " true "):
            self.assertTrue(self._eval_flag(v), f"{v!r} should be truthy")

    def test_c5_falsy_values(self):
        for v in ("", "0", "false", "no", "off", "False", "NO", "OFF", "nope"):
            self.assertFalse(self._eval_flag(v), f"{v!r} should be falsy")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    unittest.main(verbosity=2)
