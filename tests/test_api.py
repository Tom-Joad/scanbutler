from __future__ import annotations

import io
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scanbutler import api
from scanbutler.config import Settings
from scanbutler.tokens import TokenError, TokenStore

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setenv("API_ENABLED", "true")
    monkeypatch.setenv("API_MAX_UPLOAD_MB", "1")
    settings = Settings.from_env()
    jobs = api.Jobs(tmp_path / "work" / "api" / "jobs")
    tokens = TokenStore(tmp_path / "work" / "api" / "tokens.json")
    client = TestClient(api.create_app(settings, jobs, tokens))
    return settings, jobs, tokens, client


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_tokens_hashed_and_unique(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    token = store.add("cookbutler")
    assert token.startswith("sb_")
    assert token not in (tmp_path / "tokens.json").read_text()
    assert store.check(token) == "cookbutler"
    assert store.check("sb_wrong") is None
    with pytest.raises(TokenError):
        store.add("cookbutler")
    with pytest.raises(TokenError):
        store.add("../evil")
    store.remove("cookbutler")
    assert store.check(token) is None


def test_settings_need_a_key_for_the_api(monkeypatch, tmp_path):
    from scanbutler.config import ConfigError

    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("API_ENABLED", "true")
    monkeypatch.setenv("STACKS_ENABLED", "false")
    monkeypatch.setenv("SCANNER_ENABLED", "false")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="api"):
        Settings.from_env()
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    assert Settings.from_env().profiles == ()


def test_no_token_no_access(setup):
    _, jobs, tokens, client = setup
    assert client.get("/api/v1/jobs").status_code == 401
    # The body of a caller without a token is never written anywhere.
    assert client.post("/api/v1/jobs", content=PDF).status_code == 401
    assert not jobs.root.exists()
    assert client.get("/api/v1/jobs", headers=auth("sb_nope")).status_code == 401
    tokens.add("cookbutler")
    assert client.get("/api/v1/jobs", headers={"Authorization": "Basic x"}).status_code == 401


def test_submit_status_and_owner(setup):
    _, jobs, tokens, client = setup
    mine, other = tokens.add("cookbutler"), tokens.add("other")
    answer = client.post("/api/v1/jobs", headers=auth(mine), content=PDF,
                         params={"filename": "Rezept.pdf", "callback_url": "http://cookbutler.lan/hook"})
    assert answer.status_code == 202
    job = answer.json()
    assert job["status"] == "queued" and job["filename"] == "Rezept.pdf"
    assert "owner" not in job and "callback_url" not in job
    assert answer.headers["location"] == f"/api/v1/jobs/{job['id']}"
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert (jobs.folder(job["id"]) / "input.pdf").read_bytes() == PDF
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=auth(mine)).json()["status"] == "queued"
    # Another program's token sees nothing of it.
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=auth(other)).status_code == 404
    assert client.get("/api/v1/jobs", headers=auth(other)).json() == {"jobs": []}
    assert client.delete(f"/api/v1/jobs/{job['id']}", headers=auth(other)).status_code == 404
    assert client.get(f"/api/v1/jobs/{job['id']}/result.pdf", headers=auth(mine)).status_code == 409
    assert client.delete(f"/api/v1/jobs/{job['id']}", headers=auth(mine)).status_code == 204
    assert not jobs.folder(job["id"]).exists()


def test_refuses_odd_files(setup):
    _, jobs, tokens, client = setup
    token = tokens.add("cookbutler")
    answer = client.post("/api/v1/jobs", headers=auth(token), content=b"MZ\x90\x00")
    assert answer.status_code == 415
    big = PDF + b"0" * (2 << 20)
    assert client.post("/api/v1/jobs", headers=auth(token), content=big).status_code == 413

    def chunks():  # no Content-Length: the limit applies while streaming
        yield PDF
        for _ in range(3):
            yield b"0" * (1 << 20)

    assert client.post("/api/v1/jobs", headers=auth(token), content=chunks()).status_code == 413
    bad_hook = client.post("/api/v1/jobs", headers=auth(token), content=PDF,
                           params={"callback_url": "file:///etc/passwd"})
    assert bad_hook.status_code == 422
    # Nothing is left behind by refused uploads.
    assert [p.name for p in jobs.root.iterdir()] == []


