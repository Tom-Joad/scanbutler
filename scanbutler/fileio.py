"""Writing state files so a crash never leaves a half-written one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_text(path: Path, text: str) -> None:
    """Write to a temporary file next to `path`, then rename it over `path`.

    A crash, an OOM kill or a full disk mid-write leaves the old file, not a
    truncated one that every later attempt would fail to read.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any, **dumps: Any) -> None:
    write_text(path, json.dumps(data, **dumps))
