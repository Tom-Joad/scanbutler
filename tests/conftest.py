from __future__ import annotations

import io
import json

import pikepdf
import pytest

from scanbutler.config import Settings


def make_pdf(path, pages: int) -> None:
    pdf = pikepdf.new()
    for i in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
        # Lets the fake OCR know which stack page it is looking at, whatever
        # chunk or call order the page arrives in.
        pdf.pages[i].obj[pikepdf.Name("/TestIndex")] = i
    pdf.save(path)


class FakeBackend:
    """Stands in for Mistral.

    OCR returns `texts[i]` for stack page i, directly or through a fake batch
    job. Batch requests whose custom_id is in `fail_batch` come back failed.
    A page whose text starts with "LETTER" is a new document; the title is
    the first line of a document's text.
    """

    ocr_model = "fake-ocr"

    def __init__(self, texts: list[str], fail_batch: set[str] | None = None):
        self.texts = texts
        self.fail_batch = fail_batch or set()
        self.ocr_calls = 0
        self.jobs: dict[str, list] = {}
        self.deleted: list[str] = []
        self.chat_calls: list[str] = []

    def ocr_pdf(self, pdf_bytes: bytes) -> list[dict]:
        self.ocr_calls += 1
        return self._ocr(pdf_bytes)

    def _ocr(self, pdf_bytes: bytes) -> list[dict]:
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            indices = [int(page.obj["/TestIndex"]) for page in pdf.pages]
        return [
            {"index": i, "markdown": self.texts[n], "header": "", "footer": "", "images": []}
            for i, n in enumerate(indices)
        ]

    def ocr_body(self, pdf_bytes: bytes) -> dict:
        return {"pdf": pdf_bytes}

    @staticmethod
    def ocr_pages(result: dict) -> list[dict]:
        return result["pages"]

    def submit_batch(self, endpoint: str, model: str, lines: list) -> dict:
        job = f"job-{len(self.jobs)}"
        self.jobs[job] = lines
        return {"job": job, "input_file": f"in-{job}"}

    def wait_batch(self, job_id: str, poll_seconds: float, max_wait_seconds: float) -> dict:
        return {"id": job_id, "status": "SUCCESS", "output_file": f"out-{job_id}", "error_file": None}

    def batch_results(self, job: dict) -> dict:
        return {
            cid: {"pages": self._ocr(body["pdf"])}
            for cid, body in self.jobs[job["id"]]
            if cid not in self.fail_batch
        }

    def delete_files(self, file_ids: list) -> None:
        self.deleted.extend(f for f in file_ids if f)

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
        # Explicit: inside the image /config exists and would be shared by all tests.
        "WORK_DIR": str(tmp_path / "data" / "work"),
        "OCR_CHUNK_PAGES": "3",
        "OCR_CONCURRENCY": "1",
        "LLM_CONCURRENCY": "1",
        "BOUNDARY_WINDOW": "4",
        "BOUNDARY_STEP": "2",
        "OCRMYPDF_ENABLED": "false",
        "STABLE_SECONDS": "0",
        # Test PDFs are truly blank pages; blankness comes from FakeBackend text.
        "BLANK_MAX_INK_PERCENT": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()
