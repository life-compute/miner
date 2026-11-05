"""
tests/test_c47_miner_client.py  —  C47: target field in result body

Tests:
  T1: QueuedResult has target field (defaults to None)
  T2: enqueue_result passes target to QueuedResult
  T3: _post_result_once includes target in body when set
  T4: _post_result_once omits target when None
  T5: target included in queue JSON serialization (round-trip)
"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch, MagicMock
import tempfile

from coordinator.client import CoordinatorClient, QueuedResult, MinerKeypair


class TestTargetField(unittest.TestCase):

    def test_T1_queued_result_has_target_field(self):
        """QueuedResult has target field, defaults to None."""
        qr = QueuedResult(
            lease_id="l1",
            molecule_hash="mol1",
            score=-6.5,
            candidate="CCO",
        )
        self.assertIsNone(qr.target)

    def test_T2_enqueue_result_passes_target(self):
        """enqueue_result stores target in QueuedResult."""
        with tempfile.TemporaryDirectory() as tmpdir:
            kp = MinerKeypair.generate()
            client = CoordinatorClient(kp, queue_path=Path(tmpdir) / "queue.jsonl")
            # Patch _ensure_drain_running to avoid background thread
            client._ensure_drain_running = lambda: None
            client.enqueue_result(
                lease_id="l2",
                molecule_hash="mol2",
                score=-7.0,
                candidate="CCC",
                target="ESR1",
            )
            items = client._load_queue()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].target, "ESR1")

    def test_T3_post_result_includes_target_in_body(self):
        """_post_result_once includes target in POST body when set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            kp = MinerKeypair.generate()
            client = CoordinatorClient(kp, coordinator_url="http://localhost:9999",
                                       queue_path=Path(tmpdir) / "q.jsonl")
            item = QueuedResult(
                lease_id="l3",
                molecule_hash="mol3",
                score=-6.0,
                candidate="CCO",
                target="CDK4",
            )

            sent_body = {}

            def mock_http(url, method, body_bytes, x_auth, timeout=20.0):
                sent_body.update(json.loads(body_bytes))
                return 200, {"accepted": True, "target": "CDK4", "molecule_hash": "mol3"}

            with patch("coordinator.client._http", side_effect=mock_http):
                result = client._post_result_once(item)

            self.assertTrue(result)
            self.assertIn("target", sent_body)
            self.assertEqual(sent_body["target"], "CDK4")

    def test_T4_post_result_omits_target_when_none(self):
        """_post_result_once omits target from body when None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            kp = MinerKeypair.generate()
            client = CoordinatorClient(kp, coordinator_url="http://localhost:9999",
                                       queue_path=Path(tmpdir) / "q.jsonl")
            item = QueuedResult(
                lease_id="l4",
                molecule_hash="mol4",
                score=-6.0,
                candidate="CCO",
                # target not set → None
            )

            sent_body = {}

            def mock_http(url, method, body_bytes, x_auth, timeout=20.0):
                sent_body.update(json.loads(body_bytes))
                return 200, {"accepted": True}

            with patch("coordinator.client._http", side_effect=mock_http):
                result = client._post_result_once(item)

            self.assertTrue(result)
            self.assertNotIn("target", sent_body)

    def test_T5_target_round_trips_through_jsonl_queue(self):
        """target field survives serialization/deserialization in queue JSONL."""
        qr = QueuedResult(
            lease_id="l5",
            molecule_hash="mol5",
            score=-5.5,
            candidate="c1ccccc1",
            target="MYC",
        )
        # asdict → json → load
        d = asdict(qr)
        json_line = json.dumps(d)
        d2 = json.loads(json_line)
        qr2 = QueuedResult(**{k: d2.get(k) for k in QueuedResult.__dataclass_fields__})
        self.assertEqual(qr2.target, "MYC")


if __name__ == "__main__":
    unittest.main()