def test_job_ids_are_checked(setup):
    _, _, tokens, client = setup
    token = tokens.add("cookbutler")
    assert client.get("/api/v1/jobs/..%2F..%2Fapi", headers=auth(token)).status_code == 404


def test_worker_runs_job_and_calls_back(setup):
    settings, jobs, tokens, client = setup
    token = tokens.add("cookbutler")
    job = client.post("/api/v1/jobs", headers=auth(token), content=PDF,
                      params={"filename": "r.pdf", "callback_url": "https://cookbutler.lan/hook"}).json()

    def run(job, folder, settings):
        (folder / "result.pdf").write_bytes(PDF)
        (folder / "result.json").write_text('{"pages": []}')
        return 2

    told = []
    worker = api.Worker(jobs, settings, threading.Event(), run=run, notify=told.append)
    assert worker.run_one()
    assert not worker.run_one()
    for thread in threading.enumerate():
        if thread.name == "api-callback":
            thread.join(5)
    done = client.get(f"/api/v1/jobs/{job['id']}", headers=auth(token)).json()
    assert done["status"] == "done" and done["pages"] == 2
    assert done["links"]["pdf"].endswith("/result.pdf")
    pdf = client.get(done["links"]["pdf"], headers=auth(token))
    assert pdf.status_code == 200 and pdf.content == PDF
    assert 'filename="r.pdf"' in pdf.headers["content-disposition"]
    assert told[0]["callback_url"] == "https://cookbutler.lan/hook"


def test_failed_job_keeps_the_error(setup):
    settings, jobs, tokens, client = setup
    token = tokens.add("cookbutler")
    job = client.post("/api/v1/jobs", headers=auth(token), content=PDF, params={"filename": "r.pdf"}).json()

    def run(job, folder, settings):
        raise RuntimeError("ocrmypdf failed in every mode")

    api.Worker(jobs, settings, threading.Event(), run=run, notify=lambda job: None).run_one()
    failed = client.get(f"/api/v1/jobs/{job['id']}", headers=auth(token)).json()
    assert failed["status"] == "failed" and "ocrmypdf" in failed["error"]


def test_recover_and_clean(setup):
    settings, jobs, tokens, client = setup
    token = tokens.add("cookbutler")
    first = client.post("/api/v1/jobs", headers=auth(token), content=PDF).json()
    second = client.post("/api/v1/jobs", headers=auth(token), content=PDF).json()
    jobs.update(first["id"], status="running")
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    jobs.update(second["id"], status="done", finished_at=old)
    worker = api.Worker(jobs, settings, threading.Event())
    worker.recover()
    assert jobs.read(first["id"])["status"] == "queued"
    worker.clean()
    assert not jobs.folder(second["id"]).exists()
    assert jobs.folder(first["id"]).exists()


def test_callback_body_and_retries():
    calls = []

    class Answer:
        def __init__(self, code):
            self.status_code = code

    codes = iter([500, 502, 200])

    def post(url, json, timeout):
        calls.append(json)
        return Answer(next(codes))

    job = {"id": "a" * 32, "status": "done", "error": None, "callback_url": "https://x.lan/h"}
    api.notify(job, post=post, sleep=lambda s: None)
    assert len(calls) == 3
    assert calls[0]["status"] == "ok" and calls[0]["links"]["pdf"].endswith("/result.pdf")


def test_image_to_pdf_upright(tmp_path):
    src = tmp_path / "photo.jpg"
    image = Image.new("RGB", (400, 300), "white")
    exif = image.getexif()
    exif[0x0112] = 6  # rotated: the camera was held upright
    image.save(src, exif=exif)
    dst = tmp_path / "photo.pdf"
    api.image_to_pdf(src, dst)
    import pikepdf

    with pikepdf.open(dst) as pdf:
        box = pdf.pages[0].mediabox
        assert float(box[3]) > float(box[2])  # portrait after the EXIF turn


def test_sniff():
    assert api.sniff(b"%PDF-1.4") == "pdf"
    assert api.sniff(b"\xff\xd8\xff\xe0") == "jpg"
    assert api.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert api.sniff(b"<html>") is None
    buffer = io.BytesIO()
    Image.new("L", (10, 10)).save(buffer, "PNG")
    assert api.sniff(buffer.getvalue()[:16]) == "png"
