from __future__ import annotations

import json
from dataclasses import replace
from pathlib import PurePosixPath

import pikepdf
import pytest

from stacksplit.config import ConfigError, Settings
from stacksplit.pdfops import page_texts
from stacksplit.pipeline import PLAN, process_stack

from .conftest import FakeBackend


def make_text_pdf(path, lines: list[str]) -> None:
    """A PDF whose pages carry a real text layer, one line of text each."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica)
    )
    for line in lines:
        pdf.add_blank_page(page_size=(595, 842))
        page = pdf.pages[-1]
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        page.Contents = pdf.make_stream(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
    pdf.save(path)


TEXTS = [
    "LETTER Blood count dated",
    "Haemoglobin 13.9, platelets 251, page 2 of 2",
    "LETTER Electricity bill",
]


def test_page_texts_reads_the_text_layer(tmp_path):
    make_text_pdf(tmp_path / "t.pdf", ["first page", "second page"])
    assert [t.strip() for t in page_texts(tmp_path / "t.pdf")] == ["first page", "second page"]


def test_tesseract_source_splits_and_names_without_mistral_ocr(settings, tmp_path):
    # OCRMYPDF_ENABLED is off in tests, so the source PDF's own text layer
    # stands in for the one ocrmypdf would add.
    stacks = replace(settings.profile("stacks"), text_source="tesseract")
    src = tmp_path / "stack.pdf"
    make_text_pdf(src, TEXTS)
    backend = FakeBackend([])  # any OCR call would fail: there are no texts to hand out

    work = process_stack(src, PurePosixPath(""), settings, backend, stacks)

    assert backend.ocr_calls == 0 and not backend.jobs
    assert sorted(p.name for p in stacks.output.glob("*.pdf")) == [
        "Blood count 2026-09-30.pdf",
        "Electricity bill undated.pdf",
    ]
    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    assert plan["text_source"] == "tesseract"
    assert [d["pages"] for d in plan["documents"]] == ["1-2", "3"]


def test_text_source_settings_are_validated(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("SCANNER_TEXT_SOURCE", "Tesseract")
    settings = Settings.from_env()
    assert settings.profile("scanner").text_source == "tesseract"
    assert settings.profile("stacks").text_source == "mistral"

    monkeypatch.setenv("SCANNER_TEXT_SOURCE", "easyocr")
    with pytest.raises(ConfigError):
        Settings.from_env()

    monkeypatch.setenv("SCANNER_TEXT_SOURCE", "tesseract")
    monkeypatch.setenv("OCRMYPDF_ENABLED", "false")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_tesseract_source_keeps_image_only_pages(settings, tmp_path):
    # A page with no text layer at all (a photo, an X-ray) must not count as
    # blank just because Tesseract found nothing to read on it.
    stacks = replace(settings.profile("stacks"), text_source="tesseract")
    src = tmp_path / "stack.pdf"
    make_text_pdf(src, ["LETTER Blood count dated", ""])

    work = process_stack(src, PurePosixPath(""), settings, FakeBackend([]), stacks)

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    assert plan["dropped_pages"] == ""
