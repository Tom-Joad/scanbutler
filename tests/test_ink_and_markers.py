from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import PurePosixPath

from PIL import Image, ImageDraw

from scanbutler.boundaries import Decision, _apply_markers
from scanbutler.ocr import Page
from scanbutler import pdfops
from scanbutler.pdfops import ink_coverage
from scanbutler.pipeline import build_plan, keep_name_plan

from .conftest import FakeBackend


def test_ink_coverage_separates_blank_tinted_and_text_pages(tmp_path):
    blank = Image.new("L", (850, 1100), 250)
    speck = blank.copy()
    ImageDraw.Draw(speck).point((400, 500), fill=0)  # dust is filtered out
    def lines(image: Image.Image, ink: int, rows: int) -> Image.Image:
        draw = ImageDraw.Draw(image)
        for row in range(rows):
            # Solid bars stand in for lines of text at the coarse 50 dpi render.
            draw.rectangle((80, 80 + row * 45, 760, 92 + row * 45), fill=ink)
        return image

    tinted_form = lines(Image.new("L", (850, 1100), 215), 150, 3)  # pale pink form in grey
    text = lines(blank.copy(), 0, 20)
    path = tmp_path / "pages.pdf"
    blank.save(path, save_all=True, append_images=[speck, tinted_form, text], resolution=100)

    coverage = ink_coverage(path)

    assert coverage[0] == 0
    assert coverage[1] == 0
    assert 0.002 < coverage[2] < 0.05  # form lines count although paper is tinted
    assert coverage[3] > 0.01


def test_pdfium_is_never_entered_from_two_threads_at_once(tmp_path, monkeypatch):
    # PDFium isn't thread-safe; parallel scans crashed the process in CI.
    path = tmp_path / "page.pdf"
    Image.new("L", (850, 1100), 250).save(path, resolution=100)
    real = pdfops.pdfium.PdfDocument
    inside, most = 0, 0
    count = threading.Lock()

    class Recording:
        def __init__(self, *args):
            nonlocal inside, most
            with count:
                inside += 1
                most = max(most, inside)
            time.sleep(0.02)  # widen the window for an overlap
            self.doc = real(*args)

        def __iter__(self):
            return iter(self.doc)

        def close(self):
            nonlocal inside
            self.doc.close()
            with count:
                inside -= 1

    monkeypatch.setattr(pdfops.pdfium, "PdfDocument", Recording)
    calls = [lambda: ink_coverage(path), lambda: pdfops.page_texts(path)] * 4
    threads = [threading.Thread(target=call) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert most == 1


def test_hallucinated_text_on_blank_page_is_still_blank():
    page = Page(index=0, markdown="2017年，公司与上海浦东发展银行" * 50, header="", footer="", has_images=False, ink=0.0)
    assert page.is_blank(15, 0.002)
    assert not Page(0, "x" * 300, "", "", False, ink=0.01).is_blank(15, 0.002)


def test_page_k_of_n_never_starts_a_document():
    pages = [
        Page(0, "Arztbrief", "", "", False),
        Page(1, "Befund ...", "", "Seite 2 von 5", False),
        Page(2, "Neuer Brief", "", "Seite 1 von 2", False),
    ]
    model = [
        Decision(0, True, 1.0, "first"),
        Decision(1, True, 0.95, "new letterhead"),
        Decision(2, False, 0.6, "continues"),
    ]
    result = _apply_markers(pages, model)
    assert [d.starts_new for d in result] == [True, False, True]


def _pdf_of(tmp_path, *images):
    path = tmp_path / "dark.pdf"
    images[0].save(path, save_all=True, append_images=list(images[1:]), resolution=100)
    return path


def _dark_photo() -> Image.Image:
    # An X-ray: mostly near-black with a lighter ribcage-like shape.
    image = Image.new("L", (850, 1100), 18)
    draw = ImageDraw.Draw(image)
    draw.ellipse((200, 250, 650, 850), fill=70)
    draw.rectangle((380, 100, 470, 1000), fill=110)
    return image


def test_dark_pages_count_as_full_of_ink_not_as_empty(tmp_path):
    black = Image.new("L", (850, 1100), 0)
    blank = Image.new("L", (850, 1100), 250)
    path = _pdf_of(tmp_path, black, _dark_photo(), blank)

    coverage = ink_coverage(path)

    assert coverage[0] > 0.9  # a black cover sheet
    assert coverage[1] > 0.9  # an X-ray
    assert coverage[2] == 0  # a really blank page stays blank


def _pages_with_dark_xrays(tmp_path):
    text = Image.new("L", (850, 1100), 250)
    for row in range(20):
        ImageDraw.Draw(text).rectangle((80, 80 + row * 45, 760, 92 + row * 45), fill=0)
    path = _pdf_of(tmp_path, text, _dark_photo(), _dark_photo())
    ink = ink_coverage(path)
    # No text on the X-rays; Mistral reports an image on them, Tesseract
    # pages always count as having one.
    texts = ["LETTER Befund\nText of a real page", "", ""]
    return [
        Page(i, text, "", "", i > 0, ink=value) for i, (text, value) in enumerate(zip(texts, ink))
    ]


def test_dark_pages_survive_build_plan(settings, tmp_path):
    settings = replace(settings, blank_max_ink=0.002)
    pages = _pages_with_dark_xrays(tmp_path)
    assert [p.is_blank(settings.blank_max_chars, settings.blank_max_ink) for p in pages] == [False, False, False]

    plan = build_plan(pages, settings, FakeBackend([]), "s.pdf", "abc", PurePosixPath(""))

    assert plan.dropped_pages == ""
    assert [d.pages for d in plan.documents] == ["1-3"]


def test_dark_pages_survive_keep_name_plan(settings, tmp_path):
    settings = replace(settings, blank_max_ink=0.002)
    pages = _pages_with_dark_xrays(tmp_path)

    plan = keep_name_plan(pages, settings, "s.pdf", "abc", PurePosixPath(""), "s")

    assert plan.dropped_pages == "" and plan.documents[0].pages == "1-3"
