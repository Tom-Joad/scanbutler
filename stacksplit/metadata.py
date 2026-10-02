"""Title, date and a short summary for one split-out document."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .ocr import Page

log = logging.getLogger(__name__)

_IMAGE_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")

SYSTEM_PROMPT = """\
You name scanned documents for a personal archive. You receive the OCR text of ONE document.
Return:
- title: a short topic of 1 to 6 words naming the document type plus the most distinguishing detail,
  written in {language}. Good examples: "Blutbild", "Befundbericht CT Thorax", "Arztbrief Kardiologie",
  "Rechnung Laborleistungen", "Lab results", "Discharge letter Orthopedics".
  No dates, no person names of the document's subject, no file extension, no quotes.
- date: the single date the document is about, as YYYY-MM-DD: for reports and lab results the
  examination or sampling date, otherwise the issue date of the letter. Never a print date, a birth
  date or a date in the future of the document. null if no such date is present.
- issuer: the issuing practice, hospital, company or authority, or null.
- summary: one sentence in {language} describing the content.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": ["string", "null"]},
        "issuer": {"type": ["string", "null"]},
        "summary": {"type": "string"},
    },
    "required": ["title", "date", "issuer", "summary"],
    "additionalProperties": False,
}


class ChatBackend(Protocol):
    def chat_json(self, system: str, user: str, schema: dict, name: str) -> dict: ...


@dataclass
class Metadata:
    title: str
    date: str | None
    issuer: str | None
    summary: str


def normalize_date(value: object) -> str | None:
    """Accept only a real calendar date in ISO form."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d{4})-(\d{2})-(\d{2})\s*", value)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def document_text(pages: list[Page], max_chars: int) -> str:
    parts = []
    for page in pages:
        chunk = "\n".join(t for t in (page.header, _IMAGE_REF.sub("", page.markdown), page.footer) if t.strip())
        parts.append(f"--- page {page.number} ---\n{chunk.strip()}")
    text = "\n\n".join(parts)
    if len(text) <= max_chars:
        return text
    # The first pages identify a document, the last pages often hold the
    # signature date; the middle of a long report adds little.
    head = int(max_chars * 0.75)
    return text[:head] + "\n\n[... middle omitted ...]\n\n" + text[-(max_chars - head) :]


def describe(pages: list[Page], backend: ChatBackend, language: str, max_chars: int) -> Metadata:
    answer = backend.chat_json(
        SYSTEM_PROMPT.format(language=language),
        document_text(pages, max_chars),
        SCHEMA,
        "document_metadata",
    )
    title = str(answer.get("title") or "").strip()
    issuer = answer.get("issuer")
    return Metadata(
        title=title,
        date=normalize_date(answer.get("date")),
        issuer=str(issuer).strip() if issuer else None,
        summary=str(answer.get("summary") or "").strip(),
    )
