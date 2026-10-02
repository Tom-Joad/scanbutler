from __future__ import annotations

from PIL import Image, ImageDraw

from stacksplit.boundaries import Decision, _apply_markers
from stacksplit.ocr import Page
from stacksplit.pdfops import ink_coverage


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
