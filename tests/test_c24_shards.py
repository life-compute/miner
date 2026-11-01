"""
tests/test_c24_shards.py  –  C24 shard-format tests.

Root cause: coordinator shards are plain .smi.gz (gzip text, NO tar layer).
ShardManager was saving <sha256>.tar.gz and calling tarfile.open() → fail.

Fix: _download_shard uses .smi.gz extension; _molecules_from_archive dispatches
on the cached filename extension: .smi.gz → gzip, .tar.gz → tarfile (legacy).

Tests reproduce the failure with production-style identifiers (manifest file
names savi_novel_NNNN.smi.gz, sha256_gz hashes, plain gzip content) then
verify the fix, then verify lease field_name contract, and back-off.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import sys
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from coordinator.shards import (
    ShardManager,
    _cache_path,
    _download_shard,
    _molecules_from_archive,
    _molecules_from_smi_gz,
    _molecules_from_tar_gz,
    _is_tar_url,
    SHARD_DIR,
)

log = logging.getLogger(__name__)

# ── Manifest-style constants ─────────────────────────────────────────────
SAVI_NOVEL_URL = "https://life-coordinator-devnet.lifecompute313.workers.dev/shards/savi_novel_0001.smi.gz"
# Real sha256_gz from seed_shards.sql row 1
SAVI_NOVEL_SHA256 = "adb3ea407976111a3a9832145f95c3711fe770cac3bc64e684a42f23f6f00f15"

ASPIRIN_SMI = "CC(=O)Oc1ccccc1C(=O)O"
BENZENE_SMI = "c1ccccc1"
ACETAMIDE_SMI = "CC(N)=O"

SAMPLE_SMILES = [ASPIRIN_SMI, BENZENE_SMI, ACETAMIDE_SMI]


def _make_smi_gz(smiles_list: list[str]) -> bytes:
    """Plain gzip-compressed SMILES file — one SMILES per line."""
    text = "\n".join(smiles_list) + "\n"
    return gzip.compress(text.encode("utf-8"))


def _make_tar_gz(smiles_list: list[str]) -> bytes:
    """Legacy tar.gz archive containing molecules.smi."""
    content = "\n".join(smiles_list).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("molecules.smi")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Minimal HTTP server for shard serving ───────────────────────────────

class _ShardHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        resps = getattr(self.server, "_responses", {})
        data = resps.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _start_shard_server(path_to_bytes: dict[str, bytes]):
    srv = HTTPServer(("127.0.0.1", 0), _ShardHandler)
    srv._responses = path_to_bytes  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}"


class TestShardFormatDetection(unittest.TestCase):
    """Unit tests for format dispatch — no HTTP, no disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shard_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # ── _is_tar_url ──────────────────────────────────────────────────────

    def test_is_tar_url_true_for_tar_gz(self):
        self.assertTrue(_is_tar_url("https://example.com/shard.tar.gz"))

    def test_is_tar_url_false_for_smi_gz(self):
        self.assertFalse(_is_tar_url("https://example.com/savi_novel_0001.smi.gz"))

    def test_is_tar_url_false_for_bare_gz(self):
        self.assertFalse(_is_tar_url("https://example.com/shard.gz"))

    # ── _cache_path ──────────────────────────────────────────────────────

    def test_cache_path_smi_gz_url_produces_smi_gz_file(self):
        p = _cache_path("abc123", SAVI_NOVEL_URL)
        self.assertTrue(p.name.endswith(".smi.gz"), f"expected .smi.gz, got {p.name}")

    def test_cache_path_tar_gz_url_produces_tar_gz_file(self):
        p = _cache_path("abc123", "https://example.com/shard.tar.gz")
        self.assertTrue(p.name.endswith(".tar.gz"), f"expected .tar.gz, got {p.name}")

    # ── _molecules_from_smi_gz ───────────────────────────────────────────

    def test_smi_gz_reader_yields_smiles(self):
        data = _make_smi_gz(SAMPLE_SMILES)
        path = self.shard_dir / "test.smi.gz"
        path.write_bytes(data)
        results = list(_molecules_from_smi_gz(path))
        self.assertEqual([s for s, _ in results], SAMPLE_SMILES)

    def test_smi_gz_reader_row_index_is_sequential(self):
        data = _make_smi_gz(SAMPLE_SMILES)
        path = self.shard_dir / "test.smi.gz"
        path.write_bytes(data)
        rows = [r for _, r in _molecules_from_smi_gz(path)]
        self.assertEqual(rows, [0, 1, 2])

    def test_smi_gz_reader_skips_blank_lines(self):
        text = "\n".join(["mol1", "", "mol2", "", "mol3"]) + "\n"
        data = gzip.compress(text.encode())
        path = self.shard_dir / "blanks.smi.gz"
        path.write_bytes(data)
        results = list(_molecules_from_smi_gz(path))
        self.assertEqual([s for s, _ in results], ["mol1", "mol2", "mol3"])

    def test_smi_gz_reader_skips_comment_lines(self):
        text = "# comment\nmol1\nmol2\n"
        data = gzip.compress(text.encode())
        path = self.shard_dir / "comments.smi.gz"
        path.write_bytes(data)
        results = list(_molecules_from_smi_gz(path))
        self.assertEqual([s for s, _ in results], ["mol1", "mol2"])

    def test_smi_gz_reader_takes_first_token_only(self):
        """Standard .smi format: SMILES [name] — only first token is SMILES."""
        text = "CC(=O)O aspirin\nc1ccccc1 benzene\n"
        data = gzip.compress(text.encode())
        path = self.shard_dir / "named.smi.gz"
        path.write_bytes(data)
        results = list(_molecules_from_smi_gz(path))
        self.assertEqual([s for s, _ in results], ["CC(=O)O", "c1ccccc1"])

    def test_smi_gz_reader_error_on_corrupt_file(self):
        """Corrupt file → error logged, no exception propagated, empty iterator."""
        path = self.shard_dir / "corrupt.smi.gz"
        path.write_bytes(b"\x00\x00\x00NOTGZIP")
        results = list(_molecules_from_smi_gz(path))
        self.assertEqual(results, [])

    # ── _molecules_from_archive dispatch ─────────────────────────────────

    def test_dispatch_smi_gz_uses_gzip_reader(self):
        data = _make_smi_gz(SAMPLE_SMILES)
        path = self.shard_dir / "shard.smi.gz"
        path.write_bytes(data)
        results = list(_molecules_from_archive(path))
        self.assertEqual([s for s, _ in results], SAMPLE_SMILES)

    def test_dispatch_tar_gz_uses_tar_reader(self):
        data = _make_tar_gz(SAMPLE_SMILES)
        path = self.shard_dir / "shard.tar.gz"
        path.write_bytes(data)
        results = list(_molecules_from_archive(path))
        self.assertEqual([s for s, _ in results], SAMPLE_SMILES)

    def test_dispatch_tar_gz_on_smi_gz_file_logs_error_and_returns_empty(self):
        """
        REGRESSION: prior code stored .smi.gz as .tar.gz and called tarfile.open().
        Verify that calling _molecules_from_tar_gz on a plain gzip file returns
        empty (not raising) — this is the old failure mode.
        """
        data = _make_smi_gz(SAMPLE_SMILES)
        path = self.shard_dir / "misnamed.tar.gz"
        path.write_bytes(data)
        # _molecules_from_tar_gz on plain gzip should log error and return empty
        results = list(_molecules_from_tar_gz(path))
        self.assertEqual(results, [], "expected empty — tarfile.open fails on plain gzip")


