#!/usr/bin/env bash
# Smoke test of the image: version, settings check, the watcher runs as
# PUID, creates the input folders (handing root-owned ones to PUID), runs
# both Paperless inputs and tag sharing against an unreachable Paperless,
# keeps the health check's heartbeat going,
# the OCR tools and language models are installed, and a language beyond
# the built-in ones is downloaded (or left out when it can't be), and a scan
# with fax images pikepdf can't decode still gets its text layer. No
# document is processed and Mistral is never called.
# Usage: tests/smoke.sh <image>
# SMOKE_TESSDATA_URL replaces the tessdata_best download URL, e.g. a local
# mirror where GitHub isn't reachable.
set -euo pipefail

IMAGE=${1:?usage: smoke.sh <image>}
WORK=$(mktemp -d)
NAME=scanbutler-smoke-$$
SECRET=smoke-test-api-key-$$
PL_SECRET=smoke-test-paperless-token-$$
PL2_SECRET=smoke-test-paperless-2-token-$$
TESSDATA_URL=${SMOKE_TESSDATA_URL:-}
# Use the caller's IDs so the test can clean up; abc must not be root.
PUID=$(id -u); PGID=$(id -g)
[[ $PUID != 0 ]] || { PUID=1000; PGID=1000; }

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # The container writes some files as root (e.g. the crontab), which the
    # calling user can't remove; clean up through a container.
    docker run --rm --entrypoint sh -v "$WORK:/w" "$IMAGE" -c 'rm -rf /w/*' >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; docker logs "$NAME" >&2 2>&1 || true; exit 1; }

# Read the whole log first: with pipefail, `docker logs | grep -q` fails at
# random when grep exits early and docker logs gets SIGPIPE.
in_log() { local out; out=$(docker logs "$NAME" 2>&1); grep -qE -- "$1" <<<"$out"; }

