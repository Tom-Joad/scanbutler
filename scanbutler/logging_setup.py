"""Structured (JSON lines) logging to stdout.

Document contents and the API key are never passed to the logger: only file
names, page numbers, counts and timings are logged.

Every line logged while a file is being worked on carries that file as
`source`, also lines from helper threads and the Mistral client, which don't
know the file themselves: with several files in progress, a batch job id
alone doesn't say which file it belongs to.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

# The file the current thread works on, as "folder/name" below the inbox.
# Thread pools carry it over through priority.keep.
_SOURCE: ContextVar[str | None] = ContextVar("source", default=None)


@contextmanager
def working_on(source: str):
    """Tag every log line in this block, and in pools it starts, with `source`."""
    token = _SOURCE.set(source)
    try:
        yield
    finally:
        _SOURCE.reset(token)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }

        # Anything passed via logger.info(..., extra={...}) becomes a field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                # ocrmypdf tags every log record with an empty "pageno".
                if key == "pageno" and value is None:
                    continue
                payload[key] = value

        source = _SOURCE.get()
        if source is not None and "source" not in payload:
            payload["source"] = source

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info).splitlines()[-1]

        return json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)


def configure(level: str = "INFO") -> None:
    """Install the JSON formatter as the only stdout handler."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx logs every request URL at INFO; that is noise at this level.
    # Both stay at WARNING even with LOG_LEVEL=DEBUG: request URLs include the
    # webhook URL, whose ID is a secret.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("ocrmypdf").setLevel(logging.WARNING)
