"""HTTP API: other programs hand in a file and fetch a searchable PDF.

POST /api/v1/jobs takes a PDF or an image as the request body and answers at once with a job id;
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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from PIL import Image, ImageOps

from . import __version__, logging_setup, pdfops, priority
from .config import Settings
from .fileio import write_json
from .ocr_plugin import HYBRID_DIR, HYBRID_RATE
from .tokens import TokensUnreadable, TokenStore

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
LEFTOVER_SECONDS = 3600
MAX_PAGE_INCHES = 16.5


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
    longest = max(frames[0].size)
    stated = float(dpi[0]) if dpi and dpi[0] else 0.0
    # A scanner states its resolution; phones and cameras write 72 dpi or
    # nothing, which would make a 4000-pixel photo a 55-inch page that OCR
    # reads as tiny print. Trust the value only if it gives a page up to A3
    # (16.5 in); otherwise take the long side as A4 (11.7 in).
    if stated >= 100 and longest / stated <= MAX_PAGE_INCHES:
        resolution = stated
    else:
        resolution = min(600.0, max(150.0, longest / 11.7))
    tmp = dst.with_name(dst.name + ".tmp")
    frames[0].save(tmp, "PDF", resolution=resolution, save_all=True, append_images=frames[1:])
    tmp.replace(dst)


class Jobs:
    """The job folders: job.json with the state, the input, the results."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._unreadable: set[str] = set()
        self.wake = threading.Event()

    def folder(self, job_id: str) -> Path:
        if not _ID.match(job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def read(self, job_id: str) -> dict:
        """The job; KeyError if it is gone or its job.json can't be read."""
        path = self.folder(job_id) / "job.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(job_id) from None
        except (OSError, ValueError) as exc:
            # Cut off by something outside this program. Said once; cleanup
            # removes the folder like one without a job.json (leftovers).
            if job_id not in self._unreadable:
                self._unreadable.add(job_id)
                log.error("api job unreadable", extra={"job": job_id, "error": f"{type(exc).__name__}: {exc}"[:200]})
            raise KeyError(job_id) from None

    def write(self, job: dict) -> None:
        path = self.folder(job["id"]) / "job.json"
        write_json(path, job, ensure_ascii=False)

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

    def claim(self, job_id: str) -> dict | None:
        """Mark a queued job running; None if it was deleted or taken meanwhile."""
        with self._lock:
            try:
                job = self.read(job_id)
            except KeyError:
                return None
            if job["status"] != "queued":
                return None
            job.update(status="running", started_at=_now())
            self.write(job)
            return job

    def delete_unless_running(self, job_id: str) -> bool:
        """Delete a job unless the worker has it; under the lock the worker claims with."""
        with self._lock:
            if self.read(job_id)["status"] == "running":
                return False
            self.delete(job_id)
            return True

    @staticmethod
    def _readable(folder: Path) -> bool:
        try:
            json.loads((folder / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return True

    def leftovers(self, older_than: float) -> list[Path]:
        """Uploads and job folders a crash left half made (no readable job.json), older than `older_than` (epoch)."""
        found = []
        if not self.root.exists():
            return found
        for path in self.root.iterdir():
            try:
                if path.stat().st_mtime >= older_than:
                    continue
            except OSError:
                continue
            if path.name.startswith(".upload-") or (path.is_dir() and not self._readable(path)):
                found.append(path)
        return found


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


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
        entry = {"page": number, "text": text, "markdown": None, "lines_from_mistral": 0}
        record = mistral / f"page-{number:04d}.json"
        if record.exists():
            info = json.loads(record.read_text(encoding="utf-8"))
            reading = mistral / f"{info['image']}.json"
            if reading.exists():
                entry["markdown"] = json.loads(reading.read_text(encoding="utf-8")).get("markdown")
            entry["lines_from_mistral"] = info.get("lines_replaced", 0) + info.get("lines_added", 0)
        pages.append(entry)
    return {"id": job["id"], "filename": job["filename"], "pages": pages}


def mistral_errors(mistral: Path) -> list[tuple[int, str]]:
    """Pages Mistral couldn't read in this run, from the plugin's page records."""
    errors = []
    for record in sorted(mistral.glob("page-*.json")):
        info = json.loads(record.read_text(encoding="utf-8"))
        if info.get("error"):
            errors.append((info["page"], info["error"]))
    return errors


def process(job: dict, folder: Path, settings: Settings) -> int:
    """Make result.pdf and result.json for one job; returns the page count."""
    source = folder / f"input.{job['kind']}"
    pdf = source if job["kind"] == "pdf" else folder / "input.pdf"
    if job["kind"] != "pdf" and not pdf.exists():
        image_to_pdf(source, pdf)
    mistral = folder / "mistral"
    mistral.mkdir(exist_ok=True)
    for record in mistral.glob("page-*.json"):  # from an attempt before a restart
        record.unlink()
    result = folder / "result.pdf"
    env = {
        **os.environ,
        HYBRID_DIR: str(mistral),
        HYBRID_RATE: str(settings.work_dir / "api" / "mistral.rate"),
        # The validated values, not whatever the container's environment holds:
        # an empty MISTRAL_TIMEOUT would crash the plugin on every page, and
        # an untrimmed key would reach the Authorization header as it is.
        "MISTRAL_API_KEY": settings.api_key,
        "MISTRAL_API_BASE": settings.api_base,
        "MISTRAL_OCR_MODEL": settings.ocr_model,
        "MISTRAL_TIMEOUT": str(settings.request_timeout),
        "MISTRAL_MAX_RPS": str(settings.max_rps),
    }
    pdfops.make_searchable(
        pdf,
        result,
        settings.ocrmypdf_languages,
        f"{settings.ocrmypdf_extra_args} {PLUGIN}".strip(),
        settings.ocr_limits,
        priority=True,
        env=env,
    )
    failed = mistral_errors(mistral)
    if failed:
        raise RuntimeError(f"Mistral OCR failed on page {failed[0][0]}: {failed[0][1]}")
    data = _result_json(job, folder, result)
    write_json(folder / "result.json", data, ensure_ascii=False)
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
                freed = _size(self.jobs.folder(job["id"]))
                self.jobs.delete(job["id"])
                log.info("api job removed", extra={"job": job["id"], "reason": "expired", "freed_mb": round(freed / 2**20, 1)})
        # A crash during an upload, or between creating a job's folder and
        # its job.json, leaves files nothing else would ever remove.
        for path in self.jobs.leftovers(now.timestamp() - LEFTOVER_SECONDS):
            freed = _size(path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            log.info("api leftover removed", extra={"name": path.name, "freed_mb": round(freed / 2**20, 1)})

    def run_one(self) -> bool:
        queued = [j for j in self.jobs.all() if j["status"] == "queued"]
        if not queued:
            return False
        job = self.jobs.claim(queued[0]["id"])
        if job is None:  # deleted by its program just now
            return True
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
        try:
            name = tokens.check(token.strip()) if scheme.lower() == "bearer" else None
        except TokensUnreadable:
            raise HTTPException(503, "the API's token file can't be read; see the log") from None
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
    async def submit(request: Request, name: Caller, filename: str = "", callback_url: str = ""):
        # The file is the raw body, read only after the token was checked
        # (the dependency above), and cut off at the limit while streaming.
        callback_url = callback_url.strip()
        if callback_url and not re.match(r"^https?://[^\s/]+\S*$", callback_url):
            raise HTTPException(422, "callback_url must be an http or https URL")
        declared = request.headers.get("content-length")
        if declared is not None:
            if not declared.isdigit():
                raise HTTPException(400, "invalid Content-Length")
            if int(declared) > limit:
                raise HTTPException(413, f"the file is larger than {settings.api_max_upload_mb} MB")
        # File work runs in a thread: a slow disk must not hold up the event
        # loop, and with it every other request.
        await run_in_threadpool(jobs.root.mkdir, parents=True, exist_ok=True)
        upload = jobs.root / f".upload-{secrets.token_hex(8)}"
        size, head = 0, b""
        try:
            out = await run_in_threadpool(upload.open, "wb")
            try:
                async for chunk in request.stream():
                    if len(head) < 16:
                        head += chunk[: 16 - len(head)]
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"the file is larger than {settings.api_max_upload_mb} MB")
                    await run_in_threadpool(out.write, chunk)
            finally:
                await run_in_threadpool(out.close)
            kind = sniff(head)
            if kind is None:
                raise HTTPException(415, "send a PDF, JPEG, PNG, TIFF or WebP file")
            filename = Path(filename.strip() or f"upload.{kind}").name[:200] or f"upload.{kind}"
            job = await run_in_threadpool(jobs.create, name, filename, kind, upload, callback_url)
        finally:
            await run_in_threadpool(upload.unlink, missing_ok=True)
        log.info("api job queued", extra={"job": job["id"], "owner": name, "kind": kind, "bytes": size})
        return JSONResponse(public(job), status_code=202, headers={"Location": f"/api/v1/jobs/{job['id']}"})

    @app.get("/api/v1/health")
    def health():
        # No token: Docker's health check calls it. Says nothing but "up".
        return {"status": "ok"}

    @app.get("/api/v1/jobs")
    def list_jobs(name: Caller):
        return {"jobs": [public(j) for j in jobs.all() if j["owner"] == name]}

    @app.get("/api/v1/jobs/{job_id}")
    def status(job_id: str, name: Caller):
        return public(own(job_id, name))

    def result(job_id: str, name: str) -> dict:
        job = own(job_id, name)
        if job["status"] != "done":
            raise HTTPException(409, f"the job is {job['status']}")
        return job

    @app.get("/api/v1/jobs/{job_id}/result.pdf")
    def result_pdf(job_id: str, name: Caller):
        job = result(job_id, name)
        stem = Path(job["filename"]).stem or "result"
        return FileResponse(jobs.folder(job_id) / "result.pdf", media_type="application/pdf", filename=f"{stem}.pdf")

    @app.get("/api/v1/jobs/{job_id}/result.json")
    def result_json(job_id: str, name: Caller):
        result(job_id, name)
        return FileResponse(jobs.folder(job_id) / "result.json", media_type="application/json")

    @app.delete("/api/v1/jobs/{job_id}", status_code=204)
    def delete(job_id: str, name: Caller):
        own(job_id, name)
        try:
            deleted = jobs.delete_unless_running(job_id)
        except KeyError:  # the cleanup removed it between the two reads
            raise HTTPException(404, "no such job") from None
        if not deleted:
            raise HTTPException(409, "the job is running; delete it when it is done")
        log.info("api job removed", extra={"job": job_id, "reason": "deleted"})
        return Response(status_code=204)

    return app


def serve(settings: Settings, stop: threading.Event) -> list[threading.Thread]:
    """Start the API server and the job worker; returns their threads.

    The caller watches them: uvicorn ends its thread when it can't start
    (the port is taken), and the container must not look healthy then.
    """
    import uvicorn

    root = settings.work_dir / "api"
    jobs = Jobs(root / "jobs")
    tokens = TokenStore(root / "tokens.json")
    try:
        if not tokens.tokens():
            log.warning("api has no token yet; create one with: scanbutler token add NAME")
    except TokensUnreadable:
        pass  # logged by the store; every request answers 503 until the file is fixed
    worker = threading.Thread(target=Worker(jobs, settings, stop).run, name="api-jobs", daemon=True)
    worker.start()
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
    http = threading.Thread(target=server.run, name="api", daemon=True)
    http.start()

    def shutdown() -> None:
        stop.wait()
        server.should_exit = True

    threading.Thread(target=shutdown, name="api-stop", daemon=True).start()
    log.info("api listening", extra={"port": settings.api_port})
    return [http, worker]
