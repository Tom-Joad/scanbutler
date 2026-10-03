"""Hand finished PDFs to Paperless-ngx through its REST API.

Paperless consumes uploads asynchronously: the upload returns a task id, and
the task tells later whether a document was created or why not (for example
a duplicate). Only a confirmed document counts as done.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class PaperlessError(RuntimeError):
    """Paperless rejected the document; retrying the same file won't help."""


class PaperlessUnavailable(RuntimeError):
    """Paperless can't be reached or refuses the token; try again later."""


# The task format changed with Paperless-ngx 3: "status" became lower case,
# "result" became "result_data" and "related_document" became
# "related_document_ids". These helpers read both formats.


def task_status(task: dict) -> str:
    return str(task.get("status", "")).upper()


def task_document_id(task: dict) -> int | None:
    candidates = [
        (task.get("result_data") or {}).get("document_id") if isinstance(task.get("result_data"), dict) else None,
        (task.get("related_document_ids") or [None])[0],
        task.get("related_document"),
    ]
    for value in candidates:
        if value is not None and str(value).isdigit():
            return int(value)
    return None


def task_message(task: dict) -> str:
    for key in ("result", "result_data", "status_display"):
        value = task.get(key)
        if value:
            return str(value)[:300]
    return "no details from Paperless"


class PaperlessClient:
    def __init__(self, url: str, token: str, timeout: float = 120.0) -> None:
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise PaperlessUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code in (401, 403):
            raise PaperlessUnavailable(f"HTTP {response.status_code}: token rejected, check PAPERLESS_TOKEN")
        if response.status_code >= 500:
            raise PaperlessUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise PaperlessError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
        return response

    def upload(self, pdf: Path, filename: str, tags: list[int]) -> str:
        """Start consumption of `pdf`; returns the consumption task id."""
        # A list value becomes one form field per tag, as the API expects.
        data = {"tags": [str(tag) for tag in tags]} if tags else {}
        with pdf.open("rb") as handle:
            response = self._request(
                "POST",
                "/api/documents/post_document/",
                files={"document": (filename, handle, "application/pdf")},
                data=data,
            )
        task_id = response.json()
        if not isinstance(task_id, str) or not task_id:
            raise PaperlessError(f"unexpected upload answer: {str(task_id)[:200]}")
        return task_id

    def task(self, task_id: str) -> dict | None:
        result = self._request("GET", "/api/tasks/", params={"task_id": task_id}).json()
        items = result.get("results", []) if isinstance(result, dict) else result
        return items[0] if items else None

    def wait(self, task_id: str, poll_seconds: float, max_wait_seconds: float) -> dict | None:
        """The finished task, or None if it is still running after max_wait_seconds."""
        deadline = time.monotonic() + max_wait_seconds
        while True:
            task = self.task(task_id)
            if task and task_status(task) in ("SUCCESS", "FAILURE", "REVOKED"):
                return task
            if time.monotonic() > deadline:
                return None
            time.sleep(poll_seconds)
