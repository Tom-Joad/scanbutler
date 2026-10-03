# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/); while the major version is 0,
minor versions may change behaviour or settings.

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

[0.8.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Tom-Joad/scan-stack-splitter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Tom-Joad/scan-stack-splitter/releases/tag/v0.1.0
