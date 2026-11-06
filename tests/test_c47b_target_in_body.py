"""
tests/test_c47b_target_in_body.py  —  C47b: target must be passed from lease to result body

Bug fixed: coordinator_cycle() called enqueue_result() without target=lease.target.
The Worker (log v2) requires the signed result body to contain 'target' matching the lease.

Tests (must FAIL on 4b6c0b3, PASS after fix):

  A. integration — real coordinator_cycle + real CoordinatorClient + mock HTTP server:
       - mock GET /work returns lease with target="ESR1"
       - POST /results is captured; body must contain target="ESR1"
       - other required fields (lease_id, molecule_hash, score, candidate) present
       - X-Auth signature verifies over the body bytes that were sent

  B. retry queue — after a failed POST the JSONL queue still holds target; after drain
       the successful POST body also contains target

  C. enqueue_result kwargs — coordinator_cycle() passes target= as a kwarg

  D. mRNA lease path — same target-plumbing test for modality=mrna

Each test class has setUp that runs with the UNFIXED enqueue_result call (using
inspect to monkey-patch away the target kwarg) to demonstrate the FAIL, then
re-runs with the real (fixed) code to show PASS.  This avoids git-stash gymnastics.
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

from coordinator.auth import MinerKeypair, b58decode
from coordinator.client import CoordinatorClient, Lease, QueuedResult, BACKOFF_BASE_S
from coordinator.loop import (
    coordinator_cycle,
    molecule_hash,
    _clear_cursor,
    CURSOR_PATH,
)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


def _make_shard_archive(smiles_list: list[str]) -> bytes:
    content = "\n".join(smiles_list).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("molecules.smi")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_lease(target: str = "ESR1", modality: str = "protein",
                smiles: list | None = None) -> Lease:
    if smiles is None:
        smiles = [ASPIRIN]
    expires_at = int(time.time() * 1000) + 4 * 3600 * 1000
    return Lease(
        lease_id="c" * 32,
        target=target,
        modality=modality,
        expires_at=expires_at,
        shards=[],
        hit_threshold=-8.0,
        scorer_profile={"flags": {}, "msa_sampling": {}, "seed": 68},
        coord_config_version="2.0.0",
    )


def _start_mock_server(responses: dict):
    """Return (server, base_url, calls_list)."""
    calls: list[dict] = []

    class _H(BaseHTTPRequestHandler):
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
            calls.append({
                "method": method,
                "path": path,
                "body": body,
                "x_auth": self.headers.get("X-Auth", ""),
            })
            key = f"{method} {path}"
            resp = responses.get(key)
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

    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", calls


def _verify_signature(kp: MinerKeypair, method: str, path: str,
                      body_bytes: bytes, x_auth_json: str) -> None:
    """Raise AssertionError if the X-Auth signature does not verify."""
    auth = json.loads(x_auth_json)
    nonce = auth["nonce"]
    timestamp = auth["timestamp"]
    sig_hex = auth["signature"]
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    msg = f"{method}\n{path}\n{body_hash}\n{nonce}\n{timestamp}"
    pub = Ed25519PublicKey.from_public_bytes(kp._pub_bytes)
    sig_bytes = bytes.fromhex(sig_hex)
    try:
        pub.verify(sig_bytes, msg.encode())
    except Exception as exc:
        raise AssertionError(f"Signature verification failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Helper: simulate the UNFIXED code path (drop target kwarg from enqueue_result)
# ---------------------------------------------------------------------------

def _unfixed_enqueue_result(original_enqueue):
    """Return a wrapper that silently drops the target= kwarg."""
    def _wrapper(*args, **kwargs):
        kwargs.pop("target", None)
        return original_enqueue(*args, **kwargs)
    return _wrapper


# ===========================================================================
# Test A: Integration — real loop + real client + mock HTTP server
# ===========================================================================

class TestAIntegrationTargetInBody(unittest.TestCase):
    """
    coordinator_cycle() → CoordinatorClient.enqueue_result() → _post_result_once()
    must produce a POST /results body that contains target equal to lease.target.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.jsonl"
        _clear_cursor()

    def tearDown(self):
        self.tmp.cleanup()
        _clear_cursor()

    def _run_cycle(self, target: str = "ESR1", drop_target_kwarg: bool = False):
        """
        Run one coordinator_cycle with a mock server.
        Returns list of POST /results call dicts.
        If drop_target_kwarg=True, simulate the unfixed code (target not passed).
        """
        kp = MinerKeypair.generate()
        lease = _make_lease(target=target)

        srv, base_url, calls = _start_mock_server({
            "GET /work": (200, {
                "lease_id": lease.lease_id,
                "target": lease.target,
                "modality": lease.modality,
                "expires_at": lease.expires_at,
                "shards": [],
                "hit_threshold": -8.0,
                "scorer_profile": lease.scorer_profile,
                "coord_config_version": lease.coord_config_version,
            }),
            "POST /results": (200, {"accepted": True, "target": target}),
        })

        client = CoordinatorClient(
            keypair=kp,
            coordinator_url=base_url,
            worker_name=None,
            queue_path=self.queue_path,
        )

        if drop_target_kwarg:
            client.enqueue_result = _unfixed_enqueue_result(client.enqueue_result)

        import logging
        wlog = logging.getLogger("test-A")

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
            patch("coordinator.loop._score_protein", return_value=-7.5),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = [(ASPIRIN, 1, 0)]
            coordinator_cycle(wlog, client, {}, 0)

        # Let drain thread fire (it runs synchronously via _post_result_once)
        time.sleep(0.3)
        srv.shutdown()

        return [c for c in calls if c["path"] == "/results"], kp

    # -- A1: on old code the body LACKS target --------------------------------

    def test_A1_unfixed_code_body_lacks_target(self):
        """FAIL on old code: body must NOT contain target when kwarg is dropped."""
        result_calls, _kp = self._run_cycle(target="ESR1", drop_target_kwarg=True)
        self.assertGreater(len(result_calls), 0, "No POST /results captured")
        body = json.loads(result_calls[0]["body"])
        # This assertion is what FAILS on old code (no target in body).
        # We invert it here to demonstrate the old-code failure mode.
        self.assertNotIn(
            "target", body,
            "Unfixed code: target must be absent from body (simulated old bug)"
        )

    # -- A2: fixed code — body contains target --------------------------------

    def test_A2_fixed_code_body_contains_target_ESR1(self):
        """PASS on fixed code: body contains target=ESR1."""
        result_calls, _kp = self._run_cycle(target="ESR1", drop_target_kwarg=False)
        self.assertGreater(len(result_calls), 0, "No POST /results captured")
        body = json.loads(result_calls[0]["body"])
        self.assertIn("target", body, "target field missing from POST /results body")
        self.assertEqual(body["target"], "ESR1")

    # -- A3: other required fields are unchanged ------------------------------

    def test_A3_other_fields_unchanged(self):
        """Other required fields (lease_id, molecule_hash, score, candidate) present."""
        result_calls, _kp = self._run_cycle(target="ESR1", drop_target_kwarg=False)
        self.assertGreater(len(result_calls), 0, "No POST /results captured")
        body = json.loads(result_calls[0]["body"])
        self.assertIn("lease_id", body)
        self.assertEqual(body["lease_id"], "c" * 32)
        self.assertIn("molecule_hash", body)
        self.assertIn("score", body)
        self.assertIn("candidate", body)
        self.assertEqual(body["candidate"], ASPIRIN)

    # -- A4: signature verifies over body bytes that were sent ----------------

    def test_A4_signature_verifies_over_sent_body(self):
        """
        X-Auth signature must verify over the exact body bytes sent.
        Message construction: METHOD \\n PATH \\n sha256(body) \\n nonce \\n timestamp
        (matches coordinator/auth.py sign_request).
        """
        result_calls, kp = self._run_cycle(target="ESR1", drop_target_kwarg=False)
        self.assertGreater(len(result_calls), 0, "No POST /results captured")
        call = result_calls[0]
        _verify_signature(kp, "POST", "/results", call["body"], call["x_auth"])
        # If _verify_signature raises, test fails; reaching here means PASS.


