from __future__ import annotations

import json
import threading

import httpx
import pytest

from stacksplit.config import ConfigError, Settings
from stacksplit.paperless import PaperlessClient, PaperlessError, PaperlessUnavailable
from stacksplit.pipeline import UPLOAD
from stacksplit.watcher import InboxWatcher

from .conftest import FakeBackend, make_pdf


class FakePaperless:
    def __init__(self, outcome="SUCCESS", result="Success. New document id 42 created", unavailable=False):
        self.outcome, self.result, self.unavailable = outcome, result, unavailable
        self.uploads: list[tuple[str, list[int]]] = []
        self.contents: list[tuple[int, str]] = []
        self.finished = True

    def upload(self, pdf, filename, tags):
        if self.unavailable:
            raise PaperlessUnavailable("ConnectError: connection refused")
        self.uploads.append((filename, tags))
        return f"task-{len(self.uploads)}"

    def set_content(self, document_id, content):
        if getattr(self, "content_fails", False):
            raise PaperlessError("HTTP 400: content rejected")
        self.contents.append((document_id, content))

    def wait(self, task_id, poll_seconds, max_wait_seconds):
        if not self.finished:
            return None
        return {"task_id": task_id, "status": self.outcome, "result": self.result, "related_document": "42"}


@pytest.fixture
def paperless_settings(settings, monkeypatch):
    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "test-token")
    monkeypatch.setenv("PAPERLESS_TAGS", "3, 7")
    return Settings.from_env()


def watcher_for(settings, client, texts=()):
    profile = settings.profile("paperless")
    profile.inbox.mkdir(parents=True, exist_ok=True)
    backend = FakeBackend(list(texts))  # without texts, any Mistral call would fail
    return InboxWatcher(settings, profile, backend, threading.Event(), paperless=client), profile, backend


def test_upload_success_archives_and_cleans_up(paperless_settings):
    client = FakePaperless()
    watcher, profile, backend = watcher_for(paperless_settings, client)
    make_pdf(profile.inbox / "scan 01.pdf", 2)

    watcher.poll_once()

    assert client.uploads == [("scan 01.pdf", [3, 7])]
    assert (profile.archive / "scan 01.pdf").exists()
    assert not any(profile.inbox.iterdir())
    assert not profile.output.exists()  # nothing is written locally
    assert backend.ocr_calls == 0 and not backend.chat_calls and not backend.jobs
    assert not (paperless_settings.work_dir / "paperless").exists() or not any(
        (paperless_settings.work_dir / "paperless").rglob("*.pdf")
    )


def test_rejected_document_goes_to_failed(paperless_settings):
    client = FakePaperless(outcome="FAILURE", result="Not consuming scan.pdf: It is a duplicate of invoice (#12).")
    watcher, profile, _ = watcher_for(paperless_settings, client)
    make_pdf(profile.inbox / "scan.pdf", 1)

    watcher.poll_once()

    error = (profile.failed / "scan.pdf.error.txt").read_text(encoding="utf-8")
    assert "duplicate" in error


def test_unreachable_paperless_keeps_files_and_backs_off(paperless_settings):
    client = FakePaperless(unavailable=True)
    watcher, profile, _ = watcher_for(paperless_settings, client)
    make_pdf(profile.inbox / "a.pdf", 1)
    make_pdf(profile.inbox / "b.pdf", 1)

    watcher.poll_once()
    client.unavailable = False
    watcher.poll_once()  # still within the back-off: nothing is tried

    assert sorted(p.name for p in profile.inbox.iterdir()) == ["a.pdf", "b.pdf"]
    assert not client.uploads
    assert not profile.failed.exists() or not any(profile.failed.iterdir())


def test_restart_while_paperless_consumes_does_not_upload_twice(paperless_settings):
    client = FakePaperless()
    client.finished = False
    watcher, profile, _ = watcher_for(paperless_settings, client)
    make_pdf(profile.inbox / "a.pdf", 1)

    watcher.poll_once()  # uploaded, consumption not finished yet
    assert (profile.inbox / "a.pdf").exists()
    assert len(list(paperless_settings.work_dir.rglob(UPLOAD))) == 1

    client.finished = True
    watcher._retry_after = 0  # skip the back-off
    watcher.poll_once()

    assert len(client.uploads) == 1
    assert (profile.archive / "a.pdf").exists()


def test_paperless_profile_config(monkeypatch, settings):
    assert "paperless" not in [p.name for p in settings.profiles]  # off without a URL

    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test:8000")
    with pytest.raises(ConfigError, match="TOKEN"):
        Settings.from_env()
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    monkeypatch.setenv("PAPERLESS_TAGS", "3,x")
    with pytest.raises(ConfigError, match="TAGS"):
        Settings.from_env()
    monkeypatch.setenv("PAPERLESS_TAGS", "")
    profile = Settings.from_env().profile("paperless")
    assert profile.upload and not profile.split


# --- the real HTTP client against a simulated Paperless API ------------------


def client_with(handler) -> PaperlessClient:
    client = PaperlessClient("http://paperless.test:8000", "secret-token")
    client._http = httpx.Client(
        base_url="http://paperless.test:8000",
        headers={"Authorization": "Token secret-token"},
        transport=httpx.MockTransport(handler),
    )
    return client


