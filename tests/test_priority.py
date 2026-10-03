from __future__ import annotations

import shutil
import threading
import time


import httpx
import pikepdf

from stacksplit import pdfops, priority
from stacksplit.mistral import MistralClient
from stacksplit.notify import QueueReporter
from stacksplit.pdfops import JobBudget
from stacksplit.watcher import InboxWatcher

from .conftest import FakeBackend, make_pdf
from .test_text_source import make_text_pdf


def test_profiles_with_priority(settings):
    assert {p.name: p.priority for p in settings.profiles} == {"stacks": False, "scanner": True}


def test_a_run_gets_what_is_free_up_to_its_pages():
    budget = JobBudget(6)
    with budget.reserve(4) as stack:
        with budget.reserve(5, priority=True) as scan:
            assert (stack, scan) == (4, 2)  # all that was left
    with budget.reserve(2, priority=True) as small:
        assert small == 2  # a two-page scan doesn't block the other four


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_scans_go_ahead_of_waiting_stacks():
    budget = JobBudget(1)
    order = []

    def run(name, prio):
        with budget.reserve(1, priority=prio):
            order.append(name)

    with budget.reserve(1):  # the budget is busy
        stack = threading.Thread(target=run, args=("stack", False))
        stack.start()
        time.sleep(0.05)  # the stack is waiting first
        scan = threading.Thread(target=run, args=("scan", True))
        scan.start()
        wait_until(lambda: budget._priority_waiting == 1)
    stack.join(5)
    scan.join(5)
    assert order == ["scan", "stack"]


def copy_as_ocr(name, src, dst):
    """Stands in for ocrmypdf (not installed in CI): the piece comes back unchanged."""
    shutil.copyfile(src, dst)


def test_stack_text_layer_is_done_in_chunks(tmp_path, monkeypatch):
    src = tmp_path / "stack.pdf"
    make_text_pdf(src, [f"Page {n}" for n in range(1, 6)])
    seen = []

    def spy(name, src, dst, *args):
        with pikepdf.open(src) as pdf:
            seen.append((name, len(pdf.pages)))
        copy_as_ocr(name, src, dst)

    monkeypatch.setattr(pdfops, "_run_modes", spy)
    dst = tmp_path / "out.pdf"

    pdfops.make_searchable(src, dst, "eng", "", chunk_pages=2)

    assert seen == [("stack.pdf [pages 1-2]", 2), ("stack.pdf [pages 3-4]", 2), ("stack.pdf [pages 5-5]", 1)]
    assert [t.strip() for t in pdfops.page_texts(dst)] == [f"Page {n}" for n in range(1, 6)]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.pdf", "stack.pdf"]  # pieces cleaned up


def test_finished_chunks_survive_a_restart(tmp_path, monkeypatch):
    src = tmp_path / "stack.pdf"
    make_text_pdf(src, [f"Page {n}" for n in range(1, 5)])
    dst = tmp_path / "out.pdf"
    calls = []

    def fail_second(name, src, dst, *args):
        calls.append(name)
        if "pages 3-4" in name and len(calls) == 2:
            raise RuntimeError("container stopped")
        copy_as_ocr(name, src, dst)

    monkeypatch.setattr(pdfops, "_run_modes", fail_second)
    try:
        pdfops.make_searchable(src, dst, "eng", "", chunk_pages=2)
    except RuntimeError:
        pass
    pdfops.make_searchable(src, dst, "eng", "", chunk_pages=2)

    assert calls == ["stack.pdf [pages 1-2]", "stack.pdf [pages 3-4]", "stack.pdf [pages 3-4]"]
    assert len(pdfops.page_texts(dst)) == 4


def test_throttle_serves_priority_requests_first():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    c = MistralClient("key", "https://api.test/v1", "ocr", "llm", max_rps=10)
    c._http = httpx.Client(base_url="https://api.test/v1", transport=httpx.MockTransport(handler))
    c._next_slot = time.monotonic() + 0.3  # the throttle is busy for a moment

    def call(path, prio):
        with priority.marked(prio):
            c._request("GET", path)

    stacks = [threading.Thread(target=call, args=(f"/stack{n}", False)) for n in range(3)]
    for t in stacks:
        t.start()
    time.sleep(0.05)
    scan = threading.Thread(target=call, args=("/scan", True))
    scan.start()
    for t in [*stacks, scan]:
        t.join(5)

    assert seen[0] == "/v1/scan"


