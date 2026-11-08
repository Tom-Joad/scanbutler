"""Docker's health check: `python3 -m scanbutler.health`.

Healthy when the main loop touched its heartbeat in the last 5 minutes
and, with API_ENABLED, the API answers /api/v1/health.
"""

import os
import sys
import time
import urllib.request

from .config import ConfigError, _bool, _int

HEARTBEAT = "/tmp/scanbutler.heartbeat"
MAX_AGE = 300


def main() -> int:
    try:
        if time.time() - os.path.getmtime(HEARTBEAT) >= MAX_AGE:
            print("heartbeat is stale")
            return 1
    except OSError:
        print("no heartbeat")
        return 1
    try:
        api, port = _bool("API_ENABLED", False), _int("API_PORT", 8080, minimum=1)
    except ConfigError:
        api, port = False, 8080
    if api:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=4) as answer:
                if answer.status != 200:
                    print(f"api answered {answer.status}")
                    return 1
        except OSError as exc:
            print(f"api not answering: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
