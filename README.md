# scan-stack-splitter

Drop a scanned stack of paper — hundreds of pages, no separator sheets — into a
folder and get back one searchable PDF per document, named after its content:

```
stacks/inbox/Patient A/stack-01.pdf  (500 pages)
        ↓
stacks/output/Patient A/Blutbild 2026-09-30.pdf
stacks/output/Patient A/Befundbericht CT Thorax 2026-09-30.pdf
stacks/output/Patient A/Arztbrief Kardiologie 2026-08-14.pdf
...
```

A second input, `scanner/`, is meant for a document scanner that saves
straight to a network share. There, each file is one document. It gets the
same fresh OCR and content-based name, but is never split:

```
scanner/inbox/20261002_141503.pdf
        ↓
scanner/output/Rechnung Stadtwerke 2026-09-28.pdf
```

Both inputs have their own `inbox/`, `output/`, `archive/` and `failed/`
folders and their own worker. A scan is processed right away, even while a
large stack is still running. Either input can be switched off
(`STACKS_ENABLED`, `SCANNER_ENABLED`).

Document boundaries are found from the content alone. It uses
[Mistral OCR](https://docs.mistral.ai/capabilities/document_ai/basic_ocr/) for
reading and a Mistral chat model for splitting and naming. The searchable text
layer comes from a fresh Tesseract pass via [ocrmypdf](https://ocrmypdf.readthedocs.io/).

## How it works

1. **OCR**: The stack is sent to Mistral OCR in chunks of 50 pages through the
   [batch API](https://docs.mistral.ai/capabilities/batch/). That costs half
   the regular price, and a job takes minutes instead of seconds. Job ids and
   each chunk's result are stored as soon as they exist, so an interrupted run
   never pays for the same page twice. Uploads and results are deleted from
   Mistral's file storage afterwards. A chunk that fails in the batch is
   retried directly. `OCR_MODE=direct` skips the batch API.
2. **Blank pages**: Pages are dropped when their image shows almost no ink,
   typically duplex back sides. The measurement is on the scan itself, not on
   the OCR text: OCR models occasionally hallucinate whole paragraphs on an
   empty page.
3. **Boundaries** (stacks only): A chat model reads overlapping windows of 12 pages. It decides
   for each page whether that page starts a new document, using letterheads,
   salutations, headings, dates, layout changes and text that continues across
   pages. Each page's verdict comes from the window where it had the most
   context. Explicit "page *k* of *n*" markers override the model. A second
   copy of a document is kept as its own file (it shows up as `... (2).pdf`).
4. **Naming**: Each document gets a short topic title in the document's language
   (configurable) and the date it is about: the examination or sampling date for
   reports, otherwise the issue date.
5. **Text layer**: ocrmypdf replaces any existing text layer (`--force-ocr`).
   Before recognition it deskews pages, cleans the image Tesseract sees and
   upsamples it to 300 dpi. The image uses Tesseract's
   [`tessdata_best`](https://github.com/tesseract-ocr/tessdata_best) models for
   German and English. This is slower than the defaults but holds up much better
   on poor scans.
6. **Output**: The pages of each document are cut from the searchable stack into
   `<input>/output/<inbox sub-folder>/<title> <date>.pdf`. The PDF title and subject
   metadata are set too.

The original stack is then moved to `archive/`. A stack that fails is moved to
`failed/` together with an `.error.txt`. To retry, move it back into the inbox:
cached OCR is reused.

## Reviewing and correcting splits

Without separator sheets, splitting cannot be perfect. Every stack gets a work
directory, `work/<input>/<sub-folder>/<stack name>-<hash>/`, containing:

- `review.md`: every document with its page range and confidence. Documents
  are flagged ⚠ when they start in the middle ("page 3 of 5"), consist of a
  single nearly empty page, or the model was unsure.
- `decisions.json`: the model's verdict and reason for every page.
- `plan.json`: the split plan, meant to be edited by hand. Page numbers are
  1-based ranges such as `"4-6, 9"`.

To fix a split, edit `plan.json`. Change `pages`, merge entries or split them.
Set `title` to `""` to have the title and date generated again. Then run:

```bash
docker compose exec scan-stack-splitter stacksplit rebuild "stacks/Patient A/stack-01-1a2b3c4d"
```

The files listed in `written_files` are replaced. Nothing is OCR'd again.

## Setup

```bash
cp .env.example .env    # set MISTRAL_API_KEY, DATA_PATH, PUID/PGID
docker compose up -d --build
docker compose logs -f
```

Then put PDFs into `DATA_PATH/stacks/inbox/` or `DATA_PATH/scanner/inbox/`,
optionally in sub-folders. A file is
picked up once it has not changed for `STABLE_SECONDS`, so copying a large
scan over the network is safe.

On Unraid, use the template in [`unraid/`](unraid/README-UNRAID.md) instead.

One-off processing without the watcher:

```bash
docker compose run --rm scan-stack-splitter process /data/some.pdf --folder "Patient A" --profile stacks
```

All settings are environment variables. See [`.env.example`](.env.example).
The ones you are most likely to change:

| Variable | Default | Purpose |
|---|---|---|
| `MISTRAL_API_KEY` | — | Required |
| `MISTRAL_LLM_MODEL` | `mistral-large-latest` | Model for splitting and naming |
| `MISTRAL_MAX_RPS` | `1` | Requests per second; set to your account's limit |
| `OCR_MODE` | `batch` | `batch` (half price) or `direct` (immediate) |
| `STACKS_DIR` / `SCANNER_DIR` | `/data/stacks`, `/data/scanner` | Root of each input |
| `STACKS_ENABLED` / `SCANNER_ENABLED` | `true` | Switch an input off |
| `QUEUE_WEBHOOK_URL` | — | Report queue counts here, see below |
| `TITLE_LANGUAGE` | language of the document | e.g. `German` |
| `FILENAME_PATTERN` | `{title} {date}` | `{date}` is `YYYY-MM-DD` |
| `NO_DATE_LABEL` | `undated` | Used when no date is found |
| `REVIEW_CONFIDENCE` | `0.75` | Below this, a split is flagged |
| `OCRMYPDF_LANGUAGES` | `deu+eng` | Only `deu`, `eng` ship as best models |
| `OCRMYPDF_EXTRA_ARGS` | — | Appended to the ocrmypdf call |

## Queue webhook (e.g. Home Assistant)

Set `QUEUE_WEBHOOK_URL` and the container POSTs the queue state as JSON. It
sends a report whenever a count changes and repeats it every
`QUEUE_WEBHOOK_HEARTBEAT_SECONDS` (default 300), so the receiver catches up
after a restart. The payload holds counts only, never file names:

```json
{
  "queued": 3, "waiting": 2, "processing": 1, "failed": 0,
  "profiles": {
    "stacks":  {"waiting": 1, "processing": 1, "failed": 0},
    "scanner": {"waiting": 1, "processing": 0, "failed": 0}
  }
}
```

`queued` is `waiting + processing`. `failed` counts the PDFs in the `failed/`
folders. If the receiver is unreachable, a warning is logged and processing
carries on.

For Home Assistant, add a trigger-based template sensor. Use a long random
`webhook_id`: anyone who knows it can post to the webhook.

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
```

Then set
`QUEUE_WEBHOOK_URL=http://<home-assistant>:8123/api/webhook/scan-splitter-queue-CHANGE-ME`.

## Rate limits

Mistral limits requests per second and tokens per minute, per model and per
account tier. The values are not published. Look them up in Mistral's admin
panel under **API › Limits** and set `MISTRAL_MAX_RPS` slightly below the
requests-per-second limit of your `MISTRAL_LLM_MODEL`. Should a request still
hit the limit, all workers pause together and retry.

The number of requests depends on the content of the stack. Splitting takes
one request per 6 pages. Naming takes one request per document found. A
500-page stack with around 200 documents therefore needs about 280 requests,
which is roughly 20 minutes at 0.25 requests per second. Every answer is
cached in the work directory, so an interrupted run resumes without asking
again.

## Privacy

Every page is sent to Mistral's API: OCR, then splitting and naming. Only use
this for documents you are entitled to process that way. If the documents
belong to someone else, get their consent first. That matters especially for
health data.

The work directories hold the full OCR text of every stack. Delete them once
you are happy with the result. Logs contain file names, page numbers and
counts, but never document text.

## Development

```bash
docker build -t scan-stack-splitter:dev .
docker run --rm --user root --entrypoint sh -v "$PWD:/src" -w /src scan-stack-splitter:dev \
  -c "pip install -q pytest && python -m pytest -q"
```

The tests replace Mistral with a fake and need no API key.

## License

MIT
