"""SCANNER_NAMING=off: the scanner's file name is kept, no Mistral at all."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from scanbutler import pdfops
from scanbutler.config import ConfigError, Settings
from scanbutler.pipeline import PLAN, process_stack, rebuild

from .conftest import FakeBackend
from .test_text_source import make_text_pdf


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Settings from scratch: no API key, ocrmypdf on (the default)."""
    for name in ("MISTRAL_API_KEY", "OCRMYPDF_ENABLED", "SCANNER_TEXT_SOURCE", "SCANNER_NAMING",
                 "STACKS_ENABLED", "SCANNER_ENABLED", "PAPERLESS_URL", "PAPERLESS_TOKEN",
                 "PAPERLESS_TEXT_SOURCE", "PAPERLESS_2_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "data" / "work"))
    return monkeypatch


def test_scanner_without_naming_needs_no_key(env):
    env.setenv("STACKS_ENABLED", "false")
    env.setenv("SCANNER_NAMING", "off")
    settings = Settings.from_env()
    scanner = settings.profile("scanner")
    assert settings.api_key == ""
    assert scanner.naming is False and scanner.text_source == "tesseract" and not scanner.uses_mistral


@pytest.mark.parametrize(
    "setup,needed",
    [
        ({}, "stacks, scanner"),  # defaults: both inputs name documents
        ({"STACKS_ENABLED": "false"}, "scanner"),
        ({"SCANNER_NAMING": "off"}, "stacks"),
        ({"STACKS_ENABLED": "false", "SCANNER_ENABLED": "false", "PAPERLESS_URL": "http://p",
          "PAPERLESS_TOKEN": "t", "PAPERLESS_TEXT_SOURCE": "mistral"}, "paperless"),
    ],
)
def test_key_is_required_by_inputs_that_call_mistral(env, setup, needed):
    for name, value in setup.items():
        env.setenv(name, value)
    with pytest.raises(ConfigError, match=f"MISTRAL_API_KEY is not set; needed by: {needed}$"):
        Settings.from_env()


def test_paperless_with_tesseract_needs_no_key(env):
    for name, value in {"STACKS_ENABLED": "false", "SCANNER_ENABLED": "false",
                        "PAPERLESS_URL": "http://p", "PAPERLESS_TOKEN": "t"}.items():
        env.setenv(name, value)
    settings = Settings.from_env()
    assert [p.name for p in settings.profiles] == ["paperless"]


@pytest.mark.parametrize(
    "setup,message",
    [
        ({"SCANNER_NAMING": "maybe"}, "SCANNER_NAMING must be on or off"),
        ({"SCANNER_NAMING": "off", "SCANNER_TEXT_SOURCE": "mistral"}, "has no use with SCANNER_NAMING=off"),
        ({"SCANNER_NAMING": "off", "OCRMYPDF_ENABLED": "false"}, "needs OCRMYPDF_ENABLED=true"),
    ],
)
def test_naming_setting_is_validated(env, setup, message):
    env.setenv("MISTRAL_API_KEY", "x")
    for name, value in setup.items():
        env.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        Settings.from_env()


def test_naming_on_by_default(env):
    env.setenv("MISTRAL_API_KEY", "x")
    settings = Settings.from_env()
    assert settings.profile("scanner").naming and settings.profile("scanner").uses_mistral
    assert settings.profile("stacks").naming


def _scanner(settings, **changes):
    return replace(settings.profile("scanner"), naming=False, text_source="tesseract", **changes)


def test_output_keeps_the_scanner_file_name(settings, tmp_path, monkeypatch):
    scanner = _scanner(settings)
    src = tmp_path / "03102026104645.pdf"
    make_text_pdf(src, ["page one", "page two"])
    monkeypatch.setattr(pdfops, "ink_coverage", lambda path: [0.05, 0.05])
    backend = FakeBackend([])

    work = process_stack(src, PurePosixPath("Office/2026"), settings, backend, scanner)

    assert backend.ocr_calls == 0 and backend.chat_calls == []
    out = scanner.output / "Office" / "2026"
    assert [p.name for p in out.glob("*.pdf")] == ["03102026104645.pdf"]
    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    assert plan["keep_name"] is True and plan["documents"][0]["pages"] == "1-2"
    # A second scan with the same name doesn't overwrite the first.
    src2 = tmp_path / "other" / "03102026104645.pdf"
    src2.parent.mkdir()
    make_text_pdf(src2, ["another scan"])
    monkeypatch.setattr(pdfops, "ink_coverage", lambda path: [0.05])
    process_stack(src2, PurePosixPath("Office/2026"), settings, backend, scanner)
    assert sorted(p.name for p in out.glob("*.pdf")) == ["03102026104645 (2).pdf", "03102026104645.pdf"]


def test_blank_pages_are_still_dropped(settings, tmp_path, monkeypatch):
    settings = replace(settings, blank_max_ink=0.002)
    scanner = _scanner(settings)
    src = tmp_path / "scan.pdf"
    make_text_pdf(src, ["front", "", "back"])
    monkeypatch.setattr(pdfops, "ink_coverage", lambda path: [0.05, 0.0001, 0.04])

    work = process_stack(src, PurePosixPath(""), settings, FakeBackend([]), scanner)

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    assert plan["documents"][0]["pages"] == "1, 3" and plan["dropped_pages"] == "2"
    assert pdfops.page_count(scanner.output / "scan.pdf") == 2


def test_rebuild_keeps_the_name_and_refuses_retitling(settings, tmp_path, monkeypatch):
    scanner = _scanner(settings)
    src = tmp_path / "scan.pdf"
    make_text_pdf(src, ["front"])
    monkeypatch.setattr(pdfops, "ink_coverage", lambda path: [0.05])
    work = process_stack(src, PurePosixPath(""), settings, FakeBackend([]), scanner)

    assert [p.name for p in rebuild(work, settings, None)] == ["scan.pdf"]

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    plan["documents"][0]["title"] = ""
    (work / PLAN).write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(RuntimeError, match="keeps its name"):
        rebuild(work, settings, None)


def test_a_hand_edited_kept_name_stays_in_the_output_folder(settings, tmp_path, monkeypatch):
    scanner = _scanner(settings)
    src = tmp_path / "scan.pdf"
    make_text_pdf(src, ["front"])
    monkeypatch.setattr(pdfops, "ink_coverage", lambda path: [0.05])
    work = process_stack(src, PurePosixPath(""), settings, FakeBackend([]), scanner)

    plan = json.loads((work / PLAN).read_text(encoding="utf-8"))
    plan["documents"][0]["title"] = "../../escaped"
    (work / PLAN).write_text(json.dumps(plan), encoding="utf-8")
    [written] = rebuild(work, settings, None)
    assert written.parent == scanner.output
    assert "/" not in written.name and not written.name.startswith(".")
