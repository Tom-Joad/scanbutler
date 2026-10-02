"""The real client against a simulated Mistral API (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx

from stacksplit.mistral import MistralClient


class FakeApi:
    def __init__(self, rate_limited_first: int = 0):
        self.rate_limited_left = rate_limited_first
        self.calls: list[str] = []
        self.uploaded: list[dict] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = f"{request.method} {request.url.path}"
        self.calls.append(route)
        if self.rate_limited_left:
            self.rate_limited_left -= 1
            return httpx.Response(429, headers={"retry-after": "0"}, json={"message": "Rate limit exceeded"})
        if route == "POST /v1/files":
            body = request.read()
            # The JSONL part of the multipart body: one request per line.
            start = body.index(b'{"custom_id"')
            end = body.rindex(b"}\n") + 1
            self.uploaded = [json.loads(line) for line in body[start:end].split(b"\n")]
            return httpx.Response(200, json={"id": "file-in"})
        if route == "POST /v1/batch/jobs":
            assert json.loads(request.read())["endpoint"] == "/v1/ocr"
            return httpx.Response(200, json={"id": "job-1", "status": "QUEUED"})
        if route == "GET /v1/batch/jobs/job-1":
            self.polls += 1
            done = self.polls > 1
            return httpx.Response(
                200,
                json={"id": "job-1", "status": "SUCCESS" if done else "RUNNING", "output_file": "file-out" if done else None},
            )
        if route == "GET /v1/files/file-out/content":
            lines = [
                {"custom_id": "a", "response": {"status_code": 200, "body": {"pages": [{"index": 0, "markdown": "A"}]}}},
                {"custom_id": "b", "response": {"status_code": 500, "body": {"message": "failed"}}},
            ]
            return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"message": route})


def client(api: FakeApi) -> MistralClient:
    c = MistralClient("key", "https://api.test/v1", "ocr-model", "llm-model", max_attempts=3)
    c._http = httpx.Client(base_url="https://api.test/v1", transport=httpx.MockTransport(api))
    return c


def test_batch_round_trip_keeps_only_successful_results():
    api = FakeApi()
    c = client(api)

    ids = c.submit_batch("/v1/ocr", "ocr-model", [("a", c.ocr_body(b"%PDF-a")), ("b", c.ocr_body(b"%PDF-b"))])
    job = c.wait_batch(ids["job"], poll_seconds=0, max_wait_seconds=60)
    results = c.batch_results(job)
    c.delete_files([ids["input_file"], job["output_file"], None])

    assert [line["custom_id"] for line in api.uploaded] == ["a", "b"]
    assert api.uploaded[0]["body"]["document"]["document_url"].startswith("data:application/pdf;base64,")
    assert list(results) == ["a"] and c.ocr_pages(results["a"])[0]["markdown"] == "A"
    assert api.calls[-2:] == ["DELETE /v1/files/file-in", "DELETE /v1/files/file-out"]


def test_rate_limit_is_retried():
    api = FakeApi(rate_limited_first=2)
    c = client(api)

    assert c.submit_batch("/v1/ocr", "ocr-model", [("a", c.ocr_body(b"%PDF"))])["job"] == "job-1"
    assert api.calls.count("POST /v1/files") == 3
