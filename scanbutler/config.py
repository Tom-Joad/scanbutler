"""Runtime configuration, read exclusively from environment variables.

Nothing environment-specific lives in the repository: the API key, paths and
tuning knobs all come from the container's environment (see .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .languages import DEFAULT_URL as TESSDATA_DEFAULT_URL
from .languages import LanguageError, parse
from .pdfops import OcrLimits


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


# Measured: four A4 pages with 1550-dpi colour images, downsampled to 600 dpi
# and OCR'd with 4 jobs, peaked at 1.64 GB. Budget with headroom per parallel
# page, plus a base for the app itself, Ghostscript and uploads.
JOB_MEMORY_GB = 0.75
BASE_MEMORY_GB = 1.0


def memory_limit_bytes() -> int | None:
    """The container's memory limit (cgroup v2/v1), else the machine's total memory."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:  # cgroup v1 reports "no limit" as a huge number
            return int(raw)
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


CONFIG_DIR = Path("/config")


def default_work_dir(data: Path) -> Path:
    """The work folder when WORK_DIR is not set: /config, as in every
    linuxserver.io container. Outside the container (tests), data/work."""
    return CONFIG_DIR if CONFIG_DIR.is_dir() else data / "work"


def available_cpus() -> int:
    """CPUs this container may use: its CPU set and any --cpus quota (cgroup v2/v1).

    os.cpu_count() reports every core of the host, even with --cpus=2.
    """
    try:
        cpus = len(os.sched_getaffinity(0))  # honours --cpuset-cpus
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    quota = None
    try:
        limit, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if limit != "max":
            quota = int(limit) / int(period)
    except (OSError, ValueError):
        try:
            limit = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if limit > 0:
                quota = limit / period
        except (OSError, ValueError):
            pass
    if quota is not None:
        # --cpus=1.5 allows one and a half cores' worth of time: one job each.
        cpus = min(cpus, max(1, int(quota)))
    return max(1, cpus)


def auto_jobs(memory_bytes: int | None, cpus: int) -> int:
    """Parallel OCR pages that fit the memory: less memory just means slower."""
    if memory_bytes is None:
        return max(1, min(cpus, 4))
    fitting = int((memory_bytes / 2**30 - BASE_MEMORY_GB) / JOB_MEMORY_GB)
    return max(1, min(cpus, fitting))


@dataclass(frozen=True)
class PaperlessTarget:
    """Where a Paperless input uploads to, and as which user."""

    url: str
    token: str = field(repr=False)
    tags: tuple[int, ...]
    # The setting that holds the token, for error messages.
    token_setting: str = "PAPERLESS_TOKEN"


