"""Poll each profile's inbox and process every PDF once it has stopped growing.

Every profile gets its own worker thread: a document from the scanner must
not wait half an hour behind a 500-page stack. The scanner and Paperless
inputs also work on several files at once; the shared OCR job budget keeps
that within the container's memory. The Mistral client is shared, so all
workers stay within the same request-rate limit.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from . import priority
from .config import Profile, Settings
from .pdfops import looks_complete
from .naming import unique_path
from .notify import QueueReporter
from .pause import PauseGate, limit_error_in
from .paperless import PaperlessClient, PaperlessUnavailable
from .pipeline import AlreadyInProgress, process_for_paperless, process_stack

log = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/scanbutler.heartbeat")
PAPERLESS_RETRY_SECONDS = 300
FOLDER_RETRY_SECONDS = 60
# A PDF without its end is taken as still being written; only after this long
# without change is it processed anyway (and then fails with a clear error).
INCOMPLETE_GRACE_SECONDS = 600


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
        self._incomplete_logged: set[tuple[Path, int]] = set()

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
            if not (self.settings.stable_seconds == 0 and size > 0):
                return False
            unchanged = 0.0
        else:
            unchanged = now - previous[2]
            if size == 0 or unchanged < self.settings.stable_seconds:
                return False
        if looks_complete(path):
            return True
        if unchanged >= max(INCOMPLETE_GRACE_SECONDS, self.settings.stable_seconds):
            return True  # truly damaged: let it fail with an error file
        if previous is not None and previous[:2] == (size, mtime) and (path, size) not in self._incomplete_logged:
            self._incomplete_logged.add((path, size))
            log.info("file not completely written yet, waiting", extra={"profile": self.profile.name, "file": path.name})
        return False

    def _move(self, src: Path, root: Path, folder: PurePosixPath) -> Path:
        target_dir = root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(target_dir, src.stem, src.suffix)
        shutil.move(str(src), target)
        return target

    @staticmethod
    def _signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def process(self, path: Path) -> None:
        folder = PurePosixPath(path.parent.relative_to(self.profile.inbox).as_posix())
        before = self._signature(path)
        if self.reporter:
            self.reporter.set_processing(self.profile.name, True)
        try:
            with priority.marked(self.profile.priority):
                if self.profile.upload:
                    process_for_paperless(path, folder, self.settings, self.paperless, self.profile, self.backend)
                else:
                    process_stack(path, folder, self.settings, self.backend, self.profile)
        except AlreadyInProgress:
            # Next round it is either a known duplicate or, if the other upload failed, uploaded.
            log.info("identical file in progress, trying later", extra={"source": (folder / path.name).as_posix()})
            return
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
            if self._signature(path) != before:
                # Still being written when it was picked up: not a broken file.
                log.warning(
                    "file changed while processing, trying again later",
                    extra={"profile": self.profile.name, "source": (folder / path.name).as_posix(), "error": str(exc)[:300]},
                )
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
        ready = [path for path in self._candidates() if self._ready(path, now)]
        if self.profile.priority and len(ready) > 1:
            self._process_parallel(ready)
        else:
            for path in ready:
                if self.stop.is_set() or not self._may_start():
                    break
                self.process(path)
                if now < self._retry_after:
                    break
        # Forget files that vanished from the inbox.
        self._seen = {p: v for p, v in self._seen.items() if p.exists()}

    def _may_start(self) -> bool:
        # Plain Paperless uploads need no Mistral, so a Mistral pause doesn't stop them.
        return not self.profile.uses_mistral or self.gate.may_process()

    def _process_parallel(self, ready: list[Path]) -> None:
        """Work on several scans at once; their OCR shares the job budget."""
        if self.profile.uses_mistral and self.gate.paused:
            # One probe on its own first; the rest only once it got through.
            if not self.gate.may_process():
                return
            self.process(ready[0])
            if self.gate.paused or len(ready) == 1:
                return
            ready = ready[1:]
        with ThreadPoolExecutor(max_workers=min(len(ready), self.settings.ocrmypdf_jobs)) as pool:
            for path in ready:
                # While paused, may_process lets exactly one probe through.
                if self.stop.is_set() or time.monotonic() < self._retry_after or not self._may_start():
                    break
                pool.submit(self.process, path)

    def _create(self, folders: list[Path]) -> bool:
        try:
            for directory in folders:
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error(
                "cannot create the input folders; check that PUID/PGID may write there",
                extra={
                    "profile": self.profile.name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "uid": os.getuid(),
                    "gid": os.getgid(),
                    "retry_s": FOLDER_RETRY_SECONDS,
                },
            )
            return False
        return True

    def run(self) -> None:
        folders = [self.profile.inbox, self.profile.archive, self.profile.failed]
        if not self.profile.upload:
            folders.append(self.profile.output)
        while not self._create(folders):
            if self.stop.wait(FOLDER_RETRY_SECONDS):
                return
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
