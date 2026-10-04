"""One scanned stack in, a folder of named, searchable documents out.

Every expensive step leaves its result in the stack's work directory (OCR
answers, the searchable PDF, the plan), so a crashed or repeated run picks up
where it stopped instead of paying again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path, PurePosixPath

from . import boundaries, metadata, pdfops, priority, retention
from .config import Profile, Settings
from .naming import build_stem, unique_path
from .llm_cache import CachedChat
from .ocr import BatchOptions, Page, run_ocr
from .paperless import PaperlessError, PaperlessUnavailable, task_document_id, task_message, task_status
from .plan import Plan, PlannedDocument, format_pages, parse_pages

log = logging.getLogger(__name__)

SEARCHABLE = "searchable.pdf"
PLAN = "plan.json"
REVIEW = "review.md"
INK = "ink.json"
DECISIONS = "decisions.json"
LLM_CACHE = "llm"
_IMAGE_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")
UPLOAD = "paperless_task.json"
# sha256 of every original handed to Paperless -> document id and date.
LEDGER = "uploaded.json"
# Stacks get their text layer in pieces of this many pages, so a scan waits
# for one piece at most before its own OCR starts.
CHUNK_PAGES = 20
_OUTPUT_LOCK = threading.Lock()
_LEDGER_LOCK = threading.Lock()
_UPLOADING: set[str] = set()


@dataclass
class Segment:
    pages: list[Page]
    start: boundaries.Decision
    weakest_join: boundaries.Decision | None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def work_dir_for(settings: Settings, profile: Profile, folder: PurePosixPath, src: Path, digest: str) -> Path:
    # The hash suffix keeps two different scans with the same file name apart.
    return settings.work_dir / profile.name / folder / f"{src.stem}-{digest[:8]}"


def group_segments(pages: list[Page], decisions: list[boundaries.Decision]) -> list[Segment]:
    segments: list[Segment] = []
    for page, decision in zip(pages, decisions):
        if decision.starts_new or not segments:
            segments.append(Segment([page], decision, None))
            continue
        segment = segments[-1]
        segment.pages.append(page)
        if segment.weakest_join is None or decision.confidence < segment.weakest_join.confidence:
            segment.weakest_join = decision
    return segments


NEARLY_EMPTY_CHARS = 60


def _review_reason(segment: Segment, threshold: float) -> str:
    reasons = []
    first = segment.pages[0]
    marker = boundaries.page_marker(first)
    if marker and marker[0] > 1:
        reasons.append(f"starts with page {marker[0]} of {marker[1]}; earlier pages missing or misplaced")
    if len(segment.pages) == 1 and first.text_chars() < NEARLY_EMPTY_CHARS:
        reasons.append(f"single, nearly empty page {first.number}")
    if segment.start.confidence < threshold:
        reasons.append(
            f"uncertain split before page {segment.start.page_index + 1} "
            f"({segment.start.confidence:.2f}: {segment.start.reason})"
        )
    join = segment.weakest_join
    if join is not None and join.confidence < threshold:
        reasons.append(f"uncertain continuation at page {join.page_index + 1} ({join.confidence:.2f}: {join.reason})")
    return "; ".join(reasons)


def build_plan(
    pages: list[Page],
    settings: Settings,
    backend,
    source: str,
    digest: str,
    folder: PurePosixPath,
    decisions_path: Path | None = None,
    split: bool = True,
) -> Plan:
    def is_blank(page: Page) -> bool:
        return page.is_blank(settings.blank_max_chars, settings.blank_max_ink)

    blank = [p for p in pages if is_blank(p)]
    content = [p for p in pages if not is_blank(p)]
    if not content:
        raise RuntimeError("every page of the stack is blank")
    log.info("blank pages detected", extra={"blank": len(blank), "content": len(content)})

    if split:
        decisions = boundaries.detect_boundaries(
            content, backend, settings.boundary_window, settings.boundary_step, settings.llm_concurrency
        )
    else:
        # A scanner file is one document by definition; no model is asked.
        decisions = [
            boundaries.Decision(p.index, i == 0, 1.0, "single document per file") for i, p in enumerate(content)
        ]
    if decisions_path is not None:
        # Per-page verdicts with reasons: the first thing to read when a split is wrong.
        decisions_path.write_text(
            json.dumps(
                [
                    {"page": d.page_index + 1, "starts_new": d.starts_new, "confidence": d.confidence, "reason": d.reason}
                    for d in decisions
                ],
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    segments = group_segments(content, decisions)

    if not settings.drop_blank_pages:
        # Keep each blank page with the document it was scanned after.
        for page in blank:
            owner = next((s for s in reversed(segments) if s.pages[0].index < page.index), segments[0])
            owner.pages.append(page)
            owner.pages.sort(key=lambda p: p.index)

    log.info("naming documents", extra={"documents": len(segments)})
    with ThreadPoolExecutor(max_workers=settings.llm_concurrency) as pool:
        described = list(
            pool.map(
                priority.keep(lambda s: metadata.describe(
                    [p for p in s.pages if not is_blank(p)] or s.pages,
                    backend,
                    settings.title_language,
                    settings.metadata_max_chars,
                )),
                segments,
            )
        )

    documents = []
    for segment, meta in zip(segments, described):
        reason = _review_reason(segment, settings.review_confidence)
        if not meta.title:
            reason = "; ".join(filter(None, [reason, "no title found"]))
        confidence = min(
            segment.start.confidence,
            segment.weakest_join.confidence if segment.weakest_join else 1.0,
        )
        documents.append(
            PlannedDocument(
                pages=format_pages([p.index for p in segment.pages]),
                title=meta.title,
                date=meta.date,
                issuer=meta.issuer,
                summary=meta.summary,
                confidence=round(confidence, 2),
                needs_review=bool(reason),
                review_reason=reason,
            )
        )

    return Plan(
        source=source,
        source_sha256=digest,
        page_count=len(pages),
        folder=str(folder),
        documents=documents,
        dropped_pages=format_pages([p.index for p in blank]) if settings.drop_blank_pages else "",
    )


def _retitle(plan: Plan, work: Path, settings: Settings, backend) -> None:
    """Re-generate title/date for documents whose title was cleared by hand."""
    missing = [doc for doc in plan.documents if not doc.title.strip()]
    if not missing:
        return
    if backend is None:
        raise RuntimeError("documents without a title need the Mistral API, but no API key is configured")
    # Mistral OCR chunks are cached already, so this costs no OCR calls.
    pages = read_text(work / SEARCHABLE, work, settings, backend, plan.text_source)
    chat = CachedChat(backend, work / LLM_CACHE)
    for doc in missing:
        selected = [pages[i] for i in parse_pages(doc.pages, plan.page_count)]
        meta = metadata.describe(selected, chat, settings.title_language, settings.metadata_max_chars)
        doc.title, doc.date, doc.issuer, doc.summary = meta.title, meta.date, meta.issuer, meta.summary


def write_outputs(plan: Plan, work: Path, settings: Settings) -> list[Path]:
    # Several scans may finish at once; two "Invoice 2026-10-01" must not
    # both pick the same free name.
    with _OUTPUT_LOCK:
        return _write_outputs(plan, work, settings)


def _write_outputs(plan: Plan, work: Path, settings: Settings) -> list[Path]:
    output = settings.profile(plan.profile).output
    out_dir = output / plan.folder
    out_root = output.resolve()
    # plan.json is edited by hand; never let its folder point outside the output tree.
    if not out_dir.resolve().is_relative_to(out_root):
        raise ValueError(f"plan folder {plan.folder!r} points outside {output}")

    # Validate everything before touching a single file.
    page_lists = [parse_pages(doc.pages, plan.page_count) for doc in plan.documents]

    # A rebuild replaces what the previous run of this plan wrote.
    for name in plan.written_files:
        old = (output / name).resolve()
        if old.is_relative_to(out_root):
            old.unlink(missing_ok=True)
    plan.written_files = []
    plan.save(work / PLAN)

    written: list[Path] = []
    for doc, indices in zip(plan.documents, page_lists):
        stem = build_stem(settings.filename_pattern, doc.title, doc.date, settings.no_date_label)
        target = unique_path(out_dir, stem, taken=set(written))
        pdfops.write_document(work / SEARCHABLE, indices, target, doc.title or stem, doc.summary)
        written.append(target)
        plan.written_files.append(target.relative_to(output).as_posix())
        # Saved per file so a crash mid-way still knows what to clean up.
        plan.save(work / PLAN)

    write_review(plan, work)
    return written


def write_review(plan: Plan, work: Path) -> None:
    flagged = sum(doc.needs_review for doc in plan.documents)
    lines = [
        f"# Review: {plan.source}",
        "",
        f"{plan.page_count} pages, {len(plan.documents)} documents, {flagged} flagged for review.",
    ]
    if plan.dropped_pages:
        lines.append(f"Blank pages dropped: {plan.dropped_pages}")
    lines += [
        "",
        "| # | Pages | File | Confidence | Review |",
        "|---|---|---|---|---|",
    ]
    for number, (doc, name) in enumerate(zip(plan.documents, plan.written_files), start=1):
        flag = f"⚠ {doc.review_reason}" if doc.needs_review else ""
        lines.append(f"| {number} | {doc.pages} | {PurePosixPath(name).name} | {doc.confidence:.2f} | {flag} |")
    lines += [
        "",
        "## Correcting a split",
        "",
        f"1. Edit `{PLAN}` in this folder: change `pages` of the affected documents,",
        "   split or merge entries. Clear `title` to have title and date re-generated.",
        "2. Run `scanbutler rebuild \"<this folder>\"` inside the container.",
        "   The files listed in `written_files` are replaced; nothing is OCR'd again.",
        "",
    ]
    (work / REVIEW).write_text("\n".join(lines), encoding="utf-8")


def read_text(src: Path, work: Path, settings: Settings, backend, text_source: str) -> list[Page]:
    """Per-page text for splitting and naming, from the configured source."""
    if text_source == "tesseract":
        # Free and immediate, but plain text: no tables, no separate running
        # header/footer. Page markers are still found at the page edges.
        texts = pdfops.page_texts(work / SEARCHABLE)
        # Tesseract can't tell a photo or an X-ray from an empty page; both
        # yield no text. has_images=True leaves the blank decision to the
        # page's ink coverage alone, so image-only pages are never dropped.
        return [Page(index=i, markdown=text, header="", footer="", has_images=True) for i, text in enumerate(texts)]
    batch = (
        BatchOptions(settings.batch_poll_seconds, settings.batch_max_wait_hours * 3600)
        if settings.ocr_mode == "batch"
        else None
    )
    return run_ocr(src, work / "ocr", backend, settings.ocr_chunk_pages, settings.ocr_concurrency, batch)


def process_stack(src: Path, folder: PurePosixPath, settings: Settings, backend, profile: Profile) -> Path:
    """Run the whole pipeline for one input file. Returns its work directory."""
    digest = sha256(src)
    work = work_dir_for(settings, profile, folder, src, digest)
    with retention.in_use(work):
        return _process_stack(src, folder, settings, backend, profile, digest, work)


def _process_stack(
    src: Path, folder: PurePosixPath, settings: Settings, backend, profile: Profile, digest: str, work: Path
) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    source = (folder / src.name).as_posix()
    log.info("stack started", extra={"profile": profile.name, "source": source, "work_dir": str(work)})

    # The text layer comes first: with text_source "tesseract" it is also
    # where the text for splitting and naming is read from.
    searchable = work / SEARCHABLE
    if not searchable.exists():
        if settings.ocrmypdf_enabled:
            pdfops.make_searchable(
                src,
                searchable,
                settings.ocrmypdf_languages,
                settings.ocrmypdf_extra_args,
                settings.ocr_limits,
                priority=profile.priority,
                chunk_pages=0 if profile.priority else CHUNK_PAGES,
            )
        else:
            shutil.copyfile(src, searchable)

    pages = read_text(src, work, settings, backend, profile.text_source)
    if pdfops.page_count(searchable) != len(pages):
        raise RuntimeError("searchable PDF and OCR result disagree on the page count")

    ink_path = work / INK
    if ink_path.exists():
        ink = json.loads(ink_path.read_text(encoding="utf-8"))
    else:
        ink = pdfops.ink_coverage(src)
        ink_path.write_text(json.dumps(ink), encoding="utf-8")
    if len(ink) != len(pages):
        raise RuntimeError("ink measurement and OCR result disagree on the page count")
    pages = [replace(page, ink=value) for page, value in zip(pages, ink)]

    plan_path = work / PLAN
    if plan_path.exists():
        plan = Plan.load(plan_path)
        log.info("existing plan reused", extra={"source": source})
    else:
        chat = CachedChat(backend, work / LLM_CACHE)
        plan = build_plan(pages, settings, chat, source, digest, folder, work / DECISIONS, split=profile.split)
        plan.profile = profile.name
        plan.text_source = profile.text_source
        plan.save(plan_path)

    written = write_outputs(plan, work, settings)
    log.info(
        "stack done",
        extra={
            "profile": profile.name,
            "source": source,
            "documents": len(written),
            "needs_review": sum(d.needs_review for d in plan.documents),
            "review_file": str(work / REVIEW),
        },
    )
    return work


def rebuild(work: Path, settings: Settings, backend) -> list[Path]:
    """Re-cut a stack from its (hand-edited) plan without any new OCR."""
    plan_path = work / PLAN
    if not plan_path.exists():
        raise FileNotFoundError(f"no {PLAN} in {work}")
    plan = Plan.load(plan_path)
    _retitle(plan, work, settings, backend)
    written = write_outputs(plan, work, settings)
    log.info("stack rebuilt", extra={"source": plan.source, "documents": len(written)})
    return written


def paperless_content(pages: list[Page]) -> str:
    """Mistral OCR pages as one text for Paperless: Markdown, tables kept, images left out."""
    blocks = []
    for page in pages:
        parts = [page.header, _IMAGE_REF.sub("", page.markdown), page.footer]
        text = "\n\n".join(part.strip() for part in parts if part and part.strip())
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def process_for_paperless(
    src: Path, folder: PurePosixPath, settings: Settings, client, profile: Profile, backend=None
) -> int | None:
    """Add the Tesseract text layer and hand the file to Paperless-ngx.

    With text_source "mistral", Mistral OCR reads the file before the upload,
    and its text replaces the document's content in Paperless right after
    the document is created: tables and layout survive far better than in
    Tesseract's plain text. The PDF's own text layer stays Tesseract's,
    because only that one has word positions.

    Returns the new Paperless document id. The task id is stored right after
    the upload, so a restart waits for that task instead of uploading the
    file a second time.
    """
    digest = sha256(src)
    ledger = settings.work_dir / profile.name / LEDGER
    with _LEDGER_LOCK:
        uploaded = _read_ledger(ledger)
        if digest in uploaded:
            # Paperless's own duplicate check compares the uploaded file, and
            # ocrmypdf writes a slightly different PDF every run, so it can't
            # catch a scan that is dropped in twice. This check compares originals.
            raise PaperlessError(
                f"this exact file was already uploaded as Paperless document {uploaded[digest]['document']} "
                f"on {uploaded[digest]['date']}; remove its entry from {LEDGER} to upload it again"
            )
        if digest in _UPLOADING:
            raise AlreadyInProgress("an identical file is being uploaded right now")
        _UPLOADING.add(digest)
    try:
        return _upload(src, folder, settings, client, profile, backend, digest, ledger)
    finally:
        with _LEDGER_LOCK:
            _UPLOADING.discard(digest)


class AlreadyInProgress(Exception):
    """An identical file is being worked on; this one waits for the next round."""


def _read_ledger(ledger: Path) -> dict:
    return json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}


def _upload(src, folder, settings, client, profile, backend, digest: str, ledger: Path) -> int | None:
    source = (folder / src.name).as_posix()
    work = work_dir_for(settings, profile, folder, src, digest)
    work.mkdir(parents=True, exist_ok=True)
    log.info("paperless started", extra={"source": source})

    searchable = work / SEARCHABLE
    if not searchable.exists():
        if settings.ocrmypdf_enabled:
            pdfops.make_searchable(
                src,
                searchable,
                settings.ocrmypdf_languages,
                settings.ocrmypdf_extra_args,
                settings.ocr_limits,
                priority=profile.priority,
                chunk_pages=0 if profile.priority else CHUNK_PAGES,
            )
        else:
            shutil.copyfile(src, searchable)

    content = None
    if profile.text_source == "mistral":
        # Fetched before the upload, so the content can be replaced within
        # seconds of the document appearing: before a tagger watching
        # Paperless is likely to read it.
        content = paperless_content(read_text(src, work, settings, backend, "mistral"))

    state = work / UPLOAD
    if state.exists():
        task_id = json.loads(state.read_text(encoding="utf-8"))["task_id"]
        log.info("paperless upload resumed", extra={"source": source, "task": task_id})
    else:
        task_id = client.upload(searchable, src.name, list(profile.paperless.tags))
        state.write_text(json.dumps({"task_id": task_id}), encoding="utf-8")
        log.info("paperless uploaded", extra={"source": source, "task": task_id})

    task = client.wait(task_id, 5, settings.paperless_max_wait_minutes * 60)
    if task is None:
        # Still queued in Paperless. Leave everything in place; the next try
        # resumes waiting for the same task.
        raise PaperlessUnavailable(f"consumption of task {task_id} not finished yet")
    if task_status(task) != "SUCCESS":
        # A later retry (e.g. after deleting a duplicate) must upload again.
        state.unlink(missing_ok=True)
        raise PaperlessError(f"Paperless did not consume the file: {task_message(task)}")

    document = task_document_id(task)
    log.info("paperless document created", extra={"source": source, "document": document})
    if content is not None and document is not None:
        try:
            client.set_content(document, content)
            log.info("paperless content replaced", extra={"document": document, "chars": len(content)})
        except (PaperlessError, PaperlessUnavailable) as exc:
            # The document exists either way; uploading again would only
            # create a duplicate. Keep Tesseract's content and say so.
            log.warning("paperless content not replaced", extra={"document": document, "error": str(exc)[:300]})
    with _LEDGER_LOCK:  # re-read: other uploads may have finished meanwhile
        uploaded = _read_ledger(ledger)
        uploaded[digest] = {"document": document, "file": source, "date": date.today().isoformat()}
        tmp = ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(uploaded, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(ledger)
    # Nothing left to review: the work copy would only duplicate the document.
    shutil.rmtree(work, ignore_errors=True)
    return document
