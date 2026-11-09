from __future__ import annotations

import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scanbutler import api, ocr_plugin
from scanbutler.api import Worker

from .test_api import PDF, auth, setup  # noqa: F401 - the fixture
from .test_hybrid import PRINTED, block, page


def make_job(client, tokens):
    token = tokens.add("cookbutler")
    job = client.post("/api/v1/jobs", headers=auth(token), content=PDF).json()
    return token, job["id"]


def test_delete_of_a_job_the_cleanup_just_removed_is_404(setup, monkeypatch):
    _, jobs, tokens, client = setup
    token, job_id = make_job(client, tokens)
    real = api.Jobs.read
    reads = []

    def vanishing(self, job):
        reads.append(job)
        if len(reads) > 1:  # `own` read it; by the second read it is gone
            raise KeyError(job)
        return real(self, job)

    monkeypatch.setattr(api.Jobs, "read", vanishing)
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=auth(token)).status_code == 404


def test_an_unreadable_job_is_404_logged_once_and_cleaned_up(setup, caplog):
    settings, jobs, tokens, client = setup
    token, job_id = make_job(client, tokens)
    path = jobs.folder(job_id) / "job.json"
    path.write_text('{"id": "cut off', encoding="utf-8")

    with caplog.at_level("ERROR"):
        for _ in range(3):
            assert client.get(f"/api/v1/jobs/{job_id}", headers=auth(token)).status_code == 404
            assert client.get("/api/v1/jobs", headers=auth(token)).json() == {"jobs": []}
    assert sum(r.getMessage() == "api job unreadable" for r in caplog.records) == 1

    old = time.time() - 2 * api.LEFTOVER_SECONDS
    os.utime(jobs.folder(job_id), (old, old))
    assert jobs.leftovers(time.time() - api.LEFTOVER_SECONDS) == [jobs.folder(job_id)]
    Worker(jobs, settings, threading.Event()).clean()
    assert not jobs.folder(job_id).exists()


def test_an_unreadable_token_file_answers_503_and_logs_once(setup, caplog):
    _, jobs, tokens, client = setup
    token = tokens.add("cookbutler")
    good = tokens.path.read_text(encoding="utf-8")
    tokens.path.write_text("{ not json", encoding="utf-8")
    other = api.TokenStore(tokens.path)  # a fresh store has no earlier good list

    fresh = TestClient(api.create_app(setup[0], jobs, other))
    with caplog.at_level("ERROR"):
        for _ in range(3):
            assert fresh.get("/api/v1/jobs", headers=auth(token)).status_code == 503
    assert sum(r.getMessage() == "api tokens unreadable" for r in caplog.records) == 1

    # Once the file is fixed, the next request works again.
    tokens.path.write_text(good, encoding="utf-8")
    assert fresh.get("/api/v1/jobs", headers=auth(token)).status_code == 200


def test_the_last_good_token_list_is_kept(setup):
    _, jobs, tokens, client = setup
    token = tokens.add("cookbutler")
    assert client.get("/api/v1/jobs", headers=auth(token)).status_code == 200
    tokens.path.write_text("{ not json", encoding="utf-8")
    assert client.get("/api/v1/jobs", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/jobs", headers=auth("sb_nope")).status_code == 401


def test_mistral_settings_reach_the_plugin_as_validated(setup, monkeypatch, tmp_path):
    settings, jobs, tokens, client = setup
    # What a .env line without a value and a pasted key can look like.
    monkeypatch.setenv("MISTRAL_TIMEOUT", "")
    monkeypatch.setenv("MISTRAL_API_KEY", " test\n")
    monkeypatch.setenv("MISTRAL_OCR_MODEL", "")
    monkeypatch.setenv("MISTRAL_API_BASE", "")
    settings = settings.from_env()
    token, job_id = make_job(client, tokens)
    folder = jobs.folder(job_id)
    (folder / "input.pdf").write_bytes(PDF)
    captured = {}

    def fake(pdf, result, languages, extra_args, limits, **kwargs):
        captured.update(kwargs["env"])
        raise RuntimeError("stop here")

    monkeypatch.setattr(api.pdfops, "make_searchable", fake)
    with pytest.raises(RuntimeError, match="stop here"):
        api.process(jobs.read(job_id), folder, settings)

    client_ = ocr_plugin.client_from_env(captured)  # used to be float("") -> ValueError
    assert client_.ocr_model == "mistral-ocr-latest"
    assert captured["MISTRAL_API_KEY"] == "test" and captured["MISTRAL_TIMEOUT"] == "300.0"
    assert captured["MISTRAL_API_BASE"] == "https://api.mistral.ai/v1"


@pytest.mark.parametrize(
    "reading",
    [
        {"blocks": [block("text", "left", 0, 100, 100, "x")], "dimensions": {"width": 100, "height": 100}},
        {"blocks": [block("text", 0, 0, 100, 100, "x")], "dimensions": {"width": "wide", "height": 100}},
    ],
)
def test_a_malformed_mistral_answer_keeps_tesseracts_reading(tmp_path, monkeypatch, reading):
    image_file = tmp_path / "page.png"
    Image.new("L", (200, 200), 255).save(image_file)
    folder = tmp_path / "mistral"
    folder.mkdir()
    monkeypatch.setenv(ocr_plugin.HYBRID_DIR, str(folder))
    monkeypatch.setattr(ocr_plugin.TesseractOcrEngine, "generate_hocr", staticmethod(lambda *a, **k: None))

    class Parser:
        def __init__(self, path):
            pass

        def parse(self):
            return page(PRINTED)

    monkeypatch.setattr(ocr_plugin, "HocrParser", Parser)
    monkeypatch.setattr(ocr_plugin, "mistral_page", lambda image, folder: (reading, "digest"))

    result, text = ocr_plugin.HybridOcrEngine().generate_ocr(image_file, None, page_number=0)

    assert "Zutaten:" in text  # Tesseract's reading is kept
    record = json.loads((folder / "page-0001.json").read_text(encoding="utf-8"))
    assert record["page"] == 1 and record["error"]
    assert api.mistral_errors(folder) == [(1, record["error"])]  # the job fails afterwards with it
