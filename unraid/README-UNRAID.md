# Running scan-stack-splitter on Unraid

The container runs permanently and watches its inbox. It has no web interface.
Everything it does is visible in the container log.

## Install

1. **Registry login**: only needed while the GHCR image is private, and only
   once. In an Unraid terminal, run `docker login ghcr.io` with your GitHub
   username and a personal access token that has the `read:packages` scope.

2. **Add the template**: copy `scan-stack-splitter.xml` to
   `/boot/config/plugins/dockerMan/templates-user/` on the flash share. Then,
   in the Unraid web UI, go to **Docker → Add Container** and pick
   `scan-stack-splitter` from the template dropdown.

3. **Fill in the settings**:

   | Setting | Value |
   |---|---|
   | Documents | A folder on a share you can reach over SMB, e.g. `/mnt/user/<share>/Scan-Splitter` |
   | Work | `/mnt/user/appdata/scan-stack-splitter` |
   | `MISTRAL_API_KEY` | your key (masked in the UI) |
   | `MISTRAL_LLM_MODEL` | e.g. `mistral-large-latest` or a pinned version |
   | `MISTRAL_MAX_RPS` | slightly below your account's requests-per-second limit for that model |
   | `TITLE_LANGUAGE` | e.g. `German` |
   | `NO_DATE_LABEL` | e.g. `undatiert` |
   | `OCRMYPDF_JOBS` | leave some cores for the rest of the server |

4. **Apply**. On first start, the container creates `inbox/`, `output/`,
   `archive/` and `failed/` under Documents.

## Use

- Copy a scanned stack into `inbox/` or a sub-folder of it, for example
  `inbox/Person A/stack-01.pdf`. The file is picked up once it has stopped
  growing for 60 seconds, so copying over SMB is safe.
- The documents appear in `output/Person A/`. The original stack moves to
  `archive/Person A/`.
- Each stack has its own folder under Work: `Person A/stack-01-<hash>/`. Its
  `review.md` lists every document and flags uncertain splits.
- To correct a split, edit `plan.json` in that folder, then run:

  ```bash
  docker exec scan-stack-splitter stacksplit rebuild "Person A/stack-01-<hash>"
  ```

- If a stack fails, it lands in `failed/` next to an `.error.txt`. Move it back
  into `inbox/` to retry: the OCR already paid for is reused.

## Notes

- The container runs as `nobody:users` (99:100), like Unraid's own shares, so
  output files can be edited and deleted over SMB.
- Temporary page images go to `Work/tmp` on disk, not to RAM. A 500-page stack
  needs a few GB there while it is processed.
- The Work folder holds the full OCR text of every stack. It has no automatic
  cleanup. Delete a stack's folder once its documents are fine.
- Pages are sent to Mistral's API. Uploaded batch files are deleted from
  Mistral's storage after each job.