def test_priority_reaches_thread_pool_workers():
    from concurrent.futures import ThreadPoolExecutor

    with priority.marked(True), ThreadPoolExecutor(2) as pool:
        assert list(pool.map(priority.keep(lambda _: priority.current()), range(3))) == [True] * 3
        assert list(pool.map(lambda _: priority.current(), range(3))) == [False] * 3


def test_scanner_works_on_several_files_at_once(settings, monkeypatch):
    scanner = settings.profile("scanner")
    scanner.inbox.mkdir(parents=True)
    texts = ["dated report\nfirst"] * 2
    for n in range(4):
        make_pdf(scanner.inbox / f"scan-{n}.pdf", 2)

    running, peak, lock = [0], [0], threading.Lock()
    marks = []
    import stacksplit.watcher as watcher_module

    real = watcher_module.process_stack

    def slow(*args, **kwargs):
        marks.append(priority.current())
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        try:
            return real(*args, **kwargs)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(watcher_module, "process_stack", slow)
    reporter = QueueReporter("http://ha.test/hook", settings.profiles)
    InboxWatcher(settings, scanner, FakeBackend(texts), threading.Event(), reporter).poll_once()

    assert peak[0] > 1
    assert marks == [True] * 4
    assert sorted(p.name for p in scanner.archive.iterdir()) == [f"scan-{n}.pdf" for n in range(4)]
    # Same title for all four: each still got its own file name.
    assert len(list(scanner.output.glob("*.pdf"))) == 4
    assert reporter.snapshot()["processing"] == 0


def test_reporter_counts_several_files_in_progress(settings):
    scanner = settings.profile("scanner")
    scanner.inbox.mkdir(parents=True)
    for n in range(3):
        (scanner.inbox / f"{n}.pdf").write_bytes(b"%PDF")
    reporter = QueueReporter("http://ha.test/hook", settings.profiles)
    reporter.set_processing("scanner", True)
    reporter.set_processing("scanner", True)
    assert reporter.snapshot()["profiles"]["scanner"] == {"waiting": 1, "processing": 2, "failed": 0}
    reporter.set_processing("scanner", False)
    assert reporter.snapshot()["profiles"]["scanner"]["processing"] == 1




def test_a_cut_off_pdf_waits_until_it_is_complete(settings):
    stacks = settings.profile("stacks")
    stacks.inbox.mkdir(parents=True)
    whole = stacks.inbox.parent / "whole.pdf"
    make_pdf(whole, 3)
    data = whole.read_bytes()
    target = stacks.inbox / "scan.pdf"
    target.write_bytes(data[: len(data) // 2])  # the scanner paused mid-file
    watcher = InboxWatcher(settings, stacks, FakeBackend(["LETTER dated report"] * 3), threading.Event())

    watcher.poll_once()
    watcher.poll_once()
    assert target.exists() and not stacks.failed.exists()

    target.write_bytes(data)  # the scanner finished
    watcher.poll_once()
    assert not target.exists() and (stacks.archive / "scan.pdf").exists()


def test_a_file_that_changes_while_processing_stays_in_the_inbox(settings, monkeypatch):
    import stacksplit.watcher as watcher_module

    scanner = settings.profile("scanner")
    scanner.inbox.mkdir(parents=True)
    target = scanner.inbox / "scan.pdf"
    make_pdf(target, 2)

    def still_writing(path, *args, **kwargs):
        with path.open("ab") as handle:
            handle.write(b"\n% more pages\n%%EOF\n")
        raise RuntimeError("ocrmypdf failed in every mode. scan: exit code 2: InputFileError")

    monkeypatch.setattr(watcher_module, "process_stack", still_writing)
    InboxWatcher(settings, scanner, FakeBackend([]), threading.Event()).poll_once()

    assert target.exists()
    assert not scanner.failed.exists() or not any(scanner.failed.iterdir())
