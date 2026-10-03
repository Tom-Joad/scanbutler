"""PDF manipulation: text layer via ocrmypdf, page extraction via pikepdf."""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
from PIL import ImageFilter

log = logging.getLogger(__name__)

# Grey levels a pixel must be below the paper colour to count as ink.
INK_CONTRAST = 40


def page_count(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def page_texts(path: Path) -> list[str]:
    """The text layer of every page, in reading order as stored in the PDF."""
    doc = pdfium.PdfDocument(str(path))
    try:
        texts = []
        for page in doc:
            textpage = page.get_textpage()
            texts.append(textpage.get_text_range())
            textpage.close()
        return texts
    finally:
        doc.close()


def ink_coverage(path: Path, dpi: int = 50) -> list[float]:
    """Share of each page's area (0..1) that is visibly darker than the paper.

    The threshold follows the page's own background, so tinted paper and
    pale forms are measured fairly. A 5% margin is ignored (scanner edges),
    and a median filter removes dust specks. This is independent of OCR,
    which matters because OCR models can hallucinate text on blank pages.
    """
    doc = pdfium.PdfDocument(str(path))
    try:
        result = []
        for page in doc:
            image = page.render(scale=dpi / 72, grayscale=True).to_pil().convert("L")
            width, height = image.size
            image = image.crop((width // 20, height // 20, width - width // 20, height - height // 20))
            histogram = image.filter(ImageFilter.MedianFilter(3)).histogram()
            total = sum(histogram)
            running, background = 0, 255
            for value, count in enumerate(histogram):
                running += count
                if running * 2 >= total:
                    background = value
                    break
            result.append(sum(histogram[: max(0, background - INK_CONTRAST)]) / total)
        return result
    finally:
        doc.close()


# ocrmypdf modes, best first. A pure scan gets the full treatment: any old
# text layer is discarded, pages are straightened, the image Tesseract sees
# is cleaned (the output image stays as scanned) and upsampled to 300 dpi.
# A PDF that already has text (born digital, like an online bank statement,
# or scanned with the scanner's own OCR) uses --redo-ocr instead: it replaces
# invisible OCR text but keeps real digital text. --force-ocr would turn such
# a page into a picture of itself, many times the size. --redo-ocr can't be
# combined with --deskew, and it skips --clean: unpaper is meant for scans,
# and ocrmypdf rasterizes at the resolution of the sharpest image on a page,
# so one high-resolution logo made unpaper work at ~1550 dpi until the
# kernel killed it.
SCAN_MODE = ("scan", ["--force-ocr", "--rotate-pages", "--deskew", "--clean", "--oversample", "300"])
REDO_MODE = ("redo", ["--redo-ocr", "--rotate-pages", "--oversample", "300"])
# Only OCR pages without any text, with no image processing. For a tagged
# (structured, born-digital) PDF this keeps the file as it is.
PLAIN_MODE = ("plain", ["--skip-text"])


@dataclass(frozen=True)
class OcrLimits:
    """Upper bounds so one odd page can't exhaust the server.

    ocrmypdf rasterizes a page at the resolution of its sharpest image. A
    colour logo stored at ~1550 dpi turns an A4 page into a 230-megapixel
    bitmap, which killed unpaper in a 2 GB container. --redo-ocr together with
    max_ocr_mpixels handled the same page and still found its text.
    """

    # Tesseract sees at most this many megapixels per page; larger images are
    # downsampled for OCR only. Peak memory is about jobs x 16 B x this value.
    max_ocr_mpixels: int = 50
    # Seconds Tesseract may spend on one page before giving up on it.
    page_timeout: int = 300
    # Minutes one ocrmypdf run may take before the next mode is tried.
    file_timeout_minutes: int = 120
    # In the last-resort mode, pages above this are kept without new OCR.
    skip_big_mpixels: int = 200

    def args(self, mode: str) -> list[str]:
        extra = ["--max-ocr-image-mpixels", str(self.max_ocr_mpixels), "--tesseract-timeout", str(self.page_timeout)]
        if mode == "plain":
            extra += ["--skip-big", str(self.skip_big_mpixels)]
        return extra


def ocr_modes(has_text: bool, tagged: bool = False) -> list[tuple[str, list[str]]]:
    if tagged:
        # Office documents, bank statements and the like: the text is the
        # original, and re-OCR would discard the PDF's structure tree.
        return [PLAIN_MODE]
    return [REDO_MODE, PLAIN_MODE] if has_text else [SCAN_MODE, REDO_MODE, PLAIN_MODE]


def is_tagged(path: Path) -> bool:
    """Whether the PDF carries a logical structure tree (a "Tagged PDF")."""
    with pikepdf.open(path) as pdf:
        mark_info = pdf.Root.get("/MarkInfo")
        marked = bool(mark_info.get("/Marked", False)) if isinstance(mark_info, pikepdf.Dictionary) else False
        return "/StructTreeRoot" in pdf.Root or marked


def _ocrmypdf_error(returncode: int, stderr: str) -> str:
    """The lines of ocrmypdf's output that say what went wrong."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    telling = [
        line
        for line in lines
        if any(word in line.lower() for word in ("error", "exception", "failed", "errno", "not ", "cannot", "unable"))
    ]
    detail = " | ".join((telling or lines)[-6:])
    return f"exit code {returncode}: {detail[-1200:]}"


def make_searchable(
    src: Path, dst: Path, languages: str, jobs: int, extra_args: str, limits: OcrLimits = OcrLimits()
) -> None:
    """Add an invisible Tesseract text layer so every output PDF is searchable.

    Mistral OCR returns text without word positions, so it cannot place a text
    layer itself; Tesseract only has to be good enough for full-text search.
    If the best mode for the file fails, simpler ones are tried before giving up.
    """
    tmp = dst.with_name(dst.name + ".tmp.pdf")
    try:
        has_text = any(text.strip() for text in page_texts(src))
        tagged = has_text and is_tagged(src)
    except Exception:  # noqa: BLE001 - an unreadable file fails in ocrmypdf with a clearer message
        has_text = tagged = False
    log.info("text layer started", extra={"file": src.name, "has_text": has_text, "tagged": tagged})

    errors = []
    for name, options in ocr_modes(has_text, tagged):
        cmd = [
            "ocrmypdf",
            *options,
            *limits.args(name),
            "--output-type", "pdf",
            "--language", languages,
            "--jobs", str(jobs),
            "--no-progress-bar",
            *shlex.split(extra_args),
            str(src),
            str(tmp),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=limits.file_timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess(
                cmd, -1, "", f"error: not finished after {limits.file_timeout_minutes} minutes, stopped"
            )
        if result.returncode == 0:
            tmp.replace(dst)
            log.info("text layer done", extra={"file": src.name, "mode": name, "fallback": bool(errors)})
            return
        tmp.unlink(missing_ok=True)
        error = _ocrmypdf_error(result.returncode, result.stderr)
        errors.append(f"{name}: {error}")
        log.warning("text layer mode failed", extra={"file": src.name, "mode": name, "error": error})
    raise RuntimeError("ocrmypdf failed in every mode. " + " || ".join(errors))


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