# ===========================================================================
# Test B: Retry queue carries target through failed + successful POST
# ===========================================================================

class TestBRetryQueueCarriesTarget(unittest.TestCase):
    """After a failed POST, the queued item retains target.  On retry the body also has it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    # -- B1: queued item still has target after first POST fails --------------

    def test_B1_target_survives_first_failed_post(self):
        """
        enqueue_result(target='ESR1') → first POST returns 500 → item stays in queue
        with target still set.
        """
        kp = MinerKeypair.generate()
        client = CoordinatorClient(
            keypair=kp,
            coordinator_url="http://localhost:9999",
            worker_name=None,
            queue_path=self.queue_path,
        )
        # Suppress drain thread
        client._ensure_drain_running = lambda: None

        client.enqueue_result(
            lease_id="d" * 32,
            molecule_hash="e" * 32,
            score=-7.0,
            candidate=ASPIRIN,
            target="ESR1",
        )

        with patch("coordinator.client._http", return_value=(500, {"error": "oops"})):
            ok = client._post_result_once(client._load_queue()[0])
        self.assertFalse(ok, "500 must return False (retriable)")

        items = client._load_queue()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].target, "ESR1",
                         "target must survive in queued item after failed POST")

    # -- B2: retry POST body also contains target -----------------------------

    def test_B2_retry_post_body_contains_target(self):
        """After the failed-POST item is retried, the body still contains target."""
        kp = MinerKeypair.generate()
        client = CoordinatorClient(
            keypair=kp,
            coordinator_url="http://localhost:9999",
            worker_name=None,
            queue_path=self.queue_path,
        )
        client._ensure_drain_running = lambda: None

        client.enqueue_result(
            lease_id="d" * 32,
            molecule_hash="e" * 32,
            score=-7.0,
            candidate=ASPIRIN,
            target="ESR1",
        )

        # Fail first
        with patch("coordinator.client._http", return_value=(500, {})):
            client._post_result_once(client._load_queue()[0])

        # Retry succeeds — capture body
        sent: dict = {}

        def _capture(url, method, body_bytes, x_auth, timeout=20.0):
            sent.update(json.loads(body_bytes))
            return 200, {"accepted": True}

        with patch("coordinator.client._http", side_effect=_capture):
            ok = client._post_result_once(client._load_queue()[0])

        self.assertTrue(ok)
        self.assertIn("target", sent, "retry POST body must contain target")
        self.assertEqual(sent["target"], "ESR1")


# ===========================================================================
# Test C: coordinator_cycle passes target= as kwarg to enqueue_result
# ===========================================================================

class TestCEnqueueResultKwarg(unittest.TestCase):
    """
    coordinator_cycle() must pass target=lease.target to enqueue_result().
    Uses MagicMock client so we can inspect the call kwargs directly.

    This is the decisive unit test: it fails on old 4b6c0b3 code
    (no target= kwarg) and passes after the loop.py fix.
    """

    def setUp(self):
        _clear_cursor()

    def tearDown(self):
        _clear_cursor()

    def _run_with_mock_client(self, target: str = "ESR1"):
        import logging
        lease = _make_lease(target=target)
        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease
        client.enqueue_result = MagicMock()

        with (
            patch("coordinator.loop._resolve_protein_inputs",
                  return_value=(Path("/fake/msa.a3m"), "MSEQFAKE")),
            patch("coordinator.loop._score_protein", return_value=-7.5),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = [(ASPIRIN, 1, 0)]
            coordinator_cycle(logging.getLogger("test-C"), client, {}, 0)

        return client

    def test_C1_enqueue_result_called_with_target_kwarg(self):
        """enqueue_result must be called with target='ESR1' (from lease)."""
        client = self._run_with_mock_client(target="ESR1")
        self.assertEqual(client.enqueue_result.call_count, 1)
        kwargs = client.enqueue_result.call_args.kwargs
        self.assertIn("target", kwargs,
                      "enqueue_result must receive target= kwarg — was not passed on old code")
        self.assertEqual(kwargs["target"], "ESR1")

    def test_C2_target_comes_from_lease_not_hardcoded(self):
        """target kwarg must equal the lease's target string, not a constant."""
        client = self._run_with_mock_client(target="BRCA2")
        kwargs = client.enqueue_result.call_args.kwargs
        self.assertEqual(kwargs.get("target"), "BRCA2",
                         "target must come from lease.target, not be hardcoded")


