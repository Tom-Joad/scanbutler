from __future__ import annotations

import pytest

from scanbutler.boundaries import best_window, page_marker, window_starts
from scanbutler.config import ConfigError, Settings
from scanbutler.metadata import normalize_date
from scanbutler.naming import build_stem, sanitize, unique_path
from scanbutler.ocr import Page
from scanbutler.plan import format_pages, parse_pages


def page(text: str = "", header: str = "", footer: str = "", images: bool = False, index: int = 0) -> Page:
    return Page(index=index, markdown=text, header=header, footer=footer, has_images=images)


@pytest.mark.parametrize(
    ("indices", "text"),
    [([0, 1, 2], "1-3"), ([0, 2, 3, 4, 9], "1, 3-5, 10"), ([4], "5"), ([], "")],
)
def test_format_pages(indices, text):
    assert format_pages(indices) == text


def test_parse_pages_round_trip_and_order():
    assert parse_pages("1-3, 5", 10) == [0, 1, 2, 4]
    assert parse_pages("5, 1-2", 10) == [4, 0, 1]


@pytest.mark.parametrize("text", ["0", "3-2", "11", "a-b", ""])
def test_parse_pages_rejects(text):
    with pytest.raises(ValueError):
        parse_pages(text, 10)


def test_sanitize_strips_forbidden_and_reserved():
    assert sanitize('Befund: CT/MRT "Kopf"?') == "Befund CT MRT Kopf"
    assert sanitize("  trailing dots... ") == "trailing dots"
    assert sanitize("CON") == "_CON"


def test_build_stem_pattern_and_fallbacks():
    assert build_stem("{title} {date}", "Blutbild", "2026-09-30", "undated") == "Blutbild 2026-09-30"
    assert build_stem("{date} {title}", "Blutbild", None, "undatiert") == "undatiert Blutbild"
    assert build_stem("{title} {date}", "", None, "undated") == "Document undated"


def test_unique_path(tmp_path):
    (tmp_path / "A.pdf").write_bytes(b"")
    taken = {tmp_path / "A (2).pdf"}
    assert unique_path(tmp_path, "A", taken=taken) == tmp_path / "A (3).pdf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2026-09-30", "2026-09-30"), ("2026-02-30", None), ("30.09.2026", None), (None, None), (20260930, None)],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_blank_detection_ignores_markup_but_respects_images():
    assert page("  ---  \n ![img-0.jpeg](img-0.jpeg) ").is_blank(15)
    assert not page("![img-0.jpeg](img-0.jpeg)", images=True).is_blank(15)
    assert not page("Leukozyten 6,2 /nl").is_blank(15)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"footer": "Seite 2 von 3"}, (2, 3)),
        ({"header": "Page 1 of 4"}, (1, 4)),
        ({"text": "Befund ... S. 2/2"}, (2, 2)),
        ({"text": "Seite 4 von 3"}, None),
        ({"text": "Termin am 12/10"}, None),
    ],
)
def test_page_marker(kwargs, expected):
    assert page_marker(page(**kwargs)) == expected


@pytest.mark.parametrize(("count", "window", "step"), [(1, 4, 2), (4, 4, 2), (5, 4, 2), (23, 12, 6), (500, 12, 6)])
def test_windows_cover_every_page(count, window, step):
    starts = window_starts(count, window, step)
    covered = {i for s in starts for i in range(s, min(s + window, count))}
    assert covered == set(range(count))
    assert all(s + window <= max(count, window) for s in starts)


def test_best_window_prefers_centered_context():
    starts = window_starts(20, 6, 3)  # 0, 3, 6, 9, 12, 14
    assert best_window(0, starts, 6, 20) == 0
    assert best_window(7, starts, 6, 20) in (3, 6)  # 7 is 2-3 pages from both edges there
    assert best_window(19, starts, 6, 20) == 14


