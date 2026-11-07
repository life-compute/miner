"""
tests/test_c48_loop.py  –  C48: GPU OOM handling, capability headers, 503 backoff.

Must FAIL on the parent commit (before miner changes) and PASS on the new code.

Tests:
  i.   Systemic GPU OOM on first molecules → exactly ONE POST /leases/{id}/unfit
       with correct body fields; signature verifies; lease abandoned; loop moves on.
  ii.  Next GET /work carries X-GPU-Memory-MB, X-GPU-Model, X-Miner-Version.
  iii. 503 no_feasible_target → loop waits retry_after and retries without exiting.
  iv.  Failed unfit POST is retried; abandon is not lost.
  v.   Per-molecule 'Failed to process' error does NOT produce an unfit report.

Each test shows FAIL/PASS by using the real coordinator_cycle path with a mock
HTTP server that records all calls.
"""
from __future__ import annotations

import hashlib
import io
import json
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

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from coordinator.auth import MinerKeypair
from coordinator.client import CoordinatorClient, Lease, NoFeasibleTargetError
from coordinator.loop import coordinator_cycle, _clear_cursor, GpuOomError, _reset_backoff

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
LEASE_ID = "c" * 32
TARGET = "ESR1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_shard_archive(smiles_list: list[str]) -> bytes:
    content = "\n".join(smiles_list).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("molecules.smi")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_lease(
    target: str = TARGET,
    modality: str = "protein",
    smiles: Optional[list[str]] = None,
    expires_offset_ms: int = 4 * 3600 * 1000,
) -> Lease:
    if smiles is None:
        smiles = [ASPIRIN, "c1ccccc1", "CC(N)=O"]
    return Lease(
        lease_id=LEASE_ID,
        target=target,
        modality=modality,
        expires_at=int(time.time() * 1000) + expires_offset_ms,
        shards=[],
        hit_threshold=-8.0,
        scorer_profile={"flags": {}, "msa_sampling": {}, "seed": 68},
        coord_config_version="2.0.0",
    )


class _Handler(BaseHTTPRequestHandler):
    """Mock HTTP handler: looks up responses by 'METHOD /path'."""

    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        key = f"{method} {path}"
        calls = getattr(self.server, "_calls", [])
        # Store raw request headers too for capability header checks
        hdrs = dict(self.headers)
        calls.append({"method": method, "path": path, "body": body, "headers": hdrs})
        resps = getattr(self.server, "_responses", {})
        resp = resps.get(key)
        if resp is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return
        if callable(resp):
            status, data = resp(body, getattr(self.server, "_calls", []))
        else:
            status, data = resp
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start_mock_server(responses: dict):
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv._responses = responses  # type: ignore[attr-defined]
    srv._calls = []             # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _make_client(
    base_url: str,
    queue_path: Path,
    keypair: Optional[MinerKeypair] = None,
    gpu_memory_mb: int = 8192,
    gpu_model: Optional[str] = "NVIDIA GeForce RTX 5060",
    miner_version: Optional[str] = "2.0",
) -> CoordinatorClient:
    kp = keypair or MinerKeypair.generate()
    return CoordinatorClient(
        keypair=kp,
        coordinator_url=base_url,
        worker_name=None,
        queue_path=queue_path,
        gpu_memory_mb=gpu_memory_mb,
        gpu_model=gpu_model,
        miner_version=miner_version,
    )


