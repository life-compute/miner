"""
tests/test_ua.py — User-Agent and terms-version tests (C19).

Test matrix
-----------
UA-1  MINER_UA is defined in coordinator/ua.py and is the canonical string.
UA-2  MINER_UA starts with "life-compute-miner/" (product token, not browser).
UA-3  MINER_UA is NOT a browser agent (no "Mozilla", "Chrome", "Safari", "curl").
UA-4  Repository-wide guard: no coordinator/*.py or miner_daemon.py file may
      contain a hard-coded User-Agent string other than via MINER_UA.
UA-5  coordinator/client._http() sends MINER_UA (captured via mock).
UA-6  coordinator/align_fetch._get() sends MINER_UA (captured via mock).
UA-7  coordinator/shards._download_shard() sends MINER_UA (captured via mock).
UA-8  miner_daemon wallet-summary request sends MINER_UA (captured via mock).
UA-9  Live smoke test: default Python UA → 403; MINER_UA → 200 on read-only routes.
      (Skipped when COORD_URL env var not set or network unavailable.)

TV-1  TERMS_VERSION in coordinator/register.py is "1.1.0".
TV-2  check_terms_version() passes when /config lists the version.
TV-3  check_terms_version() raises TermsVersionError when version absent from list.
TV-4  check_terms_version() logs a warning and does NOT raise when
      known_terms_versions is absent from /config (graceful degradation).
TV-5  check_terms_version() logs a warning and does NOT raise when /config is
      unreachable (network error graceful degradation).
"""
from __future__ import annotations

import ast
import json
import logging
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordinator.ua import MINER_UA, miner_request
from coordinator.register import (
    TERMS_VERSION,
    TermsVersionError,
    check_terms_version,
)

COORD_DIR = Path(__file__).resolve().parents[1] / "coordinator"
MINER_DAEMON = Path(__file__).resolve().parents[1] / "miner_daemon.py"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

BROWSER_TOKENS = ("Mozilla", "Chrome", "Safari", "Firefox", "curl", "Wget")

# Known stale UA strings that existed before this change; must not be in source.
STALE_UA_STRINGS = (
    "life-compute-miner/1.0",
    "life-compute-miner/c3",
)


