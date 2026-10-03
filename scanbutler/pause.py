"""Shared pause after the Mistral account refused work (e.g. spending limit).

All workers use the same account, so one refusal pauses all of them. Files
stay in their inboxes. Every `retry_seconds` exactly one worker may try one
file as a probe: if the account answers normally again, processing resumes.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def limit_error_in(exc: BaseException) -> BaseException | None:
    """The MistralLimitError behind `exc`, following the cause/context chain."""
    from .mistral import MistralLimitError

    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, MistralLimitError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


class PauseGate:
    def __init__(self, retry_seconds: float, clock=time.monotonic) -> None:
        self.retry_seconds = retry_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._since: str | None = None
        self._next_probe = 0.0
        self._probing = False

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._reason is not None

    def status(self) -> dict:
        with self._lock:
            return {"paused": self._reason is not None, "pause_reason": self._reason, "paused_since": self._since}

    def may_process(self) -> bool:
        """True when a worker may start a file; while paused, at most one probe at a time."""
        with self._lock:
            if self._reason is None:
                return True
            if self._probing or self._clock() < self._next_probe:
                return False
            self._probing = True
        log.info("pause probe: trying one file")
        return True

    def pause(self, reason: str) -> None:
        with self._lock:
            first = self._reason is None
            self._reason = reason[:300]
            if first:
                self._since = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            self._next_probe = self._clock() + self.retry_seconds
            self._probing = False
        log.error(
            "processing paused: Mistral refused the account (spending limit, quota or API key)" if first
            else "still paused: probe refused again",
            extra={"error": reason[:300], "retry_minutes": round(self.retry_seconds / 60, 1)},
        )

    def done(self) -> None:
        """A file finished without a limit error: the account works again."""
        with self._lock:
            was_paused = self._reason is not None
            self._reason, self._since, self._probing = None, None, False
        if was_paused:
            log.info("processing resumed")
