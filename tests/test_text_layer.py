from __future__ import annotations

import subprocess

import pikepdf
import pytest

from stacksplit import pdfops

from .conftest import make_pdf
from .test_text_source import make_text_pdf


def test_mode_choice_depends_on_existing_text():
    assert [name for name, _ in pdfops.ocr_modes(has_text=False)] == ["scan", "redo", "plain"]
    assert [name for name, _ in pdfops.ocr_modes(has_text=True)] == ["redo", "plain"]
    assert [name for name, _ in pdfops.ocr_modes(has_text=True, tagged=True)] == ["plain"]
    # --redo-ocr refuses --deskew; --clean (unpaper) is for scans only and ran
    # out of memory on a born-digital page with a high-resolution logo.
    assert "--deskew" not in pdfops.REDO_MODE[1] and "--clean" not in pdfops.REDO_MODE[1]


def test_tagged_pdf_is_kept_as_it_is(tmp_path, monkeypatch):
    src = tmp_path / "statement.pdf"
    make_text_pdf(src, ["Account statement 9/2026"])

    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.Root.MarkInfo = pikepdf.Dictionary(Marked=True)
        pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot))
        pdf.save(src)
    assert pdfops.is_tagged(src)
    calls = fake_ocrmypdf(monkeypatch, [(0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", 2, "")

    assert len(calls) == 1 and "--skip-text" in calls[0]


def fake_ocrmypdf(monkeypatch, outcomes):
    """Replace ocrmypdf: each call pops (returncode, stderr) and records its options."""
    calls = []

    def run(cmd, capture_output, text):
        calls.append(cmd)
        returncode, stderr = outcomes.pop(0)
        if returncode == 0:
            with open(cmd[-1], "wb") as handle:
                handle.write(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(pdfops.subprocess, "run", run)
    return calls


def test_digital_pdf_uses_redo_ocr(tmp_path, monkeypatch):
    src = tmp_path / "statement.pdf"
    make_text_pdf(src, ["Account statement 9/2026"])
    calls = fake_ocrmypdf(monkeypatch, [(0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", 2, "")

    assert "--redo-ocr" in calls[0] and "--force-ocr" not in calls[0]
    assert "--quiet" not in calls[0]


def test_failed_mode_falls_back_and_reports_the_real_error(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    make_pdf(src, 1)
    stderr = "Start processing\nSubprocessOutputError: unpaper: [Errno 5] Input/output error\n"
    calls = fake_ocrmypdf(monkeypatch, [(7, stderr), (0, "")])

    pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", 2, "")

    assert "--force-ocr" in calls[0] and "--redo-ocr" in calls[1]
    assert (tmp_path / "out.pdf").exists()


def test_all_modes_failing_raises_with_details(tmp_path, monkeypatch):
    src = tmp_path / "scan.pdf"
    make_pdf(src, 1)
    fake_ocrmypdf(monkeypatch, [(7, "Ghostscript: [Errno 28] No space left on device"), (7, "boom"), (6, "boom")])

    with pytest.raises(RuntimeError) as failure:
        pdfops.make_searchable(src, tmp_path / "out.pdf", "deu+eng", 2, "")

    message = str(failure.value)
    assert "scan: exit code 7" in message and "No space left on device" in message
    assert "plain: exit code 6" in message
    assert not (tmp_path / "out.pdf").exists()
