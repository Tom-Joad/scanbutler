"""HTTP API: other programs hand in a file and fetch a searchable PDF.

POST /api/v1/jobs takes a PDF or an image and answers at once with a job id;
the caller asks for the status, or names a `callback_url` that is told when
the job is done, and then downloads result.pdf and result.json. Each job
lives in its own folder under WORK_DIR/api/jobs, so queued jobs survive a
restart. The text layer comes from Tesseract and Mistral OCR together (see
hybrid.py), with Mistral's direct API: a caller is waiting.
"""

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageOps

from . import __version__, logging_setup, pdfops, priority
from .config import Settings
from .ocr_plugin import HYBRID_DIR
from .tokens import TokenStore

log = logging.getLogger(__name__)

PLUGIN = "--plugin scanbutler.ocr_plugin"
STATES = ("queued", "running", "done", "failed")
_ID = re.compile(r"^[0-9a-f]{32}$")
# File types by their first bytes; everything else is refused.
_MAGIC = [
    (b"%PDF-", "pdf"),
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"II*\x00", "tif"),
    (b"MM\x00*", "tif"),
]
CALLBACK_DELAYS = (2, 5)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sniff(head: bytes) -> str | None:
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def image_to_pdf(src: Path, dst: Path) -> None:
    """A PDF of the image's pages, upright, at its stated or an A4-like resolution."""
    with Image.open(src) as image:
        frames = []
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            frame = ImageOps.exif_transpose(image.copy())
            frames.append(frame.convert("L" if frame.mode in ("1", "L", "LA", "I;16") else "RGB"))
        dpi = image.info.get("dpi")
    if dpi and dpi[0] >= 72:
        resolution = float(dpi[0])
    else:
        # Phone photos say nothing useful; take the long side as 11.7 in (A4).
        longest = max(frames[0].size)
        resolution = min(600.0, max(150.0, longest / 11.7))
    tmp = dst.with_name(dst.name + ".tmp")
    frames[0].save(tmp, "PDF", resolution=resolution, save_all=True, append_images=frames[1:])
    tmp.replace(dst)


class Jobs:
    """The job folders: job.json with the state, the input, the results."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self.wake = threading.Event()

    def folder(self, job_id: str) -> Path:
        if not _ID.match(job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def read(self, job_id: str) -> dict:
        path = self.folder(job_id) / "job.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(job_id) from None

    def write(self, job: dict) -> None:
        path = self.folder(job["id"]) / "job.json"
        tmp = path.with_name("job.json.tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def update(self, job_id: str, **changes) -> dict:
        with self._lock:
            job = self.read(job_id)
            job.update(changes)
            self.write(job)
            return job

    def create(self, owner: str, filename: str, kind: str, upload: Path, callback_url: str) -> dict:
        job_id = secrets.token_hex(16)
        folder = self.folder(job_id)
        folder.mkdir(parents=True)
        upload.replace(folder / f"input.{kind}")
        job = {
            "id": job_id,
            "owner": owner,
            "status": "queued",
            "filename": filename,
            "kind": kind,
            "callback_url": callback_url,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "pages": None,
            "error": None,
        }
        self.write(job)
        self.wake.set()
        return job

    def all(self) -> list[dict]:
        jobs = []
        if not self.root.exists():
            return jobs
        for folder in self.root.iterdir():
            try:
                jobs.append(self.read(folder.name))
            except (KeyError, ValueError, OSError):
                continue
        return sorted(jobs, key=lambda j: j["created_at"])

    def delete(self, job_id: str) -> None:
        shutil.rmtree(self.folder(job_id), ignore_errors=True)


def public(job: dict) -> dict:
    """What the caller sees of a job: no owner, no callback URL."""
    view = {k: job.get(k) for k in ("id", "status", "filename", "created_at", "started_at", "finished_at", "pages", "error")}
    base = f"/api/v1/jobs/{job['id']}"
    view["links"] = {"self": base}
    if job["status"] == "done":
        view["links"].update({"pdf": f"{base}/result.pdf", "json": f"{base}/result.json"})
    return view


def _result_json(job: dict, folder: Path, pdf: Path) -> dict:
    """Per page: the PDF's text layer, and Mistral's markdown where Mistral read the page."""
    mistral = folder / "mistral"
    texts = pdfops.page_texts(pdf)
    pages = []
    for number, text in enumerate(texts, start=1):
        entry = {"page": number, "text": text, "markdown": None, "blocks_from_mistral": 0}
        record = mistral / f"page-{number:04d}.json"
        if record.exists():
            info = json.loads(record.read_text(encoding="utf-8"))
            reading = mistral / f"{info['image']}.json"
            if reading.exists():
                entry["markdown"] = json.loads(reading.read_text(encoding="utf-8")).get("markdown")
            entry["blocks_from_mistral"] = info.get("replaced", 0)
        pages.append(entry)
    return {"id": job["id"], "filename": job["filename"], "pages": pages}


def process(job: dict, folder: Path, settings: Settings) -> int:
    """Make result.pdf and result.json for one job; returns the page count."""
    source = folder / f"input.{job['kind']}"
    pdf = source if job["kind"] == "pdf" else folder / "input.pdf"
    if job["kind"] != "pdf" and not pdf.exists():
        image_to_pdf(source, pdf)
    mistral = folder / "mistral"
    mistral.mkdir(exist_ok=True)
    result = folder / "result.pdf"
    env = {**os.environ, HYBRID_DIR: str(mistral)}
    pdfops.make_searchable(
        pdf,
        result,
        settings.ocrmypdf_languages,
        f"{settings.ocrmypdf_extra_args} {PLUGIN}".strip(),
        settings.ocr_limits,
        priority=True,
        env=env,
    )
    data = _result_json(job, folder, result)
    tmp = folder / "result.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "result.json")
    return len(data["pages"])


def notify(job: dict, post=httpx.post, sleep=time.sleep) -> None:
    """Tell the caller's callback URL that the job finished; never fails the job."""
    url = job.get("callback_url")
    if not url:
        return
    body = {
        "id": job["id"],
        "status": "ok" if job["status"] == "done" else "failed",
        "message": job.get("error") or "",
        "links": public(job)["links"],
    }
    for attempt, delay in enumerate((*CALLBACK_DELAYS, None), start=1):
        try:
            response = post(url, json=body, timeout=10)
            if response.status_code < 400:
                log.info("callback sent", extra={"job": job["id"], "http": response.status_code})
                return
            reason = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
        if delay is None:
            log.warning("callback failed", extra={"job": job["id"], "reason": reason, "attempts": attempt})
            return
        sleep(delay)