def test_config_validation(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("BOUNDARY_WINDOW", "6")
    monkeypatch.setenv("BOUNDARY_STEP", "6")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("BOUNDARY_STEP", "3")
    monkeypatch.setenv("FILENAME_PATTERN", "{date}")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.delenv("FILENAME_PATTERN")
    monkeypatch.delenv("MISTRAL_API_KEY")
    with pytest.raises(ConfigError):
        Settings.from_env()
    assert Settings.from_env(require_api_key=False).boundary_step == 3


def test_file_names_drop_invisible_format_characters():
    from scanbutler.naming import sanitize

    # U+202E would make "Invoice<RLO>fdp.exe" display as "Invoiceexe.pdf".
    assert sanitize("Invoice‮fdp.exe") == "Invoicefdp.exe"
    assert sanitize("Lab​results") == "Labresults"


def test_settings_repr_has_no_secrets(monkeypatch):
    import dataclasses

    from scanbutler.config import Settings

    hidden = {f.name for f in dataclasses.fields(Settings) if not f.repr}
    assert {"api_key", "paperless_token"} <= hidden


def test_bad_setting_is_logged_as_config_invalid(monkeypatch, capsys, tmp_path):
    import json as _json

    from scanbutler.__main__ import main

    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("OCR_MODE", "sometimes")
    assert main(["run"]) == 2
    line = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "config_invalid" and "OCR_MODE" in line["reason"]


def test_interactive_commands_log_to_stderr(monkeypatch, capsys, tmp_path):
    import json as _json

    from scanbutler.__main__ import main

    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("OCR_MODE", "sometimes")
    assert main(["process", str(tmp_path / "x.pdf")]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert _json.loads(err.strip().splitlines()[-1])["event"] == "config_invalid"


def test_an_unexpected_error_is_one_json_line(monkeypatch, capsys, tmp_path):
    import json as _json

    from scanbutler import __main__ as entry

    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", "x")

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "/config/api/tokens.json")

    monkeypatch.setattr(entry, "run_all", boom)
    monkeypatch.setattr(entry, "prepare_languages", lambda settings: settings)
    assert entry.main(["run"]) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    crashed = [_json.loads(line) for line in lines if '"crashed"' in line]
    assert len(crashed) == 1 and "PermissionError" in crashed[0]["error"]
    assert all(line.startswith("{") for line in lines)  # no bare traceback lines


def test_log_time_is_local_with_offset(time_zone):
    import json
    import logging

    from scanbutler.logging_setup import JsonFormatter

    time_zone("Europe/Berlin")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "starting", None, None)
    record.created = 1793000000.0  # winter: UTC+1
    assert json.loads(JsonFormatter().format(record))["ts"].endswith("+01:00")


@pytest.fixture
def valid_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("MISTRAL_API_KEY", "x")


def test_work_retention_days_keeps_what_1_9_0_accepted(monkeypatch, valid_env):
    monkeypatch.setenv("WORK_RETENTION_DAYS", "0")
    settings = Settings.from_env()
    assert settings.work_retention_days == 0 and settings.notes == ()
    for raw, days in (("7.5", 8), ("7.4", 7), ("-1", 0), ("-0.5", 0)):
        monkeypatch.setenv("WORK_RETENTION_DAYS", raw)
        settings = Settings.from_env()
        assert settings.work_retention_days == days and isinstance(days, int)
        assert any("WORK_RETENTION_DAYS" in note and raw in note for note in settings.notes)
    monkeypatch.setenv("WORK_RETENTION_DAYS", "soon")
    with pytest.raises(ConfigError, match="WORK_RETENTION_DAYS"):
        Settings.from_env()


def test_adjusted_settings_are_warned_about_after_starting(monkeypatch, capsys, valid_env):
    from scanbutler import __main__ as entry

    monkeypatch.setenv("WORK_RETENTION_DAYS", "-1")
    monkeypatch.setattr(entry, "run_all", lambda *a, **k: None)
    monkeypatch.setattr(entry, "prepare_languages", lambda settings: settings)
    assert entry.main(["run"]) == 0
    assert '"setting adjusted"' in capsys.readouterr().out


def test_log_level_is_checked(monkeypatch, valid_env):
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    with pytest.raises(ConfigError, match="LOG_LEVEL must be one of"):
        Settings.from_env()
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings.from_env().log_level == "DEBUG"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MISTRAL_TIMEOUT", "0"),
        ("MISTRAL_TIMEOUT", "-3"),
        ("MISTRAL_MAX_RPS", "-1"),
        ("PAUSE_RETRY_MINUTES", "-5"),
        ("PAUSE_RETRY_MINUTES", "0"),
        ("REVIEW_CONFIDENCE", "7"),
        ("REVIEW_CONFIDENCE", "-0.1"),
        ("BLANK_MAX_INK_PERCENT", "nan"),
        ("BLANK_MAX_INK_PERCENT", "101"),
        ("BLANK_MAX_INK_PERCENT", "-1"),
        ("BATCH_MAX_WAIT_HOURS", "-1"),
        ("BATCH_MAX_WAIT_HOURS", "inf"),
        ("PAPERLESS_MAX_WAIT_MINUTES", "0"),
        ("PAPERLESS_SHARE_TAGS_MINUTES", "nan"),
        ("API_RESULT_HOURS", "0.5"),
        ("API_PORT", "99999"),
        ("API_PORT", "0"),
    ],
)
def test_numeric_settings_are_range_checked(monkeypatch, valid_env, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


def test_numeric_settings_accept_their_limits(monkeypatch, valid_env):
    for name, value in (("MISTRAL_MAX_RPS", "0"), ("REVIEW_CONFIDENCE", "1"), ("BLANK_MAX_INK_PERCENT", "100"),
                        ("API_PORT", "65535"), ("MISTRAL_TIMEOUT", "0.5")):
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.max_rps == 0 and settings.api_port == 65535 and settings.blank_max_ink == 1


def test_check_config_reports_a_bad_setting_in_one_line(monkeypatch, capsys, tmp_path):
    from scanbutler.__main__ import main

    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert main(["check-config"]) == 2
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("MISTRAL_API_KEY is not set") and err.count("\n") == 1
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    assert main(["check-config"]) == 0
    assert capsys.readouterr() == ("", "")