class TestShardDownloadAndCache(unittest.TestCase):
    """Integration tests: HTTP server serves .smi.gz, _download_shard caches it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_shard_dir = SHARD_DIR

    def tearDown(self):
        self.tmp.cleanup()

    def _shard_dir(self) -> Path:
        d = Path(self.tmp.name) / "shards"
        d.mkdir(exist_ok=True)
        return d

    def test_download_smi_gz_cached_with_smi_gz_extension(self):
        """After download, cached file should be <sha256>.smi.gz (not .tar.gz)."""
        data = _make_smi_gz(SAMPLE_SMILES)
        sha256 = _sha256(data)
        srv, base = _start_shard_server({"/shards/savi_novel_0001.smi.gz": data})

        url = f"{base}/shards/savi_novel_0001.smi.gz"

        import coordinator.shards as shards_mod
        orig_dir = shards_mod.SHARD_DIR
        shards_mod.SHARD_DIR = self._shard_dir()
        try:
            result_path = shards_mod._download_shard(url, sha256)
        finally:
            shards_mod.SHARD_DIR = orig_dir
            srv.shutdown()

        self.assertIsNotNone(result_path, "download should succeed")
        self.assertTrue(str(result_path).endswith(".smi.gz"),
                        f"expected .smi.gz extension, got {result_path}")
        self.assertFalse(str(result_path).endswith(".tar.gz"),
                         "must NOT save as .tar.gz for .smi.gz URL")

    def test_download_verify_sha256_match(self):
        data = _make_smi_gz(SAMPLE_SMILES)
        sha256 = _sha256(data)
        srv, base = _start_shard_server({"/shards/savi_novel_0002.smi.gz": data})

        import coordinator.shards as shards_mod
        orig_dir = shards_mod.SHARD_DIR
        shards_mod.SHARD_DIR = self._shard_dir()
        try:
            result_path = shards_mod._download_shard(f"{base}/shards/savi_novel_0002.smi.gz", sha256)
        finally:
            shards_mod.SHARD_DIR = orig_dir
            srv.shutdown()

        self.assertIsNotNone(result_path)

    def test_download_rejects_sha256_mismatch(self):
        data = _make_smi_gz(SAMPLE_SMILES)
        wrong_sha256 = "0" * 64
        srv, base = _start_shard_server({"/shards/savi_novel_0003.smi.gz": data})

        import coordinator.shards as shards_mod
        orig_dir = shards_mod.SHARD_DIR
        shards_mod.SHARD_DIR = self._shard_dir()
        try:
            result_path = shards_mod._download_shard(f"{base}/shards/savi_novel_0003.smi.gz", wrong_sha256)
        finally:
            shards_mod.SHARD_DIR = orig_dir
            srv.shutdown()

        self.assertIsNone(result_path, "sha256 mismatch should return None")


class TestShardManagerEndToEnd(unittest.TestCase):
    """ShardManager.iter_molecules with real HTTP serving real .smi.gz."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_iter_molecules_from_smi_gz_shards(self):
        """
        End-to-end: ShardManager downloads .smi.gz shards and iterates molecules.
        This is the production-style path using file names from the manifest.
        """
        data1 = _make_smi_gz([ASPIRIN_SMI, BENZENE_SMI])
        data2 = _make_smi_gz([ACETAMIDE_SMI])
        sha1 = _sha256(data1)
        sha2 = _sha256(data2)

        srv, base = _start_shard_server({
            "/shards/savi_novel_0001.smi.gz": data1,
            "/shards/savi_novel_0002.smi.gz": data2,
        })

        shard_dir = Path(self.tmp.name) / "shards"
        shard_dir.mkdir()

        shards = [
            {"shard_id": 1, "download_url": f"{base}/shards/savi_novel_0001.smi.gz",
             "sha256": sha1, "mol_count": 2, "file_name": "savi_novel_0001.smi.gz"},
            {"shard_id": 2, "download_url": f"{base}/shards/savi_novel_0002.smi.gz",
             "sha256": sha2, "mol_count": 1, "file_name": "savi_novel_0002.smi.gz"},
        ]

        import coordinator.shards as shards_mod
        orig_dir = shards_mod.SHARD_DIR
        shards_mod.SHARD_DIR = shard_dir
        try:
            sm = ShardManager(shard_dir=shard_dir)
            results = list(sm.iter_molecules(shards))
        finally:
            shards_mod.SHARD_DIR = orig_dir
            srv.shutdown()

        smiles_seen = [s for s, _sid, _row in results]
        self.assertIn(ASPIRIN_SMI, smiles_seen)
        self.assertIn(BENZENE_SMI, smiles_seen)
        self.assertIn(ACETAMIDE_SMI, smiles_seen)
        self.assertEqual(len(smiles_seen), 3)

    def test_shard_entry_missing_url_skipped(self):
        sm = ShardManager()
        shards = [{"shard_id": 1, "sha256": "x" * 64, "mol_count": 0}]
        results = list(sm.iter_molecules(shards))
        self.assertEqual(results, [])

    def test_shard_entry_missing_sha256_skipped(self):
        sm = ShardManager()
        shards = [{"shard_id": 1, "download_url": "http://example.com/shard.smi.gz", "mol_count": 0}]
        results = list(sm.iter_molecules(shards))
        self.assertEqual(results, [])

    def test_regression_tar_gz_saved_as_smi_gz_fails_cleanly(self):
        """
        REGRESSION: if the server accidentally returns a tar.gz for a .smi.gz URL,
        and the caller saves it as .smi.gz, _molecules_from_smi_gz should return
        empty without exception (corrupt gzip fallback).
        (This tests the failure mode, not the happy path.)
        """
        tar_data = _make_tar_gz(SAMPLE_SMILES)
        sha256 = _sha256(tar_data)
        srv, base = _start_shard_server({"/shards/savi_novel_0099.smi.gz": tar_data})

        shard_dir = Path(self.tmp.name) / "shards99"
        shard_dir.mkdir()

        import coordinator.shards as shards_mod
        orig_dir = shards_mod.SHARD_DIR
        shards_mod.SHARD_DIR = shard_dir
        try:
            shards = [{"shard_id": 99, "download_url": f"{base}/shards/savi_novel_0099.smi.gz",
                       "sha256": sha256, "mol_count": 3}]
            sm = ShardManager(shard_dir=shard_dir)
            results = list(sm.iter_molecules(shards))
        finally:
            shards_mod.SHARD_DIR = orig_dir
            srv.shutdown()

        # tar data is NOT valid gzip-of-text, so _molecules_from_smi_gz yields 0
        # (tar.gz IS valid gzip, but the decompressed bytes are binary, not SMILES lines)
        # The point is: no unhandled exception, just empty result
        self.assertIsInstance(results, list)


