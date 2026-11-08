"""Text layer from Tesseract and Mistral OCR together.

Tesseract places every word exactly but can't read handwriting; Mistral OCR
reads handwriting well but only says which paragraph ("block") a text
belongs to. Both look at the same page image. Within each Mistral block,
Mistral's words are aligned character by character with the lines Tesseract
found there, so every line gets its share of Mistral's text. A line both
read alike keeps Tesseract's words, and search hits and selections fit word
by word; any other line (handwriting, faint print) gets Mistral's words,
spread over the line. A block Tesseract found no line in gets Mistral's
lines in equal stripes. Mistral's text that Tesseract found no line for (a
handwritten line it missed) goes into the gap below the line before it.

Deciding per block was too coarse: Mistral returns a whole handwritten
recipe as one block, and Tesseract reads neat handwriting half right, so the
block as a whole looked close enough while many lines were wrong.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from ocrmypdf.models.ocr_element import BoundingBox, OcrClass, OcrElement

# Share of matching characters at which Tesseract's reading of a line is
# kept: all of them. Printed lines agree letter for letter; one wrong letter
# ("Stlck" for "Stück", 0.97) would hide the word from search, and Mistral's
# reading is the better one wherever the two differ.
KEEP_RATIO = 1.0

# Shorter runs of matching letters say nothing: "her" or "en" turn up in any
# two German lines, misread or not.
MIN_MATCH = 4

# Tesseract words outside every Mistral block below this confidence are
# dropped: what Mistral saw no text in is a flourish, a margin or a stain.
NOISE_CONFIDENCE = 0.6

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


def _stripes(block: Block) -> list[OcrElement]:
    """Mistral's lines of a block Tesseract found nothing in, in equal stripes."""
    box, lines = block.box, block.lines
    step = box.height / len(lines)
    return [
        _line_of_words(text, BoundingBox(box.left, box.top + i * step, box.right, box.top + (i + 1) * step))
        for i, text in enumerate(lines)
    ]


def _take(words: list[str], letters: int) -> int:
    """How many of `words` come closest to `letters` letters."""
    count, total = 0, 0
    for word in words:
        size = len(_letters(word))
        if abs(total + size - letters) > abs(total - letters):
            break
        count, total = count + 1, total + size
    return count


