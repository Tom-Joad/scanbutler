"""Command line entry point: `scanbutler run|process|rebuild`."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
import tempfile
from pathlib import Path, PurePosixPath

from . import __version__, languages, logging_setup, pdfops
from .config import ConfigError, Settings, available_cpus, default_work_dir, memory_limit_bytes
from .mistral import MistralClient
from .paperless import client_for
from .pipeline import process_for_paperless, process_stack, rebuild
from .tokens import TokenError, TokenStore
from .watcher import run_all

log = logging.getLogger("scanbutler")


def _client(settings: Settings) -> MistralClient:
    return MistralClient(
        settings.api_key,
        settings.api_base,
        settings.ocr_model,
        settings.llm_model,
        settings.request_timeout,
        max_rps=settings.max_rps,
        reasoning_effort=settings.reasoning_effort,
        rate_file=rate_file(settings) if settings.api_enabled else None,
    )


def rate_file(settings: Settings) -> Path:
    """The request slots shared with the API's OCR processes."""
    return settings.work_dir / "api" / "mistral.rate"


def prepare_tmpdir(work_dir: Path) -> None:
    """Point TMPDIR at the work folder unless it is set.

    ocrmypdf renders every page into the temp dir: several GB for a large
    stack. Inside the work folder they stay on disk instead of a RAM-backed
    /tmp, and the folder has to exist before the first tempfile is created.
    """
    wanted = os.environ.get("TMPDIR") or str(work_dir / "tmp")
    try:
        Path(wanted).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fallback = work_dir / "tmp"
        log.warning("TMPDIR not usable, using the work folder", extra={"tmpdir": wanted, "error": str(exc)})
        fallback.mkdir(parents=True, exist_ok=True)
        wanted = str(fallback)
    os.environ["TMPDIR"] = wanted
    tempfile.tempdir = None  # re-read TMPDIR on next use


def prepare_languages(settings: Settings) -> Settings:
    """Download languages that aren't built in; point Tesseract at them.

    Returns the settings with the languages that are actually available.
    """
    wanted = settings.ocrmypdf_languages.split("+")
    ready = languages.prepare(wanted, settings.work_dir / "tessdata", languages.system_dir(), settings.tessdata_url)
    if ready.tessdata:
        os.environ["TESSDATA_PREFIX"] = str(ready.tessdata)
    log.info(
        "languages ready",
        extra={
            "languages": "+".join(ready.languages),
            "downloaded": ready.downloaded,
            "cached": ready.cached,
            "missing": ready.missing,
        },
    )
    return dataclasses.replace(settings, ocrmypdf_languages="+".join(ready.languages))


def _process_one(args: argparse.Namespace, settings: Settings, client: MistralClient) -> None:
    profile = settings.profile(args.profile)
    if profile.upload:
        paperless = client_for(profile.paperless)
        try:
            document = process_for_paperless(args.pdf, PurePosixPath(args.folder), settings, paperless, profile, client)
        finally:
            paperless.close()
        print(f"Paperless document {document}")
    else:
        profile.output.mkdir(parents=True, exist_ok=True)
        process_stack(args.pdf, PurePosixPath(args.folder), settings, client, profile)


def _token(args: argparse.Namespace) -> int:
    # Only WORK_DIR matters here; the other settings may be incomplete.
    work = Path(os.environ["WORK_DIR"]) if os.environ.get("WORK_DIR", "").strip() else default_work_dir(
        Path(os.environ.get("DATA_DIR", "/data").strip() or "/data")
    )
    store = TokenStore(work / "api" / "tokens.json")
    try:
        if args.action == "add":
            print(store.add(args.name))
            print(f"Token for {args.name!r}; it is not shown again.", file=sys.stderr)
        elif args.action == "remove":
            store.remove(args.name)
            print(f"Token {args.name!r} removed.", file=sys.stderr)
        else:
            for known in store.tokens():
                print(f"{known.name}\t{known.created_at}")
    except TokenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scanbutler", description="Turn scanned paper into named, searchable PDFs.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="watch the inboxes of all enabled profiles (default)")
    one = sub.add_parser("process", help="process a single PDF without moving it")
    one.add_argument("pdf", type=Path)
    one.add_argument("--folder", default="", help="output sub-folder (default: none)")
    one.add_argument(
        "--profile",
        default="stacks",
        choices=["stacks", "scanner", "paperless", "paperless-2"],
        help="stacks: split; scanner: one document per file; paperless: text layer, then upload",
    )
    again = sub.add_parser("rebuild", help="re-cut a stack from its edited plan.json")
    again.add_argument("work_dir", help="the stack's work directory, absolute or relative to WORK_DIR")
    sub.add_parser("check-config", help="check the settings and exit; used by the container's init step")
    token = sub.add_parser("token", help="manage API tokens")
    token_sub = token.add_subparsers(dest="action", required=True)
    token_sub.add_parser("add", help="create a token for a program; it is shown once").add_argument("name")
    token_sub.add_parser("list", help="list the tokens' names")
    token_sub.add_parser("remove", help="revoke a token").add_argument("name")
    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "token":
        return _token(args)
    if command == "check-config":
        try:
            Settings.from_env()
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0

    try:
        settings = Settings.from_env(require_api_key=command != "rebuild")
    except ConfigError as exc:
        # A JSON line like every other; LOG_LEVEL itself may be what's wrong.
        logging_setup.configure("INFO")
        log.error("config_invalid", extra={"reason": str(exc)})
        return 2
    logging_setup.configure(settings.log_level)
    pdfops.configure_jobs(settings.ocrmypdf_jobs)
    memory = memory_limit_bytes()
    log.info(
        "starting",
        extra={
            "version": __version__,
            "command": command,
            "memory_gb": round(memory / 2**30, 1) if memory else None,
            "cpus": available_cpus(),
            "ocr_jobs_total": settings.ocrmypdf_jobs,
            "ocr_priority": [p.name for p in settings.profiles if p.priority],
            "work_retention_days": settings.work_retention_days,
            "paperless_share": list(settings.paperless_share_kinds),
            "api": settings.api_port if settings.api_enabled else False,
        },
    )

    prepare_tmpdir(settings.work_dir)
    if settings.ocrmypdf_enabled and command != "rebuild":
        settings = prepare_languages(settings)

    if command == "rebuild":
        work = Path(args.work_dir)
        if not work.is_absolute():
            work = settings.work_dir / work
        backend = _client(settings) if settings.api_key else None
        files = rebuild(work, settings, backend)
        for path in files:
            print(path)
        return 0

    client = _client(settings)
    try:
        if command == "process":
            with logging_setup.working_on((PurePosixPath(args.folder) / args.pdf.name).as_posix()):
                _process_one(args, settings, client)
        else:
            run_all(settings, client)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
