# scan-stack-splitter

[![Build and push image](https://github.com/Tom-Joad/scan-stack-splitter/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/Tom-Joad/scan-stack-splitter/actions/workflows/build-and-push.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Turn scanned paper into searchable PDFs, one per document, named after their
content. It runs as a Docker container that watches up to three folders.

**Stacks.** Drop a scan of a whole pile of paper, with hundreds of pages and
no separator sheets. The container finds where each document begins from the
content alone and splits the scan:

```
stacks/inbox/Household/stack-01.pdf  (500 pages)
        ↓
stacks/output/Household/Blood count 2026-09-30.pdf
stacks/output/Household/CT report chest 2026-09-30.pdf
stacks/output/Household/Electricity bill 2026-08-14.pdf
...
```

**Scanner.** Point a document scanner that saves to a network share at the
second inbox. Each file there is one document. It gets the same OCR and
naming, but is never split:

```
scanner/inbox/20261002_141503.pdf
        ↓
scanner/output/Insurance renewal notice 2026-09-28.pdf
```

**Paperless** (optional). Files dropped here only get the Tesseract text
layer and are then uploaded to [Paperless-ngx](https://docs.paperless-ngx.com/).
Paperless, or an AI tagger working with it such as
[Zettelrobbe](https://github.com/admonstrator/zettelrobbe), takes care of
the title, tags and correspondent:

```
paperless/inbox/scan.pdf  →  text layer  →  Paperless-ngx document #1234
```

Titles are written in the language of each document unless you set
`TITLE_LANGUAGE`.

Under the hood:

- [ocrmypdf](https://ocrmypdf.readthedocs.io/) with Tesseract adds a fresh,
  invisible text layer, so every output PDF is searchable.
- [Mistral OCR](https://docs.mistral.ai/capabilities/document_ai/basic_ocr/)
  reads stacks through Mistral's batch API, at half the regular price. Its
  structured text makes splitting more reliable. Scanner files are read from
  the Tesseract layer instead, which costs nothing and adds no waiting time.
- A Mistral chat model decides where documents begin and names them.

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Reviewing and correcting splits](#reviewing-and-correcting-splits)
- [Configuration](#configuration)
- [Queue webhook (Home Assistant)](#queue-webhook-home-assistant)
- [Spending limit and paused processing](#spending-limit-and-paused-processing)
- [Choosing the text source](#choosing-the-text-source)
- [Paperless-ngx input](#paperless-ngx-input)
- [Rate limits and cost](#rate-limits-and-cost)
- [Privacy](#privacy)
- [Unraid](#unraid)
- [Development](#development)

## Quick start

You need Docker and a [Mistral API key](https://console.mistral.ai/).

```bash
git clone https://github.com/Tom-Joad/scan-stack-splitter.git
cd scan-stack-splitter
cp .env.example .env        # set MISTRAL_API_KEY, and DATA_PATH, PUID, PGID if needed
docker compose up -d --build
docker compose logs -f
```

The prebuilt image `ghcr.io/tom-joad/scan-stack-splitter` can replace the
local build, see `docker-compose.yml`.

On first start, the container creates this layout under `DATA_PATH`:

```
stacks/     inbox/  output/  archive/  failed/
scanner/    inbox/  output/  archive/  failed/
paperless/  inbox/          archive/  failed/    (only with PAPERLESS_URL set)
work/
```

Put PDFs into `stacks/inbox/` or `scanner/inbox/`, optionally in
sub-folders. Sub-folders are mirrored in `output/`. A file is picked up once
it has not changed for `STABLE_SECONDS` (60 s by default), so copying a large
scan over the network is safe.

To process a single file once, without the watcher:

```bash
docker compose run --rm scan-stack-splitter process /data/some.pdf --profile stacks --folder "Household"
```

## How it works

1. **OCR** (stacks by default). Pages go to Mistral OCR in chunks of 50
   through the
   [batch API](https://docs.mistral.ai/capabilities/batch/). A job takes
   minutes instead of seconds and costs half as much. Job ids and results are
   stored as soon as they exist, so an interrupted run never pays for a page
   twice. Uploads and results are deleted from Mistral's file storage
   afterwards. A chunk that fails inside a batch is retried directly.
   `OCR_MODE=direct` skips the batch API.

   Scanner files skip this step by default. Their text comes from the
   Tesseract layer that step 5 adds anyway. Either input can be switched with
   `STACKS_TEXT_SOURCE` / `SCANNER_TEXT_SOURCE`; see
   [Choosing the text source](#choosing-the-text-source).
2. **Blank pages.** A page is dropped when its image shows almost no ink,
   typically the back of a duplex scan. The check uses the image itself, not
   the OCR text, because OCR models sometimes invent whole paragraphs on an
   empty page.
3. **Boundaries** (stacks only). A chat model reads overlapping windows of 12
   pages and decides, page by page, whether a new document starts. It looks at
   letterheads, salutations, headings, dates, layout changes and sentences
   that run across pages. Each page's verdict comes from the window where it
   had the most context on both sides. Explicit "page *k* of *n*" markers
   override the model. A second copy of a document becomes a file of its own,
   for example `... (2).pdf`.
4. **Naming.** Each document gets a short title of 1 to 6 words, naming its
   type and the detail that sets it apart, plus the date it is about. For
   reports and lab results, that is the examination or sampling date. For
   letters, it is the issue date.
5. **Text layer.** ocrmypdf adds Tesseract's text, choosing the mode per
   file:
   - **Pure scans** (no text at all): `--force-ocr`. Pages are deskewed, the
     image Tesseract reads is cleaned, and poor scans are upsampled to 300
     dpi.
   - **PDFs that already contain text**: `--redo-ocr`. This covers born-digital
     PDFs such as online bank statements, and scans that carry the scanner's
     own OCR. An old OCR layer is replaced, while real digital text stays as it
     is. Forcing OCR would turn such pages into pictures of themselves, many
     times the size.

   If a mode fails, a simpler one is tried before the file counts as failed.
   The last resort OCRs only pages without text and does no image processing.
   The log and `.error.txt` show ocrmypdf's actual error message. The image
   ships Tesseract's
   [`tessdata_best`](https://github.com/tesseract-ocr/tessdata_best) models
   for German and English. They are slower than the defaults, but hold up much
   better on poor scans.
6. **Output.** Each document's pages are cut from the searchable scan into
   `<input>/output/<sub-folder>/<title> <date>.pdf`. The PDF's title and
   subject metadata are set as well.

The original file then moves to `archive/`. A file that fails moves to
`failed/` together with an `.error.txt`. Move it back into `inbox/` to retry:
OCR that was already paid for is reused.

To check that a PDF really has a text layer, open it and search for a word
with Ctrl+F, or select the text. In the document's font list, the invisible
layer shows up as `GlyphLessFont`.

## Reviewing and correcting splits

Without separator sheets, splitting cannot be perfect. Every input file gets
a work directory, `work/<input>/<sub-folder>/<file name>-<hash>/`. It holds:

- `review.md`: every document with its page range. A document is flagged ⚠
  when it starts mid-document (for example on "page 3 of 5"), when it is a
  single, nearly empty page, or when the model was unsure.
- `decisions.json`: the model's verdict and reason for every page.
- `plan.json`: the split plan, meant to be edited by hand. Page numbers are
  1-based ranges such as `"4-6, 9"`.

To fix a split, edit `plan.json`: change `pages`, or merge and split entries.
Set `title` to `""` to have the title and date generated again. Then run:

```bash
docker compose exec scan-stack-splitter stacksplit rebuild "stacks/Household/stack-01-1a2b3c4d"
```

The files listed in `written_files` are replaced. Nothing is OCR'd again.

## Configuration

All settings are environment variables. [`.env.example`](.env.example) lists
them with comments.

**Mistral**

| Variable | Default | Purpose |
|---|---|---|
| `MISTRAL_API_KEY` | — | Required |
| `MISTRAL_LLM_MODEL` | `mistral-large-latest` | Model for splitting and naming |
| `MISTRAL_OCR_MODEL` | `mistral-ocr-latest` | OCR model |
| `MISTRAL_MAX_RPS` | `1` | Requests per second across all workers; set it below your account's limit |
| `OCR_MODE` | `batch` | `batch` (half price, minutes) or `direct` (full price, seconds) |
| `BATCH_POLL_SECONDS` | `15` | How often a running batch job is checked |
| `BATCH_MAX_WAIT_HOURS` | `24` | A job still running after this is cancelled; its chunks are retried directly |
| `PAUSE_RETRY_MINUTES` | `30` | Probe interval while paused, see [Spending limit](#spending-limit-and-paused-processing) |
| `MISTRAL_API_BASE` | `https://api.mistral.ai/v1` | API endpoint |
| `MISTRAL_TIMEOUT` | `300` | Seconds per request |

**Folders and inputs**

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `/data` | Parent of the default folders below |
| `STACKS_DIR` / `SCANNER_DIR` | `$DATA_DIR/stacks`, `$DATA_DIR/scanner` | Root of each input; `inbox/`, `output/`, `archive/` and `failed/` live below it |
| `STACKS_ENABLED` / `SCANNER_ENABLED` | `true` | Switch an input off |
| `STACKS_TEXT_SOURCE` / `SCANNER_TEXT_SOURCE` | `mistral` / `tesseract` | Text for splitting and naming: `mistral` (Mistral OCR) or `tesseract` (free, from the text layer). The scanner falls back to `mistral` when `OCRMYPDF_ENABLED=false` |
| `WORK_DIR` | `$DATA_DIR/work` | OCR results, plans and review files |
| `TMPDIR` | `/tmp` | Temporary page images; point it at a disk for large stacks |
| `POLL_INTERVAL` | `30` | Seconds between inbox checks |
| `STABLE_SECONDS` | `60` | A file must stay unchanged this long before it is picked up |

**Paperless-ngx input**

| Variable | Default | Purpose |
|---|---|---|
| `PAPERLESS_URL` | — | Base URL of Paperless-ngx, e.g. `http://paperless:8000`; setting it enables the input |
| `PAPERLESS_TOKEN` | — | API token of the Paperless user that should own the documents |
| `PAPERLESS_DIR` | `$DATA_DIR/paperless` | Root of the input; `inbox/`, `archive/` and `failed/` live below it |
| `PAPERLESS_TAGS` | — | Comma-separated tag ids to add on upload, e.g. `3,7` |
| `PAPERLESS_TEXT_SOURCE` | `tesseract` | `mistral` replaces the document's content in Paperless with Mistral OCR's text, tables included; see [below](#paperless-ngx-input) |
| `PAPERLESS_MAX_WAIT_MINUTES` | `30` | How long to wait for Paperless to consume a file before trying again later |

**Naming**

| Variable | Default | Purpose |
|---|---|---|
| `TITLE_LANGUAGE` | each document's language | For example `English` or `German` |
| `FILENAME_PATTERN` | `{title} {date}` | `{title}` is required; `{date}` is `YYYY-MM-DD` |
| `NO_DATE_LABEL` | `undated` | Replaces `{date}` when no date is found |

**Splitting and blank pages**

| Variable | Default | Purpose |
|---|---|---|
| `BOUNDARY_WINDOW` / `BOUNDARY_STEP` | `12` / `6` | Pages per model request, and how far each window moves on |
| `REVIEW_CONFIDENCE` | `0.75` | Below this model confidence, a document is flagged |
| `DROP_BLANK_PAGES` | `true` | `false` keeps blank pages with the document before them |
| `BLANK_MAX_INK_PERCENT` | `0.2` | Pages with less visible ink than this are blank |
| `BLANK_MAX_CHARS` | `15` | Pages with no image and at most this much text are blank |
| `METADATA_MAX_CHARS` | `24000` | Text per document sent for naming; longer documents are shortened in the middle |

**Text layer**

| Variable | Default | Purpose |
|---|---|---|
| `OCRMYPDF_ENABLED` | `true` | `false` keeps the scan's own text layer, if any |
| `OCRMYPDF_LANGUAGES` | `deu+eng` | Tesseract languages; only `deu` and `eng` ship as best models |
| `OCRMYPDF_JOBS` | number of CPUs | Parallel Tesseract jobs |
| `OCRMYPDF_EXTRA_ARGS` | — | Appended to the ocrmypdf call |

**Throughput, webhook and logging**

| Variable | Default | Purpose |
|---|---|---|
| `OCR_CHUNK_PAGES` | `50` | Pages per OCR request |
| `OCR_CONCURRENCY` / `LLM_CONCURRENCY` | `3` / `4` | Parallel requests; `MISTRAL_MAX_RPS` still applies |
| `QUEUE_WEBHOOK_URL` | — | Report queue counts here, see [below](#queue-webhook-home-assistant) |
| `QUEUE_WEBHOOK_CHECK_SECONDS` | `10` | How often the queue is counted |
| `QUEUE_WEBHOOK_HEARTBEAT_SECONDS` | `300` | Resend interval without changes |
| `LOG_LEVEL` | `INFO` | Logs are JSON lines on stdout |

## Queue webhook (Home Assistant)

With `QUEUE_WEBHOOK_URL` set, the container POSTs the queue state as JSON.
It sends whenever a number changes, and again every
`QUEUE_WEBHOOK_HEARTBEAT_SECONDS`, so the receiver catches up after a
restart. The payload holds counts only, never file names:

```json
{
  "queued": 3, "waiting": 2, "processing": 1, "failed": 0,
  "paused": false, "pause_reason": null, "paused_since": null,
  "profiles": {
    "stacks":  {"waiting": 1, "processing": 1, "failed": 0},
    "scanner": {"waiting": 1, "processing": 0, "failed": 0}
  }
}
```

- `queued` is `waiting + processing`.
- `failed` counts the PDFs in the `failed/` folders.
- `profiles` has one entry per enabled input; `paperless` appears only when
  `PAPERLESS_URL` is set.
- `paused`, `pause_reason` and `paused_since` are always present. The last
  two are `null` unless processing is paused. `pause_reason` is Mistral's
  raw error text, up to 300 characters. `paused_since` is an ISO 8601
  timestamp in UTC.

Every send and every failure appears in the container log, with the error
text but never the URL. An unchanged error is repeated at most once per
heartbeat interval. If the receiver is unreachable, processing carries on.

For Home Assistant, a trigger-based template entity reads the webhook. Pick a
long random `webhook_id`, because anyone who knows it can post to it:

```yaml
template:
  - triggers:
      - trigger: webhook
        webhook_id: scan-splitter-queue-CHANGE-ME
        allowed_methods: [POST]
        local_only: true
    sensor:
      - name: Scan-Splitter queue
        unique_id: scan_splitter_queue
        state: "{{ trigger.json.queued }}"
        unit_of_measurement: files
        attributes:
          waiting: "{{ trigger.json.waiting }}"
          processing: "{{ trigger.json.processing }}"
          stacks_waiting: "{{ trigger.json.profiles.stacks.waiting | default(0) }}"
          scanner_waiting: "{{ trigger.json.profiles.scanner.waiting | default(0) }}"
      - name: Scan-Splitter failed
        unique_id: scan_splitter_failed
        state: "{{ trigger.json.failed }}"
        unit_of_measurement: files
    binary_sensor:
      - name: Scan-Splitter paused
        unique_id: scan_splitter_paused
        state: "{{ trigger.json.paused | default(false) }}"
        attributes:
          reason: "{{ trigger.json.pause_reason }}"
          since: "{{ trigger.json.paused_since }}"
```

Then set
`QUEUE_WEBHOOK_URL=http://<home-assistant>:8123/api/webhook/scan-splitter-queue-CHANGE-ME`.

## Spending limit and paused processing

Mistral can refuse an account, for example because its spending limit is
reached, its quota is used up or its API key is rejected. In that case,
processing pauses instead of moving file after file to `failed/`.

A refusal is an HTTP 401, 402 or 403, or a 429 that is not the ordinary
per-second rate limit. Mistral does not document how a reached spending
limit is answered, so this errs on the side of pausing.

While paused:

- Files stay in their inboxes, including the one that hit the limit.
- The log shows `processing paused` with Mistral's error text, and the
  webhook reports `"paused": true`.
- Every `PAUSE_RETRY_MINUTES`, one file is tried as a probe. Once it goes
  through, processing resumes on its own and the log shows
  `processing resumed`. That happens, for example, after you raise the limit
  or a new billing month starts.

## Choosing the text source

Splitting and naming read the text of each page. Per input, it can come from
Mistral OCR (`mistral`) or from the Tesseract text layer (`tesseract`). The
Tesseract layer is added to every output PDF either way. By default, stacks
use `mistral` and scanner files use `tesseract`.

| | `mistral` | `tesseract` |
|---|---|---|
| Cost | about $1 per 500 pages (batch) | none |
| Extra waiting time | about a minute per batch job | none |
| Tables, headers, footers | structured; headers and footers separate | plain text |
| Poor scans, handwriting | expected to be better (not measured) | expected to be weaker |

A comparison on one test stack used the same text layer for both sources.
The stack held 80 real documents (156 pages of mostly clean office and
medical scans):

| | `mistral` | `tesseract` |
|---|---|---|
| Boundaries found | 77 of 80 | 75 of 80 |
| Clear misses | 0 | 2, both flagged for review (image-heavy pages with little text) |
| Same document type in the title | — | 64 of 81 documents |
| Same date | — | 73 of 81 documents; the differences favoured neither source |

The remaining misses of both runs were debatable cases. One example is two
X-ray views of the same examination that had been filed as two documents.

In short: Mistral OCR splits somewhat better. For naming alone, as with
scanner files, the difference was not measurable. That is why the defaults
are what they are. If your scanner files are handwritten or of poor quality,
`SCANNER_TEXT_SOURCE=mistral` may be worth the cost.

## Paperless-ngx input

Set `PAPERLESS_URL` and `PAPERLESS_TOKEN` to enable a third inbox,
`paperless/inbox/`. It is meant for documents that Paperless-ngx should name
and tag itself, for example with an AI tagger such as
[Zettelrobbe](https://github.com/admonstrator/zettelrobbe). For each file:

1. ocrmypdf adds the Tesseract text layer, with the same settings as for the
   other inputs. By default, no paid service is involved; see
   [Content from Mistral OCR](#content-from-mistral-ocr) for the option.
2. The PDF is uploaded through `POST /api/documents/post_document/`, keeping
   its file name and adding `PAPERLESS_TAGS`, if set.
3. The container follows Paperless's consumption task until a document has
   been created. Only then does the original move to `archive/`, and the work
   copy is deleted.

### Content from Mistral OCR

By default, the content field Paperless shows and searches comes from the
Tesseract text layer. In that text, tables lose their structure. Set
`PAPERLESS_TEXT_SOURCE=mistral`, and Mistral OCR reads each file before the
upload, through the batch API. Right after Paperless confirms the new
document, its content is replaced with Mistral's Markdown text. A lab report
then reads:

```
Laborbefund vom 30.09.2026

| Parameter | Ergebnis | Einheit | Referenz |
| --- | --- | --- | --- |
| Leukozyten | 6,2 | /nl | 3,9 - 10,2 |
```

The same page in Tesseract's text reads `Leukozyten 6,2 /nl 3,9 - 10,2`, one
line per row, with no columns.

- The PDF's own text layer stays Tesseract's, because only that one has word
  positions for search and selection in the file.
- Cost: Mistral OCR at batch price, about $1 per 500 pages. No chat model is
  involved.
- An AI tagger such as Zettelrobbe should see the better text. The content is
  replaced a few seconds after the document appears, and taggers usually poll
  less often. If yours reacts instantly, it may read Tesseract's text first.
- If replacing the content fails, the document stays in Paperless with
  Tesseract's text. A warning is logged, and the file is not uploaded again.
- A Mistral pause (see [Spending limit](#spending-limit-and-paused-processing))
  holds this input too, because it uses Mistral. With the default
  `tesseract`, the input keeps uploading during a pause.

Failure handling:

- **Paperless unreachable, or token rejected.** The file stays in the inbox,
  and the input retries after 5 minutes. A file that is still being consumed
  is not uploaded a second time after a restart: the task id is stored.
- **Paperless rejects the document**, for example as a duplicate. The file
  moves to `failed/` with Paperless's message in the `.error.txt`.
- **The same original dropped in twice.** The file moves to `failed/` and
  names the Paperless document it already became. Paperless's own duplicate
  check cannot catch this, because the text layer makes every upload a
  slightly different file. The container therefore keeps a register of
  uploaded originals in `work/paperless/uploaded.json`. Remove an entry there
  to upload that file again.

Paperless decides by itself whether to run its own OCR. With the default
`PAPERLESS_OCR_MODE=auto`, it keeps the text layer added here. With `redo`
or `force`, it replaces it.

Sub-folders in `paperless/inbox/` are allowed and mirrored in `archive/`, but
Paperless itself doesn't see them. Use `PAPERLESS_TAGS`, or Paperless
workflows, to sort documents.

Tested with Paperless-ngx 3.2. The task format of version 2 is supported as
well.

## Rate limits and cost

Mistral limits requests per second and tokens per minute. The limits depend
on the model and the account tier, and they are not published. Look yours up
in Mistral's console under **API › Limits** and set `MISTRAL_MAX_RPS` a
little below the requests-per-second limit of your `MISTRAL_LLM_MODEL`. If a
request still runs into the limit, all workers pause together and retry.

Splitting needs one request per 6 pages, and naming one request per document.
A 500-page stack holding about 200 documents takes roughly 280 requests,
which is about 20 minutes at 0.25 requests per second. A scanner file needs
a single request and, with the default text source, no OCR. Every answer is
cached in the work directory.

At the prices published in October 2026, a 500-page stack costs about
US$1.60: about $1.00 for batch OCR and about $0.60 for the chat model.
Check [Mistral's pricing](https://docs.mistral.ai/inference/pricing) for
current figures.

## Privacy

Every page is sent to Mistral's API, first for OCR, then for splitting and
naming. Only use this for documents you are entitled to process that way. If
the documents belong to someone else, get their consent first, especially for
health or financial records.

- The work directories hold the full OCR text of every input file. There is
  no automatic cleanup, so delete a file's work directory once its documents
  are fine.
- Logs contain file names, page numbers and counts, never document text.
- The Paperless input keeps no copy once a document is confirmed in
  Paperless. Only the register of uploaded originals remains: checksum, file
  name, document id and date.
- The queue webhook sends counts only.

## Unraid

A Docker template and step-by-step instructions are in
[`unraid/`](unraid/README-UNRAID.md).

## Development

There is no local Python setup to maintain: the image contains everything,
and the tests run inside it.

```bash
docker build -t scan-stack-splitter:dev .
docker run --rm --user root --entrypoint sh -v "$PWD:/src" -w /src scan-stack-splitter:dev \
  -c "pip install -q pytest && python -m pytest -q"
```

The tests replace Mistral with a fake and need no API key. CI runs them
together with `pip-audit` and `gitleaks` on every push. Images are built only
for version tags (`v*`), for `linux/amd64` and `linux/arm64`. Each image is
signed with cosign and ships an SBOM and provenance.

See [CHANGELOG.md](CHANGELOG.md) for the release history and
[SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Disclaimer

This is an independent project, not affiliated with or endorsed by Mistral
AI or any scanner manufacturer. Product names belong to their owners.

Splitting and naming are automated and can be wrong. Check `review.md` and
the results before relying on them, for example before discarding paper
originals. The software comes without warranty; see the [license](LICENSE).

## License

[MIT](LICENSE)
