from __future__ import annotations

import json

import httpx

from scanbutler.notify import QueueReporter, describe_error


def make(settings, responder):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.read()))
        return responder()

    reporter = QueueReporter("http://ha.test/api/webhook/queue", settings.profiles, heartbeat_seconds=300)
    return reporter, httpx.Client(transport=httpx.MockTransport(handler)), sent


def test_reports_counts_on_change_and_heartbeat_only(settings):
    stacks, scanner = settings.profile("stacks"), settings.profile("scanner")
    for directory in (stacks.inbox / "Person A", scanner.inbox, scanner.failed):
        directory.mkdir(parents=True)
    (stacks.inbox / "Person A" / "stack-01.pdf").write_bytes(b"%PDF")
    (stacks.inbox / "Person A" / "stack-02.pdf").write_bytes(b"%PDF")
    (scanner.inbox / "scan.pdf").write_bytes(b"%PDF")
    (scanner.inbox / ".partial.pdf").write_bytes(b"")  # hidden: not counted
    (scanner.failed / "bad.pdf").write_bytes(b"x")
    (scanner.failed / "bad.pdf.error.txt").write_text("x")
    reporter, client, sent = make(settings, lambda: httpx.Response(200))
    reporter.set_processing("stacks", True)

    assert reporter.report_if_due(client, now=0)
    assert sent[-1] == {
        "queued": 3,
        "waiting": 2,
        "processing": 1,
        "failed": 1,
        "paused": False,
        "pause_reason": None,
        "paused_since": None,
        "profiles": {
            "stacks": {"waiting": 1, "processing": 1, "failed": 0},
            "scanner": {"waiting": 1, "processing": 0, "failed": 1},
        },
    }
    assert "stack-01" not in json.dumps(sent)  # counts only, never file names

    assert not reporter.report_if_due(client, now=10)  # unchanged: nothing sent
    (scanner.inbox / "scan.pdf").unlink()
    assert reporter.report_if_due(client, now=20)  # changed
    assert sent[-1]["queued"] == 2
    assert reporter.report_if_due(client, now=400)  # heartbeat
    assert len(sent) == 3


def test_unreachable_receiver_is_retried_and_never_raises(settings):
    reporter, client, sent = make(settings, lambda: httpx.Response(503))

    assert not reporter.report_if_due(client, now=0)
    assert not reporter.report_if_due(client, now=1)
    assert len(sent) == 2  # a failed report counts as not sent, so it is tried again


def test_log_shows_sends_and_errors_without_leaking_the_webhook_id(settings, caplog):
    responses = iter([httpx.Response(404, text="Not Found"), httpx.Response(404, text="Not Found"), httpx.Response(200)])
    reporter, client, _ = make(settings, lambda: next(responses))
    caplog.set_level("INFO", logger="scanbutler.notify")

    reporter.report_if_due(client, now=0)
    reporter.report_if_due(client, now=1)  # same error again: not repeated in the log
    reporter.report_if_due(client, now=2)

    events = [(r.getMessage(), getattr(r, "error", None), getattr(r, "reason", None)) for r in caplog.records]
    assert events == [
        ("queue webhook failed", "HTTP 404 Not Found: Not Found", None),
        ("queue webhook reachable again", None, None),
        ("queue webhook sent", None, "change"),
    ]
    assert "api/webhook/queue" not in caplog.text


def test_connection_errors_are_described_without_url():
    request = httpx.Request("POST", "http://ha.test/api/webhook/secret-id")
    text = describe_error(httpx.ConnectError(f"cannot reach {request.url}", request=request))
    assert text == "ConnectError: cannot reach <webhook url>"
