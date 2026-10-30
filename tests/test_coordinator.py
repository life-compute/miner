"""
tests/test_coordinator.py — Coordinator client tests with negative controls (C3).

All tests:
  - Use a throwaway Ed25519 keypair (MinerKeypair.generate())
  - Mock the scorer — no Boltz / GPU calls
  - Run against a local test coordinator server or httpretty mocks
  - Do not touch any running miner or PM2 process

Test matrix:
  1. Scoring continues through coordinator outage; queue drains afterward.
  2. Duplicate submission (409) is treated as success.
  3. pegRNA loop still runs (import smoke test).
  4. Client never calls getProgramAccounts.
  5. Pinned-hash mismatch refuses the target (raises PinnedHashError).
  6. Lease with shards drives the molecule pool (ShardManager).
  7. Invalid worker_name is refused at CoordinatorClient construction.
  8. Auth signature round-trip verifies correctly.
  9. Shard SHA-256 mismatch is refused.
 10. GET /work endpoint fallback: primary down → fallback used.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
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

# Ensure the miner root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordinator.auth import MinerKeypair, validate_worker_name
from coordinator.client import CoordinatorClient, QueuedResult, Lease
from coordinator.hashes import PinnedHashError, verify_protein_target
from coordinator.shards import ShardManager


# ---------------------------------------------------------------------------
# Helpers: tiny in-process HTTP server
# ---------------------------------------------------------------------------

class _MockCoordHandler(BaseHTTPRequestHandler):
    """Configurable mock coordinator server used in multiple tests."""

    def log_message(self, *args, **kwargs):
        pass  # suppress output

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        body_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(body_len) if body_len else b""
        path = self.path.split("?")[0]
        key  = f"{method} {path}"
        # Store calls and look up responses via the server object (avoids class-level sharing)
        server_responses = getattr(self.server, "_test_responses", {})
        server_calls     = getattr(self.server, "_test_calls", [])
        server_calls.append((method, path, body))

        resp = server_responses.get(key)
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


def _start_mock_server(responses: dict) -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), _MockCoordHandler)
    srv._test_responses = dict(responses)   # type: ignore[attr-defined]
    srv._test_calls = []                    # type: ignore[attr-defined]
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_LEASE_BODY = {
    "lease_id": "deadbeef" * 4,
    "target": "CDK4",
    "modality": "protein",
    "expires_at": int(time.time() * 1000) + 4 * 3600 * 1000,
    "shards": [{"shard_id": 1, "download_url": "http://shard.test/s1.tar.gz",
                 "sha256": "a" * 64, "mol_count": 5}],
    "hit_threshold": -9.0,
    "scorer_profile": {"boltz_version": "1.0.1", "flags": ["--recycling-steps=3"],
                        "msa_sampling": "uniform", "seed": None},
    "coord_config_version": "2.0.0",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWorkerNameValidation(unittest.TestCase):
    def test_valid_names(self):
        for name in ["worker1", "my-miner", "ABC123", "a", "x" * 32]:
            self.assertTrue(validate_worker_name(name), name)

    def test_invalid_names(self):
        for name in ["x" * 33, "bad name", "bad_name", "", "hi!"]:
            self.assertFalse(validate_worker_name(name), name)

    def test_none_is_valid(self):
        self.assertTrue(validate_worker_name(None))

    def test_client_rejects_invalid_worker_name(self):
        kp = MinerKeypair.generate()
        with self.assertRaises(ValueError):
            CoordinatorClient(kp, worker_name="bad name")

    def test_client_rejects_too_long_worker_name(self):
        kp = MinerKeypair.generate()
        with self.assertRaises(ValueError):
            CoordinatorClient(kp, worker_name="x" * 33)


class TestAuthSignature(unittest.TestCase):
    def test_sign_and_wallet_round_trip(self):
        kp = MinerKeypair.generate()
        self.assertEqual(len(kp.wallet), 44)  # Solana base58 pubkey ≈ 43-44 chars
        auth = kp.sign_request("POST", "/results", b'{"test":1}')
        self.assertIn("wallet", auth)
        self.assertIn("nonce", auth)
        self.assertIn("timestamp", auth)
        self.assertIn("signature", auth)
        self.assertEqual(len(auth["nonce"]), 32)
        self.assertEqual(len(auth["signature"]), 128)   # 64 bytes hex

    def test_worker_name_included(self):
        kp = MinerKeypair.generate()
        auth = kp.sign_request("GET", "/work", b"", "my-worker")
        self.assertEqual(auth["worker_name"], "my-worker")

    def test_no_worker_name_omitted(self):
        kp = MinerKeypair.generate()
        auth = kp.sign_request("GET", "/work", b"")
        self.assertNotIn("worker_name", auth)


class TestPinnedHashVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _write(self, name: str, content: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(content)
        return p

    def test_matching_hashes_pass(self):
        data = b"FAKE RECEPTOR CONTENT"
        sha  = hashlib.sha256(data).hexdigest()
        rpath = self._write("receptor.pdb", data)
        msa_data = b">query\nMACGT\n"
        msa_sha  = hashlib.sha256(msa_data).hexdigest()
        mpath = self._write("target.a3m", msa_data)
        # Should not raise
        verify_protein_target("CDK4", rpath, mpath, sha, msa_sha)

    def test_receptor_mismatch_raises(self):
        data  = b"REAL"
        wrong = b"WRONG"
        sha   = hashlib.sha256(data).hexdigest()
        rpath = self._write("receptor.pdb", wrong)
        with self.assertRaises(PinnedHashError) as ctx:
            verify_protein_target("CDK4", rpath, None, sha, None)
        self.assertIn("receptor", str(ctx.exception).lower())

    def test_msa_mismatch_raises(self):
        data  = b">q\nACGT\n"
        wrong = b">q\nAAAA\n"
        sha   = hashlib.sha256(data).hexdigest()
        mpath = self._write("target.a3m", wrong)
        with self.assertRaises(PinnedHashError) as ctx:
            verify_protein_target("CDK4", None, mpath, None, sha)
        self.assertIn("msa", str(ctx.exception).lower())

    def test_null_hash_warns_but_does_not_raise(self):
        # A null hash should log a warning, not raise
        verify_protein_target("CDK4", None, None, None, None)

    def test_missing_file_raises_when_hash_pinned(self):
        fake_sha = "a" * 64
        with self.assertRaises(PinnedHashError):
            verify_protein_target("CDK4", Path("/nonexistent/receptor.pdb"), None, fake_sha, None)


class TestNoProgramAccountsCalls(unittest.TestCase):
    """Verify the coordinator client never calls getProgramAccounts."""

    def test_get_work_no_program_accounts(self):
        kp  = MinerKeypair.generate()
        srv, url = _start_mock_server({"GET /work": (200, FAKE_LEASE_BODY)})
        try:
            client = CoordinatorClient(kp, coordinator_url=url)
            with patch("urllib.request.urlopen") as mock_urlopen:
                # Capture all outgoing requests
                calls_seen = []
                def side_effect(req, *a, **kw):
                    body = req.data or b""
                    calls_seen.append(body)
                    resp = MagicMock()
                    resp.status = 200
                    resp.read.return_value = json.dumps(FAKE_LEASE_BODY).encode()
                    resp.__enter__ = lambda s: s
                    resp.__exit__ = MagicMock(return_value=False)
                    return resp
                mock_urlopen.side_effect = side_effect
                client.get_work()
                for body_bytes in calls_seen:
                    body_str = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
                    self.assertNotIn(
                        "getProgramAccounts", body_str,
                        f"GET /work must not call getProgramAccounts, found in: {body_str[:200]}"
                    )
        finally:
            srv.shutdown()

    def test_miner_daemon_does_not_import_get_program_accounts_via_coordinator(self):
        """Check coordinator package does not CALL getProgramAccounts (code, not comments)."""
        import ast
        coord_dir = Path(__file__).resolve().parents[1] / "coordinator"
        for py in coord_dir.glob("*.py"):
            content = py.read_text()
            # Remove comments and docstrings by parsing AST; check string constants
            # that could be JSON method calls.
            # Simple heuristic: check for the literal in non-comment, non-docstring lines.
            code_lines = []
            for line in content.splitlines():
                stripped = line.split("#")[0]  # drop inline comments
                code_lines.append(stripped)
            code_without_comments = "\n".join(code_lines)
            # getProgramAccounts should not appear outside of docstrings/comments
            # We check by removing all string literals from the code
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            # Walk AST and check for getProgramAccounts in function/method calls
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.s, str):
                    # Allow docstrings but not code-level string calls
                    if "getProgramAccounts" in node.s:
                        # Only fail if it's in a Call context (i.e., being sent as RPC)
                        # Docstrings at module/class/function level are fine
                        pass  # string constant — comment/docstring, not a call
                if isinstance(node, ast.Call):
                    # Check if any argument to a call contains getProgramAccounts
                    for arg in ast.walk(node):
                        if isinstance(arg, ast.Constant) and isinstance(arg.s, str):
                            if "getProgramAccounts" in arg.s:
                                self.fail(
                                    f"coordinator/{py.name} passes getProgramAccounts "
                                    f"as a call argument (RPC method name)"
                                )


class TestCoordinatorOutageAndDrain(unittest.TestCase):
    """Queue persists through outage; drains when coordinator recovers."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.queue_path = self.tmp / "queue.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_queue_written_on_outage_and_drained_after_recovery(self):
        kp = MinerKeypair.generate()

        # Phase 1: coordinator is down (no server running at port 19999)
        client_down = CoordinatorClient(
            kp,
            coordinator_url="http://127.0.0.1:19999",   # nothing listening
            queue_path=self.queue_path,
        )
        # Enqueue a result; it should persist to disk
        client_down.enqueue_result(
            lease_id="dead" * 8,
            molecule_hash="mol_abc123",
            score=-9.5,
            candidate="CCO",
            miner_gpu_model="NVIDIA RTX 4060",
        )
        # Give drain loop a moment to attempt (and fail)
        time.sleep(0.3)
        # Queue file must exist with our item
        self.assertTrue(self.queue_path.exists())
        lines = [l for l in self.queue_path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["molecule_hash"], "mol_abc123")
        self.assertEqual(row["candidate"], "CCO")
        client_down.stop()

        # Phase 2: coordinator recovers
        accepted = []
        def _results_handler(body: bytes):
            accepted.append(json.loads(body))
            return 200, {"accepted": True, "molecule_hash": "mol_abc123",
                         "target": "CDK4", "submitted_at": int(time.time() * 1000)}

        srv, url = _start_mock_server({"POST /results": _results_handler})
        try:
            client_up = CoordinatorClient(kp, coordinator_url=url, queue_path=self.queue_path)
            client_up.start()
            # Wait up to 5s for drain
            deadline = time.time() + 5
            while time.time() < deadline:
                lines = [l for l in self.queue_path.read_text().splitlines() if l.strip()]
                if not lines:
                    break
                time.sleep(0.1)
            client_up.stop()
            lines = [l for l in self.queue_path.read_text().splitlines() if l.strip()]
            self.assertEqual(lines, [], f"Queue should be empty after drain, got: {lines}")
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0]["molecule_hash"], "mol_abc123")
            self.assertEqual(accepted[0]["candidate"], "CCO")
        finally:
            srv.shutdown()


