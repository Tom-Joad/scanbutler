from __future__ import annotations

from ocrmypdf.models.ocr_element import BoundingBox, OcrClass, OcrElement

from scanbutler import hybrid


def word(text, left, top, right, bottom):
    return OcrElement(ocr_class=OcrClass.WORD, bbox=BoundingBox(left, top, right, bottom), text=text)


def line(*words):
    box = BoundingBox(
        min(w.bbox.left for w in words), min(w.bbox.top for w in words),
        max(w.bbox.right for w in words), max(w.bbox.bottom for w in words),
    )
    return OcrElement(ocr_class=OcrClass.LINE, bbox=box, children=list(words))


def page(*lines):
    par = OcrElement(ocr_class=OcrClass.PARAGRAPH, bbox=BoundingBox(0, 0, 1000, 1000), children=list(lines))
    return OcrElement(ocr_class=OcrClass.PAGE, bbox=BoundingBox(0, 0, 1000, 1000), children=[par])


def block(kind, left, top, right, bottom, content):
    return {"type": kind, "top_left_x": left, "top_left_y": top, "bottom_right_x": right, "bottom_right_y": bottom,
            "content": content}


PRINTED = line(word("Zutaten:", 100, 100, 250, 140), word("500", 260, 100, 320, 140), word("g", 330, 100, 350, 140),
               word("Mehl", 360, 100, 450, 140))
# Tesseract's reading of a handwritten line.
SCRIBBLE = line(word("Ei@r", 100, 300, 200, 350), word("vcrq;", 210, 300, 330, 350))


def test_plain_strips_markdown():
    assert hybrid.plain("# Title\n\n- **eins**\n1. zwei | drei\n![img](x.png)") == ["Title", "eins", "zwei drei"]


def test_agreement():
    assert hybrid.agreement("Zutaten: 500 g Mehl", "Zutaten 500g Mehl") == 1.0
    assert hybrid.agreement("Ei@r vcrq;", "Eier verquirlen") < hybrid.KEEP_RATIO
    assert hybrid.agreement("", "Text") == 0.0


def test_blocks_scaled_to_the_image():
    raw = {"dimensions": {"width": 500, "height": 500},
           "blocks": [block("text", 50, 50, 250, 75, "Zutaten"), block("table", 0, 0, 10, 10, "[tbl](tbl)")]}
    [only] = hybrid.blocks_from_page(raw, 1000, 1000)
    assert (only.box.left, only.box.top, only.box.right, only.box.bottom) == (100, 100, 500, 150)


def test_blocks_with_fractions():
    raw = {"blocks": [block("text", 0.1, 0.1, 0.5, 0.15, "Zutaten")]}
    [only] = hybrid.blocks_from_page(raw, 1000, 2000)
    assert (only.box.left, only.box.top, only.box.right, only.box.bottom) == (100, 200, 500, 300)


def test_printed_text_keeps_tesseract_words():
    tree = page(PRINTED)
    blocks = hybrid.blocks_from_page({"blocks": [block("text", 90, 90, 460, 150, "Zutaten: 500 g Mehl")]}, 1000, 1000)
    merged, stats = hybrid.merge(tree, blocks)
    assert merged is tree
    assert (stats.kept, stats.replaced) == (1, 0)


def test_handwriting_replaced_on_tesseracts_line():
    tree = page(PRINTED, SCRIBBLE)
    blocks = hybrid.blocks_from_page(
        {"blocks": [block("text", 90, 90, 460, 150, "Zutaten: 500 g Mehl"),
                    block("text", 90, 290, 600, 360, "Eier verquirlen")]}, 1000, 1000)
    merged, stats = hybrid.merge(tree, blocks)
    assert (stats.kept, stats.replaced) == (1, 1)
    assert hybrid.text_of(merged) == "Zutaten: 500 g Mehl\nEier verquirlen\n"
    new = merged.lines[-1]
    # On the line Tesseract found, not on the whole block.
    assert (new.bbox.top, new.bbox.bottom, new.bbox.left, new.bbox.right) == (300, 350, 100, 330)
    first, second = new.words
    assert first.bbox.right <= second.bbox.left


def test_block_tesseract_saw_nothing_in_gets_stripes():
    tree = page(PRINTED)
    blocks = hybrid.blocks_from_page({"blocks": [block("text", 100, 500, 900, 700, "eins\nzwei")]}, 1000, 1000)
    merged, stats = hybrid.merge(tree, blocks)
    assert stats.replaced == 1
    one, two = merged.lines[-2:]
    assert (one.bbox.top, one.bbox.bottom, two.bbox.top, two.bbox.bottom) == (500, 600, 600, 700)


def test_words_outside_blocks_stay():
    tree = page(PRINTED, SCRIBBLE)
    blocks = hybrid.blocks_from_page({"blocks": [block("text", 90, 290, 600, 360, "Eier verquirlen")]}, 1000, 1000)
    merged, _ = hybrid.merge(tree, blocks)
    assert hybrid.text_of(merged).startswith("Zutaten: 500 g Mehl\n")
    assert "Ei@r" not in hybrid.text_of(merged)
