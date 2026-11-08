"""ocrmypdf plugin: Tesseract's text layer, with Mistral's text where Tesseract misread.

Loaded with `ocrmypdf --plugin scanbutler.ocr_plugin` (see pdfops.make_searchable)
and only then; the folder inputs keep plain Tesseract. ocrmypdf runs it in its
own process, so it is configured through the environment:

- SCANBUTLER_HYBRID_DIR: folder for Mistral's answer per page image, named by
  the image's hash. A retry in another ocrmypdf mode sends a different (e.g.
  not deskewed) image and asks again; the same image is never paid twice.
- MISTRAL_API_KEY, MISTRAL_API_BASE, MISTRAL_OCR_MODEL, MISTRAL_TIMEOUT.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import tempfile
from pathlib import Path

from ocrmypdf import hookimpl
from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine
from ocrmypdf.hocrtransform.hocr_parser import HocrParser
from PIL import Image

from . import hybrid

log = logging.getLogger(__name__)

HYBRID_DIR = "SCANBUTLER_HYBRID_DIR"


def _client():
    from .mistral import MistralClient

    return MistralClient(
        os.environ.get("MISTRAL_API_KEY", ""),
        os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1").rstrip("/"),
        os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
        "",
        float(os.environ.get("MISTRAL_TIMEOUT", "300")),
    )


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


def mistral_page(image_file: Path, folder: Path, ocr=None) -> dict:
    """Mistral's reading of one page image, from the folder if it was asked before."""
    digest = hashlib.sha256(image_file.read_bytes()).hexdigest()
    cached = folder / f"{digest}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    with Image.open(image_file) as image:
        body = image_body(image)
    if ocr is None:
        client = _client()
        try:
            result = client._post("/ocr", {"model": client.ocr_model, **body})
        finally:
            client.close()
    else:
        result = ocr(body)
    pages = result.get("pages") or [{}]
    page = pages[0]
    tmp = cached.with_suffix(".tmp")
    tmp.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cached)
    return page


class HybridOcrEngine(TesseractOcrEngine):
    """Tesseract, corrected block by block with Mistral OCR."""

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
        reading = mistral_page(Path(input_file), folder)
        with Image.open(input_file) as image:
            width, height = image.size
        blocks = hybrid.blocks_from_page(reading, width, height)
        merged, stats = hybrid.merge(page, blocks)
        record = {
            "page": page_number + 1,
            "image": hashlib.sha256(Path(input_file).read_bytes()).hexdigest(),
            "blocks": stats.blocks,
            "lines_kept": stats.lines_kept,
            "lines_replaced": stats.lines_replaced,
            "lines_added": stats.lines_added,
            "noise_dropped": stats.noise_dropped,
        }
        (folder / f"page-{page_number + 1:04d}.json").write_text(json.dumps(record), encoding="utf-8")
        log.info("page %d: %d lines from Mistral, %d kept", page_number + 1, stats.lines_replaced, stats.lines_kept)
        return merged, hybrid.text_of(merged)


@hookimpl(tryfirst=True)
def get_ocr_engine(options):
    if options is not None and getattr(options, "ocr_engine", "auto") not in ("auto", "tesseract"):
        return None
    return HybridOcrEngine()
