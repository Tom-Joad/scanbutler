"""Keep Paperless-ngx tags shared between all users.

Paperless gives every new tag an owner, and other users only see a tag that
has no owner or is shared with them. On an instance with several users, a
tag that one of them or an AI tagger creates is invisible to everyone else.
Paperless has no setting for ownerless tags, so this check removes the
owner from every tag that has one, every PAPERLESS_SHARE_TAGS_MINUTES.

Tags listed in PAPERLESS_SHARE_TAGS_READONLY are protected instead: they
keep their owner, and every other user may see them, but not change them.
A tagger's work-queue marker should not be renamed or deleted by accident.
"""

from __future__ import annotations

import logging
import threading

from .paperless import PaperlessClient, PaperlessError, PaperlessUnavailable

log = logging.getLogger(__name__)


def _users(tag: dict, kind: str) -> list[int]:
    return sorted((tag.get("permissions") or {}).get(kind, {}).get("users", []))


def _groups(tag: dict) -> list[int]:
    permissions = tag.get("permissions") or {}
    return [g for kind in ("view", "change") for g in permissions.get(kind, {}).get("groups", [])]


class TagSharer:
    def __init__(self, client: PaperlessClient, readonly: tuple[str, ...], interval_seconds: float) -> None:
        self.client = client
        self.readonly = {name.casefold() for name in readonly}
        self.interval = interval_seconds
        self._last_error: str | None = None
        self._ownerless_warned: set[int] = set()

    def check_once(self) -> None:
        tags = self.client.tags()
        protected = [t for t in tags if t["name"].casefold() in self.readonly]
        owned = [t for t in tags if t["name"].casefold() not in self.readonly and t.get("owner") is not None]
        if owned:
            self.client.set_tag_permissions([t["id"] for t in owned], owner=None)
            # Ids, not names: tag names come from the documents.
            log.info("tags shared", extra={"count": len(owned), "ids": sorted(t["id"] for t in owned)})
        if protected:
            self._protect(protected)

    def _protect(self, tags: list[dict]) -> None:
        users = self.client.user_ids()
        for tag in tags:
            owner = tag.get("owner")
            if owner is None:
                # Without an owner, everyone may change it; there is no one to
                # hand it back to. Say so once, then leave it alone.
                if tag["id"] not in self._ownerless_warned:
                    self._ownerless_warned.add(tag["id"])
                    log.warning(
                        "read-only tag has no owner, so every user may change it; give it an owner in Paperless",
                        extra={"tag_id": tag["id"]},
                    )
                continue
            viewers = sorted(user for user in users if user != owner)
            if _users(tag, "view") == viewers and not _users(tag, "change") and not _groups(tag):
                continue
            self.client.set_tag_permissions([tag["id"]], owner=owner, view_users=viewers)
            log.info("tag made read-only for other users", extra={"tag_id": tag["id"], "users": len(viewers)})

    def run(self, stop: threading.Event) -> None:
        log.info(
            "sharing paperless tags",
            extra={"interval_s": self.interval, "read_only_tags": len(self.readonly)},
        )
        while not stop.is_set():
            try:
                self.check_once()
                if self._last_error is not None:
                    log.info("tag sharing works again")
                    self._last_error = None
            except (PaperlessError, PaperlessUnavailable) as exc:
                # Paperless away, or a token that may not change other users'
                # tags (HTTP 403). Logged once per distinct error, not every minute.
                message = str(exc)[:300]
                if message != self._last_error:
                    log.warning("tags could not be shared", extra={"error": message})
                    self._last_error = message
            except Exception:  # noqa: BLE001 - sharing must never stop the uploads
                log.exception("tag sharing failed")
            stop.wait(self.interval)
