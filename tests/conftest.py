from __future__ import annotations

import io
import json

import pikepdf
import pytest

from stacksplit.config import Settings


def make_pdf(path, pages: int) -> None:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
    pdf.save(path)


class FakeBackend:
    """Stands in for Mistral.

    OCR hands out `texts` in page order (tests run with OCR_CONCURRENCY=1).
    A page whose text starts with "LETTER" is a new document; the title is
    the first line of a document's text.
    """

    def __init__(self, texts: list[str]):
        self.texts = texts
        self.served = 0
        self.ocr_calls = 0
        self.chat_calls: list[str] = []

    def ocr_pdf(self, pdf_bytes: bytes) -> list[dict]:
        self.ocr_calls += 1
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            count = len(pdf.pages)
        pages = []
        for i in range(count):
            text = self.texts[self.served]
            self.served += 1
            pages.append({"index": i, "markdown": text, "header": "", "footer": "", "images": []})
        return pages

    def chat_json(self, system: str, user: str, schema: dict, name: str) -> dict:
        self.chat_calls.append(name)
        if name == "page_boundaries":
            items = json.loads(user)
            return {
                "pages": [
                    {
                        "page": item["page"],
                        "starts_new_document": item["text"].startswith("LETTER"),
                        "confidence": 0.5 if "unsure" in item["text"] else 0.95,
                        "reason": "fake",
                    }
                    for item in items
                ]
            }
        first_line = user.split("---\n", 1)[1].splitlines()[0]
        title = first_line.removeprefix("LETTER").removesuffix("dated").strip()
        return {"title": title, "date": "2026-09-30" if "dated" in user else None, "issuer": None, "summary": "fake"}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    env = {
        "MISTRAL_API_KEY": "test-key",
        "DATA_DIR": str(tmp_path / "data"),
        "OCR_CHUNK_PAGES": "3",
        "OCR_CONCURRENCY": "1",
        "LLM_CONCURRENCY": "1",
        "BOUNDARY_WINDOW": "4",
        "BOUNDARY_STEP": "2",
        "OCRMYPDF_ENABLED": "false",
        "STABLE_SECONDS": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()
