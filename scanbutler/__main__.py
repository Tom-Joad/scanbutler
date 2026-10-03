"""Command line entry point: `scanbutler run|process|rebuild`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path, PurePosixPath

from . import __version__, logging_setup, pdfops
from .config import ConfigError, Settings, available_cpus, memory_limit_bytes
from .mistral import MistralClient
from .paperless import PaperlessClient
from .pipeline import process_for_paperless, process_stack, rebuild
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
    )


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
        choices=["stacks", "scanner", "paperless"],
        help="stacks: split; scanner: one document per file; paperless: text layer, then upload",
    )
    again = sub.add_parser("rebuild", help="re-cut a stack from its edited plan.json")
    again.add_argument("work_dir", help="the stack's work directory, absolute or relative to WORK_DIR")
    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        settings = Settings.from_env(require_api_key=command != "rebuild")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
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
        },
    )

    # ocrmypdf renders every page into the temp dir. TMPDIR may point into a
    # mounted volume (to keep gigabytes of page images off RAM-backed /tmp),
    # and that directory has to exist before the first tempfile is created.
    if tmpdir := os.environ.get("TMPDIR"):
        Path(tmpdir).mkdir(parents=True, exist_ok=True)

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
            profile = settings.profile(args.profile)
            if profile.upload:
                paperless = PaperlessClient(settings.paperless_url, settings.paperless_token)
                try:
                    document = process_for_paperless(
                        args.pdf, PurePosixPath(args.folder), settings, paperless, profile, client
                    )
                finally:
                    paperless.close()
                print(f"Paperless document {document}")
            else:
                profile.output.mkdir(parents=True, exist_ok=True)
                process_stack(args.pdf, PurePosixPath(args.folder), settings, client, profile)
        else:
            run_all(settings, client)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
