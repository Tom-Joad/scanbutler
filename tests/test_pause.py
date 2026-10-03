from __future__ import annotations

import threading

import httpx
import pytest

from scanbutler.mistral import MistralClient, MistralError, MistralLimitError, is_limit_response
from scanbutler.pause import PauseGate, limit_error_in
from scanbutler.watcher import InboxWatcher

from .conftest import FakeBackend, make_pdf


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"detail": "Unauthorized"}, True),
        (402, {"message": "Payment required"}, True),
        (403, {"message": "Forbidden"}, True),
        (429, {"message": "Rate limit exceeded", "type": "rate_limited", "code": "1300"}, False),
        (429, {"message": "Monthly spending limit reached"}, True),
        (500, {"message": "Internal error"}, False),
    ],
)
def test_limit_responses_are_told_apart_from_rate_limits(status, body, expected):
    assert is_limit_response(httpx.Response(status, json=body)) is expected


def test_client_raises_limit_error_without_retrying():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"detail": "Unauthorized"})

    client = MistralClient("key", "https://api.test/v1", "ocr", "llm", max_attempts=5)
    client._http = httpx.Client(base_url="https://api.test/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(MistralLimitError):
        client.ocr_pdf(b"%PDF")
    assert len(calls) == 1


def test_limit_error_is_found_behind_wrapping_exceptions():
    try:
        try:
            raise MistralLimitError("HTTP 401")
        except MistralError as inner:
            raise RuntimeError("chunk failed") from inner
    except RuntimeError as outer:
        assert isinstance(limit_error_in(outer), MistralLimitError)
    assert limit_error_in(ValueError("unrelated")) is None


def test_gate_allows_one_probe_per_retry_interval():
    now = [0.0]
    gate = PauseGate(retry_seconds=1800, clock=lambda: now[0])
    assert gate.may_process()

    gate.pause("HTTP 401: Unauthorized")
    assert gate.status()["paused"] and gate.status()["pause_reason"] == "HTTP 401: Unauthorized"
    assert not gate.may_process()

    now[0] = 1800
    assert gate.may_process()  # the probe
    assert not gate.may_process()  # only one at a time
    gate.pause("HTTP 401: Unauthorized")  # probe refused: wait again
    assert not gate.may_process()

    now[0] = 3600
    assert gate.may_process()
    gate.done()
    assert not gate.paused and gate.may_process()


class RefusingBackend(FakeBackend):
    refuse = True

    def submit_batch(self, endpoint, model, lines):
        if self.refuse:
            raise MistralLimitError("/files: HTTP 401: {\"detail\":\"Unauthorized\"}")
        return super().submit_batch(endpoint, model, lines)


def test_watcher_pauses_keeps_files_and_resumes_after_probe(settings):
    scanner = settings.profile("scanner")
    scanner.inbox.mkdir(parents=True)
    texts = ["LETTER Rechnung\nBetrag 42 EUR fuer Laborleistungen nach GOAE, zahlbar in 30 Tagen"]
    make_pdf(scanner.inbox / "a.pdf", 1)
    make_pdf(scanner.inbox / "b.pdf", 1)
    now = [0.0]
    gate = PauseGate(retry_seconds=1800, clock=lambda: now[0])
    backend = RefusingBackend(texts)
    watcher = InboxWatcher(settings, scanner, backend, threading.Event(), gate=gate)

    watcher.poll_once()

    # The account refused: every file stays in the inbox, none counts as failed.
    assert gate.paused
    assert sorted(p.name for p in scanner.inbox.iterdir()) == ["a.pdf", "b.pdf"]
    assert not scanner.failed.exists() or not any(scanner.failed.iterdir())

    watcher.poll_once()  # still within the retry interval: nothing happens
    assert len(list(scanner.inbox.iterdir())) == 2

    backend.refuse = False  # e.g. the limit was raised
    now[0] = 1800
    watcher.poll_once()

    assert not gate.paused
    assert not any(scanner.inbox.iterdir())
    assert len(list(scanner.output.glob("*.pdf"))) == 2