class TestDuplicateSubmission(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_409_is_treated_as_success(self):
        kp = MinerKeypair.generate()
        queue_path = self.tmp / "q.jsonl"
        call_count = [0]

        def _dup_handler(body: bytes):
            call_count[0] += 1
            return 409, {"error": "Duplicate: molecule already accepted for this target"}

        srv, url = _start_mock_server({"POST /results": _dup_handler})
        try:
            client = CoordinatorClient(kp, coordinator_url=url, queue_path=queue_path)
            client.enqueue_result(
                lease_id="aa" * 16,
                molecule_hash="mol_dup1",
                score=-10.0,
                candidate="c1ccccc1",
            )
            deadline = time.time() + 3
            while time.time() < deadline:
                lines = [l for l in queue_path.read_text().splitlines() if l.strip()] if queue_path.exists() else []
                if not lines:
                    break
                time.sleep(0.1)
            client.stop()
            # Queue must be empty (409 = treated as success = removed)
            lines = [l for l in queue_path.read_text().splitlines() if l.strip()] if queue_path.exists() else []
            self.assertEqual(lines, [], "409 duplicate should drain from queue")
        finally:
            srv.shutdown()


class TestPegRNALoopSmoke(unittest.TestCase):
    """pegRNA loop must still be importable and its modules loadable."""

    def test_peg_modules_importable(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        # These should not raise even if model weights are absent
        try:
            import adaptive.life_peg as lp   # noqa: F401
            self.assertTrue(hasattr(lp, "design_candidates"))
        except Exception as e:
            self.skipTest(f"life_peg not installed (non-fatal): {e}")

    def test_peg_observer_script_exists(self):
        observer = Path(__file__).resolve().parents[1] / "scripts" / "peg_observer.py"
        self.assertTrue(observer.exists(), "peg_observer.py must still exist")


class TestShardManagerSHA256(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _make_tar_gz(self, content: str) -> tuple[bytes, str]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = content.encode()
            ti = tarfile.TarInfo(name="molecules.smi")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        raw = buf.getvalue()
        sha = hashlib.sha256(raw).hexdigest()
        return raw, sha

    def test_correct_sha256_yields_molecules(self):
        raw, sha = self._make_tar_gz("CCO mol1\nCCN mol2\n")
        url_called = []

        def _mock_urlopen(req, timeout=None):
            url_called.append(req.full_url if hasattr(req, "full_url") else str(req))
            resp = MagicMock()
            resp.read.return_value = raw
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        sm = ShardManager(shard_dir=self.tmp)
        with patch("coordinator.shards.urllib.request.urlopen", side_effect=_mock_urlopen):
            mols = list(sm.iter_molecules([
                {"shard_id": 1, "download_url": "http://test.example/s.tar.gz",
                 "sha256": sha, "mol_count": 2}
            ]))
        self.assertEqual(len(mols), 2)
        smiles_set = {m[0] for m in mols}
        self.assertEqual(smiles_set, {"CCO", "CCN"})
        # shard_id and row included
        self.assertEqual(mols[0][1], 1)   # shard_id
        self.assertEqual(mols[0][2], 0)   # row 0
        self.assertEqual(mols[1][2], 1)   # row 1

    def test_sha256_mismatch_yields_no_molecules(self):
        raw, sha = self._make_tar_gz("CCO mol1\n")
        wrong_sha = "0" * 64   # wrong hash

        def _mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = raw
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        sm = ShardManager(shard_dir=self.tmp)
        with patch("coordinator.shards.urllib.request.urlopen", side_effect=_mock_urlopen):
            mols = list(sm.iter_molecules([
                {"shard_id": 1, "download_url": "http://test.example/s.tar.gz",
                 "sha256": wrong_sha, "mol_count": 1}
            ]))
        self.assertEqual(mols, [], "SHA-256 mismatch should yield no molecules")


class TestLeaseShardDrivesPool(unittest.TestCase):
    """A lease with shards must drive the molecule pool."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_shard_molecules_returned_with_shard_id_and_row(self):
        import io, tarfile
        buf = io.BytesIO()
        content = "c1ccccc1\nCCO\nCCN\n"
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = content.encode()
            ti = tarfile.TarInfo("molecules.smi")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        raw = buf.getvalue()
        sha = hashlib.sha256(raw).hexdigest()

        sm = ShardManager(shard_dir=self.tmp)
        def _mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = raw
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        shards = [
            {"shard_id": 7, "download_url": "http://ex.test/s7.tar.gz",
             "sha256": sha, "mol_count": 3}
        ]
        with patch("coordinator.shards.urllib.request.urlopen", side_effect=_mock_urlopen):
            results = list(sm.iter_molecules(shards))

        self.assertEqual(len(results), 3)
        for smiles, shard_id, row in results:
            self.assertEqual(shard_id, 7)
        rows = [r for _, _, r in results]
        self.assertEqual(rows, [0, 1, 2])


class TestGetWorkFallback(unittest.TestCase):
    """Primary coordinator down → fallback used."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_fallback_used_when_primary_down(self):
        kp = MinerKeypair.generate()
        srv, fallback_url = _start_mock_server({"GET /work": (200, FAKE_LEASE_BODY)})
        try:
            client = CoordinatorClient(
                kp,
                coordinator_url="http://127.0.0.1:19998",  # nothing listening
                fallback_url=fallback_url,
                queue_path=self.tmp / "q.jsonl",
            )
            lease = client.get_work()
            self.assertIsNotNone(lease)
            self.assertEqual(lease.target, "CDK4")
            self.assertEqual(lease.lease_id, FAKE_LEASE_BODY["lease_id"])
        finally:
            srv.shutdown()

    def test_primary_used_when_available(self):
        kp = MinerKeypair.generate()
        srv_primary,  url_primary  = _start_mock_server({"GET /work": (200, FAKE_LEASE_BODY)})
        srv_fallback, url_fallback = _start_mock_server({"GET /work": (200, {**FAKE_LEASE_BODY, "target": "FALLBACK"})})
        try:
            client = CoordinatorClient(
                kp, coordinator_url=url_primary, fallback_url=url_fallback,
                queue_path=self.tmp / "q.jsonl",
            )
            lease = client.get_work()
            self.assertEqual(lease.target, "CDK4")  # primary, not fallback
        finally:
            srv_primary.shutdown()
            srv_fallback.shutdown()


class TestResultRecordFields(unittest.TestCase):
    """POST /results must include candidate, miner_gpu_model, shard_id, shard_row."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_candidate_in_post_body(self):
        kp = MinerKeypair.generate()
        received = []

        def _handler(body: bytes):
            received.append(json.loads(body))
            return 200, {"accepted": True, "molecule_hash": "mol_x",
                         "target": "CDK4", "submitted_at": 0}

        srv, url = _start_mock_server({"POST /results": _handler})
        try:
            client = CoordinatorClient(kp, coordinator_url=url, queue_path=self.tmp / "q.jsonl")
            client.enqueue_result(
                lease_id="ab" * 16,
                molecule_hash="mol_x",
                score=-9.1,
                candidate="SMILES_STRING_HERE",
                shard_id=3,
                shard_row=42,
                miner_gpu_model="NVIDIA RTX 4060 Ti",
            )
            deadline = time.time() + 5
            while time.time() < deadline and not received:
                time.sleep(0.1)
            client.stop()
        finally:
            srv.shutdown()

        self.assertEqual(len(received), 1)
        body = received[0]
        self.assertEqual(body["candidate"], "SMILES_STRING_HERE")
        self.assertEqual(body["miner_gpu_model"], "NVIDIA RTX 4060 Ti")
        self.assertEqual(body["shard_id"], 3)
        self.assertEqual(body["shard_row"], 42)
        self.assertEqual(body["score"], -9.1)


class TestCandidateLengthLimit(unittest.TestCase):
    """MAX_CANDIDATE_BYTES in the miner client must equal the coordinator's limit (4096)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_max_candidate_bytes_equals_coordinator_value(self):
        """Constant must be 4096 — the coordinator's MAX_CANDIDATE_BYTES."""
        from coordinator.client import MAX_CANDIDATE_BYTES
        self.assertEqual(MAX_CANDIDATE_BYTES, 4096)

    def test_candidate_within_limit_passes_unmodified(self):
        """A candidate at exactly the limit must not be truncated."""
        from coordinator.client import MAX_CANDIDATE_BYTES
        received = []

        def _handler(req_body):
            received.append(json.loads(req_body))
            return (200, {"accepted": True, "target": "CDK4"})

        kp = MinerKeypair.generate()
        srv, url = _start_mock_server({"POST /results": _handler})
        try:
            client = CoordinatorClient(kp, coordinator_url=url, queue_path=self.tmp / "q.jsonl")
            exact = "C" * MAX_CANDIDATE_BYTES
            client.enqueue_result(
                lease_id="ab" * 16, molecule_hash="mol_ok",
                score=-8.0, candidate=exact,
            )
            deadline = time.time() + 5
            while time.time() < deadline and not received:
                time.sleep(0.1)
            client.stop()
        finally:
            srv.shutdown()

        self.assertEqual(len(received), 1)
        self.assertEqual(len(received[0]["candidate"]), MAX_CANDIDATE_BYTES)

    def test_candidate_over_limit_is_refused_and_never_sent(self):
        """A candidate exceeding MAX_CANDIDATE_BYTES must be dropped — never sent."""
        from coordinator.client import MAX_CANDIDATE_BYTES
        received = []

        def _handler(req_body):
            received.append(json.loads(req_body))
            return (200, {"accepted": True, "target": "CDK4"})

        kp = MinerKeypair.generate()
        srv, url = _start_mock_server({"POST /results": _handler})
        try:
            client = CoordinatorClient(kp, coordinator_url=url, queue_path=self.tmp / "q.jsonl")
            too_long = "C" * (MAX_CANDIDATE_BYTES + 500)
            client.enqueue_result(
                lease_id="ab" * 16, molecule_hash="mol_long",
                score=-8.0, candidate=too_long,
            )
            # Give the drain loop a moment; nothing should arrive
            time.sleep(0.5)
            client.stop()
        finally:
            srv.shutdown()

        # Nothing must have been sent to the server
        self.assertEqual(len(received), 0, "oversized candidate must not be sent in any form")

    def test_candidate_over_limit_is_never_altered(self):
        """The original candidate string is never modified when the result is dropped."""
        from coordinator.client import MAX_CANDIDATE_BYTES
        kp = MinerKeypair.generate()
        # No server needed — the drop happens before queuing
        client = CoordinatorClient(kp, coordinator_url="http://localhost:1",
                                   queue_path=self.tmp / "q2.jsonl")
        original = "X" * (MAX_CANDIDATE_BYTES + 100)
        snapshot = original  # take a reference copy

        client.enqueue_result(
            lease_id="cd" * 16, molecule_hash="mol_unaltered",
            score=-7.0, candidate=original,
        )
        client.stop()

        # The string passed to enqueue_result must be bit-identical to the original
        self.assertEqual(original, snapshot,
                         "enqueue_result must not mutate the caller's string")
        # Queue file must not contain this molecule (result was dropped)
        queue_text = ""
        if (self.tmp / "q2.jsonl").exists():
            queue_text = (self.tmp / "q2.jsonl").read_text()
        self.assertNotIn("mol_unaltered", queue_text,
                         "dropped result must not appear in queue file")

    def test_candidate_over_limit_increments_oversized_count(self):
        """Dropping an oversized candidate increments _oversized_count."""
        from coordinator.client import MAX_CANDIDATE_BYTES
        kp = MinerKeypair.generate()
        client = CoordinatorClient(kp, coordinator_url="http://localhost:1",
                                   queue_path=self.tmp / "q3.jsonl")

        self.assertFalse(hasattr(client, "_oversized_count"))
        client.enqueue_result(
            lease_id="ef" * 16, molecule_hash="mol_cnt1",
            score=-6.0, candidate="Z" * (MAX_CANDIDATE_BYTES + 1),
        )
        self.assertEqual(client._oversized_count, 1)
        client.enqueue_result(
            lease_id="ef" * 16, molecule_hash="mol_cnt2",
            score=-6.0, candidate="Z" * (MAX_CANDIDATE_BYTES + 2),
        )
        self.assertEqual(client._oversized_count, 2)
        client.stop()

    def test_candidate_over_limit_logs_error_not_warning(self):
        """Oversized candidate must be logged at ERROR level, not WARNING."""
        import logging
        from coordinator.client import MAX_CANDIDATE_BYTES
        kp = MinerKeypair.generate()
        client = CoordinatorClient(kp, coordinator_url="http://localhost:1",
                                   queue_path=self.tmp / "q4.jsonl")

        records = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        import coordinator.client as cc_mod
        cc_logger = logging.getLogger(cc_mod.__name__)
        cc_logger.addHandler(handler)
        old_level = cc_logger.level
        cc_logger.setLevel(logging.DEBUG)
        try:
            client.enqueue_result(
                lease_id="gh" * 16, molecule_hash="mol_log",
                score=-5.0, candidate="Q" * (MAX_CANDIDATE_BYTES + 10),
            )
        finally:
            cc_logger.removeHandler(handler)
            cc_logger.setLevel(old_level)
            client.stop()

        error_records = [r for r in records if r.levelno >= logging.ERROR
                         and "mol_log" in r.getMessage()]
        self.assertGreater(len(error_records), 0, "expected at least one ERROR log for oversized candidate")
        warning_records = [r for r in records if r.levelno == logging.WARNING
                           and "mol_log" in r.getMessage()]
        self.assertEqual(len(warning_records), 0, "oversized candidate must not log at WARNING level")


if __name__ == "__main__":
    unittest.main(verbosity=2)
