from __future__ import annotations

import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from scanbutler import logging_setup, priority
from scanbutler.watcher import InboxWatcher

from .conftest import FakeBackend, make_pdf
from .test_pipeline import STACK

log = logging.getLogger("scanbutler.test")


@pytest.fixture
def lines():
    """The JSON lines the real formatter writes, as dicts."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_setup.JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def test_lines_carry_the_file_being_worked_on(lines):
    log.info("before")
    with logging_setup.working_on("Household/scan.pdf"):
        log.info("inside")
        log.info("explicit", extra={"source": "other.pdf"})
    log.info("after")

    by_event = {line["event"]: line for line in lines()}
    assert "source" not in by_event["before"] and "source" not in by_event["after"]
    assert by_event["inside"]["source"] == "Household/scan.pdf"
    assert by_event["explicit"]["source"] == "other.pdf"  # an explicit value wins


def test_pool_tasks_keep_the_source_and_the_priority(lines):
    def task(n):
        log.info("task", extra={"n": n})
        return priority.current()

    with logging_setup.working_on("a.pdf"), priority.marked(True):
        # One wrapped function, many threads at once: each call gets its own context copy.
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(pool.map(priority.keep(task), range(40)))

    tasks = [line for line in lines() if line["event"] == "task"]
    assert len(tasks) == 40 and {line["source"] for line in tasks} == {"a.pdf"}


def test_two_files_at_once_are_not_mixed_up(lines):
    barrier = threading.Barrier(2)

    def work(name):
        with logging_setup.working_on(name):
            barrier.wait()  # both are inside their block at the same time
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(priority.keep(lambda i: log.info("step", extra={"for": name})), range(5)))

    threads = [threading.Thread(target=work, args=(n,)) for n in ("one.pdf", "two.pdf")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    steps = [line for line in lines() if line["event"] == "step"]
    assert len(steps) == 10 and all(line["source"] == line["for"] for line in steps)


class LoggingBackend(FakeBackend):
    """Logs from inside its calls, as the Mistral client does ("api retry", "batch waiting")."""

    def chat_json(self, system, user, schema, name):
        log.info("backend call", extra={"call": name, "worker": threading.current_thread().name})
        return super().chat_json(system, user, schema, name)


def test_a_watched_stack_tags_every_line_of_its_run(settings, lines):
    profile = settings.profile("stacks")
    (profile.inbox / "Household").mkdir(parents=True)
    make_pdf(profile.inbox / "Household" / "stack.pdf", len(STACK))
    watcher = InboxWatcher(settings, profile, LoggingBackend(STACK), threading.Event())

    watcher.poll_once()

    run = [line for line in lines() if line["event"] != "watching inbox"]
    calls = [line for line in run if line["event"] == "backend call"]
    # Boundary questions and naming run in thread pools, not in the watcher's thread.
    assert calls and all(line["worker"] != threading.current_thread().name for line in calls)
    assert {"stack started", "naming documents", "stack done"} <= {line["event"] for line in run}
    untagged = [line["event"] for line in run if line.get("source") != "Household/stack.pdf"]
    assert untagged == []
