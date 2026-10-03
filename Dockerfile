FROM python:3.12-slim-bookworm

# image.source is what makes a GHCR package inherit the repository's
# visibility instead of staying private on its own.
LABEL org.opencontainers.image.source="https://github.com/Tom-Joad/scanbutler" \
      org.opencontainers.image.title="Scanbutler" \
      org.opencontainers.image.description="Split scanned PDF stacks into named, searchable documents using Mistral OCR" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Tesseract and Ghostscript power the searchable text layer; unpaper backs
# ocrmypdf's --clean.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends \
      tesseract-ocr ghostscript unpaper curl ca-certificates \
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
    done; \
    apt-get purge --yes curl && apt-get autoremove --yes

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY scanbutler ./scanbutler
RUN printf '#!/bin/sh\nexec python -m scanbutler "$@"\n' > /usr/local/bin/scanbutler \
 && chmod +x /usr/local/bin/scanbutler

ENV PYTHONPATH=/app

# Overridden by `user:` in docker-compose to match the owner of the shares.
RUN useradd --system --uid 10001 --no-create-home splitter
USER splitter

# A watcher thread touches the heartbeat every 30 s, also while a long
# stack is being processed.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/scanbutler.heartbeat') < 300 else 1)"

ENTRYPOINT ["scanbutler"]
CMD ["run"]