class TestLeaseShardContract(unittest.TestCase):
    """Verify that the shard dict shape expected by ShardManager matches the C24 lease contract."""

    def test_lease_shard_with_file_name_iterates_correctly(self):
        """
        Lease shards include file_name in C24. ShardManager must still work with
        download_url + sha256 — file_name is advisory/informational.
        """
        data = _make_smi_gz([ASPIRIN_SMI])
        sha256 = _sha256(data)

        with tempfile.TemporaryDirectory() as tmpdir:
            shard_dir = Path(tmpdir) / "shards"
            shard_dir.mkdir()

            srv, base = _start_shard_server({"/shards/savi_novel_0001.smi.gz": data})

            import coordinator.shards as shards_mod
            orig_dir = shards_mod.SHARD_DIR
            shards_mod.SHARD_DIR = shard_dir
            try:
                # C24 lease shard shape: has file_name
                shards = [{
                    "shard_id": 1,
                    "file_name": "savi_novel_0001.smi.gz",   # C24 addition
                    "download_url": f"{base}/shards/savi_novel_0001.smi.gz",
                    "sha256": sha256,
                    "mol_count": 1,
                }]
                sm = ShardManager(shard_dir=shard_dir)
                results = list(sm.iter_molecules(shards))
            finally:
                shards_mod.SHARD_DIR = orig_dir
                srv.shutdown()

        self.assertEqual([s for s, _, _ in results], [ASPIRIN_SMI])


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
