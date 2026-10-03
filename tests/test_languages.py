from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
import pytest

from scanbutler import languages
from scanbutler.config import ConfigError, Settings


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def system(tmp_path: Path) -> Path:
    """A built-in tessdata folder with deu, eng, osd, a config folder and the font."""
    folder = tmp_path / "system"
    (folder / "configs").mkdir(parents=True)
    (folder / "configs" / "hocr").write_text("tessedit_create_hocr 1\n")
    (folder / "pdf.ttf").write_bytes(b"font")
    for code in ("deu", "eng", "osd"):
        (folder / f"{code}.traineddata").write_bytes(code.encode())
    return folder


@pytest.fixture
def fake_models(monkeypatch):
    """Serve fake model files and record what was requested."""
    models = {"fra": b"french model", "ita": b"italian model"}
    sums = dict(languages.checksums())
    sums.update({code: sha(data) for code, data in models.items()})
    monkeypatch.setattr(languages, "checksums", lambda: sums)
    requests: list[str] = []

    def fake_download(url: str, dest: Path, expected: str, timeout: float = 60.0) -> None:
        requests.append(url)
        code = url.rsplit("/", 1)[1].removesuffix(".traineddata")
        if code not in models:
            raise httpx.ConnectError("offline")
        data = models[code]
        if sha(data) != expected:
            raise ValueError("checksum mismatch")
        dest.write_bytes(data)

    monkeypatch.setattr(languages, "download", fake_download)
    return models, requests


def test_checksums_cover_all_tessdata_best_languages():
    sums = languages.checksums()
    assert len(sums) == 124
    # The built-in models, as pinned in the Dockerfile.
    assert sums["deu"] == "8407331d6aa0229dc927685c01a7938fc5a641d1a9524f74838cdac599f0d06e"
    assert sums["osd"] == "9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff"
    assert all(len(digest) == 64 for digest in sums.values())


def test_parse_normalises_and_deduplicates():
    assert languages.parse(" DEU + eng+fra+deu ") == ["deu", "eng", "fra"]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("ger", "did you mean 'deu'"),
        ("deu+fr", "did you mean 'fra'"),
        ("xyz", "three-letter codes"),
        ("deu++eng", "empty entry"),
    ],
)
def test_parse_rejects_unknown_codes(spec, message):
    with pytest.raises(languages.LanguageError, match=message):
        languages.parse(spec)


def test_settings_report_a_misspelt_language(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("OCRMYPDF_LANGUAGES", "deu+ger")
    with pytest.raises(ConfigError, match="did you mean 'deu'"):
        Settings.from_env()


def test_settings_normalise_languages(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("OCRMYPDF_LANGUAGES", "deu+ENG+fra")
    assert Settings.from_env().ocrmypdf_languages == "deu+eng+fra"


def test_built_in_languages_need_nothing(tmp_path, system, fake_models):
    _, requests = fake_models
    ready = languages.prepare(["deu", "eng"], tmp_path / "cache", system)
    assert ready.languages == ["deu", "eng"]
    assert ready.tessdata is None
    assert not (tmp_path / "cache").exists()
    assert requests == []


def test_downloads_once_and_links_the_built_ins(tmp_path, system, fake_models):
    models, requests = fake_models
    cache = tmp_path / "cache"
    ready = languages.prepare(["deu", "fra"], cache, system, "https://mirror.example/tessdata/")
    assert ready.languages == ["deu", "fra"]
    assert ready.downloaded == ["fra"]
    assert ready.tessdata == cache
    assert requests == ["https://mirror.example/tessdata/fra.traineddata"]
    assert (cache / "fra.traineddata").read_bytes() == models["fra"]
    # Tesseract reads everything from the cache now: models, configs, font.
    for name in ("deu.traineddata", "eng.traineddata", "osd.traineddata", "pdf.ttf"):
        assert (cache / name).read_bytes() == (system / name).read_bytes()
    assert (cache / "configs" / "hocr").is_file()

    again = languages.prepare(["deu", "fra"], cache, system)
    assert again.cached == ["fra"] and again.downloaded == []
    assert len(requests) == 1


def test_a_damaged_cached_model_is_downloaded_again(tmp_path, system, fake_models):
    models, requests = fake_models
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "fra.traineddata").write_bytes(b"truncated")
    ready = languages.prepare(["fra"], cache, system)
    assert ready.downloaded == ["fra"]
    assert (cache / "fra.traineddata").read_bytes() == models["fra"]


def test_offline_leaves_the_language_out(tmp_path, system, fake_models, caplog):
    _, requests = fake_models
    ready = languages.prepare(["deu", "eng", "por"], tmp_path / "cache", system)
    assert ready.languages == ["deu", "eng"]
    assert ready.missing == ["por"]
    assert ready.tessdata is None  # nothing beyond the built-ins in use
    assert len(requests) == 2  # one retry
    assert "language not available" in caplog.text


def test_nothing_available_falls_back_to_the_built_ins(tmp_path, system, fake_models):
    ready = languages.prepare(["por"], tmp_path / "cache", system)
    assert ready.languages == ["deu", "eng"]
    assert ready.missing == ["por"]


def test_stale_links_from_an_older_image_are_replaced(tmp_path, system, fake_models):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "gone.traineddata").symlink_to(tmp_path / "old-image" / "gone.traineddata")
    (cache / "deu.traineddata").symlink_to(tmp_path / "old-image" / "deu.traineddata")
    languages.prepare(["deu", "ita"], cache, system)
    assert not os.path.lexists(cache / "gone.traineddata")
    assert (cache / "deu.traineddata").resolve() == (system / "deu.traineddata").resolve()


def test_download_checks_the_checksum(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"tampered")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: client.stream(method, url))
    dest = tmp_path / "fra.traineddata"
    with pytest.raises(ValueError, match="checksum mismatch"):
        languages.download("https://mirror.example/fra.traineddata", dest, sha(b"genuine"))
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []  # no partial file left

    languages.download("https://mirror.example/fra.traineddata", dest, sha(b"tampered"))
    assert dest.read_bytes() == b"tampered"


def test_errors_never_show_the_url():
    request = httpx.Request("GET", "https://user:secret@mirror.example/fra.traineddata")
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(404, request=request))
    assert languages._describe(error) == "HTTP 404"
    assert "secret" not in languages._describe(httpx.ConnectError("x", request=request))
