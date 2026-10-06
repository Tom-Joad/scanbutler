from __future__ import annotations

import json
import threading

import httpx
import pytest

from scanbutler.config import ConfigError, Settings
from scanbutler.paperless import PaperlessClient, PaperlessError, PaperlessUnavailable
from scanbutler.pipeline import UPLOAD
from scanbutler.watcher import InboxWatcher

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
    client = FakePaperless(outcome="FAILURE", result="Not consuming scan.pdf: file type not supported")
    watcher, profile, _ = watcher_for(paperless_settings, client)
    make_pdf(profile.inbox / "scan.pdf", 1)

    watcher.poll_once()

    error = (profile.failed / "scan.pdf.error.txt").read_text(encoding="utf-8")
    assert "not supported" in error
    assert not profile.duplicates.exists()


def test_duplicate_rejected_by_paperless_goes_to_duplicates(paperless_settings, caplog):
    client = FakePaperless(outcome="FAILURE", result="Not consuming scan.pdf: It is a duplicate of Invoice ACME (#12).")
    watcher, profile, _ = watcher_for(paperless_settings, client)
    (profile.inbox / "2026").mkdir(parents=True)
    make_pdf(profile.inbox / "2026" / "scan.pdf", 1)
    caplog.set_level("INFO", logger="scanbutler.watcher")

    watcher.poll_once()

    assert (profile.duplicates / "2026" / "scan.pdf").exists()  # inbox sub-folders are mirrored
    assert not list(profile.failed.rglob("*.error.txt"))  # nothing to read
    record = next(r for r in caplog.records if r.getMessage() == "duplicate")
    assert (record.document, record.found_by) == (12, "paperless")
    assert "ACME" not in caplog.text  # the existing document's title stays out of the log


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
    from scanbutler.paperless import task_document_id, task_message, task_status

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
    assert (profile.duplicates / "scan-again.pdf").exists()
    assert not list(profile.failed.rglob("*.error.txt"))


def test_duplicate_messages_name_the_existing_document():
    from scanbutler.paperless import duplicate_of

    assert duplicate_of("Not consuming a.pdf: It is a duplicate of Invoice (#12).") == 12
    assert duplicate_of("a.pdf: It is a duplicate of Invoice (#7). Note: existing document is in the trash.") == 7
    assert duplicate_of("Duplicate file") is None  # a duplicate, but no id given
    assert duplicate_of("Not consuming a.pdf: file type not supported") is False


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
    from scanbutler.ocr import Page
    from scanbutler.pipeline import paperless_content

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


# --- a second Paperless input with its own token ----------------------------


def test_second_input_config(settings, monkeypatch):
    monkeypatch.setenv("PAPERLESS_2_URL", "http://other.test:8000")
    with pytest.raises(ConfigError, match="PAPERLESS_2_TOKEN"):
        Settings.from_env()
    monkeypatch.delenv("PAPERLESS_2_URL")
    monkeypatch.setenv("PAPERLESS_2_TOKEN", "token-two")
    with pytest.raises(ConfigError, match="PAPERLESS_URL or PAPERLESS_2_URL"):
        Settings.from_env()

    # Only a token: same instance as the first input, another user.
    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "first")
    monkeypatch.setenv("PAPERLESS_2_TAGS", "5")
    loaded = Settings.from_env()
    first, second = loaded.profile("paperless"), loaded.profile("paperless-2")
    assert (first.paperless.url, first.paperless.token, first.paperless.tags) == ("http://paperless.test:8000", "first", ())
    assert (second.paperless.url, second.paperless.token, second.paperless.tags) == ("http://paperless.test:8000", "token-two", (5,))
    assert second.root.name == "paperless-2" and second.upload and not second.split
    assert "token-two" not in repr(second)  # tokens stay out of logs

    monkeypatch.setenv("PAPERLESS_2_URL", "http://other.test:8000")
    monkeypatch.setenv("PAPERLESS_2_TEXT_SOURCE", "ocr")
    with pytest.raises(ConfigError, match="PAPERLESS_2_TEXT_SOURCE"):
        Settings.from_env()
    monkeypatch.delenv("PAPERLESS_2_TEXT_SOURCE")
    assert Settings.from_env().profile("paperless-2").paperless.url == "http://other.test:8000"

    # The second input works without the first one.
    monkeypatch.delenv("PAPERLESS_URL")
    monkeypatch.delenv("PAPERLESS_TOKEN")
    assert [p.name for p in Settings.from_env().profiles if p.upload] == ["paperless-2"]


def test_each_input_uploads_its_own_files_with_its_own_tags(paperless_settings, monkeypatch):
    monkeypatch.setenv("PAPERLESS_2_TOKEN", "second")
    monkeypatch.setenv("PAPERLESS_2_TAGS", "5")
    settings = Settings.from_env()
    first_client, second_client = FakePaperless(), FakePaperless()
    first, first_profile, _ = watcher_for(settings, first_client)
    second_profile = settings.profile("paperless-2")
    second_profile.inbox.mkdir(parents=True, exist_ok=True)
    second = InboxWatcher(settings, second_profile, FakeBackend([]), threading.Event(), paperless=second_client)
    make_pdf(first_profile.inbox / "mine.pdf", 1)
    make_pdf(second_profile.inbox / "theirs.pdf", 1)

    first.poll_once()
    second.poll_once()

    assert first_client.uploads == [("mine.pdf", [3, 7])]
    assert second_client.uploads == [("theirs.pdf", [5])]
    assert (second_profile.archive / "theirs.pdf").exists()
    # Separate duplicate registers: the same scan may go to both users.
    assert (settings.work_dir / "paperless" / "uploaded.json").exists()
    assert (settings.work_dir / "paperless-2" / "uploaded.json").exists()


def test_a_rejected_token_names_its_setting(tmp_path, monkeypatch):
    from scanbutler.config import PaperlessTarget
    from scanbutler.paperless import client_for

    pdf = tmp_path / "a.pdf"
    make_pdf(pdf, 1)
    client = client_for(PaperlessTarget("http://paperless.test:8000", "t", (), "PAPERLESS_2_TOKEN"))
    client._http = httpx.Client(base_url="http://paperless.test:8000", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(PaperlessUnavailable, match="check PAPERLESS_2_TOKEN"):
        client.upload(pdf, "a.pdf", [])
