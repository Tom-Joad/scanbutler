# linuxserver.io's Debian 13 (trixie) base: s6-overlay, PUID/PGID/UMASK/TZ,
# the abc user and docker mods, as in every linuxserver.io container.
# Pinned by digest (a multi-arch index); Dependabot proposes new digests.
FROM ghcr.io/linuxserver/baseimage-debian:trixie@sha256:e919138f1d96c20890964521fab5f2d3cb2b7150f3ad45a2fc56369d85c9ba2b

# image.source is what makes a GHCR package inherit the repository's
# visibility instead of staying private on its own.
LABEL org.opencontainers.image.source="https://github.com/Tom-Joad/scanbutler" \
      org.opencontainers.image.title="Scanbutler" \
      org.opencontainers.image.description="Turn scanned paper into named, searchable PDFs: split stacks by content, name scanner files, feed Paperless-ngx" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Tesseract and Ghostscript power the searchable text layer; unpaper backs
# ocrmypdf's --clean. The upgrade picks up Debian security fixes newer than
# the pinned base image. procps provides pwdx, which the base image's
# with-contenv needs to apply UMASK to services; the trixie base lacks it.
RUN apt-get update \
 && apt-get upgrade --yes \
 && apt-get install --yes --no-install-recommends \
      python3 python3-venv tesseract-ocr ghostscript unpaper procps \
 && rm -rf /var/lib/apt/lists/*

# Replace Debian's default models with tessdata_best: noticeably better on
# poor scans, at the cost of speed. Checksums pin the exact files.
ARG TESSDATA_BEST_TAG=4.1.0
RUN set -eu; \
    dir="$(dirname "$(find /usr/share/tesseract-ocr -name eng.traineddata | head -n1)")"; \
    for entry in \
      "deu 8407331d6aa0229dc927685c01a7938fc5a641d1a9524f74838cdac599f0d06e" \
      "eng 8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba" \
      "osd 9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff"; \
    do \
      set -- $entry; \
      curl -fsSL -o "$dir/$1.traineddata" \
        "https://github.com/tesseract-ocr/tessdata_best/raw/${TESSDATA_BEST_TAG}/$1.traineddata"; \
      echo "$2  $dir/$1.traineddata" | sha256sum -c -; \
    done

# /lsiopy is linuxserver.io's place for a Python venv and already on PATH.
# pip is only needed to build the image. It is removed afterwards: the
# libraries it bundles (urllib3, msgpack, setuptools) lag behind and would
# show up in every vulnerability scan, without ever running.
COPY requirements.txt /app/
RUN python3 -m venv /lsiopy \
 && /lsiopy/bin/pip install --no-cache-dir --requirement /app/requirements.txt \
 && /lsiopy/bin/pip uninstall --yes pip

COPY scanbutler /app/scanbutler
# s6 services (init-scanbutler-config, svc-scanbutler) and the wrappers.
COPY root/ /
RUN chmod +x /usr/local/bin/scanbutler \
      /etc/s6-overlay/s6-rc.d/init-scanbutler-config/run /etc/s6-overlay/s6-rc.d/svc-scanbutler/run \
 && printf 'Scanbutler version: %s\n' "$(python3 -c 'import scanbutler; print(scanbutler.__version__)')" > /build_version

# Plans, caches, the upload ledger and temporary page images.
VOLUME /config

# The watcher touches the heartbeat every 30 s, also while a long stack is
# being processed.
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
    CMD python3 -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/scanbutler.heartbeat') < 300 else 1)"

# The entrypoint stays the base image's /init (s6-overlay), which must run as
# PID 1: don't add `--init` to `docker run`.