echo "== version"
version=$(docker run --rm --entrypoint python3 "$IMAGE" -m scanbutler --version)
[[ $version =~ ^scanbutler\ [0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "unexpected version '$version'"

echo "== OCR tools and language models"
langs=$(docker run --rm --entrypoint tesseract "$IMAGE" --list-langs 2>&1)
for lang in deu eng osd; do
    grep -qx "$lang" <<<"$langs" || fail "tesseract language $lang missing"
done
docker run --rm --entrypoint sh "$IMAGE" -c 'command -v gs && command -v unpaper' >/dev/null \
    || fail "ghostscript or unpaper missing"

echo "== a scan with fax images pikepdf can't decode still gets its text layer"
TESTS=$(cd "$(dirname "$0")" && pwd)
text=$(docker run --rm --entrypoint sh -v "$TESTS:/tests:ro" "$IMAGE" -c '
    cd /tmp && python3 /tests/ccitt_pdf.py fax.pdf && python3 -c "
from pathlib import Path
import pypdfium2
from scanbutler import logging_setup, pdfops
logging_setup.configure()
pdfops.configure_jobs(2)
pdfops.make_searchable(Path(\"fax.pdf\"), Path(\"out.pdf\"), \"deu+eng\", \"\")
print(pypdfium2.PdfDocument(\"out.pdf\")[0].get_textpage().get_text_range())
"' 2>&1) || fail "no text layer for the fax scan: $text"
grep -q "Leukocytes" <<<"$text" || fail "the fax scan's text layer lacks its text: $text"
grep -q '"input_streams_unreadable"' <<<"$text" || fail "no warning about the unreadable fax images"

echo "== without MISTRAL_API_KEY the watcher reports a configuration error"
docker run -d --name "$NAME" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
    in_log 'configuration error: MISTRAL_API_KEY' && break
    sleep 1
done
in_log 'configuration error: MISTRAL_API_KEY' || fail "no configuration error without MISTRAL_API_KEY"
docker rm -f "$NAME" >/dev/null

echo "== a misspelt language is a configuration error"
docker run -d --name "$NAME" -e MISTRAL_API_KEY="$SECRET" -e OCRMYPDF_LANGUAGES=deu+ger "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
    in_log "did you mean 'deu'" && break
    sleep 1
done
in_log "configuration error: OCRMYPDF_LANGUAGES: unknown language 'ger' \(did you mean 'deu'\?\)" \
    || fail "no configuration error for OCRMYPDF_LANGUAGES=deu+ger"
docker rm -f "$NAME" >/dev/null

echo "== a language that can't be downloaded is left out"
docker run -d --name "$NAME" -e MISTRAL_API_KEY="$SECRET" -e OCRMYPDF_LANGUAGES=deu+eng+por \
    -e TESSDATA_URL=http://127.0.0.1:9 "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
    in_log '"event":"languages ready"' && break
    sleep 1
done
in_log '"event":"language not available, left out".*"language":"por"' || fail "no warning for the missing language"
in_log '"event":"languages ready","languages":"deu\+eng".*"missing":\["por"\]' || fail "por wasn't left out"
[[ $(docker inspect -f '{{.State.Status}}' "$NAME") == running ]] || fail "stopped without the language"
docker rm -f "$NAME" >/dev/null

echo "== start (downloads fra; both Paperless inputs and tag sharing on)"
mkdir -p "$WORK/config" "$WORK/data"
# An input folder Docker would have created itself belongs to root; the
# init must hand it to abc.
docker run --rm --entrypoint sh -v "$WORK/data:/d" "$IMAGE" -c 'mkdir /d/paperless-2' >/dev/null
# Nothing listens on port 9: Paperless counts as unreachable.
docker run -d --name "$NAME" -e PUID="$PUID" -e PGID="$PGID" -e MISTRAL_API_KEY="$SECRET" \
    -e OCRMYPDF_LANGUAGES=deu+eng+fra ${TESSDATA_URL:+-e TESSDATA_URL="$TESSDATA_URL"} \
    -e PAPERLESS_URL=http://127.0.0.1:9 -e PAPERLESS_TOKEN="$PL_SECRET" \
    -e PAPERLESS_2_TOKEN="$PL2_SECRET" -e PAPERLESS_SHARE_TAGS=true \
    -e PAPERLESS_SHARE_CORRESPONDENTS=true -e PAPERLESS_SHARE_DOCUMENT_TYPES=true \
    -v "$WORK/config:/config" -v "$WORK/data:/data" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
    in_log '"event":"watching inbox"' && break
    sleep 2
done
in_log '"event":"starting".*"version"' || fail "no starting line with the version"
in_log 'BASED ON IMAGES FROM LINUXSERVER\.IO' || fail "the startup banner is not ours"
if in_log 'Based on images from linuxserver\.io'; then fail "the base image's banner is still shown"; fi
in_log '"event":"watching inbox"' || fail "the watcher did not start"
in_log '"event":"languages ready","languages":"deu\+eng\+fra","downloaded":\["fra"\]' \
    || fail "fra was not downloaded"
[[ $(stat -c %u "$WORK/config/tessdata/fra.traineddata") == "$PUID" ]] || fail "the model isn't owned by PUID"
langs=$(docker exec "$NAME" env TESSDATA_PREFIX=/config/tessdata tesseract --list-langs 2>&1)
for lang in deu eng fra osd; do
    grep -qx "$lang" <<<"$langs" || fail "tesseract doesn't see $lang in the work folder"
done

# The health check's own command.
docker exec "$NAME" python3 -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/scanbutler.heartbeat') < 300 else 1)" \
    || fail "no fresh heartbeat"

USERS=$(docker exec "$NAME" ps -eo user,args | awk '/python3 -m scanbutler run/ && !/awk/ {print $1}')
[[ -n $USERS ]] || fail "watcher not running"
[[ $USERS != *root* ]] || fail "watcher runs as root"

# abc's home must be readable by abc: ocrmypdf looks for fonts below it.
# No `| head`: with pipefail, an early exit of head can fail the pipeline.
PIDS=$(docker exec "$NAME" pgrep -f "python3 -m scanbutler run")
PID=${PIDS%%$'\n'*}
# Read as abc: root may not read another user's environ without ptrace.
WATCHER_HOME=$(docker exec --user abc "$NAME" sh -c "tr '\\0' '\\n' < /proc/$PID/environ" | sed -n 's/^HOME=//p')
[[ $WATCHER_HOME == /config ]] || fail "the watcher's HOME is '$WATCHER_HOME', not /config"

in_log '"event":"watching inbox","profile":"paperless-2"' || fail "the second Paperless input did not start"
for _ in $(seq 1 30); do
    in_log '"event":"tags could not be shared' && break
    sleep 1
done
in_log '"event":"tags could not be shared' || fail "tag sharing did not run"
in_log '"event":"correspondents could not be shared' || fail "correspondent sharing did not run"
in_log '"event":"document types could not be shared' || fail "document type sharing did not run"
[[ $(docker inspect -f '{{.State.Status}}' "$NAME") == running ]] || fail "stopped with Paperless unreachable"

for dir in stacks/inbox scanner/inbox paperless/inbox paperless-2 paperless-2/inbox; do
    [[ -d $WORK/data/$dir ]] || fail "$dir was not created"
    [[ $(stat -c %u "$WORK/data/$dir") == "$PUID" ]] || fail "$dir not owned by PUID"
done
[[ $(stat -c %u "$WORK/config") == "$PUID" ]] || fail "/config not owned by PUID"

# The API key and the tokens are never written to a file or the log.
for secret in "$SECRET" "$PL_SECRET" "$PL2_SECRET"; do
    if docker exec "$NAME" grep -rqs "$secret" /config /data /tmp /app; then
        fail "a secret was written to a file"
    fi
    if in_log "$secret"; then fail "a secret is in the log"; fi
done

echo "OK"
