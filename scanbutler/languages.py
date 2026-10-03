"""Tesseract language models beyond the ones built into the image.

The image ships tessdata_best models for deu, eng and osd. Any other language
named in OCRMYPDF_LANGUAGES is downloaded once from tessdata_best at a pinned
tag, checked against the SHA-256 list shipped with the package, and kept in
the work folder, so it survives image updates. Tesseract then reads all
models from that folder: the built-in files are linked into it.

A language that can't be downloaded is left out with a warning; the built-in
languages always work, also offline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TESSDATA_BEST_TAG = "4.1.0"
DEFAULT_URL = f"https://github.com/tesseract-ocr/tessdata_best/raw/{TESSDATA_BEST_TAG}"
FALLBACK = ("deu", "eng")
SUFFIX = ".traineddata"
CHUNK = 1 << 20

# Codes people commonly try instead of Tesseract's: ISO 639-1 and the
# ISO 639-2/B ("bibliographic") variants.
HINTS = {
    "de": "deu", "ger": "deu", "en": "eng", "fr": "fra", "fre": "fra", "it": "ita", "es": "spa",
    "pt": "por", "nl": "nld", "dut": "nld", "pl": "pol", "tr": "tur", "ru": "rus", "cs": "ces",
    "cze": "ces", "uk": "ukr", "sv": "swe", "da": "dan", "no": "nor", "nb": "nor", "fi": "fin",
    "hu": "hun", "ro": "ron", "rum": "ron", "el": "ell", "gre": "ell", "ja": "jpn", "zh": "chi_sim",
    "chi": "chi_sim", "ko": "kor", "ar": "ara", "he": "heb", "hr": "hrv", "sk": "slk", "slo": "slk",
    "sl": "slv", "sq": "sqi", "alb": "sqi", "fa": "fas", "per": "fas", "bg": "bul", "sr": "srp",
    "lt": "lit", "lv": "lav", "et": "est", "is": "isl", "ice": "isl", "ca": "cat", "eu": "eus",
    "baq": "eus", "cy": "cym", "wel": "cym", "ga": "gle", "hi": "hin", "vi": "vie", "th": "tha",
    "id": "ind", "ms": "msa", "may": "msa", "la": "lat", "lb": "ltz", "mk": "mkd", "mac": "mkd",
}


class LanguageError(ValueError):
    """An OCRMYPDF_LANGUAGES value that names no known model."""


@cache
def checksums() -> dict[str, str]:
    """Language code -> SHA-256 of its tessdata_best model."""
    sums = {}
    for line in (Path(__file__).with_name("tessdata_best.sha256")).read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            digest, code = line.split()
            sums[code] = digest
    return sums


def parse(spec: str) -> list[str]:
    """Split "deu+eng+fra" into codes, checking each against tessdata_best."""
    codes = []
    for raw in spec.split("+"):
        code = raw.strip().lower()
        if not code:
            raise LanguageError(f"OCRMYPDF_LANGUAGES has an empty entry: {spec!r}")
        if code not in checksums():
            hint = HINTS.get(code)
            message = f"OCRMYPDF_LANGUAGES: unknown language {raw.strip()!r}"
            if hint:
                message += f" (did you mean {hint!r}?)"
            else:
                message += " (Tesseract uses three-letter codes such as deu, eng, fra)"
            raise LanguageError(message)
        if code not in codes:
            codes.append(code)
    return codes


def system_dir() -> Path | None:
    """The folder Tesseract reads its built-in models from."""
    try:
        result = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'"([^"]+)"', result.stdout + result.stderr)
    return Path(match.group(1)) if match else None


@dataclass
class Prepared:
    languages: list[str]
    tessdata: Path | None = None  # set when Tesseract must read from the cache
    downloaded: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _link_builtins(system: Path, cache_dir: Path) -> None:
    """Make the built-in models, configs and font visible in the cache folder.

    Symlinks follow image updates; where the file system refuses them, the
    files are copied instead.
    """
    for entry in cache_dir.iterdir():
        if entry.is_symlink() and not entry.exists():
            entry.unlink()  # points into an older image
    for source in system.iterdir():
        target = cache_dir / source.name
        if target.is_symlink():
            if target.resolve() == source.resolve():
                continue
            target.unlink()
        elif target.exists():
            continue  # a downloaded model of the same name
        try:
            target.symlink_to(source)
        except FileExistsError:
            continue  # a `docker exec ... scanbutler` run linked it just now
        except OSError:
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)


def download(url: str, dest: Path, expected: str, timeout: float = 60.0) -> None:
    """Fetch url to dest, atomically, only if its SHA-256 matches."""
    # Not mkstemp: its files are always 0600, and the model should get the
    # permissions UMASK asks for, like every other file in the work folder.
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    try:
        digest = hashlib.sha256()
        with tmp.open("xb") as fh, httpx.stream(
            "GET", url, follow_redirects=True, timeout=httpx.Timeout(timeout, connect=10.0)
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(CHUNK):
                digest.update(chunk)
                fh.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("checksum mismatch")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _describe(exc: Exception) -> str:
    """The error without the URL: a mirror URL may carry credentials."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


def prepare(languages: list[str], cache_dir: Path, system: Path | None, base_url: str = DEFAULT_URL) -> Prepared:
    """Make every requested language available; leave out what can't be."""
    installed = {p.name[: -len(SUFFIX)] for p in system.glob(f"*{SUFFIX}")} if system else set()
    needed = [code for code in languages if code not in installed]
    if not needed:
        return Prepared(list(languages))

    result = Prepared([])
    cache_dir.mkdir(parents=True, exist_ok=True)
    if system:
        _link_builtins(system, cache_dir)
    for code in needed:
        path = cache_dir / f"{code}{SUFFIX}"
        expected = checksums()[code]
        if path.is_file() and not path.is_symlink() and _sha256(path) == expected:
            result.cached.append(code)
            continue
        url = f"{base_url.rstrip('/')}/{code}{SUFFIX}"
        for attempt in (1, 2):
            try:
                download(url, path, expected)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                if attempt == 2:
                    log.warning("language not available, left out", extra={"language": code, "error": _describe(exc)})
                    result.missing.append(code)
            else:
                result.downloaded.append(code)
                break

    result.languages = [code for code in languages if code not in result.missing]
    if not result.languages:
        result.languages = [code for code in FALLBACK if code in installed] or list(FALLBACK)
        log.warning("no requested language available, using the built-in ones", extra={"languages": "+".join(result.languages)})
    if any(code not in installed for code in result.languages):
        result.tessdata = cache_dir
    return result
