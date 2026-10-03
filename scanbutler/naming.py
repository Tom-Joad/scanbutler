"""File names that are safe on Linux, Windows/SMB shares and macOS."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

MAX_STEM = 150


def sanitize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    # Titles come from a language model reading untrusted documents. Format
    # characters such as U+202E (right-to-left override) could make a name
    # display differently from what it is, so they are dropped.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = _FORBIDDEN.sub(" ", text)
    text = _SPACES.sub(" ", text).strip(" .")
    if text.upper() in _RESERVED:
        text = f"_{text}"
    return text[:MAX_STEM].rstrip(" .")


def build_stem(pattern: str, title: str, date: str | None, no_date_label: str) -> str:
    stem = pattern.replace("{title}", title.strip() or "Document").replace("{date}", date or no_date_label)
    return sanitize(stem) or "Document"


def unique_path(directory: Path, stem: str, suffix: str = ".pdf", taken: set[Path] | None = None) -> Path:
    """First free `stem.pdf`, `stem (2).pdf`, ... not on disk and not in `taken`."""
    taken = taken or set()
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists() or candidate in taken:
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate
