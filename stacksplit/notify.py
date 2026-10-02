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


@dataclass
class QueueReporter:
    url: str
    profiles: tuple[Profile, ...]
    check_seconds: float = 10.0
    heartbeat_seconds: float = 300.0
    timeout: float = 10.0
    _processing: dict[str, bool] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_sent: dict | None = None
    _last_time: float = 0.0

    def set_processing(self, profile: str, busy: bool) -> None:
        with self._lock:
            self._processing[profile] = busy

    def snapshot(self) -> dict:
        per_profile = {}
        with self._lock:
            busy = dict(self._processing)
        for profile in self.profiles:
            in_inbox = count_pdfs(profile.inbox)
            # The file being worked on stays in the inbox until it is done.
            processing = 1 if busy.get(profile.name) and in_inbox else 0
            per_profile[profile.name] = {
                "waiting": in_inbox - processing,
                "processing": processing,
                "failed": count_pdfs(profile.failed),
            }
        totals = {key: sum(p[key] for p in per_profile.values()) for key in ("waiting", "processing", "failed")}
        return {"queued": totals["waiting"] + totals["processing"], **totals, "profiles": per_profile}

    def report_if_due(self, client: httpx.Client, now: float) -> bool:
        state = self.snapshot()
        if state == self._last_sent and now - self._last_time < self.heartbeat_seconds:
            return False
        try:
            response = client.post(self.url, json=state, timeout=self.timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Never let the receiver being down affect processing; try again next check.
            log.warning("queue webhook failed", extra={"error": str(exc)[:200]})
            return False
        if state != self._last_sent:
            log.info("queue reported", extra={"queued": state["queued"], "failed": state["failed"]})
        self._last_sent, self._last_time = state, now
        return True

    def run(self, stop: threading.Event) -> None:
        with httpx.Client() as client:
            while not stop.is_set():
                self.report_if_due(client, time.monotonic())
                stop.wait(self.check_seconds)