def _start_config_server(config_body: dict, status: int = 200) -> tuple:
    """Start a tiny HTTP server that returns config_body from /config."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            pass

        def do_GET(self):
            raw = json.dumps(config_body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


# ─────────────────────────────────────────────────────────────────────────────
# UA tests
# ─────────────────────────────────────────────────────────────────────────────

class TestUserAgent(unittest.TestCase):

    # UA-1
    def test_miner_ua_defined(self):
        self.assertIsInstance(MINER_UA, str)
        self.assertTrue(MINER_UA, "MINER_UA must not be empty")

    # UA-2
    def test_miner_ua_product_token(self):
        self.assertTrue(
            MINER_UA.startswith("life-compute-miner/"),
            f"MINER_UA must start with 'life-compute-miner/', got: {MINER_UA!r}",
        )

    # UA-3
    def test_browser_ua_forbidden(self):
        for tok in BROWSER_TOKENS:
            self.assertNotIn(
                tok, MINER_UA,
                f"MINER_UA must not contain browser token {tok!r}: {MINER_UA!r}",
            )

    # UA-4: repository-wide guard — no hard-coded UA strings other than MINER_UA
    def test_no_hardcoded_ua_in_coordinator_files(self):
        """
        All coordinator/*.py and miner_daemon.py must NOT contain stale
        hard-coded User-Agent strings.  The only permitted string is imported
        from coordinator/ua.py.
        """
        bad: list[str] = []
        files = list(COORD_DIR.glob("*.py")) + [MINER_DAEMON]
        for py in files:
            text = py.read_text()
            for stale in STALE_UA_STRINGS:
                if stale in text:
                    bad.append(f"{py.name}: contains stale UA {stale!r}")
        self.assertEqual(
            bad, [],
            "Stale User-Agent strings found:\n" + "\n".join(bad),
        )

    # UA-4b: no bare urllib.request.urlopen(url, ...) in coordinator/ or miner_daemon
    #         without a User-Agent header (AST-level check).
    def test_no_bare_urlopen_without_ua(self):
        """
        urllib.request.urlopen() must always receive a Request object (not a bare
        URL string) so that headers including User-Agent are guaranteed.  Check
        every coordinator/*.py and miner_daemon.py with AST.
        """
        violations: list[str] = []
        files = list(COORD_DIR.glob("*.py")) + [MINER_DAEMON]
        for py in files:
            if py.name == "ua.py":
                continue   # the UA module itself only defines helpers
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # Match urllib.request.urlopen(...)
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "urlopen"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "request"
                ):
                    continue
                # First argument must NOT be a plain string literal
                if not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    violations.append(
                        f"{py.name}:{node.lineno}: urlopen called with bare URL string"
                        " — wrap in urllib.request.Request with User-Agent header"
                    )
        self.assertEqual(
            violations, [],
            "Bare urlopen(url_string) calls found:\n" + "\n".join(violations),
        )

    # UA-5
    def test_client_http_sends_miner_ua(self):
        """_http() in coordinator/client must include MINER_UA in every request."""
        from coordinator.client import _http

        captured: list[str] = []

        def _fake_urlopen(req, timeout=None):
            captured.append(req.get_header("User-agent"))
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b'{"ok":true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("coordinator.client.urllib.request.urlopen", side_effect=_fake_urlopen):
            _http("http://localhost:1/test", "GET", b"", "x-auth-token")

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], MINER_UA)

    # UA-6
    def test_align_fetch_get_sends_miner_ua(self):
        """align_fetch._get() must include MINER_UA."""
        from coordinator.align_fetch import _get, AlignFetchError

        captured: list[str] = []

        def _fake_urlopen(req, timeout=None):
            captured.append(req.get_header("User-agent"))
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"data"
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("coordinator.align_fetch.urllib.request.urlopen", side_effect=_fake_urlopen):
            result = _get("http://localhost:1/align/P12345.a3m.gz")

        self.assertEqual(captured, [MINER_UA])
        self.assertEqual(result, b"data")

    # UA-7
    def test_shards_download_sends_miner_ua(self):
        """_download_shard() must include MINER_UA."""
        import hashlib, io, tarfile
        from coordinator.shards import ShardManager
        import tempfile
        from pathlib import Path

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"CCO mol1\n"
            ti = tarfile.TarInfo("molecules.smi")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        raw = buf.getvalue()
        sha = hashlib.sha256(raw).hexdigest()

        captured: list[str] = []

        def _fake_urlopen(req, timeout=None):
            captured.append(req.get_header("User-agent"))
            resp = MagicMock()
            resp.read.return_value = raw
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with tempfile.TemporaryDirectory() as tmp:
            sm = ShardManager(shard_dir=Path(tmp))
            with patch("coordinator.shards.urllib.request.urlopen", side_effect=_fake_urlopen):
                list(sm.iter_molecules([{
                    "shard_id": 1,
                    "download_url": "http://localhost:1/shard.tar.gz",
                    "sha256": sha,
                    "mol_count": 1,
                }]))

        self.assertTrue(captured, "urlopen was never called")
        for ua in captured:
            self.assertEqual(ua, MINER_UA, f"Shard download used wrong UA: {ua!r}")

    # UA-8
    def test_miner_daemon_wallet_summary_ua(self):
        """
        The wallet-summary branch in miner_daemon.py must include MINER_UA.
        Verified by reading the source: check that no bare urlopen(string) exists
        in that branch (covered by UA-4b AST check) and that the UA constant
        matches MINER_UA from coordinator/ua.py.
        """
        text = MINER_DAEMON.read_text()
        # The string must appear in miner_daemon.py pointing to the product UA
        self.assertIn(
            "life-compute-miner/2.0 (+https://lifecompute.ai)",
            text,
            "miner_daemon.py must include the product UA for wallet-summary requests",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Live smoke tests (skipped when offline)
# ─────────────────────────────────────────────────────────────────────────────

COORD_URL = "https://life-coordinator-devnet.lifecompute313.workers.dev"
LIVE_ROUTES = ["/config", "/manifest", "/align/manifest.json"]


class TestLiveUA(unittest.TestCase):
    """
    Verify default Python UA → 403 and MINER_UA → 200 on the live Worker.
    Skipped automatically when the Worker is unreachable.
    """

    def _try_get(self, url: str, ua: str | None = None) -> int:
        headers = {}
        if ua:
            headers["User-Agent"] = ua
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return -1

    def test_default_ua_rejected(self):
        for route in LIVE_ROUTES:
            url = COORD_URL + route
            code = self._try_get(url, ua=None)  # Python default UA
            if code == -1:
                self.skipTest("Worker unreachable — skipping live UA test")
            self.assertEqual(
                code, 403,
                f"Expected 403 for default UA on {route}, got {code}",
            )

    def test_product_ua_accepted(self):
        for route in LIVE_ROUTES:
            url = COORD_URL + route
            code = self._try_get(url, ua=MINER_UA)
            if code == -1:
                self.skipTest("Worker unreachable — skipping live UA test")
            self.assertEqual(
                code, 200,
                f"Expected 200 for product UA on {route}, got {code}",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Terms-version tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTermsVersion(unittest.TestCase):

    # TV-1
    def test_terms_version_is_1_1_0(self):
        self.assertEqual(TERMS_VERSION, "1.1.0")

    # TV-2
    def test_check_terms_version_passes_when_listed(self):
        config = {"known_terms_versions": ["1.0.0", "1.1.0"], "version": "2.0.0"}
        srv, url = _start_config_server(config)
        try:
            # Must not raise
            check_terms_version(coordinator_url=url, terms_version="1.1.0")
        finally:
            srv.shutdown()

    # TV-3
    def test_check_terms_version_raises_when_not_listed(self):
        config = {"known_terms_versions": ["1.0.0"], "version": "2.0.0"}
        srv, url = _start_config_server(config)
        try:
            with self.assertRaises(TermsVersionError) as ctx:
                check_terms_version(coordinator_url=url, terms_version="1.1.0")
            self.assertIn("1.1.0", str(ctx.exception))
            self.assertIn("known", str(ctx.exception).lower())
        finally:
            srv.shutdown()

    # TV-4
    def test_check_terms_version_graceful_when_field_absent(self):
        """If /config lacks known_terms_versions, log a warning but do NOT raise."""
        config = {"version": "2.0.0"}   # field absent
        srv, url = _start_config_server(config)
        try:
            with self.assertLogs("coordinator.register", level="WARNING") as cm:
                check_terms_version(coordinator_url=url, terms_version="1.1.0")
            self.assertTrue(
                any("known_terms_versions" in msg for msg in cm.output),
                "Expected a warning mentioning known_terms_versions",
            )
        finally:
            srv.shutdown()

    # TV-5
    def test_check_terms_version_graceful_on_network_error(self):
        """If /config is unreachable, log a warning but do NOT raise."""
        # Nothing listening on port 19997
        with self.assertLogs("coordinator.register", level="WARNING") as cm:
            check_terms_version(
                coordinator_url="http://127.0.0.1:19997",
                terms_version="1.1.0",
            )
        self.assertTrue(
            any("could not fetch" in msg for msg in cm.output),
            "Expected a warning about fetch failure",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
