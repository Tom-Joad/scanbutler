"""Mistral OCR over a large PDF, chunked and cached per chunk.

A 500-page scan easily exceeds the request size the OCR endpoint accepts, so
the stack is cut into fixed-size chunks. Each chunk's answer is cached in the
work directory: an interrupted run, or a re-run after a crash later in the
pipeline, never pays for the same page twice.
"""

from __future__ import annotations

import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pikepdf

log = logging.getLogger(__name__)

_IMAGE_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_NOISE = re.compile(r"[\s#*_|`>\-=~.:]+")


class OcrBackend(Protocol):
    ocr_model: str

    def ocr_pdf(self, pdf_bytes: bytes) -> list[dict]: ...
    def ocr_body(self, pdf_bytes: bytes) -> dict: ...
    def ocr_pages(self, result: dict) -> list[dict]: ...
    def submit_batch(self, endpoint: str, model: str, lines: list[tuple[str, dict]]) -> dict[str, str]: ...
    def wait_batch(self, job_id: str, poll_seconds: float, max_wait_seconds: float) -> dict: ...
    def batch_results(self, job: dict) -> dict[str, dict]: ...
    def delete_files(self, file_ids: list[str | None]) -> None: ...


@dataclass(frozen=True)
class Page:
    """One OCR'd page. `index` is 0-based within the source stack."""

    index: int
    markdown: str
    header: str
    footer: str
    has_images: bool
    # Measured on the page image (see pdfops.ink_coverage); None if unknown.
    ink: float | None = None

    @property
    def number(self) -> int:
        return self.index + 1

    def text_chars(self) -> int:
        """Characters of real text, ignoring image references and markup."""
        text = _IMAGE_REF.sub("", "\n".join((self.header, self.markdown, self.footer)))
        return len(_NOISE.sub("", text))

    def is_blank(self, max_chars: int, max_ink: float = 0.0) -> bool:
        # The image decides first: on an empty page OCR models sometimes
        # invent whole paragraphs, in a language that isn't even there.
        if self.ink is not None and self.ink < max_ink:
            return True
        return not self.has_images and self.text_chars() <= max_chars


def _chunk_bytes(pdf: pikepdf.Pdf, start: int, stop: int) -> bytes:
    part = pikepdf.new()
    part.pages.extend(pdf.pages[start:stop])
    buffer = io.BytesIO()
    part.save(buffer)
    return buffer.getvalue()


def _page_from_api(offset: int, raw: dict) -> Page:
    return Page(
        index=offset + int(raw.get("index", 0)),
        markdown=raw.get("markdown") or "",
        header=raw.get("header") or "",
        footer=raw.get("footer") or "",
        has_images=bool(raw.get("images")),
    )


@dataclass(frozen=True)
class BatchOptions:
    """Use Mistral's batch API: half the price, minutes instead of seconds."""

    poll_seconds: float = 15.0
    max_wait_seconds: float = 24 * 3600.0
    # Base64 inflates PDFs by a third; jobs are split to stay below this upload size.
    max_upload_bytes: int = 200 * 2**20


BATCH_STATE = "batch_jobs.json"


def _custom_id(start: int) -> str:
    return f"chunk-{start:05d}"


def _store(cache: Path, pages: list[dict], start: int, expected: int) -> None:
    if len(pages) != expected:
        raise RuntimeError(f"OCR returned {len(pages)} pages for a {expected}-page chunk at page {start + 1}")
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache)
    log.info("ocr chunk done", extra={"first_page": start + 1, "pages": expected})


def _group_by_size(items: list[tuple[int, Path, bytes]], limit: int) -> list[list[tuple[int, Path, bytes]]]:
    groups: list[list[tuple[int, Path, bytes]]] = [[]]
    size = 0
    for item in items:
        encoded = len(item[2]) * 4 // 3
        if groups[-1] and size + encoded > limit:
            groups.append([])
            size = 0
        groups[-1].append(item)
        size += encoded
    return groups


