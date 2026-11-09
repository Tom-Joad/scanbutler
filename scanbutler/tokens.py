"""API tokens: one per calling program, created with `scanbutler token add NAME`.

A token is shown once; the file keeps only its SHA-256, so a copy of the
work folder lets nobody call the API. The API rereads the file when it
changes, so adding or removing a token needs no restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "sb_"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TokenError(ValueError):
    pass


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Token:
    name: str
    sha256: str
    created_at: str


class TokensUnreadable(TokenError):
    """tokens.json can't be parsed and there is no earlier good list to go on with."""


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None
        self._tokens: list[Token] = []
        self._unreadable = False

    def _load(self) -> list[Token]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._seen, self._tokens = None, []
            return []
        seen = (stat.st_mtime_ns, stat.st_size)
        if seen != self._seen:
            self._seen = seen  # look at this version of the file once, not on every request
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._tokens = [Token(t["name"], t["sha256"], t.get("created_at", "")) for t in data.get("tokens", [])]
                self._unreadable = False
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                # Hand-edited, or cut off by something outside this program.
                # Keep the last good list if there is one.
                self._unreadable = True
                log.error("api tokens unreadable", extra={"file": self.path.name, "error": f"{type(exc).__name__}: {exc}"[:200]})
        if self._unreadable and not self._tokens:
            raise TokensUnreadable(f"{self.path.name} can't be read")
        return self._tokens

    def tokens(self) -> list[Token]:
        with self._lock:
            return list(self._load())

    def check(self, token: str) -> str | None:
        """The name the token belongs to, or None."""
        if not token.startswith(PREFIX):
            return None
        digest = _digest(token)
        found = None
        for known in self.tokens():
            # Compare against every entry, so the time taken says nothing.
            if hmac.compare_digest(known.sha256, digest):
                found = known.name
        return found

    def _save(self, tokens: list[Token]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        data = {"tokens": [t.__dict__ for t in tokens]}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        tmp.replace(self.path)

    def add(self, name: str) -> str:
        """Create a token for `name` and return it; it can't be shown again."""
        if not NAME.match(name):
            raise TokenError("names are 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
        with self._lock:
            tokens = list(self._load())
            if any(t.name == name for t in tokens):
                raise TokenError(f"a token named {name!r} exists; remove it first to replace it")
            token = PREFIX + secrets.token_urlsafe(32)
            self._save([*tokens, Token(name, _digest(token), _now())])
            return token

    def remove(self, name: str) -> None:
        with self._lock:
            tokens = list(self._load())
            if not any(t.name == name for t in tokens):
                raise TokenError(f"no token named {name!r}")
            self._save([t for t in tokens if t.name != name])
