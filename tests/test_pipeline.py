from __future__ import annotations

import json
from pathlib import PurePosixPath

import pikepdf

from stacksplit.pipeline import PLAN, REVIEW, process_stack, rebuild
from stacksplit.watcher import InboxWatcher

from .conftest import FakeBackend, make_pdf

STACK = [
    "LETTER Blutbild dated\nLeukozyten 6,2",
    "Erythrozyten 4,8\nSeite 2 von 2",
    "",  # duplex back side
    "LETTER Befundbericht CT\nThorax ohne Befund",
    "Beurteilung: unauffaellig unsure",
    "LETTER Rechnung\nBetrag 42 EUR fuer Laborleistungen nach GOAE, zahlbar innerhalb von 30 Tagen",
    "",
]


def titles(paths):
    return sorted(p.name for p in paths)


def test_process_splits_names_and_drops_blanks(settings, tmp_path):
    src = tmp_path / "stack.pdf"
    make_pdf(src, len(STACK))
    backend = FakeBackend(STACK)

    work = process_stack(src, PurePosixPath("Patient A"), settings, backend)

    out = settings.output_dir / "Patient A"
    assert titles(out.iterdir()) == [
        "Befundbericht CT undated.pdf",
        "Blutbild 2026-09-30.pdf",
        "Rechnung undated.pdf",
    ]
    with pikepdf.open(out / "Blutbild 2026-09-30.pdf") as pdf:
        assert len(pdf.pages) == 2
        assert str(pdf.docinfo["/Title"]) == "Blutbild"

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    assert [d["pages"] for d in plan["documents"]] == ["1-2", "4-5", "6"]
    assert plan["dropped_pages"] == "3, 7"
    ct = plan["documents"][1]
    assert ct["needs_review"] and "page 5" in ct["review_reason"]
    assert "1 flagged" in (work / REVIEW).read_text(encoding="utf-8")


def test_rerun_uses_caches_and_replaces_outputs(settings, tmp_path):
    src = tmp_path / "stack.pdf"
    make_pdf(src, len(STACK))
    backend = FakeBackend(STACK)
    process_stack(src, PurePosixPath(""), settings, backend)
    calls = (backend.ocr_calls, len(backend.chat_calls))

    process_stack(src, PurePosixPath(""), settings, backend)

    assert (backend.ocr_calls, len(backend.chat_calls)) == calls
    assert len(list(settings.output_dir.glob("*.pdf"))) == 3


def test_rebuild_from_edited_plan_retitles_cleared_entries(settings, tmp_path):
    src = tmp_path / "stack.pdf"
    make_pdf(src, len(STACK))
    backend = FakeBackend(STACK)
    work = process_stack(src, PurePosixPath(""), settings, backend)

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    # Merge the CT report and the invoice, and have the result re-titled.
    ct, invoice = plan["documents"][1], plan["documents"][2]
    ct["pages"], ct["title"] = "4-6", ""
    plan["documents"].remove(invoice)
    (work / PLAN).write_text(json.dumps(plan), encoding="utf-8")

    written = rebuild(work, settings, backend)

    assert titles(written) == ["Befundbericht CT undated.pdf", "Blutbild 2026-09-30.pdf"]
    assert titles(settings.output_dir.glob("*.pdf")) == titles(written)
    with pikepdf.open(settings.output_dir / "Befundbericht CT undated.pdf") as pdf:
        assert len(pdf.pages) == 3


def test_watcher_archives_success_and_quarantines_failure(settings):
    folder = settings.inbox_dir / "Patient A"
    folder.mkdir(parents=True)
    make_pdf(folder / "good.pdf", len(STACK))
    (folder / "broken.pdf").write_bytes(b"not a pdf")

    watcher = InboxWatcher(settings, FakeBackend(STACK))
    for path in watcher._candidates():
        assert watcher._ready(path, 0.0)
        watcher.process(path)

    assert (settings.archive_dir / "Patient A" / "good.pdf").exists()
    assert (settings.failed_dir / "Patient A" / "broken.pdf").exists()
    assert (settings.failed_dir / "Patient A" / "broken.pdf.error.txt").exists()
    assert not any(folder.iterdir())
