from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scanbutler import pdfops, watcher
from scanbutler.watcher import InboxWatcher

from .conftest import FakeBackend, make_pdf


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def test_a_timeout_kills_the_workers_too(tmp_path):
    pidfile = tmp_path / "child.pid"
    # Stands in for ocrmypdf: starts a worker that outlives it, then hangs.
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        pdfops.run_group([sys.executable, "-c", script], timeout=1.5)
    child = int(pidfile.read_text())
    wait_until(lambda: not alive(child))


def test_run_group_returns_output_and_code():
    result = pdfops.run_group([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], 30)
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")


def test_an_error_in_parallel_processing_is_logged(settings, monkeypatch, caplog):
    scanner = settings.profile("scanner")
    scanner.inbox.mkdir(parents=True)
    for n in range(2):
        make_pdf(scanner.inbox / f"scan-{n}.pdf", 1)

    def broken(self, *args, **kwargs):
        raise OSError("read-only share")

    monkeypatch.setattr(InboxWatcher, "_move", broken)
    monkeypatch.setattr(watcher, "process_stack", lambda *a, **k: None)
    with caplog.at_level("ERROR"):
        InboxWatcher(settings, scanner, FakeBackend([]), threading.Event()).poll_once()

    failures = [r for r in caplog.records if r.getMessage() == "inbox poll failed"]
    assert sorted(r.source for r in failures) == ["scan-0.pdf", "scan-1.pdf"]
    assert all(r.profile == "scanner" and r.exc_info for r in failures)


@pytest.mark.parametrize("dead", ["scanner", "queue-webhook"])
def test_the_service_stops_when_one_thread_ends(settings, monkeypatch, caplog, tmp_path, dead):
    from dataclasses import replace

    settings = replace(settings, queue_webhook_url="http://ha.test/hook")
    monkeypatch.setattr(watcher, "HEARTBEAT", tmp_path / "heartbeat")
    monkeypatch.setattr(watcher, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(watcher.signal, "signal", lambda *a: None)
    release = threading.Event()

    def run(self, *args):
        name = getattr(self, "profile", None)
        name = name.name if name else "queue-webhook"
        if name != dead:
            release.wait(5)  # the others keep running

    monkeypatch.setattr(InboxWatcher, "run", run)
    monkeypatch.setattr(watcher.QueueReporter, "run", run)
    try:
        with caplog.at_level("ERROR"):
            watcher.run_all(settings, FakeBackend([]))
    finally:
        release.set()

    [record] = [r for r in caplog.records if r.getMessage().startswith("stopped:")]
    assert record.threads == [dead]
