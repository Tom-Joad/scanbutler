"""PDF manipulation: text layer via ocrmypdf, page extraction via pikepdf."""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

import pikepdf

log = logging.getLogger(__name__)


def page_count(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def make_searchable(src: Path, dst: Path, languages: str, jobs: int, extra_args: str) -> None:
    """Add an invisible Tesseract text layer so every output PDF is searchable.

    Mistral OCR returns text without word positions, so it cannot place a text
    layer itself; Tesseract only has to be good enough for full-text search.

    Any existing text layer is discarded (--force-ocr): scanner-made OCR is
    often worse than a fresh pass. The remaining options trade speed for
    recognition quality on poor scans: straighten skewed pages, clean the
    image Tesseract sees (the output image stays as scanned), and upsample
    low-resolution scans to 300 dpi before recognition.
    """
    tmp = dst.with_name(dst.name + ".tmp.pdf")
    cmd = [
        "ocrmypdf",
        "--force-ocr",
        "--rotate-pages",
        "--deskew",
        "--clean",
        "--oversample", "300",
        "--output-type", "pdf",
        "--language", languages,
        "--jobs", str(jobs),
        "--quiet",
        *shlex.split(extra_args),
        str(src),
        str(tmp),
    ]
    log.info("text layer started", extra={"file": src.name})
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ocrmypdf failed with exit code {result.returncode}: {result.stderr.strip()[-800:]}")
    tmp.replace(dst)
    log.info("text layer done", extra={"file": src.name})


def write_document(src: Path, page_indices: list[int], dst: Path, title: str, subject: str) -> None:
    """Copy the given 0-based pages of `src` into a new PDF at `dst`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    with pikepdf.open(src) as pdf:
        out = pikepdf.new()
        for index in page_indices:
            out.pages.append(pdf.pages[index])
        with out.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:title"] = title
            if subject:
                meta["dc:description"] = subject
        out.docinfo["/Title"] = title
        if subject:
            out.docinfo["/Subject"] = subject
        out.save(tmp)
    tmp.replace(dst)
