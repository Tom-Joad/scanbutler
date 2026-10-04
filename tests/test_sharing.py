from __future__ import annotations

import json
import logging

import httpx
import pytest

from scanbutler.config import ConfigError, Settings
from scanbutler.paperless import PaperlessClient
from scanbutler.sharing import TagSharer

ADMIN, TAGGER, OTHER = 1, 2, 3


def perms(view=(), change=()):
    return {"view": {"users": list(view), "groups": []}, "change": {"users": list(change), "groups": []}}


class FakeApi:
    """A Paperless that holds tags and applies set_permissions like the real one."""

    def __init__(self, tags, forbidden=False, page_size=2):
        self.tags = {t["id"]: t for t in tags}
        self.forbidden = forbidden
        self.page_size = page_size
        self.posts: list[dict] = []

    def _page(self, items, request):
        page = int(request.url.params["page"])
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        more = page * self.page_size < len(items)
        return httpx.Response(200, json={"count": len(items), "next": "next" if more else None, "results": chunk})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags/":
            assert request.url.params["full_perms"] == "true"
            return self._page(list(self.tags.values()), request)
        if request.url.path == "/api/users/":
            return self._page([{"id": u} for u in (ADMIN, TAGGER, OTHER)], request)
        if request.url.path == "/api/bulk_edit_objects/":
            if self.forbidden:
                return httpx.Response(403, json={"detail": "Insufficient permissions"})
            body = json.loads(request.content)
            self.posts.append(body)
            assert body["object_type"] == "tags" and body["operation"] == "set_permissions" and body["merge"] is False
            for tag_id in body["objects"]:
                self.tags[tag_id].update(owner=body["owner"], permissions=body["permissions"])
            return httpx.Response(200, json={"result": "OK"})
        return httpx.Response(404)


def sharer_for(api, readonly=()):
    client = PaperlessClient("http://paperless.test:8000", "t")
    client._http = httpx.Client(base_url="http://paperless.test:8000", transport=httpx.MockTransport(api))
    return TagSharer(client, tuple(readonly), 60)


def tag(id, name, owner, permissions=None):
    return {"id": id, "name": name, "owner": owner, "permissions": permissions or perms()}


def test_owned_tags_lose_their_owner_on_every_page():
    api = FakeApi([tag(1, "Invoice", ADMIN), tag(2, "Health", TAGGER), tag(3, "Car", None), tag(4, "Tax", OTHER, perms([ADMIN]))])

    sharer_for(api).check_once()

    assert [t["owner"] for t in api.tags.values()] == [None] * 4
    assert api.tags[4]["permissions"] == perms()
    assert len(api.posts) == 1 and sorted(api.posts[0]["objects"]) == [1, 2, 4]  # one call, the unowned one untouched


def test_the_log_carries_ids_not_tag_names(caplog):
    api = FakeApi([tag(1, "Blood count", ADMIN)])
    with caplog.at_level(logging.INFO):
        sharer_for(api).check_once()
    record = next(r for r in caplog.records if r.message == "tags shared")
    assert record.ids == [1] and record.count == 1
    assert "Blood count" not in caplog.text and not any("Blood count" in str(vars(r)) for r in caplog.records)


def test_nothing_to_do_means_no_write():
    api = FakeApi([tag(1, "Invoice", None), tag(2, "Health", None)])
    sharer_for(api).check_once()
    assert api.posts == []


def test_read_only_tag_keeps_its_owner_and_is_visible_to_everyone_else():
    api = FakeApi([tag(1, "Invoice", TAGGER), tag(9, "ai-processed", TAGGER, perms(change=[OTHER]))])
    sharer = sharer_for(api, ["AI-Processed"])  # names match regardless of case

    sharer.check_once()
    sharer.check_once()  # already right: no second write

    assert api.tags[9]["owner"] == TAGGER
    assert api.tags[9]["permissions"] == perms(view=[ADMIN, OTHER])
    assert api.tags[1]["owner"] is None
    assert len(api.posts) == 2


def test_ownerless_read_only_tag_is_left_alone_with_one_warning(caplog):
    api = FakeApi([tag(9, "ai-processed", None)])
    sharer = sharer_for(api, ["ai-processed"])
    with caplog.at_level(logging.WARNING):
        sharer.check_once()
        sharer.check_once()
    assert api.posts == []
    assert sum("no owner" in r.message for r in caplog.records) == 1


def test_a_token_without_permission_warns_once_and_keeps_running(caplog):
    import threading

    api = FakeApi([tag(1, "Invoice", ADMIN)], forbidden=True)
    sharer = sharer_for(api)
    stop = threading.Event()
    calls = []
    original = sharer.check_once

    def counting():
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        original()

    sharer.check_once = counting
    sharer.interval = 0
    with caplog.at_level(logging.WARNING):
        sharer.run(stop)

    assert len(calls) == 3
    assert sum("could not be shared" in r.message for r in caplog.records) == 1
    assert api.tags[1]["owner"] == ADMIN


def test_share_tags_config(settings, monkeypatch):
    assert settings.paperless_share_tags is False
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS", "true")
    with pytest.raises(ConfigError, match="PAPERLESS_URL"):
        Settings.from_env()
    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS_READONLY", " ai-processed , inbox ,")
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS_MINUTES", "0")
    with pytest.raises(ConfigError, match="MINUTES"):
        Settings.from_env()
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS_MINUTES", "5")
    loaded = Settings.from_env()
    assert loaded.paperless_share_tags
    assert loaded.paperless_share_tags_readonly == ("ai-processed", "inbox")
    assert loaded.paperless_share_tags_minutes == 5
