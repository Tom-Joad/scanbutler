from __future__ import annotations

import io
import base64

import pikepdf
import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from scanbutler import api, images, ocr_plugin
from scanbutler.config import Settings
from scanbutler.tokens import TokenStore


def _text_page(mode: str, white: int, black: int) -> Image.Image:
    """A page with bars of 'text', in a mode and range of its own."""
    image = Image.new(mode, (400, 300), white)
    draw = ImageDraw.Draw(image)
    for row in range(5):
        draw.rectangle((40, 40 + row * 45, 360, 60 + row * 45), fill=black)
    return image


def _render_grey(pdf_path) -> Image.Image:
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        return doc[0].render(scale=1, grayscale=True).to_pil().convert("L")
    finally:
        doc.close()


@pytest.mark.parametrize("suffix", ["tif", "png"])
def test_16_bit_greyscale_keeps_its_content(tmp_path, suffix):
    src = tmp_path / f"scan.{suffix}"
    # A scanner's 16-bit scan: paper near 60000, ink near 5000.
    _text_page("I;16", 60000, 5000).save(src)
    assert Image.open(src).mode == "I;16"
    dst = tmp_path / "scan.pdf"

    images.to_pdf(src, dst)

    rendered = _render_grey(dst)
    low, high = rendered.getextrema()
    assert high > 200 and low < 60  # paper is light, the bars are dark


def test_16_bit_values_are_scaled_not_clipped():
    image = Image.new("I;16", (4, 4), 30000)
    assert images.to_8bit(image).getpixel((0, 0)) == 117
    assert images.to_8bit(image).mode == "L"


def test_plugin_sends_16_bit_pages_scaled(tmp_path):
    body = ocr_plugin.image_body(_text_page("I;16", 60000, 5000))
    jpeg = base64.b64decode(body["document"]["image_url"].split(",", 1)[1])
    low, high = Image.open(io.BytesIO(jpeg)).getextrema()
    assert high > 200 and low < 60


def test_multi_page_tiff_becomes_one_pdf_page_per_frame(tmp_path):
    src = tmp_path / "scan.tif"
    first = _text_page("L", 255, 0)
    first.save(src, save_all=True, append_images=[_text_page("RGB", 255, 0), _text_page("I;16", 60000, 5000)])
    dst = tmp_path / "scan.pdf"

    images.to_pdf(src, dst)

    with pikepdf.open(dst) as pdf:
        assert len(pdf.pages) == 3
    assert not list(tmp_path.glob("*.tmp*"))


def test_too_many_frames_are_refused_before_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "MAX_FRAMES", 3)
    src = tmp_path / "many.tif"
    page = Image.new("L", (10, 10), 255)
    page.save(src, save_all=True, append_images=[page.copy() for _ in range(3)])
    with pytest.raises(images.ImageRefused, match="4 frames"):
        images.check(src)
    with pytest.raises(images.ImageRefused):
        images.to_pdf(src, tmp_path / "many.pdf")
    assert not (tmp_path / "many.pdf").exists()


def test_too_many_pixels_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "MAX_MPIXELS", 1)
    src = tmp_path / "big.png"
    Image.new("L", (1200, 1000), 255).save(src)
    with pytest.raises(images.ImageRefused, match="megapixels"):
        images.check(src)


def test_a_decompression_bomb_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    src = tmp_path / "bomb.png"
    Image.new("L", (100, 100), 255).save(src)
    with pytest.raises(images.ImageRefused):
        images.check(src)


def test_api_refuses_an_image_with_too_many_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setenv("API_ENABLED", "true")
    settings = Settings.from_env()
    jobs = api.Jobs(tmp_path / "work" / "api" / "jobs")
    tokens = TokenStore(tmp_path / "work" / "api" / "tokens.json")
    client = TestClient(api.create_app(settings, jobs, tokens))
    headers = {"Authorization": f"Bearer {tokens.add('t')}"}
    monkeypatch.setattr(images, "MAX_FRAMES", 2)

    page = Image.new("L", (10, 10), 255)
    many = io.BytesIO()
    page.save(many, "TIFF", save_all=True, append_images=[page, page])
    answer = client.post("/api/v1/jobs", headers=headers, content=many.getvalue())
    assert answer.status_code == 413 and "frames" in answer.json()["detail"]

    one = io.BytesIO()
    page.save(one, "PNG")
    assert client.post("/api/v1/jobs", headers=headers, content=one.getvalue()).status_code == 202
    broken = b"\x89PNG\r\n\x1a\n" + b"not an image"
    assert client.post("/api/v1/jobs", headers=headers, content=broken).status_code == 422
    assert [j["status"] for j in jobs.all()] == ["queued"]
