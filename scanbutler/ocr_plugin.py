"""ocrmypdf plugin: Tesseract's text layer, with Mistral's text where Tesseract misread.

Loaded with `ocrmypdf --plugin scanbutler.ocr_plugin` (see pdfops.make_searchable)
and only then; the folder inputs keep plain Tesseract. ocrmypdf runs it in its
own process, so it is configured through the environment:

- SCANBUTLER_HYBRID_DIR: folder for Mistral's answer per page image, named by
  the image's hash. A retry in another ocrmypdf mode sends a different (e.g.
  not deskewed) image and asks again; the same image is never paid twice.
- SCANBUTLER_MISTRAL_RATE_FILE: request slots shared with the main process,
  so all of them together keep to MISTRAL_MAX_RPS.
- MISTRAL_API_KEY, MISTRAL_API_BASE, MISTRAL_OCR_MODEL, MISTRAL_TIMEOUT,
  MISTRAL_MAX_RPS.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from ocrmypdf import hookimpl
from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine
from ocrmypdf.hocrtransform.hocr_parser import HocrParser
from PIL import Image

from . import hybrid
from .fileio import write_json

log = logging.getLogger(__name__)

HYBRID_DIR = "SCANBUTLER_HYBRID_DIR"


HYBRID_RATE = "SCANBUTLER_MISTRAL_RATE_FILE"

_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _ocr(body: dict) -> dict:
    """POST /ocr with this process's client: one connection, MISTRAL_MAX_RPS kept."""
    global _CLIENT
    from .mistral import MistralClient

    with _CLIENT_LOCK:
        if _CLIENT is None:
            rate = os.environ.get(HYBRID_RATE, "").strip()
            _CLIENT = MistralClient(
                os.environ.get("MISTRAL_API_KEY", ""),
                os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1").rstrip("/"),
                os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
                "",
                float(os.environ.get("MISTRAL_TIMEOUT", "300")),
                max_rps=float(os.environ.get("MISTRAL_MAX_RPS", "1") or 1),
                rate_file=Path(rate) if rate else None,
            )
        client = _CLIENT
    return client._post("/ocr", {"model": client.ocr_model, **body})


def image_body(image: Image.Image) -> dict:
    """An /ocr request for one page image, sent as JPEG (a fraction of the PNG)."""
    buffer = io.BytesIO()
    image.convert("L" if image.mode in ("1", "L", "LA", "I;16") else "RGB").save(buffer, "JPEG", quality=90)
    return {
        "document": {
            "type": "image_url",
            "image_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
        },
        "include_blocks": True,
        "include_image_base64": False,
    }


def mistral_page(image_file: Path, folder: Path, ocr=None) -> tuple[dict, str]:
    """Mistral's reading of one page image and the image's hash; asked once per image."""
    digest = hashlib.sha256(image_file.read_bytes()).hexdigest()
    cached = folder / f"{digest}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8")), digest
    with Image.open(image_file) as image:
        body = image_body(image)
    result = (ocr or _ocr)(body)
    pages = result.get("pages") or [{}]
    page = pages[0]
    write_json(cached, page, ensure_ascii=False)
    return page, digest


class HybridOcrEngine(TesseractOcrEngine):
    """Tesseract, corrected line by line with Mistral OCR."""

    def __str__(self) -> str:
        return f"Tesseract OCR {TesseractOcrEngine.version()} + Mistral OCR"

    @staticmethod
    def supports_generate_ocr() -> bool:
        return True

    def generate_ocr(self, input_file: Path, options, page_number: int = 0):
        with tempfile.TemporaryDirectory() as tmp:
            hocr, text = Path(tmp) / "page.hocr", Path(tmp) / "page.txt"
            TesseractOcrEngine.generate_hocr(input_file, hocr, text, options)
            page = HocrParser(hocr).parse()
        folder = Path(os.environ[HYBRID_DIR])
        record_path = folder / f"page-{page_number + 1:04d}.json"
        try:
            reading, digest = mistral_page(Path(input_file), folder)
        except Exception as exc:  # noqa: BLE001 - reported per page, see below
            # Raising here would fail ocrmypdf, and its next mode would send
            # the page again (a differently prepared image, so no cache hit):
            # paid again, to fail again. The page keeps Tesseract's reading;
            # the record tells the caller that Mistral failed.
            write_json(record_path, {"page": page_number + 1, "error": str(exc)[:500]})
            log.warning("page %d: Mistral OCR failed: %s", page_number + 1, exc)
            return page, hybrid.text_of(page)
        with Image.open(input_file) as image:
            width, height = image.size
        blocks = hybrid.blocks_from_page(reading, width, height)
        merged, stats = hybrid.merge(page, blocks)
        record = {
            "page": page_number + 1,
            "image": digest,
            "blocks": stats.blocks,
            "lines_kept": stats.lines_kept,
            "lines_replaced": stats.lines_replaced,
            "lines_added": stats.lines_added,
            "noise_dropped": stats.noise_dropped,
        }
        write_json(record_path, record)
        log.info("page %d: %d lines from Mistral, %d kept", page_number + 1, stats.lines_replaced, stats.lines_kept)
        return merged, hybrid.text_of(merged)


@hookimpl(tryfirst=True)
def get_ocr_engine(options):
    if options is not None and getattr(options, "ocr_engine", "auto") not in ("auto", "tesseract"):
        return None
    return HybridOcrEngine()