class Worker:
    """Runs queued jobs one after the other, and removes old ones."""

    def __init__(self, jobs: Jobs, settings: Settings, stop: threading.Event, run=process, notify=notify) -> None:
        self.jobs = jobs
        self.settings = settings
        self.stop = stop
        self._run = run
        self._notify = notify

    def recover(self) -> None:
        """A job that was running when the container stopped starts again."""
        for job in self.jobs.all():
            if job["status"] == "running":
                self.jobs.update(job["id"], status="queued", started_at=None)
                log.info("api job requeued", extra={"job": job["id"]})

    def clean(self, now: datetime | None = None) -> None:
        keep = timedelta(hours=self.settings.api_result_hours)
        now = now or datetime.now(timezone.utc)
        for job in self.jobs.all():
            finished = job.get("finished_at")
            if finished and now - datetime.fromisoformat(finished.replace("Z", "+00:00")) > keep:
                self.jobs.delete(job["id"])
                log.info("api job removed", extra={"job": job["id"], "reason": "expired"})

    def run_one(self) -> bool:
        queued = [j for j in self.jobs.all() if j["status"] == "queued"]
        if not queued:
            return False
        job = self.jobs.update(queued[0]["id"], status="running", started_at=_now())
        started = time.monotonic()
        log.info("api job started", extra={"job": job["id"], "owner": job["owner"], "kind": job["kind"]})
        try:
            with logging_setup.working_on(f"api/{job['id']}"), priority.marked(True):
                pages = self._run(job, self.jobs.folder(job["id"]), self.settings)
        except Exception as exc:  # noqa: BLE001 - the job fails, the worker goes on
            job = self.jobs.update(job["id"], status="failed", finished_at=_now(), error=str(exc)[:1000])
            log.exception("api job failed", extra={"job": job["id"]})
        else:
            job = self.jobs.update(job["id"], status="done", finished_at=_now(), pages=pages)
            log.info(
                "api job done",
                extra={"job": job["id"], "pages": pages, "seconds": round(time.monotonic() - started, 1)},
            )
        # A slow or dead callback URL must not hold up the next job.
        threading.Thread(target=self._notify, args=(job,), name="api-callback", daemon=True).start()
        return True

    def run(self) -> None:
        self.recover()
        next_clean = 0.0
        while not self.stop.is_set():
            if time.monotonic() >= next_clean:
                next_clean = time.monotonic() + 600
                try:
                    self.clean()
                except Exception:  # noqa: BLE001
                    log.exception("api job cleanup failed")
            try:
                if self.run_one():
                    continue
            except Exception:  # noqa: BLE001 - e.g. the work folder went away for a moment
                log.exception("api worker failed")
            self.jobs.wake.wait(30)
            self.jobs.wake.clear()


