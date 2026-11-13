from __future__ import annotations

import threading

import httpx
import pytest

from scanbutler.mistral import MistralClient, MistralError, MistralLimitError, MistralUnavailable, is_limit_response
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

    def submit_batch(self, endpoint, model, lines, on_uploaded=None):
        if self.refuse:
            raise MistralLimitError("/files: HTTP 401: {\"detail\":\"Unauthorized\"}")
        return super().submit_batch(endpoint, model, lines, on_uploaded)


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


def _client(handler, attempts=3):
    client = MistralClient("key", "https://api.test/v1", "ocr", "llm", max_attempts=attempts)
    client._http = httpx.Client(base_url="https://api.test/v1", transport=httpx.MockTransport(handler))
    return client


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, {"message": "Too many requests"}, False),  # nothing says "quota": the rate limit
        (429, ["not", "an", "object"], False),
        (429, "just text", False),
        (429, None, False),
        (429, {"message": "Insufficient credit"}, True),
    ],
)
def test_429_is_only_a_limit_when_it_says_so(status, body, expected):
    assert is_limit_response(httpx.Response(status, json=body)) is expected
    assert is_limit_response(httpx.Response(429, content=b"<html>busy</html>")) is False


def test_outage_after_all_retries_is_unavailable_not_a_bad_file(monkeypatch):
    monkeypatch.setattr("scanbutler.mistral.time.sleep", lambda s: None)
    client = _client(lambda request: httpx.Response(503, text="down"))
    client._back_off = lambda *a, **k: None
    with pytest.raises(MistralUnavailable):
        client.ocr_pdf(b"%PDF")

    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    client = _client(refuse)
    client._back_off = lambda *a, **k: None
    with pytest.raises(MistralUnavailable):
        client.ocr_pdf(b"%PDF")
    # A 400 is the file's fault and stays a plain error.
    with pytest.raises(MistralError) as caught:
        _client(lambda request: httpx.Response(400, json={"message": "bad"})).ocr_pdf(b"%PDF")
    assert not isinstance(caught.value, MistralUnavailable)


def test_uploads_are_not_repeated_after_a_read_timeout():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler)
    client._back_off = lambda *a, **k: None
    with pytest.raises(MistralUnavailable):
        client.submit_batch("/v1/ocr", "m", [("a", {"x": 1})])
    assert calls == ["/v1/files"]  # one try: the upload may have arrived


class DownBackend(FakeBackend):
    down = True

    def submit_batch(self, endpoint, model, lines, on_uploaded=None):
        if self.down:
            raise MistralUnavailable("/files: transport error: ConnectError")
        return super().submit_batch(endpoint, model, lines, on_uploaded)


def test_watcher_keeps_the_file_during_a_mistral_outage_and_retries(settings):
    scanner = settings.profile("scanner")
    scanner.inbox.mkdir(parents=True)
    make_pdf(scanner.inbox / "a.pdf", 1)
    texts = ["LETTER Rechnung\nBetrag 42 EUR fuer Laborleistungen nach GOAE, zahlbar in 30 Tagen"]
    backend = DownBackend(texts)
    watcher = InboxWatcher(settings, scanner, backend, threading.Event())

    watcher.poll_once()

    assert [p.name for p in scanner.inbox.iterdir()] == ["a.pdf"]
    assert not scanner.failed.exists() or not any(scanner.failed.iterdir())
    watcher._retry_after = 0.0  # the retry interval passed
    backend.down = False
    watcher.poll_once()
    assert not any(scanner.inbox.iterdir())
    assert len(list(scanner.output.glob("*.pdf"))) == 1


def test_a_failure_while_stopping_leaves_the_file_in_the_inbox(settings):
    scanner = settings.profile("scanner")
    scanner.inbox.mkdir(parents=True)
    make_pdf(scanner.inbox / "a.pdf", 1)

    class Closed(FakeBackend):
        def submit_batch(self, *args, **kwargs):
            stop.set()  # SIGTERM arrives; the client is closed under the worker
            raise RuntimeError("Cannot send a request, as the client has been closed.")

    stop = threading.Event()
    watcher = InboxWatcher(settings, scanner, Closed(["x"]), stop)

    watcher.poll_once()

    assert [p.name for p in scanner.inbox.iterdir()] == ["a.pdf"]
    assert not scanner.failed.exists() or not any(scanner.failed.iterdir())
