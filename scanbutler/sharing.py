"""Keep Paperless-ngx tags, correspondents and document types shared between all users.

Paperless gives every new tag, correspondent and document type an owner, and
other users only see one that has no owner or is shared with them. On an
instance with several users, what one of them or an AI tagger creates is
invisible to everyone else. Paperless has no setting for ownerless objects,
so this check removes the owner from every one that has one, for each kind
switched on (PAPERLESS_SHARE_TAGS, _CORRESPONDENTS, _DOCUMENT_TYPES), every
PAPERLESS_SHARE_TAGS_MINUTES.

Tags listed in PAPERLESS_SHARE_TAGS_READONLY are protected instead: they
keep their owner, and every other user may see them, but not change them.
A tagger's work-queue marker should not be renamed or deleted by accident.
"""

from __future__ import annotations

import logging
import threading

from .paperless import PaperlessClient, PaperlessError, PaperlessUnavailable

log = logging.getLogger(__name__)

KINDS = ("tags", "correspondents", "document_types")


def _label(kind: str) -> str:
    return kind.replace("_", " ")


def _users(obj: dict, kind: str) -> list[int]:
    return sorted((obj.get("permissions") or {}).get(kind, {}).get("users", []))


def _groups(obj: dict) -> list[int]:
    permissions = obj.get("permissions") or {}
    return [g for kind in ("view", "change") for g in permissions.get(kind, {}).get("groups", [])]


class Sharer:
    def __init__(
        self, client: PaperlessClient, kinds: tuple[str, ...], readonly_tags: tuple[str, ...], interval_seconds: float
    ) -> None:
        self.client = client
        self.kinds = kinds
        self.readonly = {name.casefold() for name in readonly_tags}
        self.interval = interval_seconds
        # kind -> the last error, so a lasting one is logged once, not every minute
        self._last_error: dict[str, str] = {}
        self._ownerless_warned: set[int] = set()

    def check_once(self, kind: str) -> None:
        objects = self.client.objects(kind)
        readonly = self.readonly if kind == "tags" else set()
        protected = [o for o in objects if o["name"].casefold() in readonly]
        owned = [o for o in objects if o["name"].casefold() not in readonly and o.get("owner") is not None]
        if owned:
            self.client.set_permissions(kind, [o["id"] for o in owned], owner=None)
            # Ids, not names: names come from the documents.
            log.info(f"{_label(kind)} shared", extra={"count": len(owned), "ids": sorted(o["id"] for o in owned)})
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
            self.client.set_permissions("tags", [tag["id"]], owner=owner, view_users=viewers)
            log.info("tag made read-only for other users", extra={"tag_id": tag["id"], "users": len(viewers)})

    def check_all(self) -> None:
        """One round over every kind; a failing kind doesn't hold up the others."""
        for kind in self.kinds:
            try:
                self.check_once(kind)
                if self._last_error.pop(kind, None) is not None:
                    log.info("sharing works again", extra={"kind": kind})
            except (PaperlessError, PaperlessUnavailable) as exc:
                # Paperless away, or a token that may not change other users'
                # objects (HTTP 403).
                message = str(exc)[:300]
                if message != self._last_error.get(kind):
                    log.warning(f"{_label(kind)} could not be shared", extra={"error": message})
                    self._last_error[kind] = message
            except Exception:  # noqa: BLE001 - sharing must never stop the uploads
                log.exception("sharing failed", extra={"kind": kind})

    def run(self, stop: threading.Event) -> None:
        log.info(
            "sharing paperless objects",
            extra={"kinds": list(self.kinds), "interval_s": self.interval, "read_only_tags": len(self.readonly)},
        )
        while not stop.is_set():
            self.check_all()
            stop.wait(self.interval)
