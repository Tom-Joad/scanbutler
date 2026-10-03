from __future__ import annotations

import json
import threading
from pathlib import PurePosixPath

import pikepdf
import pytest

from scanbutler.pipeline import PLAN, REVIEW, process_stack, rebuild
from scanbutler.watcher import InboxWatcher

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
    stacks = settings.profile("stacks")
    src = tmp_path / "stack.pdf"
    make_pdf(src, len(STACK))
    backend = FakeBackend(STACK)

    work = process_stack(src, PurePosixPath("Patient A"), settings, backend, stacks)

    out = stacks.output / "Patient A"
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


def test_scanner_profile_keeps_one_document_per_file(settings, tmp_path):
    scanner = settings.profile("scanner")
    src = tmp_path / "scan.pdf"
    make_pdf(src, len(STACK))
    backend = FakeBackend(STACK)

    work = process_stack(src, PurePosixPath(""), settings, backend, scanner)

    # No splitting: one file holding every non-blank page, named after the first.
    assert titles(scanner.output.iterdir()) == ["Blutbild 2026-09-30.pdf"]
    with pikepdf.open(scanner.output / "Blutbild 2026-09-30.pdf") as pdf:
        assert len(pdf.pages) == 5
    assert "page_boundaries" not in backend.chat_calls
    assert json.loads((work / PLAN).read_text(encoding="utf-8"))["profile"] == "scanner"
    assert work.relative_to(settings.work_dir).parts[0] == "scanner"
    assert not any(settings.profile("stacks").output.glob("*.pdf"))


def test_rerun_uses_caches_and_replaces_outputs(settings, tmp_path):
    stacks = settings.profile("stacks")
    src = tmp_path / "stack.pdf"
    make_pdf(src, len(STACK))
    backend = FakeBackend(STACK)
    process_stack(src, PurePosixPath(""), settings, backend, stacks)
    calls = (len(backend.jobs), backend.ocr_calls, len(backend.chat_calls))

    process_stack(src, PurePosixPath(""), settings, backend, stacks)

    assert (len(backend.jobs), backend.ocr_calls, len(backend.chat_calls)) == calls
    assert len(list(stacks.output.glob("*.pdf"))) == 3


def test_rebuild_from_edited_plan_retitles_cleared_entries(settings, tmp_path):
    stacks = settings.profile("stacks")
    src = tmp_path / "stack.pdf"
    make_pdf(src, len(STACK))
    backend = FakeBackend(STACK)
    work = process_stack(src, PurePosixPath(""), settings, backend, stacks)

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    # Merge the CT report and the invoice, and have the result re-titled.
    ct, invoice = plan["documents"][1], plan["documents"][2]
    ct["pages"], ct["title"] = "4-6", ""
    plan["documents"].remove(invoice)
    (work / PLAN).write_text(json.dumps(plan), encoding="utf-8")

    written = rebuild(work, settings, backend)

    assert titles(written) == ["Befundbericht CT undated.pdf", "Blutbild 2026-09-30.pdf"]
    assert titles(stacks.output.glob("*.pdf")) == titles(written)
    with pikepdf.open(stacks.output / "Befundbericht CT undated.pdf") as pdf:
        assert len(pdf.pages) == 3


def test_rebuild_refuses_a_plan_folder_outside_the_output(settings, tmp_path):
    stacks = settings.profile("stacks")
    src = tmp_path / "stack.pdf"
    make_pdf(src, len(STACK))
    work = process_stack(src, PurePosixPath(""), settings, FakeBackend(STACK), stacks)

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    plan["folder"] = "../../escaped"
    (work / PLAN).write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ValueError, match="outside"):
        rebuild(work, settings, None)
    assert not (stacks.root.parent / "escaped").exists()


def test_watcher_archives_success_and_quarantines_failure(settings):
    stacks = settings.profile("stacks")
    folder = stacks.inbox / "Patient A"
    folder.mkdir(parents=True)
    make_pdf(folder / "good.pdf", len(STACK))
    (folder / "broken.pdf").write_bytes(b"not a pdf\n%%EOF\n")  # complete, but damaged

    watcher = InboxWatcher(settings, stacks, FakeBackend(STACK), threading.Event())
    watcher.poll_once()

    assert (stacks.archive / "Patient A" / "good.pdf").exists()
    assert (stacks.failed / "Patient A" / "broken.pdf").exists()
    assert (stacks.failed / "Patient A" / "broken.pdf.error.txt").exists()
    assert not any(folder.iterdir())


def test_profiles_have_separate_folders(settings):
    stacks, scanner = settings.profile("stacks"), settings.profile("scanner")
    assert stacks.split and not scanner.split
    assert {stacks.inbox, stacks.output}.isdisjoint({scanner.inbox, scanner.output})
