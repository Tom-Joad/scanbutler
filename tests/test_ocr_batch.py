from __future__ import annotations

import json
from pathlib import Path

import pikepdf

from scanbutler.ocr import BATCH_STATE, BatchOptions, _chunk_bytes, _group_by_size, run_ocr

from .conftest import FakeBackend, make_pdf

TEXTS = [f"page text {i}" for i in range(7)]
OPTIONS = BatchOptions(poll_seconds=0, max_wait_seconds=1)


def ocr(tmp_path, backend, batch=OPTIONS):
    src = tmp_path / "stack.pdf"
    if not src.exists():
        make_pdf(src, len(TEXTS))
    return run_ocr(src, tmp_path / "ocr", backend, chunk_pages=3, concurrency=1, batch=batch)


def test_batch_fetches_every_chunk_and_cleans_up(tmp_path):
    backend = FakeBackend(TEXTS)

    pages = ocr(tmp_path, backend)

    assert [p.markdown for p in pages] == TEXTS
    assert backend.ocr_calls == 0
    assert len(backend.jobs) == 1 and len(backend.jobs["job-0"]) == 3  # chunks of 3, 3, 1 pages
    assert backend.deleted == ["in-job-0", "out-job-0"]
    assert not (tmp_path / "ocr" / BATCH_STATE).exists()


def test_failed_batch_chunk_falls_back_to_direct_ocr(tmp_path):
    backend = FakeBackend(TEXTS, fail_batch={"chunk-00003"})

    pages = ocr(tmp_path, backend)

    assert [p.markdown for p in pages] == TEXTS
    assert backend.ocr_calls == 1


def test_restart_resumes_submitted_job_instead_of_paying_again(tmp_path):
    # State as left by a crash right after submission: a job at Mistral and
    # its id on disk, but no chunk results yet.
    backend = FakeBackend(TEXTS)
    make_pdf(tmp_path / "stack.pdf", len(TEXTS))
    with pikepdf.open(tmp_path / "stack.pdf") as pdf:
        backend.jobs["job-earlier"] = [
            (f"chunk-{s:05d}", backend.ocr_body(_chunk_bytes(pdf, s, min(s + 3, len(TEXTS))))) for s in (0, 3, 6)
        ]
    (tmp_path / "ocr").mkdir()
    (tmp_path / "ocr" / BATCH_STATE).write_text(
        json.dumps([{"job": "job-earlier", "input_file": "in-earlier", "chunks": [0, 3, 6]}]), encoding="utf-8"
    )

    pages = ocr(tmp_path, backend)

    assert [p.markdown for p in pages] == TEXTS
    assert list(backend.jobs) == ["job-earlier"]  # nothing new submitted
    assert "in-earlier" in backend.deleted


def test_direct_mode_skips_batch(tmp_path):
    backend = FakeBackend(TEXTS)

    pages = ocr(tmp_path, backend, batch=None)

    assert [p.markdown for p in pages] == TEXTS
    assert backend.ocr_calls == 3 and not backend.jobs


def test_jobs_are_split_by_upload_size():
    items = [(i, Path(f"{i}.json"), b"x" * 300) for i in range(5)]  # 400 bytes each once base64'd
    groups = _group_by_size(items, limit=1000)
    assert [[start for start, _, _ in g] for g in groups] == [[0, 1], [2, 3], [4]]
