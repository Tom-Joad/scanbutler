"""Thin REST client for the two Mistral endpoints this tool needs.

Plain HTTP instead of the official SDK: the SDK's import paths and method
names have changed between major versions, while the REST contract for
/v1/ocr and /v1/chat/completions has stayed stable.
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import priority

log = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
# Still failing after every retry, these say the service is down or busy, not
# that this file is bad. A 500 can be the answer to one unreadable document,
# so it keeps failing the file (as do 409 and 425).
_OUTAGE_STATUS = {408, 429, 502, 503, 504}


class MistralError(RuntimeError):
    """Raised when the API returns an unusable answer after all retries."""


class MistralUnavailable(MistralError):
    """Mistral could not be reached or kept failing (network, 5xx, rate limit) for all attempts.

    Not the file's fault: the watcher keeps the file in the inbox and tries
    again later, like for an unreachable Paperless.
    """


class MistralLimitError(MistralError):
    """The account cannot be used right now: spending limit, exhausted quota or a rejected key.

    Retrying the next file would fail the same way, so the watcher pauses
    instead of moving file after file to failed/.
    """


# How Mistral answers a reached spending limit is not documented. Reports
# range from a bare 401 to 402/403; 429 is used for the ordinary
# per-second rate limit, which is retried, but may carry a quota message too.
_LIMIT_STATUS = {401, 402, 403}


# Words that mark a 429 as an exhausted quota rather than the per-second
# rate limit. Any other 429 is treated as the rate limit: it is retried with
# the server's Retry-After, and a file is never failed for it.
_QUOTA_WORDS = ("quota", "spend", "billing", "credit", "balance", "insufficient", "monthly")


def is_limit_response(response: httpx.Response) -> bool:
    if response.status_code in _LIMIT_STATUS:
        return True
    if response.status_code != 429:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):  # a list, a string, null: nothing to read a quota from
        return False
    if body.get("type") == "rate_limited":
        return False
    message = str(body.get("message", "")).lower()
    return "rate limit" not in message and any(word in message for word in _QUOTA_WORDS)


# A longer Retry-After than this is not waited out inside one request.
_MAX_RETRY_AFTER = 120.0
# Retrying these cannot create a second copy on the server: the request never
# reached it. After any other transport error (a read timeout, say) the server
# may have acted already, so uploads and job creation are not repeated.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)


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
        reasoning_effort: str = "",
        rate_file: Path | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=api_base,
            timeout=httpx.Timeout(timeout, connect=30.0),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        self.ocr_model = ocr_model
        self.llm_model = llm_model
        self.reasoning_effort = reasoning_effort
        self.max_attempts = max_attempts
        # Shared by all worker threads: after a 429 nobody sends until the
        # cooldown is over, instead of each thread hammering the limit alone.
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        # Account limits are per model and only visible in Mistral's admin
        # panel, so the request rate is a setting; 0 disables the throttle.
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._next_slot = 0.0
        # Shared with other processes (the API's OCR runs in ocrmypdf's
        # workers): they all keep to the same MISTRAL_MAX_RPS through it.
        self._rate_file = rate_file
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
            if self._rate_file is not None:
                self._shared_slot()

    def _shared_slot(self) -> None:
        """Wait for the next request slot of all processes using the rate file."""
        self._rate_file.parent.mkdir(parents=True, exist_ok=True)
        # "a+" creates the file; after truncate(0) appending writes at 0.
        with open(self._rate_file, "a+", encoding="ascii") as handle:
            # Held while waiting: the processes queue up on the lock.
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                try:
                    slot = float(handle.read().strip() or 0)
                except ValueError:
                    slot = 0.0
                now = time.time()
                # Never longer than one interval: a clock jump or a stale
                # file must not stall every request.
                wait = min(max(0.0, slot - now), self._min_interval)
                if wait:
                    time.sleep(wait)
                handle.seek(0)
                handle.truncate()
                handle.write(f"{time.time() + self._min_interval:.3f}")
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _back_off(self, attempt: int, retry_after: str | None, path: str, reason: object) -> None:
        delay = min(60.0, 2.0**attempt) + random.uniform(0, 1)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), _MAX_RETRY_AFTER))
            except ValueError:
                pass
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)
        log.warning("api retry", extra={"path": path, "reason": reason, "attempt": attempt, "delay_s": round(delay, 1)})

    def _request(self, method: str, path: str, *, repeatable: bool = True, **kwargs: Any) -> httpx.Response:
        """Send with retries. `repeatable=False` (uploads, job creation) is not
        repeated after a transport error that may have reached the server."""
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_cooldown()
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts or not (repeatable or isinstance(exc, _NOT_SENT)):
                    raise MistralUnavailable(f"{path}: transport error: {exc}") from exc
                self._back_off(attempt, None, path, type(exc).__name__)
                continue

            if response.status_code < 400:
                return response

            if is_limit_response(response):
                raise MistralLimitError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")

            if response.status_code in _RETRY_STATUS and attempt < self.max_attempts:
                self._back_off(attempt, response.headers.get("retry-after"), path, response.status_code)
                continue

            if response.status_code in _OUTAGE_STATUS:
                raise MistralUnavailable(f"{path}: HTTP {response.status_code} after {attempt} attempts: {response.text[:300]}")
            # The error body describes the request problem; it never contains
            # document text, so it is safe to surface.
            raise MistralError(f"{path}: HTTP {response.status_code}: {response.text[:500]}")

        raise MistralUnavailable(f"{path}: giving up after {self.max_attempts} attempts")

    def _post(self, path: str, body: dict[str, Any], *, repeatable: bool = True) -> dict[str, Any]:
        return self._request("POST", path, repeatable=repeatable, json=body).json()

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

    def submit_batch(
        self,
        endpoint: str,
        model: str,
        lines: list[tuple[str, dict[str, Any]]],
        on_uploaded: Callable[[str], None] | None = None,
    ) -> dict[str, str]:
        """Upload requests as JSONL and start a batch job. Returns the ids to persist.

        `on_uploaded` gets the upload's id before the job is created, so the
        caller can record it: if the process dies in between, the next start
        deletes the upload. Neither request is repeated after a transport
        error that may have reached Mistral (a read timeout), as that could
        leave a second upload or job behind.
        """
        payload = "".join(json.dumps({"custom_id": cid, "body": body}) + "\n" for cid, body in lines).encode("utf-8")
        uploaded = self._request(
            "POST",
            "/files",
            repeatable=False,
            files={"file": ("requests.jsonl", payload, "application/jsonl")},
            data={"purpose": "batch"},
        ).json()
        if on_uploaded:
            on_uploaded(uploaded["id"])
        try:
            job = self._post(
                "/batch/jobs",
                {"input_files": [uploaded["id"]], "model": model, "endpoint": endpoint, "metadata": {"tool": "scanbutler"}},
                repeatable=False,
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
            # Reasoning models such as Mistral Large 4 reject greedy sampling
            # (temperature 0) unless top_p is exactly 1.
            "top_p": 1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },
        }
        # Only reasoning models accept the parameter; Large 3 rejects it.
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
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
