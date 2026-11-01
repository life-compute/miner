"""
tests/test_c28_client.py — C28 miner-side coordinator client tests.

Coverage
--------
1.  404 on POST /results is retried; item stays in queue after first attempt.
2.  404 on POST /results for 3rd attempt drops the item (single log line).
3.  not_found_attempts is persisted to JSONL between retries.
4.  Start-up drop: queued results with expires_at in the past are removed.
5.  Start-up drop: queued results with expires_at in the future are kept.
6.  Start-up drop: queued results with expires_at=0 (unknown) are kept.
7.  Start-up drop: count logged matches items actually removed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from coordinator.auth import MinerKeypair
from coordinator.client import CoordinatorClient, QueuedResult


# ---------------------------------------------------------------------------
# Minimal mock coordinator HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Simple one-shot handler; reads instructions from the test via srv._responses."""

    def do_POST(self) -> None:
        # Read body
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)

        srv = self.server  # type: ignore[attr-defined]
        code, body = srv._responses.pop(0) if srv._responses else (500, {"error": "no response queued"})
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # silence HTTP access log
        pass


def _start_server(responses: list) -> tuple[HTTPServer, str]:
    """Start a throwaway HTTP server; returns (server, base_url)."""
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv._responses = responses  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Helper to build a CoordinatorClient with a temp queue
# ---------------------------------------------------------------------------

def _make_client(base_url: str, tmp_dir: str) -> CoordinatorClient:
    kp = MinerKeypair.generate()
    return CoordinatorClient(
        keypair=kp,
        coordinator_url=base_url,
        fallback_url=None,
        worker_name=None,
        queue_path=Path(tmp_dir) / "queue.jsonl",
    )


def _queue_item(client: CoordinatorClient, mol: str = "mol-1",
                expires_at: int = 0, not_found_attempts: int = 0) -> None:
    """Directly write a queued item to the client's JSONL queue."""
    item = QueuedResult(
        lease_id="lease-abc",
        molecule_hash=mol,
        score=-8.0,
        candidate="CC(=O)O",
        shard_id=1,
        shard_row=0,
        expires_at=expires_at,
        not_found_attempts=not_found_attempts,
    )
    client._append_queue(item)


def _load_raw(client: CoordinatorClient) -> list[dict]:
    if not client.queue_path.exists():
        return []
    lines = client.queue_path.read_text().splitlines()
    return [json.loads(l) for l in lines if l.strip()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class Test404RetryLogic(unittest.TestCase):

    def test_1_first_404_keeps_item_in_queue(self):
        """First 404 → returns False (retry); item stays in queue."""
        responses = [
            (404, {"error": "Lease not found or already expired"}),
        ]
        srv, base_url = _start_server(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _make_client(base_url, tmp)
                _queue_item(client, "mol-1")
                item = client._load_queue()[0]
                result = client._post_result_once(item)
                self.assertFalse(result, "First 404 should return False (retry)")
                # Item must still be in the queue
                remaining = _load_raw(client)
                self.assertEqual(len(remaining), 1, "Item should still be in queue after 1st 404")
        finally:
            srv.shutdown()

    def test_2_third_404_drops_item(self):
        """After 3 accumulated 404s the item is dropped (returns True)."""
        # We simulate the 3rd 404 by pre-loading not_found_attempts=2
        responses = [
            (404, {"error": "Lease not found or already expired"}),
        ]
        srv, base_url = _start_server(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _make_client(base_url, tmp)
                # Pre-populate item at attempt 2 (so next 404 is attempt 3 ≥ 3 → drop)
                _queue_item(client, "mol-drop", not_found_attempts=2)
                item = client._load_queue()[0]
                result = client._post_result_once(item)
                self.assertTrue(result, "3rd 404 should return True (drop)")
        finally:
            srv.shutdown()

    def test_3_not_found_attempts_persisted(self):
        """not_found_attempts is written back to JSONL after a retryable 404."""
        responses = [
            (404, {"error": "Lease not found or already expired"}),
        ]
        srv, base_url = _start_server(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = _make_client(base_url, tmp)
                _queue_item(client, "mol-persist")
                item = client._load_queue()[0]
                client._post_result_once(item)  # first 404

                rows = _load_raw(client)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["not_found_attempts"], 1,
                                 "not_found_attempts should be 1 after first 404")
        finally:
            srv.shutdown()


class TestStartupStaleDrop(unittest.TestCase):

    def test_4_stale_item_dropped(self):
        """Items with expires_at in the past are removed at startup."""
        with tempfile.TemporaryDirectory() as tmp:
            client = _make_client("http://localhost:1", tmp)
            past_ms = int(time.time() * 1000) - 60_000  # 1 minute ago
            _queue_item(client, "mol-stale", expires_at=past_ms)
            dropped = client.drop_stale_queued_results()
            self.assertEqual(dropped, 1)
            remaining = _load_raw(client)
            self.assertEqual(len(remaining), 0, "Stale item should be removed")

    def test_5_future_item_kept(self):
        """Items with expires_at in the future are kept."""
        with tempfile.TemporaryDirectory() as tmp:
            client = _make_client("http://localhost:1", tmp)
            future_ms = int(time.time() * 1000) + 4 * 3600 * 1000  # +4 hours
            _queue_item(client, "mol-fresh", expires_at=future_ms)
            dropped = client.drop_stale_queued_results()
            self.assertEqual(dropped, 0)
            remaining = _load_raw(client)
            self.assertEqual(len(remaining), 1, "Fresh item should be kept")

    def test_6_unknown_expires_kept(self):
        """Items with expires_at=0 (unknown) are kept."""
        with tempfile.TemporaryDirectory() as tmp:
            client = _make_client("http://localhost:1", tmp)
            _queue_item(client, "mol-unknown", expires_at=0)
            dropped = client.drop_stale_queued_results()
            self.assertEqual(dropped, 0)
            remaining = _load_raw(client)
            self.assertEqual(len(remaining), 1, "Unknown-expiry item should be kept")

    def test_7_count_logged_matches_removed(self):
        """drop_stale_queued_results returns the correct count."""
        with tempfile.TemporaryDirectory() as tmp:
            client = _make_client("http://localhost:1", tmp)
            past_ms = int(time.time() * 1000) - 1
            future_ms = int(time.time() * 1000) + 3600_000
            _queue_item(client, "mol-a", expires_at=past_ms)
            _queue_item(client, "mol-b", expires_at=past_ms)
            _queue_item(client, "mol-c", expires_at=future_ms)
            _queue_item(client, "mol-d", expires_at=0)

            dropped = client.drop_stale_queued_results()
            self.assertEqual(dropped, 2, "Should drop exactly 2 stale items")
            remaining = _load_raw(client)
            self.assertEqual(len(remaining), 2, "2 items (fresh + unknown) should remain")
            mols_remaining = {r["molecule_hash"] for r in remaining}
            self.assertIn("mol-c", mols_remaining)
            self.assertIn("mol-d", mols_remaining)


if __name__ == "__main__":
    unittest.main()
