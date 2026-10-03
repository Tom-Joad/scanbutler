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
from .paperless import PaperlessClient, PaperlessUnavailable
from .pipeline import process_for_paperless, process_stack

log = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/stacksplit.heartbeat")
PAPERLESS_RETRY_SECONDS = 300


class InboxWatcher:
    def __init__(
        self,
        settings: Settings,
        profile: Profile,
        backend,
        stop: threading.Event,
        reporter: QueueReporter | None = None,
        gate: PauseGate | None = None,
        paperless=None,
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.backend = backend
        self.stop = stop
        self.reporter = reporter
        self.gate = gate or PauseGate(settings.pause_retry_minutes * 60)
        self.paperless = paperless
        # While Paperless is unreachable, files wait in the inbox until then.
        self._retry_after = 0.0
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
            if self.profile.upload:
                process_for_paperless(path, folder, self.settings, self.paperless, self.profile, self.backend)
            else:
                process_stack(path, folder, self.settings, self.backend, self.profile)
        except PaperlessUnavailable as exc:
            # Not this file's fault: keep it in the inbox and try again later.
            self._retry_after = time.monotonic() + PAPERLESS_RETRY_SECONDS
            log.warning(
                "paperless unavailable, retrying later",
                extra={"error": str(exc)[:300], "retry_minutes": PAPERLESS_RETRY_SECONDS // 60},
            )
            return
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the watcher
            if limit := limit_error_in(exc):
                # Not this file's fault: leave it in the inbox and stop
                # sending more until the account accepts work again.
                self.gate.pause(str(limit))
                return
            if self.profile.uses_mistral:  # a plain Paperless upload says nothing about Mistral
                self.gate.done()
            log.exception("stack failed", extra={"profile": self.profile.name, "source": (folder / path.name).as_posix()})
            target = self._move(path, self.profile.failed, folder)
            target.with_name(target.name + ".error.txt").write_text(
                f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}", encoding="utf-8"
            )
        else:
            if self.profile.uses_mistral:
                self.gate.done()
            self._move(path, self.profile.archive, folder)
        finally:
            self._seen.pop(path, None)
            if self.reporter:
                self.reporter.set_processing(self.profile.name, False)

    def poll_once(self) -> None:
        now = time.monotonic()
        if now < self._retry_after:
            return
        for path in self._candidates():
            if self.stop.is_set():
                return
            if self._ready(path, now):
                # Plain Paperless uploads need no Mistral, so a Mistral pause doesn't stop them.
                if self.profile.uses_mistral and not self.gate.may_process():
                    break
                self.process(path)
                if now < self._retry_after:
                    break
        # Forget files that vanished from the inbox.
        self._seen = {p: v for p, v in self._seen.items() if p.exists()}

    def run(self) -> None:
        folders = [self.profile.inbox, self.profile.archive, self.profile.failed]
        if not self.profile.upload:
            folders.append(self.profile.output)
        for directory in folders:
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

    paperless = PaperlessClient(settings.paperless_url, settings.paperless_token) if settings.paperless_url else None
    workers = [
        threading.Thread(
            target=InboxWatcher(settings, profile, backend, stop, reporter, gate, paperless).run,
            name=profile.name,
            daemon=True,
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