def create_app(settings: Settings, jobs: Jobs, tokens: TokenStore) -> FastAPI:
    app = FastAPI(title="Scanbutler", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    limit = settings.api_max_upload_mb * 2**20

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def caller(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        name = tokens.check(token.strip()) if scheme.lower() == "bearer" else None
        if name is None:
            raise HTTPException(401, "a valid API token is needed", headers={"WWW-Authenticate": "Bearer"})
        return name

    Caller = Annotated[str, Depends(caller)]

    def own(job_id: str, name: str) -> dict:
        try:
            job = jobs.read(job_id)
        except KeyError:
            raise HTTPException(404, "no such job") from None
        if job["owner"] != name:
            raise HTTPException(404, "no such job")
        return job

    @app.post("/api/v1/jobs", status_code=202)
    async def submit(
        name: Caller,
        file: Annotated[UploadFile, File()],
        callback_url: Annotated[str, Form()] = "",
    ):
        callback_url = callback_url.strip()
        if callback_url and not re.match(r"^https?://[^\s/]+\S*$", callback_url):
            raise HTTPException(422, "callback_url must be an http or https URL")
        jobs.root.mkdir(parents=True, exist_ok=True)
        upload = jobs.root / f".upload-{secrets.token_hex(8)}"
        size, head = 0, b""
        try:
            with upload.open("wb") as out:
                while chunk := await file.read(1 << 20):
                    if not head:
                        head = chunk[:16]
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"the file is larger than {settings.api_max_upload_mb} MB")
                    out.write(chunk)
            kind = sniff(head)
            if kind is None:
                raise HTTPException(415, "send a PDF, JPEG, PNG, TIFF or WebP file")
            filename = Path(file.filename or f"upload.{kind}").name[:200]
            job = jobs.create(name, filename, kind, upload, callback_url)
        finally:
            upload.unlink(missing_ok=True)
        log.info("api job queued", extra={"job": job["id"], "owner": name, "kind": kind, "bytes": size})
        return JSONResponse(public(job), status_code=202, headers={"Location": f"/api/v1/jobs/{job['id']}"})

    @app.get("/api/v1/jobs")
    def list_jobs(name: Caller):
        return {"jobs": [public(j) for j in jobs.all() if j["owner"] == name]}

    @app.get("/api/v1/jobs/{job_id}")
    def status(job_id: str, name: Caller):
        return public(own(job_id, name))

    def result(job_id: str, name: str, file: str) -> Path:
        job = own(job_id, name)
        if job["status"] != "done":
            raise HTTPException(409, f"the job is {job['status']}")
        return jobs.folder(job_id) / file

    @app.get("/api/v1/jobs/{job_id}/result.pdf")
    def result_pdf(job_id: str, name: Caller):
        job = own(job_id, name)
        stem = Path(job["filename"]).stem or "result"
        return FileResponse(result(job_id, name, "result.pdf"), media_type="application/pdf", filename=f"{stem}.pdf")

    @app.get("/api/v1/jobs/{job_id}/result.json")
    def result_json(job_id: str, name: Caller):
        return FileResponse(result(job_id, name, "result.json"), media_type="application/json")

    @app.delete("/api/v1/jobs/{job_id}", status_code=204)
    def delete(job_id: str, name: Caller):
        job = own(job_id, name)
        if job["status"] == "running":
            raise HTTPException(409, "the job is running; delete it when it is done")
        jobs.delete(job_id)
        log.info("api job removed", extra={"job": job_id, "reason": "deleted"})
        return Response(status_code=204)

    return app


def serve(settings: Settings, stop: threading.Event) -> None:
    """Start the API server and the job worker in background threads."""
    import uvicorn

    root = settings.work_dir / "api"
    jobs = Jobs(root / "jobs")
    tokens = TokenStore(root / "tokens.json")
    if not tokens.tokens():
        log.warning("api has no token yet; create one with: scanbutler token add NAME")
    threading.Thread(target=Worker(jobs, settings, stop).run, name="api-jobs", daemon=True).start()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, jobs, tokens),
            host="0.0.0.0",
            port=settings.api_port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
        )
    )
    threading.Thread(target=server.run, name="api", daemon=True).start()

    def shutdown() -> None:
        stop.wait()
        server.should_exit = True

    threading.Thread(target=shutdown, name="api-stop", daemon=True).start()
    log.info("api listening", extra={"port": settings.api_port})
