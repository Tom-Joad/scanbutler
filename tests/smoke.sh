#!/usr/bin/env bash
# Smoke test of the image: version, settings check, the watcher runs as
# PUID, creates the input folders, keeps the health check's heartbeat going,
# and the OCR tools and language models are installed. No document is
# processed and Mistral is never called.
# Usage: tests/smoke.sh <image>
set -euo pipefail

IMAGE=${1:?usage: smoke.sh <image>}
WORK=$(mktemp -d)
NAME=scanbutler-smoke-$$
SECRET=smoke-test-api-key-$$
# Use the caller's IDs so the test can clean up; abc must not be root.
PUID=$(id -u); PGID=$(id -g)
[[ $PUID != 0 ]] || { PUID=1000; PGID=1000; }

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
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

echo "== without MISTRAL_API_KEY the watcher reports a configuration error"
docker run -d --name "$NAME" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
    in_log 'configuration error: MISTRAL_API_KEY' && break
    sleep 1
done
in_log 'configuration error: MISTRAL_API_KEY' || fail "no configuration error without MISTRAL_API_KEY"
docker rm -f "$NAME" >/dev/null

echo "== start"
mkdir -p "$WORK/config" "$WORK/data"
docker run -d --name "$NAME" -e PUID="$PUID" -e PGID="$PGID" -e MISTRAL_API_KEY="$SECRET" \
    -v "$WORK/config:/config" -v "$WORK/data:/data" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
    in_log '"event":"watching inbox"' && break
    sleep 2
done
in_log '"event":"starting".*"version"' || fail "no starting line with the version"
in_log '"event":"watching inbox"' || fail "the watcher did not start"

# The health check's own command.
docker exec "$NAME" python3 -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/scanbutler.heartbeat') < 300 else 1)" \
    || fail "no fresh heartbeat"

USERS=$(docker exec "$NAME" ps -eo user,args | awk '/python3 -m scanbutler run/ && !/awk/ {print $1}')
[[ -n $USERS ]] || fail "watcher not running"
[[ $USERS != *root* ]] || fail "watcher runs as root"

for dir in stacks/inbox scanner/inbox; do
    [[ -d $WORK/data/$dir ]] || fail "$dir was not created"
    [[ $(stat -c %u "$WORK/data/$dir") == "$PUID" ]] || fail "$dir not owned by PUID"
done
[[ $(stat -c %u "$WORK/config") == "$PUID" ]] || fail "/config not owned by PUID"

# The API key is never written to a file or the log.
if docker exec "$NAME" grep -rqs "$SECRET" /config /data /tmp /app; then
    fail "the API key was written to a file"
fi
if in_log "$SECRET"; then fail "the API key is in the log"; fi

echo "OK"
