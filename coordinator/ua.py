"""
coordinator/ua.py — canonical User-Agent token for all LIFE Compute miner HTTP requests.

This is the SINGLE source of truth for the User-Agent header.
Every outgoing HTTP request to the coordinator (or files served by it) MUST
use MINER_UA from this module.  Do not hard-code the string anywhere else.

Cloudflare rejects Python's default agent (\"Python-urllib/<version>\") with
error 1010 before authentication runs.  A browser-style agent is also forbidden
by policy (see tests/test_ua.py::TestUserAgent::test_browser_ua_forbidden).

Usage::

    from coordinator.ua import miner_request
    req = miner_request(url, method="GET", headers={"X-Auth": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ...

Or, when building headers manually::

    from coordinator.ua import MINER_UA
    headers = {"User-Agent": MINER_UA, "Content-Type": "application/json"}
"""

from __future__ import annotations

import urllib.request
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# Canonical product token — change only here, never elsewhere in the repo.
# ──────────────────────────────────────────────────────────────────────────────
MINER_UA: str = "life-compute-miner/2.0 (+https://lifecompute.ai)"


def miner_request(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
) -> urllib.request.Request:
    """
    Build a ``urllib.request.Request`` with ``MINER_UA`` already set.

    Any ``User-Agent`` key in *headers* is silently replaced with ``MINER_UA``
    so callers cannot accidentally pass the wrong agent.
    """
    merged: dict = dict(headers or {})
    merged["User-Agent"] = MINER_UA          # always overwrite
    return urllib.request.Request(
        url, data=data, headers=merged, method=method
    )