@dataclass(frozen=True)
class Profile:
    """One input channel with its own folders.

    `stacks` takes large scans holding many documents and splits them;
    `scanner` takes what a document scanner drops off, one document per file.
    """

    name: str
    root: Path
    split: bool
    # Where the text for splitting and naming comes from: "mistral" (Mistral
    # OCR, paid) or "tesseract" (the text layer ocrmypdf adds anyway, free).
    text_source: str = "mistral"
    # Hand the result to Paperless-ngx instead of writing it to output/.
    paperless: PaperlessTarget | None = None

    @property
    def upload(self) -> bool:
        return self.paperless is not None

    @property
    def env_prefix(self) -> str:
        """Prefix of this input's settings: "paperless-2" -> "PAPERLESS_2"."""
        return self.name.upper().replace("-", "_")

    @property
    def priority(self) -> bool:
        """Single documents someone is waiting for go ahead of stacks."""
        return not self.split

    @property
    def uses_mistral(self) -> bool:
        """Whether files of this input call Mistral (and so obey its pause)."""
        # Stacks and scanner always ask the chat model for names; the
        # Paperless input only calls Mistral when it fetches Mistral OCR.
        return not self.upload or self.text_source == "mistral"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def failed(self) -> Path:
        return self.root / "failed"


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_base: str
    ocr_model: str
    llm_model: str
    request_timeout: float
    max_rps: float

    profiles: tuple[Profile, ...]
    work_dir: Path

    poll_interval: int
    stable_seconds: int

    ocr_chunk_pages: int
    ocr_mode: str
    batch_poll_seconds: int
    batch_max_wait_hours: float
    ocr_concurrency: int
    llm_concurrency: int

    boundary_window: int
    boundary_step: int
    review_confidence: float
    drop_blank_pages: bool
    blank_max_chars: int
    blank_max_ink: float
    metadata_max_chars: int

    title_language: str
    filename_pattern: str
    no_date_label: str

    ocrmypdf_enabled: bool
    ocrmypdf_languages: str
    tessdata_url: str
    ocrmypdf_jobs: int
    ocrmypdf_extra_args: str
    ocr_limits: OcrLimits

    pause_retry_minutes: float
    paperless_url: str
    paperless_token: str
    paperless_max_wait_minutes: float
    paperless_share_tags: bool
    paperless_share_correspondents: bool
    paperless_share_document_types: bool
    paperless_share_tags_readonly: tuple[str, ...]
    paperless_share_tags_minutes: float
    queue_webhook_url: str
    queue_webhook_check_seconds: int
    queue_webhook_heartbeat_seconds: int
    work_retention_days: float

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

        ocr_mode = _str("OCR_MODE", "batch").lower()
        if ocr_mode not in {"batch", "direct"}:
            raise ConfigError(f"OCR_MODE must be batch or direct, got {ocr_mode!r}")

        try:
            languages = "+".join(parse(_str("OCRMYPDF_LANGUAGES", "deu+eng")))
        except LanguageError as exc:
            raise ConfigError(str(exc)) from exc

        data = Path(_str("DATA_DIR", "/data"))
        # Splitting profits from Mistral OCR's structure; naming a single
        # scanner file showed no measurable difference (README > Choosing the
        # text source), so scanner files skip the paid OCR by default. Without
        # ocrmypdf there is no Tesseract layer to read, so fall back to Mistral.
        scanner_default = "tesseract" if _bool("OCRMYPDF_ENABLED", True) else "mistral"
        profiles = [
            Profile(
                name,
                Path(_str(f"{name.upper()}_DIR", str(data / name))),
                split,
                _str(f"{name.upper()}_TEXT_SOURCE", default_source).lower(),
            )
            for name, split, default_source in (("stacks", True, "mistral"), ("scanner", False, scanner_default))
            if _bool(f"{name.upper()}_ENABLED", True)
        ]
        paperless_url = os.environ.get("PAPERLESS_URL", "").strip()
        paperless_token = os.environ.get("PAPERLESS_TOKEN", "").strip()
        if paperless_url and not paperless_token:
            raise ConfigError("PAPERLESS_URL is set but PAPERLESS_TOKEN is not")
        # The second input uploads with its own token, so its documents
        # belong to another Paperless user; by default on the same instance.
        second_token = os.environ.get("PAPERLESS_2_TOKEN", "").strip()
        second_url = _str("PAPERLESS_2_URL", paperless_url)
        if os.environ.get("PAPERLESS_2_URL", "").strip() and not second_token:
            raise ConfigError("PAPERLESS_2_URL is set but PAPERLESS_2_TOKEN is not")
        if second_token and not second_url:
            raise ConfigError("PAPERLESS_2_TOKEN needs PAPERLESS_URL or PAPERLESS_2_URL")
        for name, url, token in (("paperless", paperless_url, paperless_token), ("paperless-2", second_url, second_token)):
            if not (url and token):
                continue
            prefix = name.upper().replace("-", "_")
            try:
                tags = tuple(int(t) for t in os.environ.get(f"{prefix}_TAGS", "").replace(" ", "").split(",") if t)
            except ValueError as exc:
                raise ConfigError(f"{prefix}_TAGS must be comma-separated tag ids, e.g. 3,7") from exc
            # Paperless (and an AI tagger behind it) does the naming and
            # tagging; this input only adds the Tesseract text layer.
            profiles.append(
                Profile(
                    name,
                    Path(_str(f"{prefix}_DIR", str(data / name))),
                    False,
                    # mistral: Mistral OCR's text (with tables) replaces the
                    # document's content in Paperless after upload.
                    _str(f"{prefix}_TEXT_SOURCE", "tesseract").lower(),
                    PaperlessTarget(url, token, tags, f"{prefix}_TOKEN"),
                )
            )
        share_tags = _bool("PAPERLESS_SHARE_TAGS", False)
        share_correspondents = _bool("PAPERLESS_SHARE_CORRESPONDENTS", False)
        share_document_types = _bool("PAPERLESS_SHARE_DOCUMENT_TYPES", False)
        for name, on in (
            ("PAPERLESS_SHARE_TAGS", share_tags),
            ("PAPERLESS_SHARE_CORRESPONDENTS", share_correspondents),
            ("PAPERLESS_SHARE_DOCUMENT_TYPES", share_document_types),
        ):
            if on and not paperless_url:
                raise ConfigError(f"{name} needs PAPERLESS_URL and PAPERLESS_TOKEN")
        share_minutes = _float("PAPERLESS_SHARE_TAGS_MINUTES", 1.0)
        if share_minutes <= 0:
            raise ConfigError(f"PAPERLESS_SHARE_TAGS_MINUTES must be > 0, got {share_minutes}")
        for profile in profiles:
            if profile.text_source not in {"mistral", "tesseract"}:
                raise ConfigError(
                    f"{profile.env_prefix}_TEXT_SOURCE must be mistral or tesseract, got {profile.text_source!r}"
                )
            if profile.text_source == "tesseract" and not profile.upload and not _bool("OCRMYPDF_ENABLED", True):
                raise ConfigError(f"{profile.env_prefix}_TEXT_SOURCE=tesseract needs OCRMYPDF_ENABLED=true")
        if not profiles:
            raise ConfigError("STACKS_ENABLED and SCANNER_ENABLED are both off")
        return cls(
            api_key=api_key,
            api_base=_str("MISTRAL_API_BASE", "https://api.mistral.ai/v1").rstrip("/"),
            ocr_model=_str("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
            llm_model=_str("MISTRAL_LLM_MODEL", "mistral-large-latest"),
            request_timeout=_float("MISTRAL_TIMEOUT", 300.0),
            max_rps=_float("MISTRAL_MAX_RPS", 1.0),
            profiles=tuple(profiles),
            work_dir=Path(os.environ["WORK_DIR"]) if os.environ.get("WORK_DIR", "").strip() else default_work_dir(data),
            poll_interval=_int("POLL_INTERVAL", 30, minimum=1),
            stable_seconds=_int("STABLE_SECONDS", 60, minimum=0),
            ocr_chunk_pages=_int("OCR_CHUNK_PAGES", 50, minimum=1),
            ocr_mode=ocr_mode,
            batch_poll_seconds=_int("BATCH_POLL_SECONDS", 15, minimum=1),
            batch_max_wait_hours=_float("BATCH_MAX_WAIT_HOURS", 24.0),
            ocr_concurrency=_int("OCR_CONCURRENCY", 3, minimum=1),
            llm_concurrency=_int("LLM_CONCURRENCY", 4, minimum=1),
            boundary_window=window,
            boundary_step=step,
            review_confidence=_float("REVIEW_CONFIDENCE", 0.75),
            drop_blank_pages=_bool("DROP_BLANK_PAGES", True),
            blank_max_chars=_int("BLANK_MAX_CHARS", 15),
            # Percent of the page area: blank scans measure 0-0.2 %, a sparse text page about 1 %.
            blank_max_ink=_float("BLANK_MAX_INK_PERCENT", 0.2) / 100,
            metadata_max_chars=_int("METADATA_MAX_CHARS", 24000, minimum=1000),
            title_language=_str("TITLE_LANGUAGE", "the language of the document"),
            filename_pattern=pattern,
            no_date_label=_str("NO_DATE_LABEL", "undated"),
            ocrmypdf_enabled=_bool("OCRMYPDF_ENABLED", True),
            ocrmypdf_languages=languages,
            tessdata_url=_str("TESSDATA_URL", TESSDATA_DEFAULT_URL),
            ocrmypdf_jobs=(
                auto_jobs(memory_limit_bytes(), available_cpus())
                if _str("OCRMYPDF_JOBS", "auto").lower() == "auto"
                else _int("OCRMYPDF_JOBS", 1, minimum=1)
            ),
            ocrmypdf_extra_args=os.environ.get("OCRMYPDF_EXTRA_ARGS", "").strip(),
            ocr_limits=OcrLimits(
                max_ocr_mpixels=_int("OCRMYPDF_MAX_OCR_MPIXELS", 50, minimum=1),
                page_timeout=_int("OCRMYPDF_PAGE_TIMEOUT", 300, minimum=10),
                file_timeout_minutes=_int("OCRMYPDF_FILE_TIMEOUT_MINUTES", 120, minimum=1),
                skip_big_mpixels=_int("OCRMYPDF_SKIP_BIG_MPIXELS", 200, minimum=1),
                max_image_dpi=_int("OCRMYPDF_MAX_IMAGE_DPI", 600, minimum=0),
            ),
            pause_retry_minutes=_float("PAUSE_RETRY_MINUTES", 30.0),
            paperless_url=paperless_url,
            paperless_token=paperless_token,
            paperless_max_wait_minutes=_float("PAPERLESS_MAX_WAIT_MINUTES", 30.0),
            paperless_share_tags=share_tags,
            paperless_share_correspondents=share_correspondents,
            paperless_share_document_types=share_document_types,
            paperless_share_tags_readonly=tuple(
                name.strip() for name in os.environ.get("PAPERLESS_SHARE_TAGS_READONLY", "").split(",") if name.strip()
            ),
            paperless_share_tags_minutes=share_minutes,
            queue_webhook_url=os.environ.get("QUEUE_WEBHOOK_URL", "").strip(),
            queue_webhook_check_seconds=_int("QUEUE_WEBHOOK_CHECK_SECONDS", 10, minimum=1),
            queue_webhook_heartbeat_seconds=_int("QUEUE_WEBHOOK_HEARTBEAT_SECONDS", 300, minimum=10),
            work_retention_days=max(0.0, _float("WORK_RETENTION_DAYS", 30.0)),
            log_level=_str("LOG_LEVEL", "INFO"),
        )

    @property
    def paperless_share_kinds(self) -> tuple[str, ...]:
        """The Paperless object kinds to keep ownerless, as API names."""
        return tuple(
            kind
            for kind, on in (
                ("tags", self.paperless_share_tags),
                ("correspondents", self.paperless_share_correspondents),
                ("document_types", self.paperless_share_document_types),
            )
            if on
        )

    def profile(self, name: str) -> Profile:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        raise ConfigError(f"profile {name!r} is not enabled")

