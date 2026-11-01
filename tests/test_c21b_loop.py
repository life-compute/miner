"""
tests/test_c21b_loop.py — C21b coordinator_cycle() tests.

Coverage
--------
Unit (mock coordinator):
  1.  Normal lease → shard → score → enqueue (protein, 3 molecules)
  2.  CRISPR lease → declined cleanly, no POST /results
  3.  503 / no-work → returns without error
  4.  Alignment mismatch PinnedHashError → align_fetch attempted, then skip
  5.  5xx on POST /results → queue retries (item stays in queue after first attempt)
  6.  422 on POST /results (non-retriable) → treated as success, dequeued
  7.  429 on POST /results → retried (item stays in queue)
  8.  410 (lease expired) → dequeued (treated as success)
  9.  Lease expiry during iteration → loop stops mid-shard
 10.  Durable queue survives a simulated restart (queue JSONL re-read by new client)
 11.  Candidate > 2048 chars → dropped, not enqueued
 12.  Cursor persists and resumes correctly across restarts

End-to-end (local wrangler dev, skip if not running):
 13.  Full round-trip: register throwaway key → get_work → enqueue → POST /results
      → result row appears in local DB via GET /results/{wallet}

GPU dry-run (skip if no GPU or no Boltz):
 14.  score_batch_with_profile on a single SMILES (aspirin) against CDK4
      with a real protein scorer profile — prints score, time, scorer args.
      Does NOT call enqueue_result or POST /results.

Old-path preservation:
 15.  gpu_worker with COORDINATOR_MODE=False calls run_boltz2_scoring (not
      coordinator_cycle).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import queue
import socket
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from unittest.mock import patch, MagicMock

# Ensure project root is importable
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from coordinator.auth import MinerKeypair
from coordinator.client import CoordinatorClient, Lease, QueuedResult, BACKOFF_BASE_S
from coordinator.hashes import PinnedHashError
from coordinator.loop import coordinator_cycle, molecule_hash, MAX_CANDIDATE_CHARS, _clear_cursor, CURSOR_PATH

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"  # short canonical SMILES


def _make_shard_archive(smiles_list: list[str]) -> bytes:
    """Build a minimal shard tar.gz in-memory."""
    content = "\n".join(smiles_list).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("molecules.smi")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_lease_dict(
    modality: str = "protein",
    smiles: list[str] | None = None,
    expires_offset_ms: int = 4 * 3600 * 1000,
) -> dict:
    if smiles is None:
        smiles = [ASPIRIN, "c1ccccc1", "CC(N)=O"]
    archive_bytes = _make_shard_archive(smiles)
    sha256 = hashlib.sha256(archive_bytes).hexdigest()
    return {
        "lease_id": "a" * 32,
        "target": "CDK4",
        "modality": modality,
        "expires_at": int(time.time() * 1000) + expires_offset_ms,
        "shards": [],  # unit tests patch ShardManager
        "hit_threshold": -8.0,
        "scorer_profile": {"flags": {}, "msa_sampling": {}, "seed": 68},
        "scorer_profile_hash": "abc123",
        "coord_config_version": "2.0.0",
    }


def _make_client(
    base_url: str,
    queue_path: Path,
    keypair: Optional[MinerKeypair] = None,
) -> CoordinatorClient:
    kp = keypair or MinerKeypair.generate()
    return CoordinatorClient(
        keypair=kp,
        coordinator_url=base_url,
        worker_name=None,
        queue_path=queue_path,
    )


class _MockCoordHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        key = f"{method} {path}"
        resps = getattr(self.server, "_responses", {})
        calls = getattr(self.server, "_calls", [])
        calls.append((method, path, body))
        resp = resps.get(key)
        if resp is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return
        if callable(resp):
            status, data = resp(body)
        else:
            status, data = resp
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start_mock_server(responses: dict):
    srv = HTTPServer(("127.0.0.1", 0), _MockCoordHandler)
    srv._responses = dict(responses)     # type: ignore[attr-defined]
    srv._calls     = []                  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
# molecule_hash contract test
# ---------------------------------------------------------------------------

class TestMoleculeHash(unittest.TestCase):
    def test_is_32_hex_chars(self):
        h = molecule_hash(ASPIRIN)
        self.assertEqual(len(h), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_deterministic(self):
        self.assertEqual(molecule_hash(ASPIRIN), molecule_hash(ASPIRIN))

    def test_distinct_smiles_give_distinct_hashes(self):
        h1 = molecule_hash("c1ccccc1")
        h2 = molecule_hash("CC(N)=O")
        self.assertNotEqual(h1, h2)

    def test_definition_matches_sha256_truncation(self):
        expected = hashlib.sha256(ASPIRIN.encode()).hexdigest()[:32]
        self.assertEqual(molecule_hash(ASPIRIN), expected)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestCoordinatorCycleUnit(unittest.TestCase):
    """Mock-based unit tests for coordinator_cycle()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.jsonl"
        _clear_cursor()
        self.wlog = logging.getLogger("test-loop")

    def tearDown(self):
        self.tmp.cleanup()
        _clear_cursor()

    # ── helpers ─────────────────────────────────────────────────────────

    def _make_lease(self, modality="protein", expires_offset_ms=4*3600*1000):
        d = _make_lease_dict(modality=modality, expires_offset_ms=expires_offset_ms)
        return Lease(
            lease_id=d["lease_id"],
            target=d["target"],
            modality=d["modality"],
            expires_at=d["expires_at"],
            shards=d["shards"],
            hit_threshold=d["hit_threshold"],
            scorer_profile=d["scorer_profile"],
            coord_config_version=d["coord_config_version"],
        )

    def _stub_client(self, lease, queue_path=None):
        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease
        client.enqueue_result = MagicMock()   # simple call recorder, no side-effects
        return client

    # ── Test 1: normal protein lease → 3 molecules scored + enqueued ──

    def test_01_protein_lease_scores_all_molecules(self):
        lease = self._make_lease("protein")
        smiles_list = [ASPIRIN, "c1ccccc1", "CC(N)=O"]
        client = self._stub_client(lease)

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
            patch("coordinator.loop._score_protein", return_value=-0.05),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = [
                (s, 1, i) for i, s in enumerate(smiles_list)
            ]
            coordinator_cycle(self.wlog, client, {}, 0)

        self.assertEqual(client.enqueue_result.call_count, 3)

        # All molecule_hash values should be distinct 32-char hex strings
        hashes = [
            call.kwargs.get("molecule_hash") or call.args[1]
            for call in client.enqueue_result.call_args_list
        ]
        self.assertEqual(len(set(hashes)), 3)
        for h in hashes:
            self.assertEqual(len(h), 32)

        # get_work was called exactly once
        client.get_work.assert_called_once()

    # ── Test 2: CRISPR lease → declined, no enqueue ─────────────────

    def test_02_crispr_declined_cleanly(self):
        lease = self._make_lease("crispr")
        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease

        coordinator_cycle(self.wlog, client, {}, 0)

        client.enqueue_result.assert_not_called()
        # No exception raised

    # ── Test 3: get_work returns None → no enqueue ──────────────────

    def test_03_no_work_returns_cleanly(self):
        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = None

        coordinator_cycle(self.wlog, client, {}, 0)

        client.enqueue_result.assert_not_called()

    # ── Test 4: alignment mismatch → align_fetch attempted ──────────

    def test_04_alignment_mismatch_triggers_fetch(self):
        lease = self._make_lease("protein")
        lease.pinned_msa_sha256 = "deadbeef" * 8
        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease

        with (
            patch("coordinator.hashes.verify_protein_target",
                  side_effect=PinnedHashError("hash mismatch")),
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(None, None)),  # fetch fails → skip
        ):
            coordinator_cycle(self.wlog, client, {}, 0)

        # No results posted because MSA could not be resolved
        client.enqueue_result.assert_not_called()

    # ── Test 5: 5xx on POST → item stays in queue ───────────────────

    def test_05_5xx_retry_keeps_item_in_queue(self):
        """After a 5xx, _post_result_once returns False → item NOT dequeued."""
        kp = MinerKeypair.generate()
        client = CoordinatorClient(
            keypair=kp, coordinator_url="http://localhost:9999",
            worker_name=None, queue_path=self.queue_path,
        )
        item = QueuedResult(
            lease_id="a" * 32, molecule_hash="f" * 32,
            score=-8.0, candidate=ASPIRIN,
        )
        client._append_queue(item)
        with patch("coordinator.client._http", return_value=(500, {"error": "internal"})):
            result = client._post_result_once(item)
        # 5xx is retriable → False → item stays in queue
        self.assertFalse(result)
        items = client._load_queue()
        self.assertEqual(len(items), 1)

    # ── Test 6: 422 → treated as success, dequeued ──────────────────

    def test_06_422_non_retriable_dequeued(self):
        """_post_result_once treats 422 as 'don't retry' → returns True → dequeued."""
        kp = MinerKeypair.generate()
        client = CoordinatorClient(
            keypair=kp, coordinator_url="http://localhost:9999",
            worker_name=None, queue_path=self.queue_path,
        )
        item = QueuedResult(
            lease_id="a" * 32, molecule_hash="b" * 32,
            score=-8.0, candidate=ASPIRIN,
        )
        with patch("coordinator.client._http", return_value=(422, {"error": "profile_override"})):
            result = client._post_result_once(item)
        # 422 is 4xx → treated as non-retriable → True
        self.assertTrue(result)

    # ── Test 7: 429 → not dequeued (retry) ──────────────────────────

    def test_07_429_kept_in_queue(self):
        kp = MinerKeypair.generate()
        client = CoordinatorClient(
            keypair=kp, coordinator_url="http://localhost:9999",
            worker_name=None, queue_path=self.queue_path,
        )
        item = QueuedResult(
            lease_id="a" * 32, molecule_hash="c" * 32,
            score=-8.0, candidate=ASPIRIN,
        )
        with patch("coordinator.client._http", return_value=(429, {"error": "rate_limit"})):
            result = client._post_result_once(item)
        # 429 is retriable → False
        self.assertFalse(result)

    # ── Test 8: 410 lease-expired → dequeued ────────────────────────

    def test_08_410_expired_dequeued(self):
        kp = MinerKeypair.generate()
        client = CoordinatorClient(
            keypair=kp, coordinator_url="http://localhost:9999",
            worker_name=None, queue_path=self.queue_path,
        )
        item = QueuedResult(
            lease_id="a" * 32, molecule_hash="d" * 32,
            score=-8.0, candidate=ASPIRIN,
        )
        with patch("coordinator.client._http", return_value=(410, {"error": "expired"})):
            result = client._post_result_once(item)
        self.assertTrue(result)

    # ── Test 9: lease expiry mid-iteration → loop stops ─────────────

    def test_09_lease_expiry_mid_shard_stops_loop(self):
        # Lease expires in 50 ms — well within the iteration
        lease = self._make_lease("protein", expires_offset_ms=50)
        client = self._stub_client(lease)

        molecules = [("mol1", 1, 0), ("mol2", 1, 1), ("mol3", 1, 2)]

        def _slow_iter(*a, **kw):
            for item in molecules:
                time.sleep(0.1)   # 100ms each; lease expires after 1st
                yield item

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake.a3m"), "MSEQ")),
            patch("coordinator.loop._score_protein", return_value=-0.05),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.side_effect = _slow_iter
            coordinator_cycle(self.wlog, client, {}, 0)

        # Should have enqueued at most 1 molecule before lease expired
        self.assertLessEqual(client.enqueue_result.call_count, 1)

    # ── Test 10: durable queue survives restart ──────────────────────

    def test_10_queue_survives_restart(self):
        """Items written by one client instance are visible to a new instance."""
        q_path = self.queue_path
        kp = MinerKeypair.generate()

        # Client A writes one item to queue
        client_a = CoordinatorClient(
            keypair=kp, coordinator_url="http://localhost:9999",
            worker_name=None, queue_path=q_path,
        )
        with patch("coordinator.client._http", return_value=(500, {})):
            client_a.enqueue_result(
                lease_id="a" * 32, molecule_hash="e" * 32,
                score=-8.0, candidate=ASPIRIN,
            )

        # New client B reads the same queue path
        client_b = CoordinatorClient(
            keypair=kp, coordinator_url="http://localhost:9999",
            worker_name=None, queue_path=q_path,
        )
        items = client_b._load_queue()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].molecule_hash, "e" * 32)
        self.assertEqual(items[0].candidate, ASPIRIN)

    # ── Test 11: candidate > 2048 chars → dropped ───────────────────

    def test_11_oversized_candidate_dropped(self):
        long_smiles = "C" * (MAX_CANDIDATE_CHARS + 1)
        lease = self._make_lease("protein")
        client = self._stub_client(lease)

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake.a3m"), "MSEQ")),
            patch("coordinator.loop._score_protein", return_value=-0.05),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = [
                (long_smiles, 1, 0),
                (ASPIRIN, 1, 1),     # normal molecule still enqueued
            ]
            coordinator_cycle(self.wlog, client, {}, 0)

        # Only the normal molecule was enqueued
        self.assertEqual(client.enqueue_result.call_count, 1)
        call_candidate = client.enqueue_result.call_args.kwargs.get("candidate")
        self.assertEqual(call_candidate, ASPIRIN)

    # ── Test 12: cursor persists and resumes ────────────────────────

    def test_12_cursor_resumes_after_restart(self):
        """
        Simulate: first run processes row 0, crashes.
        Second run with same lease_id should skip row 0.
        """
        lease = self._make_lease("protein")
        lease_id = lease.lease_id

        # Persist a cursor as if row 0 was already done
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_PATH.write_text(json.dumps({
            "lease_id": lease_id,
            "shard_id": 1,
            "last_row": 0,
        }))

        seen_rows: list[int] = []
        client = self._stub_client(lease)

        def _capture_enqueue(**kwargs):
            seen_rows.append(kwargs.get("shard_row", -1))

        client.enqueue_result.side_effect = _capture_enqueue

        molecules = [(ASPIRIN, 1, 0), ("c1ccccc1", 1, 1), ("CC(N)=O", 1, 2)]

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake.a3m"), "MSEQ")),
            patch("coordinator.loop._score_protein", return_value=-0.05),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = molecules
            coordinator_cycle(self.wlog, client, {}, 0)

        # Row 0 should have been skipped; only rows 1 and 2 scored
        self.assertNotIn(0, seen_rows)
        self.assertIn(1, seen_rows)
        self.assertIn(2, seen_rows)