def test_client_uploads_with_tags_and_reads_the_task(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/post_document/":
            seen["auth"] = request.headers["authorization"]
            seen["body"] = request.read()
            return httpx.Response(200, json="3f1c-uuid")
        if request.url.path == "/api/tasks/":
            assert request.url.params["task_id"] == "3f1c-uuid"
            return httpx.Response(200, json=[{"task_id": "3f1c-uuid", "status": "SUCCESS", "related_document": "42"}])
        return httpx.Response(404)

    pdf = tmp_path / "a.pdf"
    make_pdf(pdf, 1)
    client = client_with(handler)

    task_id = client.upload(pdf, "Scan 01.pdf", [3, 7])
    task = client.wait(task_id, poll_seconds=0, max_wait_seconds=1)

    assert seen["auth"] == "Token secret-token"
    assert b'name="tags"\r\n\r\n3' in seen["body"] and b'name="tags"\r\n\r\n7' in seen["body"]
    assert b'filename="Scan 01.pdf"' in seen["body"]
    assert task["status"] == "SUCCESS" and task["related_document"] == "42"


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, PaperlessUnavailable), (403, PaperlessUnavailable), (502, PaperlessUnavailable), (400, PaperlessError)],
)
def test_client_error_classes(tmp_path, status, error):
    pdf = tmp_path / "a.pdf"
    make_pdf(pdf, 1)
    client = client_with(lambda request: httpx.Response(status, json={"detail": "nope"}))
    with pytest.raises(error):
        client.upload(pdf, "a.pdf", [])


def test_task_list_may_be_paginated():
    client = client_with(lambda r: httpx.Response(200, json={"results": [{"status": "STARTED"}]}))
    assert client.task("x") == {"status": "STARTED"}
    assert json.dumps(client_with(lambda r: httpx.Response(200, json=[])).task("x")) == "null"


@pytest.mark.parametrize(
    ("task", "status", "document"),
    [
        # Paperless-ngx 3
        ({"status": "success", "result_data": {"document_id": 7}, "related_document_ids": [7]}, "SUCCESS", 7),
        ({"status": "failure", "result_data": {"error": "duplicate"}, "related_document_ids": []}, "FAILURE", None),
        # Paperless-ngx 2
        ({"status": "SUCCESS", "result": "Success. New document id 7 created", "related_document": "7"}, "SUCCESS", 7),
    ],
)
def test_task_formats_of_both_paperless_versions(task, status, document):
    from stacksplit.paperless import task_document_id, task_message, task_status

    assert task_status(task) == status
    assert task_document_id(task) == document
    assert task_message(task)


def test_same_original_dropped_twice_is_not_uploaded_again(paperless_settings):
    client = FakePaperless()
    watcher, profile, _ = watcher_for(paperless_settings, client)
    make_pdf(profile.inbox / "scan.pdf", 1)
    watcher.poll_once()
    (profile.archive / "scan.pdf").rename(profile.inbox / "scan-again.pdf")  # the very same bytes

    watcher.poll_once()

    assert len(client.uploads) == 1
    error = (profile.failed / "scan-again.pdf.error.txt").read_text(encoding="utf-8")
    assert "already uploaded as Paperless document 42" in error


TABLE = "| Test | Value | Unit |\n|---|---|---|\n| Leukocytes | 6.2 | /nl |"


def test_mistral_text_source_replaces_the_paperless_content(paperless_settings, monkeypatch):
    monkeypatch.setenv("PAPERLESS_TEXT_SOURCE", "mistral")
    settings = Settings.from_env()
    client = FakePaperless()
    watcher, profile, backend = watcher_for(settings, client, [TABLE, "Page two ![img-0.jpeg](img-0.jpeg)"])
    make_pdf(profile.inbox / "lab.pdf", 2)

    watcher.poll_once()

    assert backend.jobs  # Mistral OCR was used (batch)
    assert not backend.chat_calls  # but no naming: Paperless does that
    assert client.contents == [(42, TABLE + "\n\nPage two")]
    assert (profile.archive / "lab.pdf").exists()


def test_failed_content_update_keeps_the_document(paperless_settings, monkeypatch):
    monkeypatch.setenv("PAPERLESS_TEXT_SOURCE", "mistral")
    settings = Settings.from_env()
    client = FakePaperless()
    client.content_fails = True
    watcher, profile, _ = watcher_for(settings, client, ["some text"])
    make_pdf(profile.inbox / "a.pdf", 1)

    watcher.poll_once()

    assert len(client.uploads) == 1
    assert (profile.archive / "a.pdf").exists()  # not failed: the document exists in Paperless


def test_paperless_content_layout():
    from stacksplit.ocr import Page
    from stacksplit.pipeline import paperless_content

    pages = [
        Page(0, "Body one ![img-0.jpeg](img-0.jpeg)", "Letterhead", "Page 1 of 2", False),
        Page(1, "  ", "", "", True),
        Page(2, "Body two", "", "", False),
    ]
    assert paperless_content(pages) == "Letterhead\n\nBody one\n\nPage 1 of 2\n\nBody two"


def test_paperless_text_source_setting(monkeypatch, paperless_settings):
    assert paperless_settings.profile("paperless").text_source == "tesseract"
    assert not paperless_settings.profile("paperless").uses_mistral
    monkeypatch.setenv("PAPERLESS_TEXT_SOURCE", "Mistral")
    assert Settings.from_env().profile("paperless").uses_mistral
    monkeypatch.setenv("PAPERLESS_TEXT_SOURCE", "azure")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_client_patches_the_content():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"], seen["body"] = request.method, request.url.path, json.loads(request.read())
        return httpx.Response(200, json={"id": 42})

    client_with(handler).set_content(42, "| a | b |")
    assert seen == {"method": "PATCH", "path": "/api/documents/42/", "body": {"content": "| a | b |"}}
