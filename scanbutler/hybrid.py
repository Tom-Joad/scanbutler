"""Text layer from Tesseract and Mistral OCR together.

Tesseract places every word exactly but can't read handwriting; Mistral OCR
reads handwriting well but only says which paragraph ("block") a text
belongs to. Both look at the same page image. For every Mistral block the
Tesseract words inside it are compared with Mistral's text: where they agree,
Tesseract's words stay, so search hits and selections fit word by word. Where
they don't (handwriting, faint print), Mistral's text replaces them, spread
over the lines Tesseract found in the block, or over the block if it found
none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from ocrmypdf.models.ocr_element import BoundingBox, OcrClass, OcrElement

# Share of matching characters at which Tesseract's reading of a block is
# kept. Printed text measures 0.9 and more; handwriting stays far below.
KEEP_RATIO = 0.75

# Blocks whose content is no running text: tables come as a reference to a
# separate table, images as one to an image, so their area keeps Tesseract.
SKIP_TYPES = {"table", "image"}

_MARKDOWN = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),  # image references
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # links keep their text
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),  # headings
    (re.compile(r"^\s*>\s?", re.M), ""),  # quotes
    (re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M), ""),  # list markers
    (re.compile(r"\$+([^$]*)\$+"), r"\1"),  # LaTeX math delimiters
    (re.compile(r"[*_`~]+"), ""),  # emphasis, code
    (re.compile(r"<[^>\n]+>"), ""),  # HTML tags (tables in HTML format)
]


def plain(markdown: str) -> list[str]:
    """The non-empty lines of a Mistral block, without markdown."""
    text = markdown
    for pattern, replacement in _MARKDOWN:
        text = pattern.sub(replacement, text)
    lines = []
    for line in text.splitlines():
        line = " ".join(line.replace("|", " ").split())
        if line:
            lines.append(line)
    return lines


def _letters(text: str) -> str:
    return re.sub(r"\W+", "", text.lower())


def agreement(tesseract: str, mistral: str) -> float:
    """How far two readings of the same block agree, 0..1, letters and digits only."""
    a, b = _letters(tesseract), _letters(mistral)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


@dataclass(frozen=True)
class Block:
    """A Mistral block in the coordinates of the page image (pixels)."""

    kind: str
    box: BoundingBox
    lines: list[str]

    @property
    def text(self) -> str:
        return " ".join(self.lines)


def blocks_from_page(page: dict, width: float, height: float) -> list[Block]:
    """Mistral's blocks of one page, scaled to an image of `width` x `height` px.

    The coordinates are pixels of the page as Mistral measured it
    (`dimensions`); values of at most 1 are taken as fractions of the page.
    """
    raw = page.get("blocks") or []
    dims = page.get("dimensions") or {}
    keys = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")
    numbers = [float(b.get(k) or 0) for b in raw if isinstance(b, dict) for k in keys]
    normalized = bool(numbers) and max(numbers) <= 1.0
    if normalized:
        sx, sy = width, height
    else:
        sx = width / float(dims["width"]) if dims.get("width") else 1.0
        sy = height / float(dims["height"]) if dims.get("height") else 1.0
    blocks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "text")
        lines = plain(str(item.get("content") or ""))
        if kind in SKIP_TYPES or not lines:
            continue
        try:
            left, top, right, bottom = (float(item[k]) for k in keys)
        except (KeyError, TypeError, ValueError):
            continue
        left, right = sorted((max(0.0, left * sx), min(width, right * sx)))
        top, bottom = sorted((max(0.0, top * sy), min(height, bottom * sy)))
        if right - left < 1 or bottom - top < 1:
            continue
        blocks.append(Block(kind, BoundingBox(left, top, right, bottom), lines))
    return blocks


def _center_in(word: OcrElement, box: BoundingBox) -> bool:
    if word.bbox is None:
        return False
    x = (word.bbox.left + word.bbox.right) / 2
    y = (word.bbox.top + word.bbox.bottom) / 2
    return box.left <= x <= box.right and box.top <= y <= box.bottom


def _union(boxes: list[BoundingBox]) -> BoundingBox:
    return BoundingBox(
        min(b.left for b in boxes), min(b.top for b in boxes), max(b.right for b in boxes), max(b.bottom for b in boxes)
    )


def _line_of_words(text: str, box: BoundingBox) -> OcrElement:
    """A line whose words share `box` in proportion to their length."""
    words = text.split()
    total = sum(len(w) + 1 for w in words) - 1
    unit = box.width / max(1, total)
    children, x = [], box.left
    for word in words:
        right = min(box.right, x + len(word) * unit)
        children.append(
            OcrElement(ocr_class=OcrClass.WORD, bbox=BoundingBox(x, box.top, max(right, x + 0.5), box.bottom), text=word)
        )
        x = right + unit
    return OcrElement(ocr_class=OcrClass.LINE, bbox=box, children=children)


def _layout(block: Block, found: list[BoundingBox]) -> list[OcrElement]:
    """Mistral's lines of `block`, placed on the lines Tesseract found there.

    With as many Tesseract lines as Mistral lines each takes one; otherwise
    the lines share the area Tesseract saw writing in (or the whole block)
    in equal stripes.
    """
    lines = block.lines
    if len(found) == len(lines):
        boxes = sorted(found, key=lambda b: b.top)
    else:
        area = _union(found) if found else block.box
        step = area.height / len(lines)
        boxes = [
            BoundingBox(area.left, area.top + i * step, area.right, area.top + (i + 1) * step) for i in range(len(lines))
        ]
    return [_line_of_words(text, box) for text, box in zip(lines, boxes) if box.height > 0 and box.width > 0]


@dataclass
class MergeStats:
    blocks: int = 0
    kept: int = 0
    replaced: int = 0


def merge(page: OcrElement, blocks: list[Block], keep_ratio: float = KEEP_RATIO) -> tuple[OcrElement, MergeStats]:
    """`page` (Tesseract) with every block it misread replaced by Mistral's text."""
    stats = MergeStats(blocks=len(blocks))
    lines = page.lines
    replaced: set[int] = set()  # ids of Tesseract words to drop
    added: list[OcrElement] = []
    for block in blocks:
        inside = [(line, [w for w in line.words if id(w) not in replaced and _center_in(w, block.box)]) for line in lines]
        inside = [(line, words) for line, words in inside if words]
        reading = " ".join(w.text for _, words in inside for w in words)
        if inside and agreement(reading, block.text) >= keep_ratio:
            stats.kept += 1
            continue
        stats.replaced += 1
        for _, words in inside:
            replaced.update(id(w) for w in words)
        found = [_union([w.bbox for w in words]) for _, words in inside]
        new_lines = _layout(block, found)
        if new_lines:
            added.append(OcrElement(ocr_class=OcrClass.PARAGRAPH, bbox=_union([l.bbox for l in new_lines]), children=new_lines))
    if not replaced and not added:
        return page, stats
    return _without(page, replaced, added), stats


def _without(element: OcrElement, drop: set[int], add: list[OcrElement]) -> OcrElement:
    """A copy of the page tree without the words in `drop`, plus `add` at the end."""

    def prune(node: OcrElement) -> OcrElement | None:
        if node.ocr_class == OcrClass.WORD:
            return None if id(node) in drop else node
        if not node.children:
            return node
        children = [c for c in (prune(child) for child in node.children) if c is not None]
        if not children and node.ocr_class != OcrClass.PAGE:
            return None
        return _copy(node, children)

    page = prune(element) or _copy(element, [])
    return _copy(page, [*page.children, *add])


def _copy(node: OcrElement, children: list[OcrElement]) -> OcrElement:
    return replace(node, children=children)


def text_of(page: OcrElement) -> str:
    """The page's text, one line per line, for ocrmypdf's text sidecar."""
    return "\n".join(" ".join(w.text for w in line.words if w.text) for line in page.lines) + "\n"