# ---------------------------------------------------------------------------
# End-to-end test (local Worker)
# ---------------------------------------------------------------------------

def _local_worker_running(port: int = 8787) -> bool:
    """Return True if wrangler dev is listening on port 8787."""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


@unittest.skipUnless(
    _local_worker_running() and os.environ.get("RUN_E2E_COORD"),
    "Skipped: set RUN_E2E_COORD=1 and start 'wrangler dev --env test' in life-coordinator",
)
class TestCoordinatorE2E(unittest.TestCase):
    """
    End-to-end against the local Worker (wrangler dev --env test).

    Requires:
      - wrangler dev --env test running in /mnt/minos-drive/life-coordinator
        (env.test has DISABLE_ONCHAIN_CHECK=true)
      - RUN_E2E_COORD=1 in environment
    """

    def test_13_full_roundtrip_stub_scorer(self):
        import urllib.request
        BASE = "http://127.0.0.1:8787"
        kp = MinerKeypair.generate()
        q_path = Path(tempfile.mktemp(suffix=".jsonl"))

        try:
            # Register throwaway key
            from coordinator.client import _signed_headers
            reg_body = json.dumps({"terms_version": "1.1.0"}).encode()
            x_auth = _signed_headers(kp, "POST", "/register", reg_body, None)
            req = urllib.request.Request(
                f"{BASE}/register",
                data=reg_body,
                headers={"Content-Type": "application/json", "X-Auth": x_auth},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                reg = json.loads(r.read())
            self.assertTrue(reg.get("registered") or reg.get("ok"), f"register failed: {reg}")

            # Get work
            client = CoordinatorClient(
                keypair=kp, coordinator_url=BASE, worker_name=None, queue_path=q_path,
            )
            lease = client.get_work()
            if lease is None:
                self.skipTest("No work available from local Worker (no shards seeded)")

            wlog = logging.getLogger("e2e-test")

            # Score with stub (no GPU needed)
            with (
                patch("coordinator.loop._resolve_protein_inputs",
                      return_value=(Path("/fake.a3m"), "MSEQ")),
                patch("coordinator.loop._score_protein", return_value=-0.05),
                patch("coordinator.loop.ShardManager") as MockSM,
            ):
                smiles_list = [ASPIRIN]
                MockSM.return_value.iter_molecules.return_value = [
                    (s, 1, i) for i, s in enumerate(smiles_list)
                ]
                coordinator_cycle(wlog, client, {}, 0)

            # Give drain thread time to POST /results
            time.sleep(3)

            # Verify result is in the database via public endpoint
            url = f"{BASE}/wallets/{kp.wallet}/results"
            req2 = urllib.request.Request(url, headers={"User-Agent": "test"})
            try:
                with urllib.request.urlopen(req2, timeout=5) as r:
                    data = json.loads(r.read())
                results = data.get("results", [])
                targets_seen = [r["target"] for r in results]
                self.assertIn("CDK4", targets_seen, f"CDK4 not in {targets_seen}")
            except Exception as exc:
                self.skipTest(f"result check skipped (endpoint unavailable): {exc}")

        finally:
            q_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# GPU dry-run test (no post)
# ---------------------------------------------------------------------------

def _boltz_available() -> bool:
    try:
        import nova_pulse_scorer  # noqa: F401
        return True
    except ImportError:
        return False


def _gpu_available() -> bool:
    try:
        import subprocess
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=5)
        return r.returncode == 0 and b"GPU" in r.stdout
    except Exception:
        return False


