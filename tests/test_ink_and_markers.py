from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import PurePosixPath

from PIL import Image, ImageDraw

from scanbutler.boundaries import Decision, _apply_markers
from scanbutler.ocr import Page
from scanbutler import pdfops
from scanbutler.pdfops import Ink, count_words, measure_ink
from scanbutler.pipeline import build_plan, keep_name_plan

from .conftest import FakeBackend


def test_ink_separates_blank_tinted_and_text_pages(tmp_path):
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

    blank, speck, tinted_form, text = measure_ink(path)

    assert blank == Ink(pale=0, marks=0)
    assert speck == Ink(pale=0, marks=0)
    assert 0.002 < tinted_form.pale < 0.05  # form lines count although paper is tinted
    assert text.pale > 0.01 and text.marks


def _paper() -> Image.Image:
    return Image.new("L", (850, 1100), 244)


def _bars(image: Image.Image, grey: int, rows: int = 20) -> Image.Image:
    """Lines of text as the measurement sees them: bars of one grey."""
    for row in range(rows):
        ImageDraw.Draw(image).rectangle((80, 80 + row * 45, 760, 92 + row * 45), fill=grey)
    return image


def _small_print(image: Image.Image, at: tuple[int, int]) -> Image.Image:
    """A page number: strokes one or two pixels wide, a few millimetres high."""
    x, y = at
    draw = ImageDraw.Draw(image)
    draw.line((x, y, x + 6, y), fill=40, width=2)
    draw.line((x + 6, y, x, y + 10), fill=40, width=2)
    draw.line((x, y + 10, x + 6, y + 10), fill=40, width=2)
    return image


def test_pale_print_is_content_and_show_through_is_not(tmp_path):
    # A worn ribbon prints a full page only a little darker than the paper.
    # The print of the other side shows through fainter still.
    pale_print = _bars(_paper(), 244 - 28)
    show_through = _bars(_paper(), 244 - 10)
    path = _pdf_of(tmp_path, pale_print, show_through)

    pale_print, show_through = measure_ink(path)

    assert not pale_print.empty(0.001)
    assert show_through.empty(0.001)


def test_a_lone_page_number_is_content(tmp_path):
    # Next to no ink and close to the edge: only the marks give it away.
    numbered = [_small_print(_paper(), at) for at in ((420, 1060), (801, 1037), (40, 30), (423, 1063))]
    path = _pdf_of(tmp_path, *numbered, _paper())

    *numbered, blank = measure_ink(path)

    assert all(page.pale < 0.001 for page in numbered)
    assert all(page.marks and not page.empty(0.001) for page in numbered)
    assert blank.empty(0.001)


def test_a_page_number_is_seen_wherever_it_sits():
    # The marks are counted in cells; no position may fall between them.
    for dx in range(0, 16, 3):
        for dy in range(0, 16, 3):
            ink = pdfops._measure(_small_print(_paper(), (400 + dx, 1000 + dy)))
            assert ink.marks, (dx, dy)


def test_dust_and_fold_lines_leave_a_page_empty(tmp_path):
    page = _paper()
    draw = ImageDraw.Draw(page)
    for x, y in ((100, 200), (431, 507), (700, 950), (333, 111)):
        draw.point((x, y), fill=0)  # dust
    draw.line((0, 366, 850, 366), fill=244 - 25, width=3)  # a fold, a shadow lighter than ink
    path = _pdf_of(tmp_path, page)

    assert measure_ink(path)[0].empty(0.001)


def test_words_in_the_text_layer_keep_a_page():
    # A footnote in small print can measure as no ink at all.
    assert Ink(pale=0, marks=0).empty(0.001)
    assert not Ink(pale=0, marks=0, words=1).empty(0.001)
    assert count_words("Seite 6/6") == 1
    assert count_words("Keine besonderen Merkmale") == 3
    # What text recognition makes of empty paper: stray characters, no words.
    assert count_words("* i ol A A a r ¥ : i Lr ad | x > N is Co ” 4 - - “ a") == 0


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
    calls = [lambda: measure_ink(path), lambda: pdfops.page_texts(path)] * 4
    threads = [threading.Thread(target=call) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert most == 1


def test_hallucinated_text_on_blank_page_is_still_blank():
    page = Page(index=0, markdown="2017年，公司与上海浦东发展银行" * 50, header="", footer="", has_images=False, ink=Ink(0.0, 0))
    assert page.is_blank(15, 0.002)
    assert not Page(0, "x" * 300, "", "", False, ink=Ink(0.01, 0)).is_blank(15, 0.002)


def test_a_page_in_doubt_is_kept():
    # A signature: no text to speak of, hardly any ink, but marks on the page.
    signature = Page(0, "", "", "", False, ink=Ink(pale=0.0004, marks=12))
    assert not signature.is_blank(15, 0.001)
    # A page number the text layer has read.
    numbered = Page(0, "", "", "", False, ink=Ink(pale=0, marks=0, words=1))
    assert not numbered.is_blank(15, 0.001)
    # With the ink check off (BLANK_MAX_INK_PERCENT=0), the text decides as before.
    assert signature.is_blank(15, 0)


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

    coverage = measure_ink(path)

    assert coverage[0].pale > 0.9  # a black cover sheet
    assert coverage[1].pale > 0.9  # an X-ray
    assert coverage[2].empty(0.001)  # a really blank page stays blank


def _pages_with_dark_xrays(tmp_path):
    text = Image.new("L", (850, 1100), 250)
    for row in range(20):
        ImageDraw.Draw(text).rectangle((80, 80 + row * 45, 760, 92 + row * 45), fill=0)
    path = _pdf_of(tmp_path, text, _dark_photo(), _dark_photo())
    ink = measure_ink(path)
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
