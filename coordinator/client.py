"""
Coordinator HTTP client with result queue and retry backoff (C3).

Design:
- CoordinatorClient wraps signed GET /work and POST /results.
- After a scored hit, call enqueue_result(); the drain loop sends with backoff.
- Outages: results stay queued in JSONL on disk; draining resumes automatically.
- Duplicates: 409 from coordinator is logged and treated as success (result accepted).
- Endpoint order: coordinator_url first, then fallback_url if set.
- Never calls getProgramAccounts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .auth import MinerKeypair, validate_worker_name
from .ua import MINER_UA

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COORD_URL_DEFAULT = os.environ.get(
    "COORDINATOR_URL", "http://localhost:8787"
)
COORD_FALLBACK_URL: Optional[str] = os.environ.get("COORDINATOR_FALLBACK_URL")

# Backoff: first retry after BACKOFF_BASE_S, doubles each time up to BACKOFF_MAX_S
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S  = 300.0   # 5 min cap

# Maximum candidate string length — must equal the coordinator's MAX_CANDIDATE_BYTES.
# Source of truth: src/routes/results.ts  export const MAX_CANDIDATE_BYTES = 4096
# Keeping these in sync ensures the miner never sends a candidate the coordinator
# would refuse with 400 "candidate too long".
MAX_CANDIDATE_BYTES: int = 4096

# Queue JSONL location — written under the miner's data folder, never /tmp
_WORK_DIR = Path(__file__).resolve().parents[1]
QUEUE_PATH = _WORK_DIR / "data" / "coordinator_queue.jsonl"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class QueuedResult:
    lease_id: str
    molecule_hash: str
    score: float
    candidate: str
    shard_id: Optional[int] = None
    shard_row: Optional[int] = None
    miner_gpu_model: Optional[str] = None
    # internal tracking
    attempts: int = 0
    first_queued_ts: float = field(default_factory=time.time)
    # C28: lease expiry (Unix ms) — 0 means unknown; used for startup stale-drop
    expires_at: int = 0
    # C28: 404-specific retry count — separate from general attempts
    not_found_attempts: int = 0


@dataclass
class Lease:
    lease_id: str
    target: str
    modality: str
    expires_at: int          # Unix ms
    shards: list             # list of shard dicts from coordinator
    hit_threshold: Optional[float]
    scorer_profile: dict
    coord_config_version: str
    pinned_receptor_sha256: Optional[str] = None
    pinned_msa_sha256: Optional[str] = None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http(
    url: str,
    method: str,
    body_bytes: bytes,
    x_auth: str,
    timeout: float = 20.0,
) -> tuple[int, dict]:
    """
    Make an HTTP request.  Returns (status_code, parsed_json).
    Raises urllib.error.URLError / http.client.RemoteDisconnected on network error.
    """
    req = urllib.request.Request(
        url,
        data=body_bytes if body_bytes else None,
        headers={
            "Content-Type": "application/json",
            "X-Auth": x_auth,
            "User-Agent": MINER_UA,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            body = json.loads(raw)
        except Exception:
            body = {"error": str(e)}
        return e.code, body


def _signed_headers(
    keypair: MinerKeypair,
    method: str,
    path: str,
    body_bytes: bytes,
    worker_name: Optional[str],
) -> str:
    x_auth_dict = keypair.sign_request(method, path, body_bytes, worker_name)
    return json.dumps(x_auth_dict)


# ---------------------------------------------------------------------------
# CoordinatorClient
# ---------------------------------------------------------------------------

class CoordinatorClient:
    """
    Client for the LIFE Compute Coordinator API.

    Parameters
    ----------
    keypair        : MinerKeypair — Ed25519 signing key.  In production this
                     is loaded from miner-keypair.json (wallet G8wkwTUC4nyQ…).
                     In tests, pass MinerKeypair.generate().
    coordinator_url: Primary coordinator URL.
    fallback_url   : Optional secondary URL used when the primary is unavailable.
    worker_name    : Optional [A-Za-z0-9-]{1,32} worker label.
    queue_path     : Disk path for the durable result queue JSONL.
    """

    def __init__(
        self,
        keypair: MinerKeypair,
        coordinator_url: str = COORD_URL_DEFAULT,
        fallback_url: Optional[str] = COORD_FALLBACK_URL,
        worker_name: Optional[str] = None,
        queue_path: Path = QUEUE_PATH,
    ):
        if not validate_worker_name(worker_name):
            raise ValueError(
                f"Invalid worker_name {worker_name!r}: must match [A-Za-z0-9-]{{1,32}}"
            )
        self.keypair = keypair
        self.coordinator_url = coordinator_url.rstrip("/")
        self.fallback_url = fallback_url.rstrip("/") if fallback_url else None
        self.worker_name = worker_name
        self.queue_path: Path = Path(queue_path)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._drain_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Endpoint resolution
    # ------------------------------------------------------------------

    def _base_urls(self) -> list[str]:
        """Coordinator first, then optional fallback."""
        urls = [self.coordinator_url]
        if self.fallback_url:
            urls.append(self.fallback_url)
        return urls

    # ------------------------------------------------------------------
    # GET /work
    # ------------------------------------------------------------------

    def get_work(self) -> Optional[Lease]:
        """
        Fetch a lease from the coordinator (or fallback).
        Returns a Lease on success, None on failure.
        """
        path = "/work"
        body_bytes = b""
        for base in self._base_urls():
            url = base + path
            x_auth = _signed_headers(self.keypair, "GET", path, body_bytes, self.worker_name)
            try:
                status, data = _http(url, "GET", body_bytes, x_auth)
                if status == 200:
                    shards = data.get("shards", [])
                    # Attach pinned hashes if the coordinator included them
                    # (C1f: these come from the lease scorer_profile extension)
                    return Lease(
                        lease_id=data["lease_id"],
                        target=data["target"],
                        modality=data.get("modality", "protein"),
                        expires_at=data["expires_at"],
                        shards=shards,
                        hit_threshold=data.get("hit_threshold"),
                        scorer_profile=data.get("scorer_profile", {}),
                        coord_config_version=data.get("coord_config_version", ""),
                        pinned_receptor_sha256=data.get("pinned_receptor_sha256"),
                        pinned_msa_sha256=data.get("pinned_msa_sha256"),
                    )
                log.warning("[COORD] GET /work → %d: %s", status, data.get("error", ""))
            except Exception as e:
                log.warning("[COORD] GET /work from %s failed: %s", base, e)
        return None

    # ------------------------------------------------------------------
    # POST /results (enqueue + drain)
    # ------------------------------------------------------------------

    def enqueue_result(
        self,
        lease_id: str,
        molecule_hash: str,
        score: float,
        candidate: str,
        shard_id: Optional[int] = None,
        shard_row: Optional[int] = None,
        miner_gpu_model: Optional[str] = None,
        expires_at: int = 0,
    ) -> None:
        """
        Persist a scored result to the durable queue and kick the drain loop.
        Thread-safe; never blocks the caller.

        The candidate field must not exceed MAX_CANDIDATE_BYTES characters.
        If it does, the result is dropped (never sent in any altered form),
        an error is logged, and the caller carries on.

        expires_at (C28): Unix milliseconds when the lease expires.  If set,
        the drain loop drops the item at start-up when the expiry has passed.
        Pass 0 (default) when unknown.
        """
        if len(candidate) > MAX_CANDIDATE_BYTES:
            log.error(
                "[COORD] candidate refused: %d chars exceeds MAX_CANDIDATE_BYTES=%d for mol=%s"
                " — result dropped, candidate not altered",
                len(candidate), MAX_CANDIDATE_BYTES, molecule_hash,
            )
            self._oversized_count = getattr(self, "_oversized_count", 0) + 1
            return
        item = QueuedResult(
            lease_id=lease_id,
            molecule_hash=molecule_hash,
            score=score,
            candidate=candidate,
            shard_id=shard_id,
            shard_row=shard_row,
            miner_gpu_model=miner_gpu_model,
            expires_at=expires_at,
        )
        self._append_queue(item)
        self._ensure_drain_running()

    def _append_queue(self, item: QueuedResult) -> None:
        with self._lock:
            with self.queue_path.open("a") as fh:
                fh.write(json.dumps(asdict(item)) + "\n")

    def _load_queue(self) -> list[QueuedResult]:
        items: list[QueuedResult] = []
        if not self.queue_path.exists():
            return items
        with self._lock:
            lines = self.queue_path.read_text().splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                items.append(QueuedResult(**{
                    k: d.get(k)
                    for k in QueuedResult.__dataclass_fields__
                }))
            except Exception as e:
                log.debug("[COORD-QUEUE] bad line: %s", e)
        return items

    def _remove_from_queue(self, molecule_hash: str) -> None:
        """Remove all entries matching molecule_hash from queue file."""
        with self._lock:
            if not self.queue_path.exists():
                return
            lines = self.queue_path.read_text().splitlines()
            kept = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    if d.get("molecule_hash") == molecule_hash:
                        continue
                except Exception:
                    pass
                kept.append(line)
            self.queue_path.write_text("\n".join(kept) + ("\n" if kept else ""))

    def _ensure_drain_running(self) -> None:
        if self._drain_thread and self._drain_thread.is_alive():
            return
        self._drain_thread = threading.Thread(
            target=self._drain_loop,
            name="coord-drain",
            daemon=True,
        )
        self._drain_thread.start()

    def _drain_loop(self) -> None:
        """
        Background drain loop.  Retries queued results with exponential backoff.
        Stops when the queue is empty or _stop_event is set.
        Duplicate 409 responses are treated as success.
        """
        backoff = BACKOFF_BASE_S
        while not self._stop_event.is_set():
            items = self._load_queue()
            if not items:
                log.debug("[COORD-DRAIN] queue empty — drain loop exiting")
                return
            for item in items:
                if self._stop_event.is_set():
                    return
                ok = self._post_result_once(item)
                if ok:
                    self._remove_from_queue(item.molecule_hash)
                    backoff = BACKOFF_BASE_S   # reset on success
            # If still items remain, wait with backoff
            remaining = self._load_queue()
            if remaining:
                log.debug("[COORD-DRAIN] %d items remain; sleeping %.0fs", len(remaining), backoff)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
            else:
                log.debug("[COORD-DRAIN] queue drained")
                return

    def _post_result_once(self, item: QueuedResult) -> bool:
        """
        Try to POST /results for one item.
        Returns True if the coordinator accepted it (200) or it's a dup (409).
        Returns False on any retriable failure (network error, 5xx, etc.).

        C28: 404 (lease not found or expired) is retried at most 3 times.
        On the 4th 404 the item is dropped with a single log line.
        """
        path = "/results"
        body_dict: dict = {
            "lease_id": item.lease_id,
            "molecule_hash": item.molecule_hash,
            "score": item.score,
            "candidate": item.candidate,
        }
        if item.miner_gpu_model is not None:
            body_dict["miner_gpu_model"] = item.miner_gpu_model
        if item.shard_id is not None:
            body_dict["shard_id"] = item.shard_id
        if item.shard_row is not None:
            body_dict["shard_row"] = item.shard_row

        body_bytes = json.dumps(body_dict, separators=(",", ":")).encode()
        x_auth = _signed_headers(self.keypair, "POST", path, body_bytes, self.worker_name)

        for base in self._base_urls():
            url = base + path
            try:
                status, data = _http(url, "POST", body_bytes, x_auth)
                if status == 200:
                    log.info("[COORD] ✔ POST /results accepted  mol=%s  target=%s",
                             item.molecule_hash, data.get("target"))
                    return True
                if status == 409:
                    log.info("[COORD] POST /results 409 duplicate — treating as accepted  mol=%s",
                             item.molecule_hash)
                    return True
                if status == 404:
                    # C28: 404 = lease not found or expired.
                    # Retry up to 3 times; on the 4th 404 drop the item permanently.
                    item.not_found_attempts += 1
                    if item.not_found_attempts >= 3:
                        log.warning(
                            "[COORD] POST /results 404 × %d (lease not found/expired)"
                            " — dropping mol=%s permanently",
                            item.not_found_attempts, item.molecule_hash,
                        )
                        return True   # drop from queue
                    # Persist the updated not_found_attempts count back to the queue
                    self._update_not_found_attempts(item)
                    log.warning(
                        "[COORD] POST /results 404 (lease not found/expired)"
                        " mol=%s attempt %d/3 — will retry",
                        item.molecule_hash, item.not_found_attempts,
                    )
                    return False   # retry later
                if status == 410:
                    log.warning("[COORD] POST /results 410 lease expired  mol=%s — dropping",
                                item.molecule_hash)
                    return True   # drop expired leases from queue
                if status in (400, 401, 403, 422):
                    log.warning("[COORD] POST /results %d (non-retriable): %s",
                                status, data.get("error", ""))
                    return True   # don't retry auth/body errors forever
                log.warning("[COORD] POST /results %d: %s", status, data.get("error", ""))
            except Exception as e:
                log.warning("[COORD] POST /results to %s network error: %s", base, e)
        return False

    def _update_not_found_attempts(self, item: QueuedResult) -> None:
        """Rewrite the JSONL queue with the updated not_found_attempts for this item."""
        with self._lock:
            if not self.queue_path.exists():
                return
            lines = self.queue_path.read_text().splitlines()
            updated: list[str] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    if d.get("molecule_hash") == item.molecule_hash:
                        d["not_found_attempts"] = item.not_found_attempts
                        updated.append(json.dumps(d))
                        continue
                except Exception:
                    pass
                updated.append(line)
            self.queue_path.write_text("\n".join(updated) + ("\n" if updated else ""))

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background drain loop (idempotent)."""
        self._ensure_drain_running()

    def stop(self) -> None:
        """Signal the drain loop to stop."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # C28: Start-up stale-lease drop
    # ------------------------------------------------------------------

    def drop_stale_queued_results(self) -> int:
        """
        C28: Drop any queued result whose lease has already expired.

        Call once at miner start-up before kicking the drain loop.
        Returns the count of items dropped and logs a single summary line.

        An item is stale when expires_at > 0 and expires_at < time.time()*1000.
        Items with expires_at == 0 (old JSONL rows without the field) are kept.
        """
        now_ms = int(time.time() * 1000)
        items = self._load_queue()
        dropped = 0
        for item in items:
            if item.expires_at and item.expires_at < now_ms:
                self._remove_from_queue(item.molecule_hash)
                dropped += 1
        if dropped:
            log.info(
                "[COORD] start-up: dropped %d stale queued result(s) with expired leases",
                dropped,
            )
        return dropped