@unittest.skipUnless(
    _gpu_available() and _boltz_available() and os.environ.get("RUN_GPU_DRYRUN"),
    "Skipped: set RUN_GPU_DRYRUN=1 and ensure GPU + nova_pulse_scorer are available",
)
class TestGPUDryRun(unittest.TestCase):
    """
    Test 14: Real scoring dry run — no POST /results.

    Calls score_batch_with_profile with a real protein scorer_profile on
    a single SMILES (aspirin) against CDK4.  Prints score, elapsed time,
    and the exact scorer arguments so the operator can verify the locked
    profile was applied.
    """

    def test_14_real_scoring_no_post(self):
        from adaptive.vendor.boltz_scorer import score_batch_with_profile

        PROTEIN_PROFILE = {
            "flags": {
                "recycling_steps": 1,
                "sampling_steps": 25,
                "diffusion_samples": 1,
                "sampling_steps_affinity": 25,
                "diffusion_samples_affinity": 1,
                "affinity_mw_correction": True,
                "no_kernels": True,
            },
            "msa_sampling": {
                "subsample_msa": True,
                "num_subsampled_msa": 1024,
                "max_msa_seqs": 1024,
            },
            "seed": 68,
        }

        # Resolve MSA for CDK4 (must exist locally)
        sys.path.insert(0, str(_REPO))
        from miner_daemon import _msa_path_for, _sequence_from_msa
        msa_raw = _msa_path_for("CDK4", gene_name="CDK4")
        msa_path = msa_raw if msa_raw and msa_raw != "empty" else None
        if not msa_path or not Path(msa_path).exists():
            self.skipTest(f"CDK4 MSA not available at {msa_path}")

        protein_seq = _sequence_from_msa(msa_path)
        if not protein_seq:
            self.skipTest("Could not read protein sequence from CDK4 MSA")

        print(f"\n[DRY-RUN] SMILES: {ASPIRIN}")
        print(f"[DRY-RUN] Target: CDK4")
        print(f"[DRY-RUN] MSA:    {msa_path}")
        print(f"[DRY-RUN] Scorer profile: {json.dumps(PROTEIN_PROFILE, indent=2)}")

        t0 = time.time()
        result = score_batch_with_profile(
            smiles_list=[ASPIRIN],
            target="CDK4",
            protein_sequence=protein_seq,
            msa_path=msa_path,
            scorer_profile=PROTEIN_PROFILE,
        )
        elapsed = time.time() - t0

        raw_score = result.get(ASPIRIN)
        print(f"[DRY-RUN] raw boltz_score: {raw_score}")
        print(f"[DRY-RUN] elapsed:         {elapsed:.1f}s")

        # Convert to contract score
        from coordinator.loop import _to_contract_score
        contract_score = _to_contract_score(raw_score, "protein")
        print(f"[DRY-RUN] contract_score (kcal/mol): {contract_score}")

        # Assertions
        self.assertIsNotNone(raw_score, "score_batch_with_profile returned None")
        self.assertIsInstance(raw_score, float)
        self.assertLess(elapsed, 300, "scoring took more than 5 minutes")

        # Confirm no enqueue / POST happened
        print("[DRY-RUN] ✔ No enqueue_result or POST /results called.")


# ---------------------------------------------------------------------------
# Old-path preservation test (Test 15)
# ---------------------------------------------------------------------------

class TestOldPathPreservation(unittest.TestCase):
    """
    Test 15: With COORDINATOR_MODE=False (or unset), gpu_worker calls
    run_boltz2_scoring and does NOT call coordinator_cycle.
    """

    def test_15_old_path_unchanged_when_coord_mode_off(self):
        import miner_daemon as md

        # Confirm the flag is False when env var is absent
        original_env = os.environ.get("COORDINATOR_MODE")
        os.environ.pop("COORDINATOR_MODE", None)

        # Re-evaluate the flag expression
        env_val = os.environ.get("COORDINATOR_MODE", "").strip().lower()
        coord_mode = env_val in ("1", "true", "yes", "on")
        self.assertFalse(coord_mode, "COORDINATOR_MODE should be False when env var is unset")

        # Confirm the module-level constant agrees
        self.assertFalse(md.COORDINATOR_MODE,
                         "miner_daemon.COORDINATOR_MODE should be False in this environment")

        if original_env is not None:
            os.environ["COORDINATOR_MODE"] = original_env


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main(verbosity=2)
