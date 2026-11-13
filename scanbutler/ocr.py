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
from typing import Callable, Protocol

import pikepdf

from . import priority
from .fileio import write_json

log = logging.getLogger(__name__)

_IMAGE_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_CACHE_NAME = re.compile(r"^chunk_\d{5}_(\d+)\.json$")
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


def _chunk_bytes_of(pdf_path: Path, start: int, stop: int) -> bytes:
    """The bytes of one chunk, read when it is needed (and freed by the caller).

    Each call opens the file itself: a pikepdf object must not be shared by
    the OCR worker threads.
    """
    with pikepdf.open(pdf_path) as pdf:
        return _chunk_bytes(pdf, start, stop)


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
    write_json(cache, pages, ensure_ascii=False)
    log.info("ocr chunk done", extra={"first_page": start + 1, "pages": expected})


def _load_jobs(state_path: Path) -> list[dict]:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # Written atomically since 1.9.2, so only an older crash leaves this.
        # Failing on it for good would be worse than asking again.
        log.warning("batch state unreadable, ignored", extra={"file": state_path.name})
        return []


def _run_batches(
    pending: list[tuple[int, Path]],
    cache_dir: Path,
    backend,
    options: BatchOptions,
    expected: dict[int, int],
    make_bytes: Callable[[int], bytes],
    chunk_pages: int,
) -> None:
    """Fetch pending chunks through batch jobs; leaves failed chunks uncached.

    Job ids are written to disk right after submission, so a restart resumes
    waiting for jobs it already paid for instead of submitting them again.
    A job is marked `collected` before its files are deleted at Mistral, so a
    later attempt never asks for results that are gone: its chunks that
    failed go to direct OCR.
    """
    state_path = cache_dir / BATCH_STATE
    jobs = _load_jobs(state_path)
    by_start = dict(pending)

    # An upload recorded without a job: the process died between the two
    # requests. Nothing will ever read that file, and it holds the scans.
    orphans = [job for job in jobs if not job.get("job")]
    if orphans:
        log.warning("uploads without a batch job deleted", extra={"uploads": len(orphans)})
        backend.delete_files([job.get("input_file") for job in orphans])
        jobs = [job for job in jobs if job.get("job")]
        write_json(state_path, jobs)

    # A job made with another OCR_CHUNK_PAGES holds chunks of a different
    # layout; its answers would never fit the pages asked for now.
    stale = [job for job in jobs if job.get("chunk_pages", chunk_pages) != chunk_pages]
    if stale:
        log.warning("batch jobs of another chunk size dropped", extra={"jobs": len(stale), "chunk_pages": chunk_pages})
        backend.delete_files([job.get("input_file") for job in stale])
        jobs = [job for job in jobs if job not in stale]
        write_json(state_path, jobs)
    submitted = {start for job in jobs for start in job["chunks"]}

    def submit(group: list[tuple[int, bytes]]) -> None:
        lines = [(_custom_id(start), backend.ocr_body(data)) for start, data in group]
        entry: dict = {}

        def uploaded(file_id: str) -> None:
            entry.update(input_file=file_id, chunks=[], chunk_pages=chunk_pages)  # no "job" yet
            jobs.append(entry)
            write_json(state_path, jobs)

        try:
            ids = backend.submit_batch("/v1/ocr", backend.ocr_model, lines, on_uploaded=uploaded)
        except Exception:
            # The client has deleted the upload again; forget it.
            if entry in jobs:
                jobs.remove(entry)
                write_json(state_path, jobs)
            raise
        entry.update(ids, chunks=[start for start, _ in group])
        write_json(state_path, jobs)

    unsent = [start for start in by_start if start not in submitted]
    if unsent:
        # Each chunk's bytes are built when its group is uploaded and freed
        # with it; a big stack is never held in memory whole.
        group: list[tuple[int, bytes]] = []
        size = 0
        for start in unsent:
            data = make_bytes(start)
            encoded = len(data) * 4 // 3  # base64 inflates PDFs by a third
            if group and size + encoded > options.max_upload_bytes:
                submit(group)
                group, size = [], 0
            group.append((start, data))
            size += encoded
        submit(group)
    else:
        log.info("resuming batch jobs", extra={"jobs": len(jobs)})

    def delete_remote(job: dict) -> None:
        files = job.get("remote_files", [job.get("input_file")])
        if files:
            backend.delete_files(files)
            job["remote_files"] = []
            write_json(state_path, jobs)

    for job in jobs:
        if job.get("collected"):
            # Collected by an earlier run that may have died before the delete.
            delete_remote(job)
            continue
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
            try:
                _store(by_start[start], backend.ocr_pages(body), start, expected[start])
            except RuntimeError as exc:
                # Not usable (wrong page count): direct OCR asks again.
                log.warning("batch chunk unusable", extra={"first_page": start + 1, "error": str(exc)[:200]})
        job["collected"] = True
        # Recorded before the delete: a crash in between is finished on the next start.
        job["remote_files"] = [job.get("input_file"), finished.get("output_file"), finished.get("error_file")]
        write_json(state_path, jobs)
        # The uploads hold the scans themselves; don't leave them at Mistral.
        delete_remote(job)

    state_path.unlink(missing_ok=True)


def _cached_chunk_pages(cache_dir: Path, total: int, default: int) -> int:
    """The chunk size to read the cache with.

    OCR_CHUNK_PAGES may have changed since the stack was processed. If the
    cache holds a complete set of chunks of some other size, use that size:
    asking Mistral again would pay for text we already have.
    """
    sizes = {int(m.group(1)) for path in cache_dir.glob("chunk_*_*.json") if (m := _CACHE_NAME.match(path.name))}
    for size in sorted(sizes, key=lambda n: n != default):  # the current size first
        if all((cache_dir / f"chunk_{start:05d}_{size}.json").exists() for start in range(0, total, size)):
            if size != default:
                log.info("using cached ocr chunks of another size", extra={"chunk_pages": size})
            return size
    return default


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
    chunk_pages = _cached_chunk_pages(cache_dir, total, chunk_pages)
    starts = list(range(0, total, chunk_pages))
    pending = [
        (start, cache)
        for start in starts
        if not (cache := cache_dir / f"chunk_{start:05d}_{chunk_pages}.json").exists()
    ]
    expected = {start: min(chunk_pages, total - start) for start in starts}

    def make_bytes(start: int) -> bytes:
        return _chunk_bytes_of(pdf_path, start, start + expected[start])

    if pending and batch is not None:
        log.info("ocr started (batch)", extra={"file": pdf_path.name, "pages": total, "chunks_to_fetch": len(pending)})
        _run_batches(pending, cache_dir, backend, batch, expected, make_bytes, chunk_pages)
        pending = [item for item in pending if not item[1].exists()]
        if pending:
            log.warning("falling back to direct ocr", extra={"chunks": len(pending)})

    def work(item: tuple[int, Path]) -> None:
        start, cache = item
        _store(cache, backend.ocr_pdf(make_bytes(start)), start, expected[start])

    if pending:
        log.info("ocr started", extra={"file": pdf_path.name, "pages": total, "chunks_to_fetch": len(pending)})
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # list() re-raises the first worker exception here.
            list(pool.map(priority.keep(work), pending))

    pages: list[Page] = []
    for start in starts:
        raw_pages = json.loads((cache_dir / f"chunk_{start:05d}_{chunk_pages}.json").read_text(encoding="utf-8"))
        # The API indexes pages within the chunk; re-number them by position
        # so a sparse or odd "index" field can't misplace a page.
        for position, raw in enumerate(raw_pages):
            pages.append(_page_from_api(start, {**raw, "index": position}))
    return pages
