"""Delete the work folders of finished files after WORK_RETENTION_DAYS.

A work folder holds a searchable copy of its input and the full OCR text,
so without cleanup it grows as fast as the archive. A folder counts as
finished once review.md exists: write_outputs writes it last, after every
document is in place. Folders of failed or interrupted files have none and
are kept. Running `rebuild` rewrites review.md, which restarts the clock.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

REVIEW = "review.md"
PLAN = "plan.json"
CHECK_SECONDS = 6 * 3600

_active: set[Path] = set()
_lock = threading.Lock()


@contextmanager
def in_use(work: Path):
    """Mark a work folder as being processed, so cleanup leaves it alone."""
    with _lock:
        _active.add(work)
    try:
        yield
    finally:
        with _lock:
            _active.discard(work)


def _last_change(work: Path) -> float:
    return max(p.stat().st_mtime for p in (work / REVIEW, work / PLAN) if p.exists())


def _size(work: Path) -> int:
    return sum(p.stat().st_size for p in work.rglob("*") if p.is_file())


def clean(settings: Settings, now: float | None = None) -> tuple[int, int]:
    """Delete finished work folders older than the retention. Returns (folders, bytes)."""
    if settings.work_retention_days <= 0:
        return 0, 0
    now = time.time() if now is None else now
    cutoff = now - settings.work_retention_days * 86400
    removed = freed = 0
    # Paperless work folders are removed right after the upload; its
    # profile folder only holds the upload ledger, which must stay.
    for profile in settings.profiles:
        if profile.upload:
            continue
        root = settings.work_dir / profile.name
        if not root.is_dir():
            continue
        for review in list(root.rglob(REVIEW)):
            work = review.parent
            if not (work / PLAN).exists():
                continue
            with _lock:
                if work in _active:
                    continue
            try:
                if _last_change(work) > cutoff:
                    continue
                size = _size(work)
                shutil.rmtree(work)
            except OSError as exc:
                log.warning("work folder not removed", extra={"error": f"{type(exc).__name__}: {exc}"})
                continue
            removed += 1
            freed += size
            # Sub-folders mirror the inbox; drop the ones left empty.
            parent = work.parent
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    if removed:
        log.info(
            "work folders cleaned",
            extra={"folders": removed, "freed_mb": round(freed / 2**20, 1), "retention_days": settings.work_retention_days},
        )
    return removed, freed
