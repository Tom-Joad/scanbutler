# Running Scanbutler on Unraid

The container runs permanently and watches two inboxes. It has no web
interface; everything it does shows up in the container log.

## Install

1. **Add the template.** Copy `scanbutler.xml` to
   `/boot/config/plugins/dockerMan/templates-user/` on the flash drive. Then,
   in the Unraid web UI, go to **Docker → Add Container** and pick
   `scanbutler` from the template dropdown.

2. **Fill in the settings.**

   | Setting | Value |
   |---|---|
   | Stacks | e.g. `/mnt/user/<share>/scan-splitter/stacks` |
   | Scanner | e.g. `/mnt/user/<share>/scan-splitter/scanner` |
   | Paperless | optional, e.g. `/mnt/user/<share>/scan-splitter/paperless` |
   | Config | `/mnt/user/appdata/scanbutler` |
   | `PUID`, `PGID` | `99`, `100` (nobody:users, like Unraid's shares) |
   | `MISTRAL_API_KEY` | your key (masked in the UI) |
   | `MISTRAL_LLM_MODEL` | `mistral-large-latest`, or a pinned version such as `mistral-large-2512` |
   | `MISTRAL_MAX_RPS` | a little below your account's requests-per-second limit for that model |
   | `TITLE_LANGUAGE` | e.g. `English`; leave empty to use each document's language |
   | `NO_DATE_LABEL` | the word used when a document has no date, e.g. `undated` |
   | `OCRMYPDF_JOBS` | `auto`: follows the memory limit (`--memory=4g` in *Extra Parameters*) |
   | `PAPERLESS_URL`, `PAPERLESS_TOKEN` | optional, enable the Paperless input; see below |
   | `QUEUE_WEBHOOK_URL` | optional, e.g. a Home Assistant webhook; see the [main README](../README.md#queue-webhook-home-assistant) |

   The advanced view has the remaining settings, among them `UMASK` (`002`:
   new files are writable for the `users` group). Don't add `--init` or
   `--user` to *Extra Parameters*: the image uses linuxserver.io's init
   system, which sets the user from `PUID`/`PGID` itself.

3. **Apply.** On first start, the container creates `inbox/`, `output/`,
   `archive/` and `failed/` under both Stacks and Scanner. The log shows
   `watching inbox` once for each.

If you run a private build of the image, Unraid needs a one-time
`docker login ghcr.io` before the first pull. Log in with a personal access
token that has the `read:packages` scope.

## Use

### Stacks

- Copy a scanned stack into `stacks/inbox/`, or into a sub-folder such as
  `stacks/inbox/Household/stack-01.pdf`. It is picked up once it has stopped
  growing for 60 seconds, so copying over SMB is safe.
- The documents appear in `stacks/output/Household/`. The original moves to
  `stacks/archive/Household/`.
- Each stack gets a folder under Work, `stacks/Household/stack-01-<hash>/`.
  Its `review.md` lists every document and flags uncertain splits.
- To correct a split, edit `plan.json` in that folder, then run:

  ```bash
  docker exec scanbutler scanbutler rebuild "stacks/Household/stack-01-<hash>"
  ```

### Scanner

- Set the scanner to save **PDF** files to a network folder, and point it at
  the SMB path of `scanner/inbox/`. The scanner's own text recognition can stay
  off, because every file gets a fresh OCR pass here.
- Each file becomes one document in `scanner/output/`, named by its content.
  It is never split, but blank pages are dropped. The original moves to
  `scanner/archive/`.
- By default, scanner files are named from the Tesseract text layer, without
  Mistral OCR. That takes no batch wait and costs nothing. For handwriting or
  poor scans, set `SCANNER_TEXT_SOURCE=mistral`.
- The SMB user the scanner logs in with needs write access to
  `scanner/inbox/`. The container reads and moves the files as
  `nobody:users`.

### Paperless

- With `PAPERLESS_URL` and `PAPERLESS_TOKEN` set, files in `paperless/inbox/`
  get the text layer and are uploaded to Paperless-ngx. Paperless, or an AI tagger
  such as [Zettelrobbe](https://github.com/admonstrator/zettelrobbe), does the
  naming and tagging.
- `PAPERLESS_URL` must be reachable from inside the container. For a
  Paperless container on the same server, that is usually
  `http://<server-ip>:8000`. `localhost` does not work, because it would
  point at this container itself.
- Create the token in Paperless under *My Profile → API Auth Token*, for the
  user that should own the documents.
- With `PAPERLESS_TEXT_SOURCE=mistral`, the document's content in Paperless
  comes from Mistral OCR. Tables then keep their columns as Markdown. It costs
  about $1 per 500 pages, and each file waits about a minute for the batch
  job.
- If Paperless is down, files wait in the inbox and are retried every 5
  minutes. A file that was already uploaded once is not uploaded again; it
  moves to `failed/` with the Paperless document number.

### Failures and pauses

- If a file fails, it moves to the `failed/` folder of its input, next to an
  `.error.txt`. Move it back into `inbox/` to retry; OCR that was already
  paid for is reused.
- If Mistral refuses the account, processing pauses. This happens, for
  example, when the spending limit is reached or the key is rejected. The
  files stay in the inbox, and one file is retried every 30 minutes. See the
  [main README](../README.md#spending-limit-and-paused-processing).

## Updating

### From 0.x (scan-stack-splitter) to 1.0

1.0 is a new container: new name, new image, linuxserver.io conventions.
Install it from the current template as described above, then:

- **Config:** point it at your old work folder (e.g.
  `/mnt/user/appdata/scan-stack-splitter`) or copy that folder's contents
  to the new one. It holds the plans for `rebuild` and the list of files
  already uploaded to Paperless, which prevents duplicate uploads.
- **Stacks, Scanner, Paperless:** the same paths as before.
- **Variables:** take over your values (API key, model, rate limit,
  Paperless, webhook).
- Remove the old container once the new one runs.

### Regular updates

New versions are published as `ghcr.io/tom-joad/scanbutler:latest`.
**Check for Updates** on the Docker page pulls them. A container you created
earlier keeps its settings, so settings added to the template later do not
appear on their own. Add them with **Add another Path, Port, Variable**; the
[changelog](../CHANGELOG.md) names new settings.

## Notes

- The template limits the container to 4 GB (`--memory=4g` in *Extra
  Parameters*), and OCR adapts to that limit. 1 GB is the tested minimum: it
  is slower, but it doesn't fail. For full speed, allow about
  1 GB + 0.75 GB per CPU core, for example 5.5 GB on a 6-core server. More
  than that brings no further gain. See the main README under
  [System requirements](../README.md#system-requirements).

- Scanbutler runs as `PUID`/`PGID`, by default `nobody:users` (99:100) like
  Unraid's shares, with `UMASK=002`, so output files can be edited and
  deleted over SMB.
- If the text layer fails, `.error.txt` and the log show ocrmypdf's own
  message. A helper process `died with SIGKILL` means it ran out of memory.
  For errors that look like file-system problems (`Input/output error`,
  missing files), point Config at the pool directly, for example
  `/mnt/cache/appdata/scanbutler` instead of `/mnt/user/...`.
- Temporary page images go to `Config/tmp` on disk, not to RAM. A 500-page
  stack needs a few GB there while it is processed.
- The Config folder holds the full OCR text of every file and is never cleaned
  up automatically. Delete a file's folder there once its documents are fine.
- Pages are sent to Mistral's API. Uploaded batch files are deleted from
  Mistral's storage after each job.
