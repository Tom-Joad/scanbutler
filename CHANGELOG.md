# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). From 1.0.0 on, a change that
needs action when upgrading (a renamed or removed setting, a different folder
layout, a changed webhook payload) only comes with a new major version.

## [1.3.0] - 2026-10-04

### Added
- **Shared Paperless tags.** With `PAPERLESS_SHARE_TAGS=true`, every tag
  that has an owner loses it, checked every `PAPERLESS_SHARE_TAGS_MINUTES`
  (default 1). On an instance with several users, everyone then sees the
  tags that others or an AI tagger created. Tags named in
  `PAPERLESS_SHARE_TAGS_READONLY` keep their owner and become visible, but
  not changeable, for everyone else. Needs a token that may change other
  users' tags.
- **A second Paperless input.** `PAPERLESS_2_TOKEN` enables
  `paperless-2/inbox/`, which uploads with that token, so its documents
  belong to a second Paperless user. By default it uploads to the same
  instance; `PAPERLESS_2_URL`, `PAPERLESS_2_DIR`, `PAPERLESS_2_TAGS` and
  `PAPERLESS_2_TEXT_SOURCE` work as for the first input. The queue webhook
  reports it as `paperless-2`, and `scanbutler process --profile paperless-2`
  works.

### Changed
- A rejected Paperless token names the setting to check
  (`PAPERLESS_TOKEN` or `PAPERLESS_2_TOKEN`).

## [1.2.0] - 2026-10-03

### Added
- **More OCR languages.** Every language in `OCRMYPDF_LANGUAGES` that isn't
  built in (`deu`, `eng`, `osd`) is downloaded at startup from
  tessdata_best `4.1.0`, checked against SHA-256 checksums shipped in the
  image, and kept in `/config/tessdata`, so it is downloaded only once and
  survives image updates. Example: `OCRMYPDF_LANGUAGES=deu+eng+fra`.
- A misspelt language code stops the container with a hint, e.g.
  `unknown language 'ger' (did you mean 'deu'?)`.
- Without internet access, a language that can't be downloaded is left out
  with a warning instead of stopping the container.
- The log reports the languages in use (`languages ready`), and which were
  downloaded, cached or left out.
- `TESSDATA_URL` points the download at a mirror.
- The Unraid template shows `OCRMYPDF_LANGUAGES`.

## [1.1.0] - 2026-10-03

### Added
- **`WORK_RETENTION_DAYS`** (default 30): the work folder of a processed
  file is deleted that many days after it was finished. Each one holds a
  searchable copy of its input and the full OCR text, so without cleanup the
  work folder grew as fast as the archive. Until then, `rebuild` can re-cut
  the file; a `rebuild` restarts the count. Folders of failed or interrupted
  files, a file being processed, the Paperless upload ledger and temporary
  files are never touched. `0` keeps everything. The cleanup runs at start
  and every six hours, and logs `work folders cleaned` with the space freed.

## [1.0.0] - 2026-10-03

First stable release. Settings, folder layout, file naming, `plan.json` and
the webhook payload are now stable, see the versioning note above. Install
it fresh from the Unraid template or `docker-compose.yml`.

### Highlights
- Three inputs: **stacks** split large scans into documents by content,
  **scanner** names and OCRs single files, **Paperless** adds the text layer
  and uploads to Paperless-ngx.
- Scanner and Paperless files go ahead of stacks, for OCR jobs and for
  Mistral requests; OCR adapts to the container's memory and CPU limits.
- **linuxserver.io conventions:** built on their Debian 13 base with
  s6-overlay; `PUID`, `PGID`, `UMASK` and `TZ`; the work folder at `/config`,
  temporary page images in `/config/tmp`; `docker exec ... scanbutler` runs
  as the `abc` user; docker mods work as usual.
- If the input folders can't be created (wrong `PUID`/`PGID`), the log says
  so with the IDs in use, and the watcher retries every minute.
- Issue templates for bug reports and feature requests. They ask for log
  lines and settings, never for documents.
- The Unraid template carries its `TemplateURL`, so Unraid picks up template
  changes.

### Security
- The base image is pinned by digest and gets Debian's latest security
  updates at build time. The final image contains no pip. At release, no
  known vulnerability in the image had a fix available.
- Ghostscript downsampling has the same time limit as ocrmypdf
  (`OCRMYPDF_FILE_TIMEOUT_MINUTES`).
- Invisible Unicode format characters (such as U+202E, right-to-left
  override) are removed from generated file names, so a name can't display
  differently from what it is.
- Request URLs, including the webhook URL, never reach the log, also with
  `LOG_LEVEL=DEBUG`.
- Images for `linux/amd64` and `linux/arm64`, signed with cosign, with SBOM
  and provenance. GitHub Actions are pinned to commit SHAs.

## 0.x pre-releases

Versions before 1.0 were pre-releases under the name `scan-stack-splitter`.
They are listed for reference only.

## [0.12.0] - 2026-10-03

### Changed
- **Scanner and Paperless files go ahead of stacks.** The OCR jobs are one
  shared budget without fixed shares: a file gets one job per page, as many
  as are free, and waiting scanner and Paperless files are served first.
  Before, they had one job each, however many were idle.
- Stacks get their text layer in pieces of 20 pages, so a scan waits for one
  piece at most while a long stack runs. Finished pieces survive a restart.
- Requests to Mistral for scanner and Paperless files take the next free
  slot of `MISTRAL_MAX_RPS`, ahead of waiting stack requests.
- The scanner and Paperless inputs work on several files at once. The queue
  webhook's `processing` can therefore be more than 1.
- The startup log shows `ocr_priority` instead of the fixed `ocr_jobs` shares.

### Fixed
- A PDF still being written is no longer processed and moved to `failed/`.
  A ScanSnap pausing for more than `STABLE_SECONDS` while writing a stack
  left a cut-off file, which failed with `InputFileError`. Now a file is
  only picked up once it ends in `%%EOF`, and a file that changes while it
  is processed stays in the inbox for the next round. A PDF that stays
  incomplete for 10 minutes is processed anyway and fails with a clear
  message.

### Docs
- A note on Fujitsu/Ricoh ScanSnap scanners: scanning straight to a network
  folder, they save image-only PDFs, and the scanner input makes them
  searchable without a PC.

## [0.11.1] - 2026-10-03

### Fixed
- `OCRMYPDF_JOBS=auto` now respects Docker's `--cpus` and `--cpuset-cpus`
  limits. Before, it counted every core of the host. A container limited to
  2 CPUs on a 12-core host would have started up to 12 parallel OCR pages.
  The startup log now also shows the CPUs it found.

### Docs
- Sizing rule: full speed needs about 1 GB + 0.75 GB × CPU cores. More memory
  brings no further gain.

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

[1.2.0]: https://github.com/Tom-Joad/scanbutler/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Tom-Joad/scanbutler/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Tom-Joad/scanbutler/releases/tag/v1.0.0
[0.12.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/Tom-Joad/scanbutler/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/Tom-Joad/scanbutler/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Tom-Joad/scanbutler/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Tom-Joad/scanbutler/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Tom-Joad/scanbutler/releases/tag/v0.1.0
