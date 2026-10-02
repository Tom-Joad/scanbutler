"""Command line entry point: `stacksplit run|process|rebuild`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path, PurePosixPath

from . import logging_setup
from .config import ConfigError, Settings
from .mistral import MistralClient
from .pipeline import process_stack, rebuild
from .watcher import InboxWatcher

log = logging.getLogger("stacksplit")


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
    parser = argparse.ArgumentParser(prog="stacksplit", description="Split scanned PDF stacks into named, searchable documents.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="watch the inbox and process new stacks (default)")
    one = sub.add_parser("process", help="process a single PDF without moving it")
    one.add_argument("pdf", type=Path)
    one.add_argument("--folder", default="", help="output sub-folder (default: none)")
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
            process_stack(args.pdf, PurePosixPath(args.folder), settings, client)
        else:
            InboxWatcher(settings, client).run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
