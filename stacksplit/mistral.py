"""Thin REST client for the two Mistral endpoints this tool needs.

Plain HTTP instead of the official SDK: the SDK's import paths and method
names have changed between major versions, while the REST contract for
/v1/ocr and /v1/chat/completions has stayed stable.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from typing import Any

import httpx

from . import priority

log = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class MistralError(RuntimeError):
    """Raised when the API returns an unusable answer after all retries."""


class MistralLimitError(MistralError):
    """The account cannot be used right now: spending limit, exhausted quota or a rejected key.

    Retrying the next file would fail the same way, so the watcher pauses
    instead of moving file after file to failed/.
    """


# How Mistral answers a reached spending limit is not documented. Reports
# range from a bare 401 to 402/403; 429 is used for the ordinary
# per-second rate limit, which is retried, but may carry a quota message too.
_LIMIT_STATUS = {401, 402, 403}


def is_limit_response(response: httpx.Response) -> bool:
    if response.status_code in _LIMIT_STATUS:
        return True
    if response.status_code != 429:
        return False
    try:
        body = response.json()
    except ValueError:
        body = {}
    ordinary = body.get("type") == "rate_limited" or "rate limit exceeded" in str(body.get("message", "")).lower()
    return not ordinary


class MistralClient:
    def __init__(
        self,
        api_key: str,
        api_base: str,
        ocr_model: str,
        llm_model: str,
        timeout: float = 300.0,
        max_attempts: int = 10,
        max_rps: float = 0.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=api_base,
            timeout=httpx.Timeout(timeout, connect=30.0),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        self.ocr_model = ocr_model
        self.llm_model = llm_model
        self.max_attempts = max_attempts
        # Shared by all worker threads: after a 429 nobody sends until the
        # cooldown is over, instead of each thread hammering the limit alone.
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        # Account limits are per model and only visible in Mistral's admin
        # panel, so the request rate is a setting; 0 disables the throttle.
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._next_slot = 0.0
        # Slots are handed out one at a time, so a scan's request can go
        # ahead of a stack's requests that are already waiting.
        self._slot = threading.Condition()
        self._priority_waiting = 0

    def close(self) -> None:
        self._http.close()

    def _wait_for_cooldown(self) -> None:
        while True:
            with self._lock:
                remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(remaining)
        if self._min_interval:
            first = priority.current()
            with self._slot:
                if first:
                    self._priority_waiting += 1
                try:
                    while True:
                        now = time.monotonic()
                        if now >= self._next_slot and (first or not self._priority_waiting):
                            break
                        self._slot.wait(max(0.01, self._next_slot - now))
                finally:
                    if first:
                        self._priority_waiting -= 1
                self._next_slot = now + self._min_interval
                self._slot.notify_all()

    def _back_off(self, attempt: int, retry_after: str | None, path: str, reason: object) -> None:
        delay = min(60.0, 2.0**attempt) + random.uniform(0, 1)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)
        log.warning("api retry", extra={"path": path, "reason": reason, "attempt": attempt, "delay_s": round(delay, 1)})

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_cooldown()
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise MistralError(f"{path}: transport error: {exc}") from exc
                self._back_off(attempt, None, path, type(exc).__name__)
                continue

            if response.status_code < 400:
                return response

            if is_limit_response(response):
                raise MistralLimitError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")

            if response.status_code in _RETRY_STATUS and attempt < self.max_attempts:
                self._back_off(attempt, response.headers.get("retry-after"), path, response.status_code)
                continue

            # The error body describes the request problem; it never contains
            # document text, so it is safe to surface.
            raise MistralError(f"{path}: HTTP {response.status_code}: {response.text[:500]}")

        raise MistralError(f"{path}: giving up after {self.max_attempts} attempts")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json=body).json()

    # --- OCR ------------------------------------------------------------------

    def ocr_body(self, pdf_bytes: bytes) -> dict[str, Any]:
        """Request body for /v1/ocr, shared by direct and batch calls."""
        return {
            "document": {
                "type": "document_url",
                "document_url": "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii"),
            },
            # Running headers and footers carry the strongest split hints
            # (letterhead, "page 2 of 3"), so keep them as separate fields.
            "extract_header": True,
            "extract_footer": True,
            "include_image_base64": False,
        }

    @staticmethod
    def ocr_pages(result: dict[str, Any]) -> list[dict[str, Any]]:
        pages = result.get("pages")
        if not isinstance(pages, list):
            raise MistralError("/ocr: response has no pages list")
        return sorted(pages, key=lambda p: p.get("index", 0))

    def ocr_pdf(self, pdf_bytes: bytes) -> list[dict[str, Any]]:
        """OCR one PDF right away (full price) and return its pages in order."""
        return self.ocr_pages(self._post("/ocr", {"model": self.ocr_model, **self.ocr_body(pdf_bytes)}))

    # --- Batch API (half price, minutes instead of seconds) --------------------

    def submit_batch(self, endpoint: str, model: str, lines: list[tuple[str, dict[str, Any]]]) -> dict[str, str]:
        """Upload requests as JSONL and start a batch job. Returns the ids to persist."""
        payload = "".join(json.dumps({"custom_id": cid, "body": body}) + "\n" for cid, body in lines).encode("utf-8")
        uploaded = self._request(
            "POST",
            "/files",
            files={"file": ("requests.jsonl", payload, "application/jsonl")},
            data={"purpose": "batch"},
        ).json()
        try:
            job = self._post(
                "/batch/jobs",
                {"input_files": [uploaded["id"]], "model": model, "endpoint": endpoint, "metadata": {"tool": "scan-stack-splitter"}},
            )
        except Exception:
            self.delete_files([uploaded["id"]])
            raise
        log.info("batch submitted", extra={"job": job["id"], "requests": len(lines), "upload_mb": round(len(payload) / 2**20, 1)})
        return {"job": job["id"], "input_file": uploaded["id"]}

    def wait_batch(self, job_id: str, poll_seconds: float, max_wait_seconds: float) -> dict[str, Any]:
        """Poll until the job leaves QUEUED/RUNNING; cancel it after max_wait_seconds."""
        deadline = time.monotonic() + max_wait_seconds
        last_logged = None
        while True:
            job = self._request("GET", f"/batch/jobs/{job_id}").json()
            status = job.get("status")
            if status not in ("QUEUED", "RUNNING", "CANCELLATION_REQUESTED"):
                log.info(
                    "batch finished",
                    extra={"job": job_id, "status": status, "succeeded": job.get("succeeded_requests"), "failed": job.get("failed_requests")},
                )
                return job
            if time.monotonic() > deadline and status != "CANCELLATION_REQUESTED":
                log.warning("batch took too long, cancelling", extra={"job": job_id})
                self._request("POST", f"/batch/jobs/{job_id}/cancel")
            progress = (status, job.get("completed_requests"))
            if progress != last_logged:
                log.info("batch waiting", extra={"job": job_id, "status": status, "completed": job.get("completed_requests"), "total": job.get("total_requests")})
                last_logged = progress
            time.sleep(poll_seconds)

    def batch_results(self, job: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """custom_id -> response body, for every request that succeeded."""
        results: dict[str, dict[str, Any]] = {}
        if not job.get("output_file"):
            return results
        content = self._request("GET", f"/files/{job['output_file']}/content").content.decode("utf-8")
        for line in content.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            response = record.get("response") or {}
            if response.get("status_code") == 200 and isinstance(response.get("body"), dict):
                results[record["custom_id"]] = response["body"]
        return results

    def delete_files(self, file_ids: list[str | None]) -> None:
        """Remove uploads and results from Mistral's storage; best effort."""
        for file_id in filter(None, file_ids):
            try:
                self._request("DELETE", f"/files/{file_id}")
            except MistralError as exc:
                log.warning("could not delete file at Mistral", extra={"file": file_id, "error": str(exc)[:200]})

    def chat_json(self, system: str, user: str, schema: dict[str, Any], name: str) -> dict[str, Any]:
        """Run a chat completion constrained to a JSON schema and parse the result."""
        body = {
            "model": self.llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },
        }
        # A schema-constrained answer can still be truncated or malformed;
        # one re-ask is cheaper than failing a 500-page stack.
        for attempt in (1, 2):
            result = self._post("/chat/completions", body)
            try:
                content = result["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                return json.loads(content)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise MistralError(f"chat: unparseable JSON answer ({name})") from exc
                log.warning("chat answer not parseable, asking again", extra={"schema": name})
        raise AssertionError("unreachable")