def _run_batches(
    pending: list[tuple[int, Path, bytes]], cache_dir: Path, backend, options: BatchOptions, expected: dict[int, int]
) -> None:
    """Fetch pending chunks through batch jobs; leaves failed chunks uncached.

    Job ids are written to disk right after submission, so a restart resumes
    waiting for jobs it already paid for instead of submitting them again.
    """
    state_path = cache_dir / BATCH_STATE
    jobs: list[dict] = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else []
    by_start = {start: (start, cache, data) for start, cache, data in pending}
    submitted = {start for job in jobs for start in job["chunks"]}

    unsent = [item for item in pending if item[0] not in submitted]
    if unsent:
        for group in _group_by_size(unsent, options.max_upload_bytes):
            lines = [(_custom_id(start), backend.ocr_body(data)) for start, _, data in group]
            ids = backend.submit_batch("/v1/ocr", backend.ocr_model, lines)
            jobs.append({**ids, "chunks": [start for start, _, _ in group]})
            state_path.write_text(json.dumps(jobs), encoding="utf-8")
    else:
        log.info("resuming batch jobs", extra={"jobs": len(jobs)})

    for job in jobs:
        wanted = [start for start in job["chunks"] if start in by_start]
        if not wanted:
            continue
        finished = backend.wait_batch(job["job"], options.poll_seconds, options.max_wait_seconds)
        results = backend.batch_results(finished)
        for start in wanted:
            body = results.get(_custom_id(start))
            if body is None:
                log.warning("batch chunk failed", extra={"first_page": start + 1, "job": job["job"]})
                continue
            _store(by_start[start][1], backend.ocr_pages(body), start, expected[start])
        # The uploads hold the scans themselves; don't leave them at Mistral.
        backend.delete_files([job.get("input_file"), finished.get("output_file"), finished.get("error_file")])

    state_path.unlink(missing_ok=True)


def run_ocr(
    pdf_path: Path,
    cache_dir: Path,
    backend: OcrBackend,
    chunk_pages: int,
    concurrency: int,
    batch: BatchOptions | None = None,
) -> list[Page]:
    cache_dir.mkdir(parents=True, exist_ok=True)

    with pikepdf.open(pdf_path) as pdf:
        total = len(pdf.pages)
        starts = list(range(0, total, chunk_pages))
        pending: list[tuple[int, Path, bytes]] = []
        for start in starts:
            cache = cache_dir / f"chunk_{start:05d}_{chunk_pages}.json"
            if not cache.exists():
                pending.append((start, cache, _chunk_bytes(pdf, start, min(start + chunk_pages, total))))
    expected = {start: min(chunk_pages, total - start) for start in starts}

    if pending and batch is not None:
        log.info("ocr started (batch)", extra={"file": pdf_path.name, "pages": total, "chunks_to_fetch": len(pending)})
        _run_batches(pending, cache_dir, backend, batch, expected)
        pending = [item for item in pending if not item[1].exists()]
        if pending:
            log.warning("falling back to direct ocr", extra={"chunks": len(pending)})

    def work(item: tuple[int, Path, bytes]) -> None:
        start, cache, data = item
        _store(cache, backend.ocr_pdf(data), start, expected[start])

    if pending:
        log.info("ocr started", extra={"file": pdf_path.name, "pages": total, "chunks_to_fetch": len(pending)})
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # list() re-raises the first worker exception here.
            list(pool.map(work, pending))

    pages: list[Page] = []
    for start in starts:
        raw_pages = json.loads((cache_dir / f"chunk_{start:05d}_{chunk_pages}.json").read_text(encoding="utf-8"))
        # The API indexes pages within the chunk; re-number them by position
        # so a sparse or odd "index" field can't misplace a page.
        for position, raw in enumerate(raw_pages):
            pages.append(_page_from_api(start, {**raw, "index": position}))
    return pages
