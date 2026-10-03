# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/); while the major version is 0,
minor versions may change behaviour or settings.

## [0.11.0] - 2026-10-03

### Added
- `OCRMYPDF_MAX_IMAGE_DPI` (default 600): images sharper than this are
  downsampled with Ghostscript before OCR. ocrmypdf rasterizes a page at its
  sharpest image's resolution. A 1550-dpi logo made a 230-megapixel page.
  Now the page stays at about 35 megapixels, and even the full scan mode
  handles it.
- `OCRMYPDF_JOBS=auto` (new default): the number of parallel OCR pages
  follows the container's memory limit: 1 GB base plus 0.75 GB per page, at
  most one per CPU core. All inputs share one job budget, and a run that
  doesn't fit waits. Less memory means slower processing, not failure.
- README section *System requirements*: 1 GB minimum, 4 GB recommended,
  with measurements and notes for Synology and similar NAS systems.

### Changed
- The Unraid template limits the container to 4 GB (`--memory=4g`).
  `docker-compose.yml` sets `mem_limit: 4g` and keeps temporary files on
  disk instead of in a RAM-backed `/tmp`.

## [0.10.0] - 2026-10-03

### Added
- Limits for the text layer, so one odd page can't exhaust the server:
  - `OCRMYPDF_MAX_OCR_MPIXELS` (default 50): images sent to Tesseract are
    capped at this size.
  - `OCRMYPDF_PAGE_TIMEOUT` (300 s): time limit per page.
  - `OCRMYPDF_FILE_TIMEOUT_MINUTES` (120): time limit per ocrmypdf run.
  - `OCRMYPDF_SKIP_BIG_MPIXELS` (200): applies in the last-resort mode only.

  In a 2 GB container, an A4 page holding a 1550-dpi colour image now falls
  back from scan to redo mode and still gets its text. Before, it failed
  with the same error as on the NAS.

## [0.9.1] - 2026-10-03

### Fixed
- A born-digital bank statement made unpaper run out of memory. ocrmypdf
  had rasterized a page at about 1550 dpi because of a high-resolution logo,
  and the kernel killed unpaper. Two changes follow from it:
  - Tagged PDFs, which carry a structure tree, are now kept as they are and
    not re-OCR'd at all.
  - `--redo-ocr` no longer cleans images; cleaning stays with pure scans.

## [0.9.0] - 2026-10-03

### Changed
- The text layer mode now depends on the file. Pure scans keep the full
  treatment: forced OCR, deskew, clean, 300 dpi. PDFs that already contain
  text use `--redo-ocr`, which replaces old OCR but keeps real digital text.
  Born-digital PDFs, such as bank statements, are no longer rasterized. A test
  file shrank instead of growing many times over.

### Fixed
- When a text layer mode fails, simpler modes are tried before the file
  counts as failed.
- ocrmypdf's actual error message now reaches the log and `.error.txt`.
  Before, it was suppressed by `--quiet`, so a failure showed only as
  `SubprocessOutputError`.

## [0.8.0] - 2026-10-03

### Added
- `PAPERLESS_TEXT_SOURCE=mistral`: the Paperless input reads each file with
  Mistral OCR before the upload. Right after the document is created, its
  content in Paperless is replaced with Mistral's Markdown text, so tables
  keep their structure. The PDF's text layer stays Tesseract's. The default
  stays `tesseract`.

### Changed
- The Paperless input obeys the Mistral pause only when it uses Mistral.

## [0.7.0] - 2026-10-03

### Added
- Optional Paperless-ngx input (`PAPERLESS_URL`, `PAPERLESS_TOKEN`,
  `PAPERLESS_DIR`, `PAPERLESS_TAGS`, `PAPERLESS_MAX_WAIT_MINUTES`). Files in
  `paperless/inbox/` get the Tesseract text layer and are uploaded to
  Paperless. Paperless, or an AI tagger, names and tags them.
  - A file counts as done only once Paperless confirms the new document.
  - If Paperless is unreachable, files wait in the inbox.
  - A rejected document goes to `failed/` with Paperless's message.
  - A register of uploaded originals stops the same scan from being uploaded
    twice.
  - Supports the task formats of Paperless-ngx 2 and 3.
- `stacksplit process --profile paperless`.
- The Unraid template has a Paperless path and settings.

## [0.6.0] - 2026-10-02

### Added
- `STACKS_TEXT_SOURCE` and `SCANNER_TEXT_SOURCE` (`mistral` or `tesseract`).
  With `tesseract`, splitting and naming read the Tesseract text layer
  instead of Mistral OCR: no OCR cost and no batch waiting time. The README
  compares both on a test stack.

### Changed
- Scanner files now use `tesseract` by default. In the comparison, naming
  was no worse, and scans reach the output about a minute sooner at no OCR
  cost. If `OCRMYPDF_ENABLED=false`, the scanner falls back to `mistral`.
  Stacks keep `mistral`, which split noticeably better.

## [0.5.0] - 2026-10-02

First public release.

### Added
- `stacksplit --version`; the version is also logged at startup.
- `CHANGELOG.md`, `SECURITY.md` and Dependabot configuration.

### Changed
- Documentation rewritten for publication: quick start, full configuration
  reference, cost and privacy notes, disclaimer.
- The Unraid template and guide describe updating an existing container.

## [0.4.0] - 2026-10-02

### Added
- Processing pauses when Mistral refuses the account, for example because the
  spending limit is reached or the key is rejected (HTTP 401/402/403, or a
  429 that is not the ordinary rate limit). Files stay in their inboxes, and
  one file is retried as a probe every `PAUSE_RETRY_MINUTES` (new setting,
  default 30).
- The queue webhook payload always carries `paused`, `pause_reason` and
  `paused_since`.

## [0.3.1] - 2026-10-02

### Changed
- Every webhook send and failure is logged. Errors are logged with their
  text, but never with the webhook URL, whose id is the only secret. An
  unchanged error is repeated at most once per heartbeat interval.

## [0.3.0] - 2026-10-02

### Added
- Queue webhook (`QUEUE_WEBHOOK_URL`). It sends waiting, processing and
  failed counts, in total and per input, on every change plus a heartbeat.
  The README includes a Home Assistant sensor setup.

## [0.2.0] - 2026-10-02

### Added
- Scanner input: files from a document scanner get OCR and naming, one
  document per file, with no splitting.

### Changed
- Inputs now live in `stacks/` and `scanner/`, each with its own `inbox/`,
  `output/`, `archive/` and `failed/` folders and its own worker. On Unraid,
  the single Documents path is replaced by the Stacks and Scanner paths.

## [0.1.0] - 2026-10-02

### Added
- Splitting of scanned stacks into documents by content, using Mistral OCR
  and a Mistral chat model.
- Naming by topic and date.
- A fresh Tesseract text layer with `tessdata_best` models.
- Batch OCR at half price.
- Blank-page detection from ink coverage.
- Review files and hand-editable split plans with `stacksplit rebuild`.
- Request throttling and an Unraid template.

[0.11.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Tom-Joad/scan-stack-splitter/releases/tag/v0.1.0