def _verify_signature(auth_header_json: str, method: str, path: str, body_bytes: bytes) -> bool:
    """Verify Ed25519 signature using the same message construction as client."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from coordinator.auth import b58decode

    auth = json.loads(auth_header_json)
    nonce = auth["nonce"]
    timestamp = auth["timestamp"]
    signature_hex = auth["signature"]
    wallet = auth["wallet"]

    body_hash = hashlib.sha256(body_bytes).hexdigest()
    msg = f"{method}\n{path}\n{body_hash}\n{nonce}\n{timestamp}"

    # wallet is the Solana-compatible base58-encoded 32-byte Ed25519 public key
    pub_bytes = b58decode(wallet)
    pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes[:32])
    sig_bytes = bytes.fromhex(signature_hex)
    try:
        pub_key.verify(sig_bytes, msg.encode())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Test i: systemic GPU OOM → exactly ONE POST /unfit with correct body
# ---------------------------------------------------------------------------

class TestGpuOomUnfitReport(unittest.TestCase):
    """Test i: GPU OOM → single unfit POST, correct body, signature verifies,
    lease abandoned, loop continues."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.jsonl"
        _clear_cursor()
        _reset_backoff()
        self.wlog = __import__("logging").getLogger("test-c48-oom")

    def tearDown(self):
        self.tmp.cleanup()
        _clear_cursor()
        _reset_backoff()

    def _run_with_oom_on_first_molecule(self, mock_work_resp, expect_unfit_call: bool):
        """Core helper: run coordinator_cycle with an OOM on the first molecule."""
        lease = _make_lease()

        unfit_calls = []
        work_calls_after_first = []

        def _work_resp(body, calls):
            # First call: return a lease; subsequent: return empty
            work_hits = sum(1 for c in calls if c["path"] == "/work" and c["method"] == "GET")
            if work_hits <= 1:
                return mock_work_resp
            return 404, {"error": "no work"}

        responses = {
            "GET /work": mock_work_resp,
            f"POST /leases/{LEASE_ID}/unfit": (200, {"ok": True}),
            "POST /results": (200, {"ok": True}),
        }

        srv, base = _start_mock_server(responses)
        kp = MinerKeypair.generate()
        client = _make_client(base, self.queue_path, keypair=kp)

        try:
            with (
                patch("coordinator.loop._resolve_protein_inputs",
                      return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
                patch("coordinator.loop._score_protein",
                      side_effect=GpuOomError("CUDA out of memory. Tried to allocate 2.5 GiB")),
                patch("coordinator.loop.ShardManager") as MockSM,
            ):
                MockSM.return_value.iter_molecules.return_value = [
                    (ASPIRIN, 1, 0),
                ]
                coordinator_cycle(self.wlog, client, {}, 0)

            # Count unfit calls
            unfit_calls = [c for c in srv._calls
                           if c["path"] == f"/leases/{LEASE_ID}/unfit" and c["method"] == "POST"]
            work_calls = [c for c in srv._calls if c["path"] == "/work" and c["method"] == "GET"]
            return unfit_calls, work_calls, kp
        finally:
            srv.shutdown()

    def test_i_exactly_one_unfit_post_with_correct_body(self):
        """GPU OOM on first molecule → exactly 1 POST /unfit, body correct, sig verifies."""
        lease_resp = (200, {
            "lease_id": LEASE_ID,
            "target": TARGET,
            "modality": "protein",
            "expires_at": int(time.time() * 1000) + 4 * 3600 * 1000,
            "shards": [],
            "hit_threshold": -8.0,
            "scorer_profile": {"flags": {}, "msa_sampling": {}, "seed": 68},
            "coord_config_version": "2.0.0",
        })

        unfit_calls, work_calls, kp = self._run_with_oom_on_first_molecule(lease_resp, True)

        # Must be exactly one unfit call
        self.assertEqual(len(unfit_calls), 1,
                         f"Expected exactly 1 unfit POST, got {len(unfit_calls)}")
        body_bytes = unfit_calls[0]["body"]
        body = json.loads(body_bytes)

        # Required fields
        self.assertEqual(body["target"], TARGET)
        self.assertEqual(body["reason"], "gpu_oom")
        self.assertIsInstance(body["gpu_memory_mb"], int)
        self.assertGreater(body["gpu_memory_mb"], 0)
        self.assertIn("gpu_model", body)
        self.assertIsInstance(body["molecules_attempted"], int)
        error_field = body.get("error", "")
        self.assertLessEqual(len(error_field), 200,
                             f"error field length {len(error_field)} exceeds 200")
        self.assertIn("memory", error_field.lower())

        # Signature must verify
        auth_header = unfit_calls[0]["headers"].get("X-Auth") or unfit_calls[0]["headers"].get("x-auth")
        self.assertIsNotNone(auth_header, "X-Auth header missing from unfit POST")
        path = f"/leases/{LEASE_ID}/unfit"
        ok = _verify_signature(auth_header, "POST", path, body_bytes)
        self.assertTrue(ok, "X-Auth signature on unfit POST failed to verify")

    def test_i_lease_abandoned_immediately(self):
        """After OOM, no further molecule scoring happens on the same lease."""
        lease_resp = (200, {
            "lease_id": LEASE_ID,
            "target": TARGET,
            "modality": "protein",
            "expires_at": int(time.time() * 1000) + 4 * 3600 * 1000,
            "shards": [],
            "hit_threshold": -8.0,
            "scorer_profile": {"flags": {}, "msa_sampling": {}, "seed": 68},
            "coord_config_version": "2.0.0",
        })

        score_calls = []
        srv, base = _start_mock_server({
            "GET /work": lease_resp,
            f"POST /leases/{LEASE_ID}/unfit": (200, {"ok": True}),
            "POST /results": (200, {"ok": True}),
        })
        kp = MinerKeypair.generate()
        client = _make_client(base, self.queue_path, keypair=kp)
        try:
            call_count = {"n": 0}

            def _score_side_effect(*a, **kw):
                call_count["n"] += 1
                raise GpuOomError("CUDA out of memory")

            with (
                patch("coordinator.loop._resolve_protein_inputs",
                      return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
                patch("coordinator.loop._score_protein", side_effect=_score_side_effect),
                patch("coordinator.loop.ShardManager") as MockSM,
            ):
                # Three molecules in the shard
                MockSM.return_value.iter_molecules.return_value = [
                    (ASPIRIN, 1, 0),
                    ("c1ccccc1", 1, 1),
                    ("CC(N)=O", 1, 2),
                ]
                coordinator_cycle(self.wlog, client, {}, 0)

            # Only the FIRST molecule should have been attempted
            self.assertEqual(call_count["n"], 1,
                             f"Expected 1 score call (OOM on first), got {call_count['n']}")
        finally:
            srv.shutdown()


# ---------------------------------------------------------------------------
# Test ii: GET /work carries capability headers
# ---------------------------------------------------------------------------

class TestCapabilityHeaders(unittest.TestCase):
    """Test ii: GET /work includes X-GPU-Memory-MB, X-GPU-Model, X-Miner-Version.

    Tests the real CoordinatorClient.get_work() against a mock server to confirm
    capability headers are sent. Using client directly (not coordinator_cycle) since
    we're testing the HTTP request shape, not the cycle orchestration.
    """

    def test_ii_get_work_has_capability_headers(self):
        """GET /work request must carry X-GPU-Memory-MB, X-GPU-Model, X-Miner-Version."""
        responses = {
            "GET /work": (200, {
                "lease_id": LEASE_ID,
                "target": TARGET,
                "modality": "protein",
                "expires_at": int(time.time() * 1000) + 4 * 3600 * 1000,
                "shards": [],
                "hit_threshold": -8.0,
                "scorer_profile": {"flags": {}, "msa_sampling": {}, "seed": 68},
                "coord_config_version": "2.0.0",
            }),
        }
        srv, base = _start_mock_server(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                queue_path = Path(tmp) / "queue.jsonl"
                client = _make_client(
                    base, queue_path,
                    gpu_memory_mb=8192,
                    gpu_model="NVIDIA GeForce RTX 5060",
                    miner_version="2.0",
                )
                lease = client.get_work()
                self.assertIsNotNone(lease, "get_work() returned None")

            work_calls = [c for c in srv._calls if c["path"] == "/work" and c["method"] == "GET"]
            self.assertGreater(len(work_calls), 0, "No GET /work call recorded")

            hdrs = {k.lower(): v for k, v in work_calls[0]["headers"].items()}
            self.assertIn("x-gpu-memory-mb", hdrs,
                          f"X-GPU-Memory-MB missing from GET /work. Got: {list(hdrs.keys())}")
            self.assertEqual(hdrs["x-gpu-memory-mb"], "8192")

            self.assertIn("x-gpu-model", hdrs,
                          "X-GPU-Model missing from GET /work")
            self.assertEqual(hdrs["x-gpu-model"], "NVIDIA GeForce RTX 5060")

            self.assertIn("x-miner-version", hdrs,
                          "X-Miner-Version missing from GET /work")
            self.assertEqual(hdrs["x-miner-version"], "2.0")
        finally:
            srv.shutdown()


# ---------------------------------------------------------------------------
# Test iii: 503 no_feasible_target → wait + retry without exit
# ---------------------------------------------------------------------------

class TestNoFeasibleTargetBackoff(unittest.TestCase):
    """Test iii: 503 no_feasible_target → loop waits retry_after and retries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.jsonl"
        _clear_cursor()
        _reset_backoff()
        self.wlog = __import__("logging").getLogger("test-c48-503")

    def tearDown(self):
        self.tmp.cleanup()
        _clear_cursor()
        _reset_backoff()

    def test_iii_503_no_feasible_target_waits_and_returns(self):
        """503 no_feasible_target → coordinator_cycle returns (not crashes) after sleep."""
        responses = {
            "GET /work": (503, {
                "error": "no_feasible_target",
                "message": "All targets unfit for this GPU",
                "retry_after": 1,  # tiny value so test is fast
            }),
        }
        srv, base = _start_mock_server(responses)
        client = _make_client(base, self.queue_path)

        sleep_calls = []
        real_sleep = time.sleep

        def _fake_sleep(secs):
            sleep_calls.append(secs)
            # Don't actually sleep in tests
            pass

        try:
            with patch("coordinator.loop.time.sleep", side_effect=_fake_sleep):
                # Should not raise NoFeasibleTargetError; should return cleanly
                coordinator_cycle(self.wlog, client, {}, 0)
            self.assertGreater(len(sleep_calls), 0,
                               "Expected sleep() after no_feasible_target, got none")
            # Sleep duration should be >= retry_after (60s floor as per code)
            self.assertGreaterEqual(sleep_calls[0], 1,
                                    f"Sleep was {sleep_calls[0]}, expected >= 1")
        finally:
            srv.shutdown()

    def test_iii_no_exit_on_503(self):
        """coordinator_cycle must NOT raise on 503 no_feasible_target."""
        responses = {
            "GET /work": (503, {
                "error": "no_feasible_target",
                "message": "All targets unfit",
                "retry_after": 1,
            }),
        }
        srv, base = _start_mock_server(responses)
        client = _make_client(base, self.queue_path)
        try:
            with patch("coordinator.loop.time.sleep"):
                # Must not raise
                try:
                    coordinator_cycle(self.wlog, client, {}, 0)
                except NoFeasibleTargetError as e:
                    self.fail(f"coordinator_cycle raised NoFeasibleTargetError: {e}")
                except SystemExit as e:
                    self.fail(f"coordinator_cycle called sys.exit(): {e}")
        finally:
            srv.shutdown()


# ---------------------------------------------------------------------------
# Test iv: failed unfit POST is retried; abandon not lost
# ---------------------------------------------------------------------------

class TestUnfitRetry(unittest.TestCase):
    """Test iv: unfit POST failure → retried; abandon still happens."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.jsonl"
        _clear_cursor()
        _reset_backoff()
        self.wlog = __import__("logging").getLogger("test-c48-retry")

    def tearDown(self):
        self.tmp.cleanup()
        _clear_cursor()
        _reset_backoff()

    def test_iv_unfit_retried_on_500(self):
        """POST /unfit returning 500 on first attempt is retried."""
        lease_resp = (200, {
            "lease_id": LEASE_ID,
            "target": TARGET,
            "modality": "protein",
            "expires_at": int(time.time() * 1000) + 4 * 3600 * 1000,
            "shards": [],
            "hit_threshold": -8.0,
            "scorer_profile": {"flags": {}, "msa_sampling": {}, "seed": 68},
            "coord_config_version": "2.0.0",
        })

        unfit_attempt = {"n": 0}

        def _unfit_resp(body, calls):
            unfit_attempt["n"] += 1
            if unfit_attempt["n"] == 1:
                return 500, {"error": "internal server error"}
            return 200, {"ok": True}

        responses = {
            "GET /work": lease_resp,
            f"POST /leases/{LEASE_ID}/unfit": _unfit_resp,
            "POST /results": (200, {"ok": True}),
        }
        srv, base = _start_mock_server(responses)
        client = _make_client(base, self.queue_path)

        try:
            with (
                patch("coordinator.loop._resolve_protein_inputs",
                      return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
                patch("coordinator.loop._score_protein",
                      side_effect=GpuOomError("CUDA out of memory")),
                patch("coordinator.loop.ShardManager") as MockSM,
                patch("coordinator.client.time.sleep"),  # don't wait in tests
            ):
                MockSM.return_value.iter_molecules.return_value = [
                    (ASPIRIN, 1, 0),
                ]
                coordinator_cycle(self.wlog, client, {}, 0)

            # Should have retried at least once
            self.assertGreater(unfit_attempt["n"], 1,
                               f"Expected retry on 500, but unfit was attempted {unfit_attempt['n']} times")
        finally:
            srv.shutdown()

    def test_iv_lease_abandoned_even_if_unfit_fails(self):
        """Even if all unfit POST retries fail, the lease is abandoned (no further scoring)."""
        lease_resp = (200, {
            "lease_id": LEASE_ID,
            "target": TARGET,
            "modality": "protein",
            "expires_at": int(time.time() * 1000) + 4 * 3600 * 1000,
            "shards": [],
            "hit_threshold": -8.0,
            "scorer_profile": {"flags": {}, "msa_sampling": {}, "seed": 68},
            "coord_config_version": "2.0.0",
        })
        responses = {
            "GET /work": lease_resp,
            f"POST /leases/{LEASE_ID}/unfit": (500, {"error": "always fails"}),
            "POST /results": (200, {"ok": True}),
        }
        srv, base = _start_mock_server(responses)
        client = _make_client(base, self.queue_path)
        score_count = {"n": 0}

        try:
            def _oom_score(*a, **kw):
                score_count["n"] += 1
                raise GpuOomError("CUDA out of memory")

            with (
                patch("coordinator.loop._resolve_protein_inputs",
                      return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
                patch("coordinator.loop._score_protein", side_effect=_oom_score),
                patch("coordinator.loop.ShardManager") as MockSM,
                patch("coordinator.client.time.sleep"),
            ):
                MockSM.return_value.iter_molecules.return_value = [
                    (ASPIRIN, 1, 0),
                    ("c1ccccc1", 1, 1),
                    ("CC(N)=O", 1, 2),
                ]
                # Should not raise even though unfit always returns 500
                coordinator_cycle(self.wlog, client, {}, 0)

            # Only the first molecule was scored (OOM fired, loop broke)
            self.assertEqual(score_count["n"], 1,
                             f"Lease not abandoned: scored {score_count['n']} molecules after OOM")
        finally:
            srv.shutdown()


# ---------------------------------------------------------------------------
# Test v: per-molecule 'Failed to process' does NOT produce unfit report
# ---------------------------------------------------------------------------

class TestPerMoleculeErrorNoUnfit(unittest.TestCase):
    """Test v: per-molecule failure does NOT trigger unfit report."""

    def setUp(self):
        _clear_cursor()
        _reset_backoff()

    def tearDown(self):
        _clear_cursor()
        _reset_backoff()

    def test_v_per_molecule_error_no_unfit(self):
        """PerMoleculeError does NOT POST to /unfit; other molecules continue."""
        from coordinator.loop import PerMoleculeError

        lease = _make_lease()

        # Use MagicMock client — we're testing no post_unfit, not network behavior
        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease
        client.gpu_memory_mb = 8192
        client.gpu_model = "NVIDIA GeForce RTX 5060"

        call_n = {"n": 0}

        def _per_mol_err(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                raise PerMoleculeError("Failed to process mol: bad SMILES")
            return -0.05

        with tempfile.TemporaryDirectory() as tmp:
            _clear_cursor()
            wlog = __import__("logging").getLogger("test-c48-permol-v")
            with (
                patch("coordinator.loop._resolve_protein_inputs",
                      return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
                patch("coordinator.loop._score_protein", side_effect=_per_mol_err),
                patch("coordinator.loop.ShardManager") as MockSM,
            ):
                MockSM.return_value.iter_molecules.return_value = [
                    (ASPIRIN, 1, 0),
                    ("c1ccccc1", 1, 1),
                ]
                coordinator_cycle(wlog, client, {}, 0)

        # post_unfit must NOT have been called
        client.post_unfit.assert_not_called()

        # Second molecule was scored (loop continued)
        self.assertEqual(call_n["n"], 2,
                         "Per-molecule error should not stop the loop; second molecule not attempted")

    def test_v_failed_to_process_text_does_not_trigger_unfit(self):
        """'Failed to process' in stdout (Boltz skip pattern) → PerMoleculeError, not GpuOomError."""
        from coordinator.loop import PerMoleculeError, _is_gpu_oom, _is_per_molecule_error

        # This text is a Boltz-style per-molecule skip — not an OOM
        stderr = "Failed to process CCCC. Skipping."
        stdout = ""
        is_oom, _ = _is_gpu_oom(stderr, stdout)
        is_mol, _ = _is_per_molecule_error(stderr, stdout)
        self.assertFalse(is_oom,
                         "'Failed to process' should NOT be classified as GPU OOM")
        self.assertTrue(is_mol,
                        "'Failed to process' should be classified as per-molecule error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
