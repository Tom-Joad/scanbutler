"""Report the queue to a webhook (e.g. a Home Assistant webhook trigger).

The payload carries counts only, never file names: names of scanned
documents can be as revealing as their content.

A report goes out whenever a count changes, and again every heartbeat
interval even without change, so the receiver recovers by itself after a
restart on either side.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from .config import Profile
from .pause import PauseGate

log = logging.getLogger(__name__)


def count_pdfs(directory) -> int:
    try:
        return sum(
            1
            for p in directory.rglob("*")
            if p.is_file()
            and p.suffix.lower() == ".pdf"
            and not any(part.startswith(".") for part in p.relative_to(directory).parts)
        )
    except OSError:
        # The share can be away for a moment; a missing count is better than a crash.
        return 0


def describe_error(exc: httpx.HTTPError) -> str:
    """A log-safe error text.

    httpx puts the full URL into its messages; for a Home Assistant webhook
    the id in that URL is the only secret, so it must not reach the log.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        body = " ".join(response.text.split())[:200]
        return f"HTTP {response.status_code} {response.reason_phrase}" + (f": {body}" if body else "")
    message = str(exc)
    try:
        message = message.replace(str(exc.request.url), "<webhook url>")
    except RuntimeError:  # httpx raises when no request is attached
        pass
    return f"{type(exc).__name__}: {message}"[:300] if message else type(exc).__name__


@dataclass
class QueueReporter:
    url: str
    profiles: tuple[Profile, ...]
    check_seconds: float = 10.0
    heartbeat_seconds: float = 300.0
    timeout: float = 10.0
    gate: PauseGate | None = None
    _processing: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_sent: dict | None = None
    _last_time: float = 0.0
    _last_error: str | None = None
    _last_error_time: float = 0.0

    def set_processing(self, profile: str, busy: bool) -> None:
        """One file of this input started (True) or ended (False); several may run at once."""
        with self._lock:
            self._processing[profile] = max(0, self._processing.get(profile, 0) + (1 if busy else -1))

    def snapshot(self) -> dict:
        per_profile = {}
        with self._lock:
            busy = dict(self._processing)
        for profile in self.profiles:
            in_inbox = count_pdfs(profile.inbox)
            # Files being worked on stay in the inbox until they are done.
            processing = min(busy.get(profile.name, 0), in_inbox)
            per_profile[profile.name] = {
                "waiting": in_inbox - processing,
                "processing": processing,
                "failed": count_pdfs(profile.failed),
            }
        totals = {key: sum(p[key] for p in per_profile.values()) for key in ("waiting", "processing", "failed")}
        pause = self.gate.status() if self.gate else {"paused": False, "pause_reason": None, "paused_since": None}
        return {"queued": totals["waiting"] + totals["processing"], **totals, **pause, "profiles": per_profile}

    def report_if_due(self, client: httpx.Client, now: float) -> bool:
        state = self.snapshot()
        changed = state != self._last_sent
        if not changed and now - self._last_time < self.heartbeat_seconds:
            return False
        try:
            response = client.post(self.url, json=state, timeout=self.timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Never let the receiver being down affect processing; try again next check.
            error = describe_error(exc)
            # Retries run every few seconds; repeat an unchanged error only once
            # per heartbeat interval so an outage doesn't flood the log.
            if error != self._last_error or now - self._last_error_time >= self.heartbeat_seconds:
                log.warning("queue webhook failed", extra={"error": error, "queued": state["queued"]})
                self._last_error, self._last_error_time = error, now
            return False
        if self._last_error:
            log.info("queue webhook reachable again")
            self._last_error = None
        log.info(
            "queue webhook sent",
            extra={
                "reason": "change" if changed else "heartbeat",
                "status": response.status_code,
                "queued": state["queued"],
                "waiting": state["waiting"],
                "processing": state["processing"],
                "failed": state["failed"],
                "paused": state["paused"],
            },
        )
        self._last_sent, self._last_time = state, now
        return True

    def run(self, stop: threading.Event) -> None:
        with httpx.Client() as client:
            while not stop.is_set():
                self.report_if_due(client, time.monotonic())
                stop.wait(self.check_seconds)
