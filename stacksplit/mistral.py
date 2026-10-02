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

log = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class MistralError(RuntimeError):
    """Raised when the API returns an unusable answer after all retries."""


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
            with self._lock:
                now = time.monotonic()
                slot = max(now, self._next_slot)
                self._next_slot = slot + self._min_interval
            if slot > now:
                time.sleep(slot - now)

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

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_cooldown()
            try:
                response = self._http.post(path, json=body)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise MistralError(f"{path}: transport error: {exc}") from exc
                self._back_off(attempt, None, path, type(exc).__name__)
                continue

            if response.status_code < 400:
                return response.json()

            if response.status_code in _RETRY_STATUS and attempt < self.max_attempts:
                self._back_off(attempt, response.headers.get("retry-after"), path, response.status_code)
                continue

            # The error body describes the request problem; it never contains
            # document text, so it is safe to surface.
            raise MistralError(f"{path}: HTTP {response.status_code}: {response.text[:500]}")

        raise MistralError(f"{path}: giving up after {self.max_attempts} attempts")

    def ocr_pdf(self, pdf_bytes: bytes) -> list[dict[str, Any]]:
        """OCR one PDF and return its page objects in page order."""
        data_url = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
        result = self._post(
            "/ocr",
            {
                "model": self.ocr_model,
                "document": {"type": "document_url", "document_url": data_url},
                # Running headers and footers carry the strongest split hints
                # (letterhead, "page 2 of 3"), so keep them as separate fields.
                "extract_header": True,
                "extract_footer": True,
                "include_image_base64": False,
            },
        )
        pages = result.get("pages")
        if not isinstance(pages, list):
            raise MistralError("/ocr: response has no pages list")
        return sorted(pages, key=lambda p: p.get("index", 0))

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