def assign(lines: list[str], words: list[str]) -> list[list[tuple[str, bool]]]:
    """Mistral's `words`, shared out over Tesseract's `lines` in reading order.

    The letters of both readings are aligned; each word goes to the line
    most of its letters matched. Words between two matched ones go where
    Tesseract read letters it couldn't match: as many as Tesseract has at the
    end of the line before go there, as many as it has at the start of the
    line after go there. What is left belongs to no line Tesseract found (a
    line it missed); it is marked False and follows the line before, so no
    word of Mistral's is lost.
    """
    t_text, t_line = [], []
    for index, text in enumerate(lines):
        for char in _letters(text):
            t_text.append(char)
            t_line.append(index)
    m_text, m_word = [], []
    for index, word in enumerate(words):
        for char in _letters(word):
            m_text.append(char)
            m_word.append(index)
    votes = [Counter() for _ in words]
    hit = [False] * len(t_text)
    matcher = SequenceMatcher(None, "".join(t_text), "".join(m_text), autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        if size < MIN_MATCH:
            continue
        for k in range(size):
            votes[m_word[b + k]][t_line[a + k]] += 1
            hit[a + k] = True
    # Letters Tesseract read but couldn't match, at the start and end of each line.
    lead, trail = [0] * len(lines), [0] * len(lines)
    for index in range(len(lines)):
        flags = [h for h, line in zip(hit, t_line) if line == index]
        if any(flags):
            lead[index] = flags.index(True)
            trail[index] = flags[::-1].index(True)
        else:
            lead[index] = len(flags)

    line_of: list[int | None] = []
    for word, vote in zip(words, votes):
        line, count = vote.most_common(1)[0] if vote else (None, 0)
        line_of.append(line if count * 2 >= max(1, len(_letters(word))) else None)
    # Matching blocks never cross, but a word may still vote for an earlier line.
    current = 0
    for index, line in enumerate(line_of):
        if line is not None:
            current = max(current, line)
            line_of[index] = current

    shared: list[list[tuple[str, bool]]] = [[] for _ in lines]
    index = 0
    while index < len(words):
        if line_of[index] is not None:
            shared[line_of[index]].append((words[index], True))
            index += 1
            continue
        end = index
        while end < len(words) and line_of[end] is None:
            end += 1
        _share_run(words[index:end], line_of[index - 1] if index else None,
                   line_of[end] if end < len(words) else None, lead, trail, shared)
        index = end
    return shared


def _share_run(run: list[str], before: int | None, after: int | None, lead: list[int], trail: list[int],
               shared: list[list[tuple[str, bool]]]) -> None:
    """Words between the matched words of line `before` and line `after`."""
    # The start of the line after: as many letters as Tesseract read there unmatched.
    back = _take(run[::-1], lead[after]) if after is not None else 0
    body, back_words = run[: len(run) - back], run[len(run) - back:]
    # The end of the line before, then lines in between that matched nothing.
    slots = [(before, trail[before])] if before is not None else []
    first = before + 1 if before is not None else 0
    last = after if after is not None else len(lead)
    slots += [(line, lead[line]) for line in range(first, last)]
    owner = before
    for line, letters in slots:
        count = _take(body, letters)
        shared[line].extend((w, True) for w in body[:count])
        body = body[count:]
        owner = line if count or owner is None else owner
    if body:
        if owner is None:
            # Before the first match and no line to fill: it opens the line after.
            back_words = body + back_words
        else:
            shared[owner].extend((w, False) for w in body)
    if after is not None:
        shared[after].extend((w, True) for w in back_words)


def _split(words: list[str], parts: int) -> list[list[str]]:
    """`words` in `parts` runs of about the same length, in order."""
    total = sum(len(w) + 1 for w in words)
    runs: list[list[str]] = [[] for _ in range(parts)]
    done = 0
    for word in words:
        index = min(parts - 1, int(done * parts / total))
        runs[index].append(word)
        done += len(word) + 1
    return [run for run in runs if run]


def _pitch(boxes: list[BoundingBox]) -> float:
    """The usual distance from one line to the next."""
    tops = sorted(b.top for b in boxes)
    steps = sorted(b - a for a, b in zip(tops, tops[1:]) if b - a > 0)
    heights = sorted(b.height for b in boxes)
    height = heights[len(heights) // 2]
    if not steps:
        return height * 1.6
    # With few lines found, the steps may span missed lines themselves;
    # handwriting rarely leaves more than a line's height between lines.
    step = steps[len(steps) // 2]
    return max(height, min(step, height * 2))


@dataclass
class MergeStats:
    blocks: int = 0
    lines_kept: int = 0
    lines_replaced: int = 0
    lines_added: int = 0
    noise_dropped: int = 0


def _block_lines(block: Block, inside: list[tuple[OcrElement, list[OcrElement]]], keep_ratio: float,
                 stats: MergeStats, drop: set[int]) -> list[OcrElement]:
    """The block's lines in reading order; Tesseract's words they replace go into `drop`.

    Empty if Tesseract read every line as Mistral did: the block stays as it is.
    """
    boxes = [_union([w.bbox for w in words]) for _, words in inside]
    readings = [" ".join(w.text for w in words) for _, words in inside]
    shared = assign(readings, block.text.split())
    pitch = _pitch(boxes)
    new_lines, changed = [], False
    for index, ((line, words), box, reading, mine) in enumerate(zip(inside, boxes, readings, shared)):
        # Unmatched words at the end of a line belong to lines Tesseract
        # missed, if there is room for them before the next line.
        cut = len(mine)
        while cut and not mine[cut - 1][1]:
            cut -= 1
        own, tail = [w for w, _ in mine[:cut]], [w for w, _ in mine[cut:]]
        below = boxes[index + 1].top if index + 1 < len(boxes) else block.box.bottom
        slots = int((below - box.top) / pitch + 0.5) - 1
        if not (tail and own and slots >= 1):
            own, tail = own + tail, []
        text = " ".join(own)
        if text and agreement(reading, text) >= keep_ratio:
            stats.lines_kept += 1
            # Moved along with the block, so the text stays in reading order.
            new_lines.append(replace(line, children=list(words)))
        else:
            changed = True
            if text:
                stats.lines_replaced += 1
                new_lines.append(_line_of_words(text, box))
        if tail:
            changed = True
            right = max(box.right, block.box.right)
            # Spread evenly over the gap: line spacing on a photographed
            # page grows towards the bottom.
            runs = _split(tail, slots)
            step = (below - box.top) / (len(runs) + 1)
            for slot, run in enumerate(runs, start=1):
                top = box.top + slot * step
                bottom = min(top + box.height, block.box.bottom)
                if bottom > top:
                    stats.lines_added += 1
                    new_lines.append(_line_of_words(" ".join(run), BoundingBox(box.left, top, right, bottom)))
    if not changed:
        return []
    drop.update(id(w) for _, words in inside for w in words)
    return new_lines


def merge(page: OcrElement, blocks: list[Block], keep_ratio: float = KEEP_RATIO) -> tuple[OcrElement, MergeStats]:
    """`page` (Tesseract) with every line it misread replaced by Mistral's text."""
    stats = MergeStats(blocks=len(blocks))
    lines = page.lines
    drop: set[int] = set()  # ids of Tesseract words to drop
    claimed: set[int] = set()  # words inside some block
    added: list[tuple[OcrElement, int | None]] = []
    for block in blocks:
        inside = [(line, [w for w in line.words if id(w) not in claimed and _center_in(w, block.box)]) for line in lines]
        inside = [(line, words) for line, words in inside if words]
        claimed.update(id(w) for _, words in inside for w in words)
        if inside:
            new_lines = _block_lines(block, inside, keep_ratio, stats, drop)
        else:
            new_lines = _stripes(block)
            stats.lines_replaced += len(new_lines)
        if new_lines:
            para = OcrElement(ocr_class=OcrClass.PARAGRAPH, bbox=_union([l.bbox for l in new_lines]), children=new_lines)
            # Placed where the block's first Tesseract line was, so the
            # page's text stays in reading order.
            added.append((para, id(inside[0][0]) if inside else None))
    if blocks:
        for w in page.words:
            if id(w) not in claimed and w.confidence is not None and w.confidence < NOISE_CONFIDENCE:
                drop.add(id(w))
                stats.noise_dropped += 1
    if not drop and not added:
        return page, stats
    return _without(page, drop, added), stats


def _without(element: OcrElement, drop: set[int], add: list[tuple[OcrElement, int | None]]) -> OcrElement:
    """A copy of the page tree without the words in `drop`, with the paragraphs in `add`.

    A paragraph goes next to the Tesseract paragraph that held its anchor
    line: after what is left of it above the new text, otherwise before it.
    One without an anchor goes among the page's areas by its height.
    """
    anchored: dict[int, list[OcrElement]] = {}
    loose: list[OcrElement] = []
    for para, anchor in add:
        if anchor is None:
            loose.append(para)
        else:
            anchored.setdefault(anchor, []).append(para)
    placed: set[int] = set()

    def prune(node: OcrElement) -> OcrElement | None:
        if node.ocr_class == OcrClass.WORD:
            return None if id(node) in drop else node
        if not node.children:
            return node
        children: list[OcrElement] = []
        for child in node.children:
            kept = prune(child)
            mine = []
            if child.ocr_class == OcrClass.PARAGRAPH:
                for line in child.lines:
                    for para in anchored.get(id(line), []):
                        if id(para) not in placed:
                            placed.add(id(para))
                            mine.append(para)
            if not mine:
                if kept is not None:
                    children.append(kept)
                continue
            above = kept is not None and any(l.bbox and l.bbox.top < mine[0].bbox.top for l in kept.lines)
            children.extend([kept, *mine] if above else [*mine, *([kept] if kept is not None else [])])
        if not children and node.ocr_class != OcrClass.PAGE:
            return None
        return _copy(node, children)

    page = prune(element) or _copy(element, [])
    # Paragraphs whose anchor wasn't inside a paragraph, and those without one.
    loose += [para for para, _ in add if id(para) not in placed and para not in loose]
    top_level = list(page.children)
    for para in loose:
        at = next(
            (i for i, child in enumerate(top_level) if child.bbox is not None and child.bbox.top > para.bbox.top),
            len(top_level),
        )
        top_level.insert(at, para)
    return _copy(page, top_level)


def _copy(node: OcrElement, children: list[OcrElement]) -> OcrElement:
    return replace(node, children=children)


def text_of(page: OcrElement) -> str:
    """The page's text, one line per line, for ocrmypdf's text sidecar."""
    return "\n".join(" ".join(w.text for w in line.words if w.text) for line in page.lines) + "\n"
