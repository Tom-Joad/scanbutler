"""Structured (JSON lines) logging to stdout.

Document contents and the API key are never passed to the logger: only file
names, page numbers, counts and timings are logged.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("ocrmypdf").setLevel(logging.WARNING)
