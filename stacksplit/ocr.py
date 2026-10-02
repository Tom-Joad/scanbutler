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
    def ocr_pdf(self, pdf_bytes: bytes) -> list[dict]: ...


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


def run_ocr(pdf_path: Path, cache_dir: Path, backend: OcrBackend, chunk_pages: int, concurrency: int) -> list[Page]:
    cache_dir.mkdir(parents=True, exist_ok=True)

    with pikepdf.open(pdf_path) as pdf:
        total = len(pdf.pages)
        starts = list(range(0, total, chunk_pages))
        pending: list[tuple[int, Path, bytes]] = []
        for start in starts:
            cache = cache_dir / f"chunk_{start:05d}_{chunk_pages}.json"
            if not cache.exists():
                pending.append((start, cache, _chunk_bytes(pdf, start, min(start + chunk_pages, total))))

    def work(item: tuple[int, Path, bytes]) -> None:
        start, cache, data = item
        pages = backend.ocr_pdf(data)
        expected = min(chunk_pages, total - start)
        if len(pages) != expected:
            raise RuntimeError(f"OCR returned {len(pages)} pages for a {expected}-page chunk at page {start + 1}")
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache)
        log.info("ocr chunk done", extra={"first_page": start + 1, "pages": expected})

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
