from __future__ import annotations

import logging
import threading

import pytest

from scanbutler import watcher as watcher_module
from scanbutler.watcher import INCOMPLETE_GRACE_SECONDS, InboxWatcher

from .conftest import FakeBackend, make_pdf


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(watcher_module.time, "monotonic", clock)
    return clock


@pytest.fixture
def stable_settings(settings, monkeypatch):
    from scanbutler.config import Settings

    monkeypatch.setenv("STABLE_SECONDS", "60")
    return Settings.from_env()


def scanner_watcher(settings):
    profile = settings.profile("scanner")
    profile.inbox.mkdir(parents=True, exist_ok=True)
    return InboxWatcher(settings, profile, FakeBackend([]), threading.Event()), profile


def waiting_lines(caplog):
    return [r for r in caplog.records if r.message == "file still waiting"]


def test_an_empty_file_fails_after_the_grace_period(stable_settings, clock, caplog):
    watcher, profile = scanner_watcher(stable_settings)
    (profile.inbox / "scan.pdf").write_bytes(b"")

    watcher.poll_once()
    clock.now += INCOMPLETE_GRACE_SECONDS - 1
    watcher.poll_once()
    assert (profile.inbox / "scan.pdf").exists()  # a scanner may still fill it

    clock.now += 2
    with caplog.at_level(logging.WARNING):
        watcher.poll_once()

    assert not (profile.inbox / "scan.pdf").exists()
    error = (profile.failed / "scan.pdf.error.txt").read_text(encoding="utf-8")
    assert error.startswith("EmptyFile: empty file (0 bytes)")


def test_a_file_that_keeps_changing_is_named_once_and_left_alone(stable_settings, clock, caplog):
    watcher, profile = scanner_watcher(stable_settings)
    path = profile.inbox / "copying.pdf"
    with caplog.at_level(logging.WARNING):
        for step in range(30):
            path.write_bytes(b"%PDF-1.4\n" + b"x" * (step + 1))  # grows on every poll
            watcher.poll_once()
            clock.now += 30

    lines = waiting_lines(caplog)
    assert len(lines) == 1
    assert lines[0].file == "copying.pdf" and lines[0].reason == "still changing" and lines[0].minutes >= 10
    assert path.exists() and not profile.failed.exists()


def test_a_file_that_is_processed_in_time_is_not_named(stable_settings, clock, caplog):
    watcher, profile = scanner_watcher(stable_settings)
    make_pdf(profile.inbox / "scan.pdf", 1)
    with caplog.at_level(logging.WARNING):
        watcher.poll_once()
        clock.now += 61
        watcher.poll_once()  # stable now: processed (fails without texts, that's fine)
    assert waiting_lines(caplog) == []


def test_an_empty_probe_does_not_end_a_pause(stable_settings, clock):
    watcher, profile = scanner_watcher(stable_settings)
    watcher.gate.pause("spending limit reached")
    watcher.gate._next_probe = 0  # probe allowed now
    (profile.inbox / "scan.pdf").write_bytes(b"")

    watcher.poll_once()
    clock.now += INCOMPLETE_GRACE_SECONDS + 1
    watcher.poll_once()

    assert (profile.failed / "scan.pdf.error.txt").exists()
    assert watcher.gate.paused  # nothing was learned about Mistral
    assert watcher.gate.may_process()  # and the next file may probe
