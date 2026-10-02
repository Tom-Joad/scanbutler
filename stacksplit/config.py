"""Runtime configuration, read exclusively from environment variables.

Nothing environment-specific lives in the repository: the API key, paths and
tuning knobs all come from the container's environment (see .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_base: str
    ocr_model: str
    llm_model: str
    request_timeout: float

    inbox_dir: Path
    output_dir: Path
    work_dir: Path
    archive_dir: Path
    failed_dir: Path

    poll_interval: int
    stable_seconds: int

    ocr_chunk_pages: int
    ocr_concurrency: int
    llm_concurrency: int

    boundary_window: int
    boundary_step: int
    review_confidence: float
    drop_blank_pages: bool
    blank_max_chars: int
    metadata_max_chars: int

    title_language: str
    filename_pattern: str
    no_date_label: str

    ocrmypdf_enabled: bool
    ocrmypdf_languages: str
    ocrmypdf_jobs: int
    ocrmypdf_extra_args: str

    log_level: str

    @classmethod
    def from_env(cls, require_api_key: bool = True) -> "Settings":
        api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
        if require_api_key and not api_key:
            raise ConfigError("MISTRAL_API_KEY is not set")

        window = _int("BOUNDARY_WINDOW", 12, minimum=3)
        step = _int("BOUNDARY_STEP", max(1, window // 2), minimum=1)
        if step >= window:
            raise ConfigError("BOUNDARY_STEP must be smaller than BOUNDARY_WINDOW")

        pattern = _str("FILENAME_PATTERN", "{title} {date}")
        if "{title}" not in pattern:
            raise ConfigError("FILENAME_PATTERN must contain {title}")

        data = Path(_str("DATA_DIR", "/data"))
        return cls(
            api_key=api_key,
            api_base=_str("MISTRAL_API_BASE", "https://api.mistral.ai/v1").rstrip("/"),
            ocr_model=_str("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
            llm_model=_str("MISTRAL_LLM_MODEL", "mistral-medium-latest"),
            request_timeout=_float("MISTRAL_TIMEOUT", 300.0),
            inbox_dir=Path(_str("INBOX_DIR", str(data / "inbox"))),
            output_dir=Path(_str("OUTPUT_DIR", str(data / "output"))),
            work_dir=Path(_str("WORK_DIR", str(data / "work"))),
            archive_dir=Path(_str("ARCHIVE_DIR", str(data / "archive"))),
            failed_dir=Path(_str("FAILED_DIR", str(data / "failed"))),
            poll_interval=_int("POLL_INTERVAL", 30, minimum=1),
            stable_seconds=_int("STABLE_SECONDS", 60, minimum=0),
            ocr_chunk_pages=_int("OCR_CHUNK_PAGES", 50, minimum=1),
            ocr_concurrency=_int("OCR_CONCURRENCY", 3, minimum=1),
            llm_concurrency=_int("LLM_CONCURRENCY", 4, minimum=1),
            boundary_window=window,
            boundary_step=step,
            review_confidence=_float("REVIEW_CONFIDENCE", 0.75),
            drop_blank_pages=_bool("DROP_BLANK_PAGES", True),
            blank_max_chars=_int("BLANK_MAX_CHARS", 15),
            metadata_max_chars=_int("METADATA_MAX_CHARS", 24000, minimum=1000),
            title_language=_str("TITLE_LANGUAGE", "the language of the document"),
            filename_pattern=pattern,
            no_date_label=_str("NO_DATE_LABEL", "undated"),
            ocrmypdf_enabled=_bool("OCRMYPDF_ENABLED", True),
            ocrmypdf_languages=_str("OCRMYPDF_LANGUAGES", "deu+eng"),
            ocrmypdf_jobs=_int("OCRMYPDF_JOBS", os.cpu_count() or 1, minimum=1),
            ocrmypdf_extra_args=os.environ.get("OCRMYPDF_EXTRA_ARGS", "").strip(),
            log_level=_str("LOG_LEVEL", "INFO"),
        )
