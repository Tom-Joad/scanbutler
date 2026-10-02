"""Decide, page by page, where one document ends and the next begins.

The LLM looks at overlapping windows of consecutive pages. Every page is
judged in several windows; the verdict that counts is the one from the
window where that page sits closest to the middle, i.e. where the model saw
the most context on both sides. Explicit "page k of n" markers override the
model where they are unambiguous.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from .ocr import Page

log = logging.getLogger(__name__)

_IMAGE_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKER = re.compile(
    r"\b(?:seite|page|blatt|pagina|s\.)\s*(\d{1,3})\s*(?:von|of|/|aus|de|di)\s*(\d{1,3})\b",
    re.IGNORECASE,
)

HEAD_CHARS = 1200
TAIL_CHARS = 400

SYSTEM_PROMPT = """\
You split a scanned stack of paper documents into the individual documents it contains.
The stack was scanned in one go without separator sheets, so boundaries must be inferred from content alone.
You receive consecutive pages as JSON. For EVERY page listed, decide whether it starts a new document
or continues the document of the page listed directly before it.

Strong signs of a NEW document:
- letterhead, sender block or logo at the top; a new addressee block or salutation
- a new heading such as a report title, form name, invoice or lab report header
- a new date, case number or patient/customer block at the top of the page
- "page 1 of N", or a change of issuer, layout or form type
Strong signs of a CONTINUATION:
- "page k of N" with k > 1 matching the previous page
- text continuing mid-sentence or mid-table from the previous page
- the same running header/footer, case number or report continuing
- enclosures explicitly announced by the previous page (e.g. "attached: lab results") that carry no own letterhead
Duplicates: stacks often contain the same document scanned twice. A page that repeats the FIRST page of a
document already seen is a second copy and therefore starts a new document; never merge copies.

For the first page listed there is no previous page in view: answer true if it looks like the first page of a document.
First write reason (a few words, in English), then the decision. confidence is your certainty from 0.0 to 1.0.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                # reason comes before the verdict on purpose: the model writes
                # fields in order, so it argues first and decides second.
                # The other way round, reason and verdict sometimes disagreed.
                "properties": {
                    "page": {"type": "integer"},
                    "reason": {"type": "string"},
                    "starts_new_document": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": ["page", "reason", "starts_new_document", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pages"],
    "additionalProperties": False,
}


class ChatBackend(Protocol):
    def chat_json(self, system: str, user: str, schema: dict, name: str) -> dict: ...


@dataclass(frozen=True)
class Decision:
    page_index: int
    starts_new: bool
    confidence: float
    reason: str


def page_marker(page: Page) -> tuple[int, int] | None:
    """Find a "page k of n" marker in the running header/footer or the page edges."""
    body = page.markdown
    for text in (page.header, page.footer, body[-TAIL_CHARS:], body[:HEAD_CHARS // 3]):
        for match in _MARKER.finditer(text):
            k, n = int(match.group(1)), int(match.group(2))
            if 1 <= k <= n:
                return k, n
    return None


def _excerpt(page: Page) -> str:
    text = _IMAGE_REF.sub("[image]", page.markdown).strip()
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    return text[:HEAD_CHARS] + "\n[...]\n" + text[-TAIL_CHARS:]


def window_starts(count: int, window: int, step: int) -> list[int]:
    """Start offsets of overlapping windows that together cover all positions."""
    if count <= window:
        return [0]
    starts = list(range(0, count - window + 1, step))
    if starts[-1] != count - window:
        starts.append(count - window)
    return starts


def best_window(position: int, starts: list[int], window: int, count: int) -> int:
    """The window start in which `position` has the most context on both sides."""
    best, best_margin = starts[0], -1
    for start in starts:
        end = min(start + window, count) - 1
        if start <= position <= end:
            # A window edge that is also the stack edge costs no context.
            left = position - start if start > 0 else window
            right = end - position if end < count - 1 else window
            margin = min(left, right)
            if margin > best_margin:
                best, best_margin = start, margin
    return best


def _ask_window(backend: ChatBackend, pages: list[Page]) -> dict[int, Decision]:
    payload = [
        {
            "page": p.number,
            "page_marker": "{} of {}".format(*m) if (m := page_marker(p)) else None,
            "header": p.header.strip()[:300] or None,
            "footer": p.footer.strip()[:300] or None,
            "text": _excerpt(p),
        }
        for p in pages
    ]
    answer = backend.chat_json(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), SCHEMA, "page_boundaries")
    by_number = {p.number: p.index for p in pages}
    decisions: dict[int, Decision] = {}
    for item in answer.get("pages", []):
        index = by_number.get(item.get("page"))
        if index is None:
            continue
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        decisions[index] = Decision(index, bool(item.get("starts_new_document")), confidence, str(item.get("reason", ""))[:200])
    return decisions


def _apply_markers(pages: list[Page], decisions: list[Decision]) -> list[Decision]:
    result: list[Decision] = []
    for page, decision in zip(pages, decisions):
        marker = page_marker(page)
        if marker and marker[0] == 1 and marker[1] > 1:
            decision = Decision(decision.page_index, True, max(decision.confidence, 0.97), "page marker 1 of n")
        elif marker and marker[0] > 1:
            # "Page 2 of 5" never opens a document, even when page 1 of 5
            # carried no marker or OCR missed it.
            decision = Decision(decision.page_index, False, max(decision.confidence, 0.97), "page marker k of n, k > 1")
        result.append(decision)
    return result


def detect_boundaries(
    pages: list[Page], backend: ChatBackend, window: int, step: int, concurrency: int
) -> list[Decision]:
    """One decision per page in `pages` (blank pages should already be removed)."""
    if not pages:
        return []

    count = len(pages)
    starts = window_starts(count, window, step)
    log.info("boundary detection started", extra={"pages": count, "windows": len(starts)})

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        answers = dict(zip(starts, pool.map(lambda s: _ask_window(backend, pages[s : s + window]), starts)))

    decisions: list[Decision] = []
    for pos, page in enumerate(pages):
        chosen = answers[best_window(pos, starts, window, count)].get(page.index)
        if chosen is None:
            # Fall back to any window that answered for this page.
            chosen = next((a[page.index] for a in answers.values() if page.index in a), None)
        if chosen is None:
            chosen = Decision(page.index, False, 0.0, "no answer from model")
        decisions.append(chosen)

    decisions = _apply_markers(pages, decisions)
    first = decisions[0]
    decisions[0] = Decision(first.page_index, True, 1.0, "first page of stack")
    return decisions
