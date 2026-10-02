"""The split plan: a small, hand-editable JSON file per stack.

Page numbers in the plan are 1-based and written as ranges ("1-3, 5"), the
way a person reads them off a PDF viewer. Correcting a wrong split means
editing `pages` (and clearing `title` to have it re-generated), then running
`stacksplit rebuild`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

_PART = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def format_pages(indices: list[int]) -> str:
    """0-based indices -> "1-3, 5"."""
    numbers = sorted({i + 1 for i in indices})
    if not numbers:
        return ""
    parts: list[str] = []
    start = prev = numbers[0]
    for n in numbers[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if prev != start else str(start))
        if n is not None:
            start = prev = n
    return ", ".join(parts)


def parse_pages(text: str, page_count: int) -> list[int]:
    """"1-3, 5" -> [0, 1, 2, 4], in the order written. Raises ValueError."""
    indices: list[int] = []
    for part in filter(str.strip, text.split(",")):
        match = _PART.match(part)
        if not match:
            raise ValueError(f"cannot read page range {part.strip()!r}")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if not 1 <= first <= last <= page_count:
            raise ValueError(f"page range {part.strip()!r} outside 1-{page_count}")
        indices.extend(range(first - 1, last))
    if not indices:
        raise ValueError("empty page list")
    return indices


@dataclass
class PlannedDocument:
    pages: str
    title: str
    date: str | None
    issuer: str | None = None
    summary: str = ""
    confidence: float = 1.0
    needs_review: bool = False
    review_reason: str = ""


@dataclass
class Plan:
    source: str
    source_sha256: str
    page_count: int
    # Output sub-folder, relative to OUTPUT_DIR; mirrors the inbox sub-folder.
    folder: str
    documents: list[PlannedDocument]
    dropped_pages: str = ""
    written_files: list[str] = field(default_factory=list)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Plan":
        raw = json.loads(path.read_text(encoding="utf-8"))
        documents = [PlannedDocument(**doc) for doc in raw.pop("documents")]
        return cls(documents=documents, **raw)