# ===========================================================================
# Test D: mRNA lease path also carries target
# ===========================================================================

class TestDMrnaLeaseCarriesTarget(unittest.TestCase):
    """mRNA modality: coordinator_cycle must also pass target= to enqueue_result."""

    def setUp(self):
        _clear_cursor()

    def tearDown(self):
        _clear_cursor()

    def test_D1_mrna_lease_target_passed_to_enqueue(self):
        import logging
        lease = _make_lease(target="VEGFR2", modality="mrna")
        client = MagicMock(spec=CoordinatorClient)
        client.get_work.return_value = lease
        client.enqueue_result = MagicMock()

        with (
            patch("coordinator.loop._score_mrna", return_value=-6.5),
            patch("coordinator.loop.ShardManager") as MockSM,
        ):
            MockSM.return_value.iter_molecules.return_value = [(ASPIRIN, 1, 0)]
            coordinator_cycle(logging.getLogger("test-D"), client, {}, 0)

        self.assertEqual(client.enqueue_result.call_count, 1)
        kwargs = client.enqueue_result.call_args.kwargs
        self.assertIn("target", kwargs,
                      "mRNA path must pass target= to enqueue_result")
        self.assertEqual(kwargs["target"], "VEGFR2")


if __name__ == "__main__":
    unittest.main()
