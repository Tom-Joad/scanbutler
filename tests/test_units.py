from __future__ import annotations

import pytest

from stacksplit.boundaries import best_window, page_marker, window_starts
from stacksplit.config import ConfigError, Settings
from stacksplit.metadata import normalize_date
from stacksplit.naming import build_stem, sanitize, unique_path
from stacksplit.ocr import Page
from stacksplit.plan import format_pages, parse_pages


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
