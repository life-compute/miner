"""
tests/test_c14_align_fetch.py — C14 alignment-fetch requirements.

Mock-server tests for coordinator/align_fetch.py::fetch_alignment().

Cases covered
-------------
  1. Happy path — manifest hit, compressed sha OK, raw sha OK → cached .a3m returned.
  2. Name not in manifest → AlignFetchError, no file written.
  3. Compressed SHA mismatch → AlignFetchError, corrupt download refused.
  4. Raw SHA mismatch → AlignFetchError, refused and temp deleted.
  5. Low disk space → AlignFetchError raised before any HTTP call.
  6. Cache hit — valid existing file → no HTTP calls at all.
  7. Cache stale (hash mismatch) → re-fetched and replaced.
  8. Gzip decompression error → AlignFetchError.
  9. Manifest HTTP 404 → AlignFetchError.
 10. Alignment file HTTP 503 → AlignFetchError.

Rules
-----
  - No GPU calls, no PM2, no real network.
  - No real keypairs; coordinator mock needs no auth.
  - Uses Python stdlib http.server — no extra deps.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordinator.align_fetch import (
    AlignFetchError,
    MIN_FREE_BYTES,
    fetch_alignment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_a3m(content: str = ">seq\nACDEFGHIKLMNPQRSTVWY\n") -> bytes:
    """Return a minimal .a3m file as bytes."""
    return content.encode()


def _gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6)


class _MockHandler(BaseHTTPRequestHandler):
    """Simple single-threaded handler that delegates to the server's route table."""

    def log_message(self, *args, **kwargs):  # silence request logs in tests
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        routes: dict = getattr(self.server, "_routes", {})
        handler: Optional[Callable[[str], tuple[int, bytes, str]]] = routes.get(path)
        if handler is None:
            # Try prefix match
            for prefix, h in routes.items():
                if path.startswith(prefix):
                    handler = h
                    break
        if handler is None:
            self._respond(404, b'{"error":"not found"}', "application/json")
            return
        status, body, content_type = handler(path)
        self._respond(status, body, content_type)

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_server(routes: dict) -> tuple:
    """Start a mock HTTP server and return (server, base_url)."""
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    srv._routes = routes
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def _make_manifest(gz_file_name: str, compressed_sha: str) -> bytes:
    return json.dumps({
        "files": [{"gz_file_name": gz_file_name, "compressed_sha256": compressed_sha}]
    }).encode()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestAlignFetch(unittest.TestCase):
    """Mock-server tests for fetch_alignment()."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── Case 1: Happy path ────────────────────────────────────────────────────

    def test_01_happy_path(self):
        """fetch_alignment returns the cached .a3m when all hashes match."""
        raw = _make_a3m()
        gz = _gzip_bytes(raw)
        raw_sha = _sha256(raw)
        gz_sha = _sha256(gz)
        gz_name = "P12345.a3m.gz"

        routes = {
            "/align/manifest.json": lambda p: (
                200, _make_manifest(gz_name, gz_sha), "application/json"
            ),
            "/align/" + gz_name: lambda p: (200, gz, "application/gzip"),
        }
        srv, url = _start_server(routes)
        try:
            result = fetch_alignment(url, "TGT1", gz_name, raw_sha, self._tmp)
            self.assertTrue(result.exists())
            self.assertEqual(result.name, "P12345.a3m")
            self.assertEqual(result.read_bytes(), raw)
        finally:
            srv.shutdown()

    # ── Case 2: Name not in manifest ─────────────────────────────────────────

    def test_02_name_not_in_manifest(self):
        """AlignFetchError raised when gz_file_name not listed in manifest."""
        raw = _make_a3m()
        gz = _gzip_bytes(raw)
        gz_sha = _sha256(gz)
        manifest = json.dumps({
            "files": [{"gz_file_name": "XXXXXX.a3m.gz", "compressed_sha256": gz_sha}]
        }).encode()

        routes = {
            "/align/manifest.json": lambda p: (200, manifest, "application/json"),
        }
        srv, url = _start_server(routes)
        try:
            with self.assertRaises(AlignFetchError) as ctx:
                fetch_alignment(url, "TGT2", "P99999.a3m.gz", "x" * 64, self._tmp)
            self.assertIn("not found in manifest", str(ctx.exception))
            # No file should have been written
            self.assertFalse((self._tmp / "P99999.a3m").exists())
        finally:
            srv.shutdown()

    # ── Case 3: Compressed SHA mismatch ──────────────────────────────────────

    def test_03_compressed_sha_mismatch(self):
        """AlignFetchError raised when compressed SHA differs from manifest."""
        raw = _make_a3m()
        gz = _gzip_bytes(raw)
        gz_name = "Q12345.a3m.gz"
        wrong_sha = "a" * 64  # deliberate mismatch

        routes = {
            "/align/manifest.json": lambda p: (
                200, _make_manifest(gz_name, wrong_sha), "application/json"
            ),
            "/align/" + gz_name: lambda p: (200, gz, "application/gzip"),
        }
        srv, url = _start_server(routes)
        try:
            with self.assertRaises(AlignFetchError) as ctx:
                fetch_alignment(url, "TGT3", gz_name, _sha256(raw), self._tmp)
            self.assertIn("compressed SHA-256 mismatch", str(ctx.exception))
            self.assertFalse((self._tmp / "Q12345.a3m").exists())
        finally:
            srv.shutdown()

    # ── Case 4: Raw SHA mismatch ──────────────────────────────────────────────

    def test_04_raw_sha_mismatch(self):
        """AlignFetchError raised when decompressed SHA differs from pinned hash."""
        raw = _make_a3m()
        gz = _gzip_bytes(raw)
        gz_name = "R12345.a3m.gz"
        gz_sha = _sha256(gz)
        wrong_raw_sha = "b" * 64  # deliberate mismatch

        routes = {
            "/align/manifest.json": lambda p: (
                200, _make_manifest(gz_name, gz_sha), "application/json"
            ),
            "/align/" + gz_name: lambda p: (200, gz, "application/gzip"),
        }
        srv, url = _start_server(routes)
        try:
            with self.assertRaises(AlignFetchError) as ctx:
                fetch_alignment(url, "TGT4", gz_name, wrong_raw_sha, self._tmp)
            self.assertIn("raw SHA-256 mismatch", str(ctx.exception))
            # Corrupted file must not linger
            self.assertFalse((self._tmp / "R12345.a3m").exists())
        finally:
            srv.shutdown()

    # ── Case 5: Low disk space ────────────────────────────────────────────────

    def test_05_low_disk_space(self):
        """AlignFetchError raised before any HTTP call when disk space is low."""
        calls = []

        def record(p):
            calls.append(p)
            return 200, b"{}", "application/json"

        routes = {"/align/manifest.json": record}
        srv, url = _start_server(routes)
        try:
            # Patch shutil.disk_usage to return almost-zero free space
            fake_usage = shutil.disk_usage.__class__  # DiskUsage named tuple
            fake = shutil.disk_usage("/")
            fake = type(fake)(fake.total, fake.used, 100)  # 100 bytes free
            with patch("coordinator.align_fetch.shutil.disk_usage", return_value=fake):
                with self.assertRaises(AlignFetchError) as ctx:
                    fetch_alignment(url, "TGT5", "S12345.a3m.gz", "c" * 64, self._tmp)
            self.assertIn("low disk space", str(ctx.exception))
            # No HTTP call should have been made
            self.assertEqual(calls, [], "HTTP calls made despite low disk space")
        finally:
            srv.shutdown()

    # ── Case 6: Cache hit — valid ──────────────────────────────────────────────

    def test_06_cache_hit_no_network(self):
        """When a valid cached file exists, no HTTP calls are made."""
        raw = _make_a3m()
        gz_name = "T12345.a3m.gz"
        raw_sha = _sha256(raw)
        cached = self._tmp / "T12345.a3m"
        cached.write_bytes(raw)

        calls = []

        def record(p):
            calls.append(p)
            return 200, b"{}", "application/json"

        routes = {"/align/manifest.json": record}
        srv, url = _start_server(routes)
        try:
            result = fetch_alignment(url, "TGT6", gz_name, raw_sha, self._tmp)
            self.assertEqual(result, cached)
            self.assertEqual(calls, [], "HTTP calls made despite valid cache")
        finally:
            srv.shutdown()

    # ── Case 7: Cache stale — re-fetched ──────────────────────────────────────

    def test_07_stale_cache_refetch(self):
        """A cached file with wrong hash is deleted and re-fetched."""
        raw = _make_a3m()
        gz = _gzip_bytes(raw)
        gz_name = "U12345.a3m.gz"
        raw_sha = _sha256(raw)
        gz_sha = _sha256(gz)

        # Write a stale file (different content)
        cached = self._tmp / "U12345.a3m"
        cached.write_bytes(b"stale content")

        routes = {
            "/align/manifest.json": lambda p: (
                200, _make_manifest(gz_name, gz_sha), "application/json"
            ),
            "/align/" + gz_name: lambda p: (200, gz, "application/gzip"),
        }
        srv, url = _start_server(routes)
        try:
            result = fetch_alignment(url, "TGT7", gz_name, raw_sha, self._tmp)
            self.assertEqual(result.read_bytes(), raw)
        finally:
            srv.shutdown()

    # ── Case 8: Gzip decompression error ──────────────────────────────────────

    def test_08_gzip_decompress_error(self):
        """AlignFetchError raised when downloaded bytes are not valid gzip."""
        gz_name = "V12345.a3m.gz"
        bad_gz = b"this is not gzip data at all"
        gz_sha = _sha256(bad_gz)

        routes = {
            "/align/manifest.json": lambda p: (
                200, _make_manifest(gz_name, gz_sha), "application/json"
            ),
            "/align/" + gz_name: lambda p: (200, bad_gz, "application/gzip"),
        }
        srv, url = _start_server(routes)
        try:
            with self.assertRaises(AlignFetchError) as ctx:
                fetch_alignment(url, "TGT8", gz_name, "d" * 64, self._tmp)
            self.assertIn("decompression failed", str(ctx.exception))
        finally:
            srv.shutdown()

    # ── Case 9: Manifest HTTP 404 ─────────────────────────────────────────────

    def test_09_manifest_http_error(self):
        """AlignFetchError raised when manifest endpoint returns 404."""
        routes = {
            "/align/manifest.json": lambda p: (
                404, b"not found", "text/plain"
            ),
        }
        srv, url = _start_server(routes)
        try:
            with self.assertRaises(AlignFetchError):
                fetch_alignment(url, "TGT9", "W12345.a3m.gz", "e" * 64, self._tmp)
        finally:
            srv.shutdown()

    # ── Case 10: Alignment file HTTP 503 ──────────────────────────────────────

    def test_10_alignment_file_http_error(self):
        """AlignFetchError raised when the .a3m.gz endpoint returns 503."""
        raw = _make_a3m()
        gz = _gzip_bytes(raw)
        gz_name = "X12345.a3m.gz"
        gz_sha = _sha256(gz)

        routes = {
            "/align/manifest.json": lambda p: (
                200, _make_manifest(gz_name, gz_sha), "application/json"
            ),
            "/align/" + gz_name: lambda p: (
                503, b"service unavailable", "text/plain"
            ),
        }
        srv, url = _start_server(routes)
        try:
            with self.assertRaises(AlignFetchError):
                fetch_alignment(url, "TGT10", gz_name, _sha256(raw), self._tmp)
        finally:
            srv.shutdown()

    # ── Case 11: Unsafe filename refused without network call ─────────────────

    def test_11_unsafe_filename_refused(self):
        """AlignFetchError raised immediately for a filename that violates the pattern."""
        calls = []

        def record(p):
            calls.append(p)
            return 200, b"{}", "application/json"

        routes = {"/align/manifest.json": record}
        srv, url = _start_server(routes)
        try:
            with self.assertRaises(AlignFetchError) as ctx:
                fetch_alignment(url, "TGT11", "../../etc/passwd.a3m.gz", "f" * 64, self._tmp)
            self.assertIn("unsafe filename", str(ctx.exception))
            self.assertEqual(calls, [], "HTTP called despite unsafe filename")
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
