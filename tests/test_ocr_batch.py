from __future__ import annotations

import json

import pikepdf

from scanbutler.ocr import BATCH_STATE, BatchOptions, _chunk_bytes, run_ocr

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


def test_jobs_are_split_by_upload_size(tmp_path):
    backend = FakeBackend(TEXTS)

    pages = ocr(tmp_path, backend, batch=BatchOptions(poll_seconds=0, max_wait_seconds=1, max_upload_bytes=1))

    assert [p.markdown for p in pages] == TEXTS
    assert sorted(len(lines) for lines in backend.jobs.values()) == [1, 1, 1]  # one chunk per job


def test_the_backend_gets_each_chunk_as_before(tmp_path):
    backend = FakeBackend(TEXTS)

    ocr(tmp_path, backend)

    sent = {cid: body["pdf"] for cid, body in backend.jobs["job-0"]}
    with pikepdf.open(tmp_path / "stack.pdf") as pdf:
        assert sent == {f"chunk-{s:05d}": _chunk_bytes(pdf, s, min(s + 3, len(TEXTS))) for s in (0, 3, 6)}


def test_direct_mode_sends_the_same_bytes(tmp_path):
    seen = []

    class Recording(FakeBackend):
        def ocr_pdf(self, pdf_bytes):
            seen.append(pdf_bytes)
            return super().ocr_pdf(pdf_bytes)

    ocr(tmp_path, Recording(TEXTS), batch=None)

    with pikepdf.open(tmp_path / "stack.pdf") as pdf:
        assert sorted(seen) == sorted(_chunk_bytes(pdf, s, min(s + 3, len(TEXTS))) for s in (0, 3, 6))


def test_state_from_another_chunk_size_is_not_replayed(tmp_path):
    # An interrupted run with 50-page chunks, then OCR_CHUNK_PAGES=3: the old
    # job's answer has the wrong number of pages for the new layout.
    backend = FakeBackend(TEXTS)
    make_pdf(tmp_path / "stack.pdf", len(TEXTS))
    with pikepdf.open(tmp_path / "stack.pdf") as pdf:
        backend.jobs["job-old"] = [("chunk-00000", backend.ocr_body(_chunk_bytes(pdf, 0, len(TEXTS))))]
    (tmp_path / "ocr").mkdir()
    (tmp_path / "ocr" / BATCH_STATE).write_text(
        json.dumps([{"job": "job-old", "input_file": "in-old", "chunks": [0], "chunk_pages": 50}]), encoding="utf-8"
    )

    pages = ocr(tmp_path, backend)

    assert [p.markdown for p in pages] == TEXTS
    assert "in-old" in backend.deleted
    assert list(backend.jobs) == ["job-old", "job-1"]  # the new layout was submitted afresh


def test_state_without_a_chunk_size_that_does_not_fit_falls_back_to_direct_ocr(tmp_path):
    # State of 1.9.1 (no chunk_pages) whose answer has the wrong page count.
    backend = FakeBackend(TEXTS)
    make_pdf(tmp_path / "stack.pdf", len(TEXTS))
    with pikepdf.open(tmp_path / "stack.pdf") as pdf:
        backend.jobs["job-old"] = [("chunk-00000", backend.ocr_body(_chunk_bytes(pdf, 0, len(TEXTS))))]
    (tmp_path / "ocr").mkdir()
    (tmp_path / "ocr" / BATCH_STATE).write_text(
        json.dumps([{"job": "job-old", "input_file": "in-old", "chunks": [0]}]), encoding="utf-8"
    )

    pages = ocr(tmp_path, backend)

    assert [p.markdown for p in pages] == TEXTS
    assert backend.ocr_calls >= 1  # the chunk that didn't fit was read directly


def test_a_collected_job_is_not_asked_for_again(tmp_path):
    # Job 1 was collected (its files deleted at Mistral, one chunk failed);
    # waiting for job 2 then failed. The retry must not ask job 1 for results.
    class Flaky(FakeBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.asked: list[str] = []
            self.deleted_jobs: set[str] = set()
            self.fail_wait = True

        def wait_batch(self, job_id, poll_seconds, max_wait_seconds):
            if job_id == "job-1" and self.fail_wait:
                self.fail_wait = False
                raise ConnectionError("network down")
            return super().wait_batch(job_id, poll_seconds, max_wait_seconds)

        def batch_results(self, job):
            self.asked.append(job["id"])
            if job["id"] in self.deleted_jobs:
                raise FileNotFoundError("output file is gone")
            return super().batch_results(job)

        def delete_files(self, file_ids):
            super().delete_files(file_ids)
            self.deleted_jobs.update(f.removeprefix("out-") for f in file_ids if f and f.startswith("out-"))

    backend = Flaky(TEXTS, fail_batch={"chunk-00000"})
    options = BatchOptions(poll_seconds=0, max_wait_seconds=1, max_upload_bytes=1)  # one chunk per job

    try:
        ocr(tmp_path, backend, batch=options)
        raise AssertionError("the network error should have failed the first attempt")
    except ConnectionError:
        pass

    pages = ocr(tmp_path, backend, batch=options)

    assert [p.markdown for p in pages] == TEXTS
    assert backend.asked.count("job-0") == 1  # never asked again once collected
    assert not (tmp_path / "ocr" / BATCH_STATE).exists()


def test_cached_chunks_of_another_size_are_reused(tmp_path):
    backend = FakeBackend(TEXTS)
    make_pdf(tmp_path / "stack.pdf", len(TEXTS))
    first = run_ocr(tmp_path / "stack.pdf", tmp_path / "ocr", backend, chunk_pages=3, concurrency=1)
    calls = backend.ocr_calls

    again = run_ocr(tmp_path / "stack.pdf", tmp_path / "ocr", backend, chunk_pages=2, concurrency=1)

    assert [p.markdown for p in again] == [p.markdown for p in first] == TEXTS
    assert backend.ocr_calls == calls  # nothing was sent to Mistral again


def test_unreadable_batch_state_is_ignored(tmp_path):
    backend = FakeBackend(TEXTS)
    make_pdf(tmp_path / "stack.pdf", len(TEXTS))
    (tmp_path / "ocr").mkdir()
    (tmp_path / "ocr" / BATCH_STATE).write_text('[{"job": "j', encoding="utf-8")  # cut off mid-write

    assert [p.markdown for p in ocr(tmp_path, backend)] == TEXTS
