"""Delete the work folders of finished files after WORK_RETENTION_DAYS.

A work folder holds a searchable copy of its input and the full OCR text,
so without cleanup it grows as fast as the archive. A folder counts as
finished once review.md exists: write_outputs writes it last, after every
document is in place. Folders of failed or interrupted files have none and
are kept. Running `rebuild` rewrites review.md, which restarts the clock.

The Paperless inputs delete their work folder after a successful upload; one
that failed or was interrupted stays and is removed here once nothing in it
has changed for the retention (unless it still waits for a Paperless task).
Leftovers of ocrmypdf in the temp folder (WORK_DIR/tmp) go after TMP_DAYS.
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
SEARCHABLE = "searchable.pdf"
UPLOAD = "paperless_task.json"
CHECK_SECONDS = 6 * 3600
# ocrmypdf deletes its temp files itself; what a crash left behind is older than this.
TMP_DAYS = 2

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


def _newest(path: Path) -> float:
    """Last change of a folder or file, looking at everything inside."""
    return max((p.stat().st_mtime for p in (path, *path.rglob("*"))), default=0.0)


def _remove(work: Path, root: Path) -> int:
    size = _size(work) if work.is_dir() else work.stat().st_size
    shutil.rmtree(work) if work.is_dir() else work.unlink()
    # Sub-folders mirror the inbox; drop the ones left empty.
    parent = work.parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return size


def _clean_failed_uploads(root: Path, cutoff: float) -> tuple[int, int]:
    """Work folders a Paperless input left behind: its upload failed or was interrupted."""
    removed = freed = 0
    for searchable in list(root.rglob(SEARCHABLE)):
        work = searchable.parent
        with _lock:
            if work in _active:
                continue
        try:
            # A stored task id means the upload is still being followed or retried.
            if (work / UPLOAD).exists() or _newest(work) > cutoff:
                continue
            freed += _remove(work, root)
        except OSError as exc:
            log.warning("work folder not removed", extra={"error": f"{type(exc).__name__}: {exc}"})
            continue
        removed += 1
    return removed, freed


def _clean_tmp(tmp: Path, cutoff: float) -> tuple[int, int]:
    removed = freed = 0
    if not tmp.is_dir():
        return removed, freed
    for entry in list(tmp.iterdir()):
        try:
            if _newest(entry) > cutoff:
                continue
            freed += _remove(entry, tmp)
        except OSError as exc:
            log.warning("temp file not removed", extra={"error": f"{type(exc).__name__}: {exc}"})
            continue
        removed += 1
    return removed, freed


def clean(settings: Settings, now: float | None = None) -> tuple[int, int]:
    """Delete finished work folders older than the retention. Returns (folders, bytes)."""
    if settings.work_retention_days <= 0:
        return 0, 0
    now = time.time() if now is None else now
    cutoff = now - settings.work_retention_days * 86400
    removed = freed = 0
    # Paperless work folders are removed right after a successful upload; its
    # profile folder also holds the upload ledger, which must stay.
    for profile in settings.profiles:
        root = settings.work_dir / profile.name
        if not root.is_dir():
            continue
        if profile.upload:
            count, size = _clean_failed_uploads(root, cutoff)
            removed, freed = removed + count, freed + size
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
                size = _remove(work, root)
            except OSError as exc:
                log.warning("work folder not removed", extra={"error": f"{type(exc).__name__}: {exc}"})
                continue
            removed += 1
            freed += size
    # Only the default location: a TMPDIR set by the operator is theirs.
    count, size = _clean_tmp(settings.work_dir / "tmp", now - TMP_DAYS * 86400)
    removed, freed = removed + count, freed + size
    if removed:
        log.info(
            "work folders cleaned",
            extra={"folders": removed, "freed_mb": round(freed / 2**20, 1), "retention_days": settings.work_retention_days},
        )
    return removed, freed
