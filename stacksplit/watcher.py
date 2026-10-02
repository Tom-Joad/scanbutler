"""Poll each profile's inbox and process every PDF once it has stopped growing.

Every profile gets its own worker thread: a document from the scanner must
not wait half an hour behind a 500-page stack. The Mistral client is shared,
so both workers stay within the same request-rate limit.
"""

from __future__ import annotations

import logging
import shutil
import signal
import threading
import time
import traceback
from pathlib import Path, PurePosixPath

from .config import Profile, Settings
from .naming import unique_path
from .notify import QueueReporter
from .pause import PauseGate, limit_error_in
from .pipeline import process_stack

log = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/stacksplit.heartbeat")


class InboxWatcher:
    def __init__(
        self,
        settings: Settings,
        profile: Profile,
        backend,
        stop: threading.Event,
        reporter: QueueReporter | None = None,
        gate: PauseGate | None = None,
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.backend = backend
        self.stop = stop
        self.reporter = reporter
        self.gate = gate or PauseGate(settings.pause_retry_minutes * 60)
        # path -> (size, mtime_ns, monotonic time the file last changed)
        self._seen: dict[Path, tuple[int, int, float]] = {}

    def _candidates(self) -> list[Path]:
        inbox = self.profile.inbox
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
        folder = PurePosixPath(path.parent.relative_to(self.profile.inbox).as_posix())
        if self.reporter:
            self.reporter.set_processing(self.profile.name, True)
        try:
            process_stack(path, folder, self.settings, self.backend, self.profile)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the watcher
            if limit := limit_error_in(exc):
                # Not this file's fault: leave it in the inbox and stop
                # sending more until the account accepts work again.
                self.gate.pause(str(limit))
                return
            self.gate.done()
            log.exception("stack failed", extra={"profile": self.profile.name, "source": (folder / path.name).as_posix()})
            target = self._move(path, self.profile.failed, folder)
            target.with_name(target.name + ".error.txt").write_text(
                f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}", encoding="utf-8"
            )
        else:
            self.gate.done()
            self._move(path, self.profile.archive, folder)
        finally:
            self._seen.pop(path, None)
            if self.reporter:
                self.reporter.set_processing(self.profile.name, False)

    def poll_once(self) -> None:
        now = time.monotonic()
        for path in self._candidates():
            if self.stop.is_set():
                return
            if self._ready(path, now):
                if not self.gate.may_process():
                    break
                self.process(path)
        # Forget files that vanished from the inbox.
        self._seen = {p: v for p, v in self._seen.items() if p.exists()}

    def run(self) -> None:
        for directory in (self.profile.inbox, self.profile.output, self.profile.archive, self.profile.failed):
            directory.mkdir(parents=True, exist_ok=True)
        log.info("watching inbox", extra={"profile": self.profile.name, "inbox": str(self.profile.inbox)})
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - e.g. the share went away for a moment
                log.exception("inbox poll failed", extra={"profile": self.profile.name})
            self.stop.wait(self.settings.poll_interval)


def run_all(settings: Settings, backend) -> None:
    """Watch every enabled profile until SIGTERM/SIGINT."""
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        log.info("shutdown requested")
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    gate = PauseGate(settings.pause_retry_minutes * 60)
    reporter = None
    if settings.queue_webhook_url:
        reporter = QueueReporter(
            settings.queue_webhook_url,
            settings.profiles,
            settings.queue_webhook_check_seconds,
            settings.queue_webhook_heartbeat_seconds,
            gate=gate,
        )
        threading.Thread(target=reporter.run, args=(stop,), name="queue-webhook", daemon=True).start()
        log.info("queue webhook enabled", extra={"heartbeat_s": settings.queue_webhook_heartbeat_seconds})

    workers = [
        threading.Thread(
            target=InboxWatcher(settings, profile, backend, stop, reporter, gate).run, name=profile.name, daemon=True
        )
        for profile in settings.profiles
    ]
    for worker in workers:
        worker.start()

    # The main thread keeps the healthcheck heartbeat going; a single stack
    # can take an hour, which must not count as hung.
    while not stop.is_set() and any(worker.is_alive() for worker in workers):
        HEARTBEAT.touch()
        stop.wait(30)
    log.info("stopped")
