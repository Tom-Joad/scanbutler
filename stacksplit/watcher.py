"""Poll the inbox and process every PDF once it has stopped growing."""

from __future__ import annotations

import logging
import shutil
import signal
import threading
import time
import traceback
from pathlib import Path, PurePosixPath

from .config import Settings
from .naming import unique_path
from .pipeline import process_stack

log = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/stacksplit.heartbeat")


class InboxWatcher:
    def __init__(self, settings: Settings, backend) -> None:
        self.settings = settings
        self.backend = backend
        # path -> (size, mtime_ns, monotonic time the file last changed)
        self._seen: dict[Path, tuple[int, int, float]] = {}
        self._stop = False

    def _heartbeat(self) -> None:
        while not self._stop:
            HEARTBEAT.touch()
            time.sleep(30)

    def stop(self, *_: object) -> None:
        log.info("shutdown requested")
        self._stop = True

    def _candidates(self) -> list[Path]:
        inbox = self.settings.inbox_dir
        return sorted(
            p
            for p in inbox.rglob("*")
            if p.is_file() and p.suffix.lower() == ".pdf" and not any(part.startswith(".") for part in p.relative_to(inbox).parts)
        )

    def _ready(self, path: Path, now: float) -> bool:
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._seen.pop(path, None)
            return False
        size, mtime = stat.st_size, stat.st_mtime_ns
        previous = self._seen.get(path)
        if previous is None or previous[:2] != (size, mtime):
            self._seen[path] = (size, mtime, now)
            return self.settings.stable_seconds == 0 and size > 0
        return size > 0 and now - previous[2] >= self.settings.stable_seconds

    def _move(self, src: Path, root: Path, folder: PurePosixPath) -> Path:
        target_dir = root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(target_dir, src.stem, src.suffix)
        shutil.move(str(src), target)
        return target

    def process(self, path: Path) -> None:
        folder = PurePosixPath(path.parent.relative_to(self.settings.inbox_dir).as_posix())
        try:
            process_stack(path, folder, self.settings, self.backend)
        except Exception as exc:  # noqa: BLE001 - one bad stack must not stop the watcher
            log.exception("stack failed", extra={"source": (folder / path.name).as_posix()})
            target = self._move(path, self.settings.failed_dir, folder)
            target.with_name(target.name + ".error.txt").write_text(
                f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}", encoding="utf-8"
            )
        else:
            self._move(path, self.settings.archive_dir, folder)
        finally:
            self._seen.pop(path, None)

    def run(self) -> None:
        for directory in (
            self.settings.inbox_dir,
            self.settings.output_dir,
            self.settings.work_dir,
            self.settings.archive_dir,
            self.settings.failed_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        # One stack can take an hour; the healthcheck must not flag that as hung.
        threading.Thread(target=self._heartbeat, daemon=True).start()
        log.info("watching inbox", extra={"inbox": str(self.settings.inbox_dir), "poll_s": self.settings.poll_interval})

        while not self._stop:
            now = time.monotonic()
            for path in self._candidates():
                if self._stop:
                    break
                if self._ready(path, now):
                    self.process(path)
            # Forget files that vanished from the inbox.
            self._seen = {p: v for p, v in self._seen.items() if p.exists()}
            for _ in range(self.settings.poll_interval):
                if self._stop:
                    break
                time.sleep(1)
        log.info("stopped")
