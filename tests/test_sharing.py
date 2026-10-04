from __future__ import annotations

import json
import logging
import threading

import httpx
import pytest

from scanbutler.config import ConfigError, Settings
from scanbutler.paperless import PaperlessClient
from scanbutler.sharing import KINDS, Sharer

ADMIN, TAGGER, OTHER = 1, 2, 3


def perms(view=(), change=()):
    return {"view": {"users": list(view), "groups": []}, "change": {"users": list(change), "groups": []}}


class FakeApi:
    """A Paperless that holds tags, correspondents and document types and
    applies set_permissions like the real one."""

    def __init__(self, tags=(), correspondents=(), document_types=(), forbidden=(), page_size=2):
        self.store = {
            "tags": {o["id"]: o for o in tags},
            "correspondents": {o["id"]: o for o in correspondents},
            "document_types": {o["id"]: o for o in document_types},
        }
        self.forbidden = set(forbidden)  # kinds whose writes get HTTP 403
        self.page_size = page_size
        self.posts: list[dict] = []

    @property
    def tags(self):
        return self.store["tags"]

    def _page(self, items, request):
        page = int(request.url.params["page"])
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        more = page * self.page_size < len(items)
        return httpx.Response(200, json={"count": len(items), "next": "next" if more else None, "results": chunk})

    def _would_duplicate(self, kind, ids):
        store = self.store[kind]
        names = [o["name"].casefold() for i, o in store.items() if o["owner"] is None and i not in ids]
        names += [store[i]["name"].casefold() for i in ids]
        return len(names) != len(set(names))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        kind = request.url.path.strip("/").removeprefix("api/")
        if kind in self.store:
            assert request.url.params["full_perms"] == "true"
            return self._page(list(self.store[kind].values()), request)
        if kind == "users":
            return self._page([{"id": u} for u in (ADMIN, TAGGER, OTHER)], request)
        if kind == "bulk_edit_objects":
            body = json.loads(request.content)
            if body["object_type"] in self.forbidden:
                return httpx.Response(403, json={"detail": "Insufficient permissions"})
            assert body["operation"] == "set_permissions" and body["merge"] is False
            if body["owner"] is None and self._would_duplicate(body["object_type"], body["objects"]):
                # Like Paperless: unique (name, owner), checked for the whole call.
                return httpx.Response(400, text="Error performing bulk permissions edit, check logs for more detail.")
            self.posts.append(body)
            for object_id in body["objects"]:
                self.store[body["object_type"]][object_id].update(owner=body["owner"], permissions=body["permissions"])
            return httpx.Response(200, json={"result": "OK"})
        return httpx.Response(404)


def sharer_for(api, readonly=(), kinds=("tags",)):
    client = PaperlessClient("http://paperless.test:8000", "t")
    client._http = httpx.Client(base_url="http://paperless.test:8000", transport=httpx.MockTransport(api))
    return Sharer(client, tuple(kinds), tuple(readonly), 60)


def obj(id, name, owner, permissions=None):
    return {"id": id, "name": name, "owner": owner, "permissions": permissions or perms()}


tag = obj


def test_owned_tags_lose_their_owner_on_every_page():
    api = FakeApi([tag(1, "Invoice", ADMIN), tag(2, "Health", TAGGER), tag(3, "Car", None), tag(4, "Tax", OTHER, perms([ADMIN]))])

    sharer_for(api).check_once("tags")

    assert [t["owner"] for t in api.tags.values()] == [None] * 4
    assert api.tags[4]["permissions"] == perms()
    assert len(api.posts) == 1 and sorted(api.posts[0]["objects"]) == [1, 2, 4]  # one call, the unowned one untouched


def test_correspondents_and_document_types_are_shared_too():
    api = FakeApi(
        correspondents=[obj(10, "Bank", ADMIN), obj(11, "Doctor", None), obj(12, "Employer", TAGGER)],
        document_types=[obj(20, "Invoice", TAGGER), obj(21, "Letter", None)],
    )

    sharer_for(api, kinds=KINDS).check_all()

    assert all(o["owner"] is None for kind in ("correspondents", "document_types") for o in api.store[kind].values())
    assert {(p["object_type"], tuple(sorted(p["objects"]))) for p in api.posts} == {
        ("correspondents", (10, 12)),
        ("document_types", (20,)),
    }


def test_only_the_kinds_switched_on_are_touched():
    api = FakeApi([tag(1, "Invoice", ADMIN)], correspondents=[obj(10, "Bank", ADMIN)])
    sharer_for(api, kinds=("correspondents",)).check_all()
    assert api.tags[1]["owner"] == ADMIN and api.store["correspondents"][10]["owner"] is None


def test_the_read_only_list_applies_to_tags_only():
    api = FakeApi([tag(9, "ai-processed", TAGGER)], correspondents=[obj(10, "ai-processed", TAGGER)])
    sharer_for(api, ["ai-processed"], kinds=KINDS).check_all()
    assert api.tags[9]["owner"] == TAGGER
    assert api.store["correspondents"][10]["owner"] is None


def test_the_log_carries_ids_not_names(caplog):
    api = FakeApi([tag(1, "Blood count", ADMIN)], correspondents=[obj(10, "Dr. Example", ADMIN)])
    with caplog.at_level(logging.INFO):
        sharer_for(api, kinds=KINDS).check_all()
    shared = {r.message: r for r in caplog.records if r.message.endswith(" shared")}
    assert shared["tags shared"].ids == [1] and shared["tags shared"].count == 1
    assert shared["correspondents shared"].ids == [10]
    assert "Blood count" not in caplog.text and "Dr. Example" not in caplog.text
    assert not any("Blood count" in str(vars(r)) or "Dr. Example" in str(vars(r)) for r in caplog.records)


def test_nothing_to_do_means_no_write():
    api = FakeApi([tag(1, "Invoice", None), tag(2, "Health", None)])
    sharer_for(api).check_once("tags")
    assert api.posts == []


def test_read_only_tag_keeps_its_owner_and_is_visible_to_everyone_else():
    api = FakeApi([tag(1, "Invoice", TAGGER), tag(9, "ai-processed", TAGGER, perms(change=[OTHER]))])
    sharer = sharer_for(api, ["AI-Processed"])  # names match regardless of case

    sharer.check_once("tags")
    sharer.check_once("tags")  # already right: no second write

    assert api.tags[9]["owner"] == TAGGER
    assert api.tags[9]["permissions"] == perms(view=[ADMIN, OTHER])
    assert api.tags[1]["owner"] is None
    assert len(api.posts) == 2


def test_ownerless_read_only_tag_is_left_alone_with_one_warning(caplog):
    api = FakeApi([tag(9, "ai-processed", None)])
    sharer = sharer_for(api, ["ai-processed"])
    with caplog.at_level(logging.WARNING):
        sharer.check_once("tags")
        sharer.check_once("tags")
    assert api.posts == []
    assert sum("no owner" in r.message for r in caplog.records) == 1


def test_a_kind_the_token_may_not_change_warns_once_and_the_others_go_on(caplog):
    api = FakeApi([tag(1, "Invoice", ADMIN)], correspondents=[obj(10, "Bank", ADMIN)], forbidden={"correspondents"})
    sharer = sharer_for(api, kinds=KINDS)
    stop = threading.Event()
    rounds = []
    original = sharer.check_all

    def counting():
        rounds.append(1)
        if len(rounds) == 3:
            stop.set()
        original()

    sharer.check_all = counting
    sharer.interval = 0
    with caplog.at_level(logging.WARNING):
        sharer.run(stop)

    assert len(rounds) == 3
    assert sum(r.message == "correspondents could not be shared" for r in caplog.records) == 1
    assert api.store["correspondents"][10]["owner"] == ADMIN
    assert api.tags[1]["owner"] is None  # tags were shared all the same


def test_share_config(settings, monkeypatch):
    assert settings.paperless_share_kinds == ()
    for name in ("PAPERLESS_SHARE_TAGS", "PAPERLESS_SHARE_CORRESPONDENTS", "PAPERLESS_SHARE_DOCUMENT_TYPES"):
        monkeypatch.setenv(name, "true")
        with pytest.raises(ConfigError, match=f"{name} needs PAPERLESS_URL"):
            Settings.from_env()
        monkeypatch.delenv(name)
    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS", "true")
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS_READONLY", " ai-processed , inbox ,")
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS_MINUTES", "0")
    with pytest.raises(ConfigError, match="MINUTES"):
        Settings.from_env()
    monkeypatch.setenv("PAPERLESS_SHARE_TAGS_MINUTES", "5")
    loaded = Settings.from_env()
    assert loaded.paperless_share_kinds == ("tags",)
    assert loaded.paperless_share_tags_readonly == ("ai-processed", "inbox")
    assert loaded.paperless_share_tags_minutes == 5
    monkeypatch.setenv("PAPERLESS_SHARE_DOCUMENT_TYPES", "true")
    monkeypatch.setenv("PAPERLESS_SHARE_CORRESPONDENTS", "yes")
    assert Settings.from_env().paperless_share_kinds == ("tags", "correspondents", "document_types")


def test_a_duplicate_name_does_not_hold_up_the_others(caplog):
    api = FakeApi(
        correspondents=[obj(1, "Bank", None), obj(2, "bank", TAGGER), obj(3, "Doctor", ADMIN), obj(4, "Employer", TAGGER)]
    )
    sharer = sharer_for(api, kinds=("correspondents",))

    with caplog.at_level(logging.INFO):
        sharer.check_all()
        sharer.check_all()  # the duplicate is still there: no second warning

    store = api.store["correspondents"]
    assert store[3]["owner"] is None and store[4]["owner"] is None
    assert store[2]["owner"] == TAGGER  # left as it was
    stuck = [r for r in caplog.records if r.message == "correspondents not shared"]
    assert len(stuck) == 1 and stuck[0].id == 2 and stuck[0].same_name_as == 1
    shared = next(r for r in caplog.records if r.message == "correspondents shared")
    assert shared.ids == [3, 4]
    assert "Bank" not in caplog.text and "bank" not in str(vars(stuck[0]))


def test_two_owned_objects_with_one_name_share_the_first():
    api = FakeApi(document_types=[obj(1, "Invoice", ADMIN), obj(2, "Invoice", TAGGER)])
    sharer_for(api, kinds=("document_types",)).check_all()
    owners = [o["owner"] for o in api.store["document_types"].values()]
    assert owners == [None, TAGGER]


def test_a_merged_duplicate_is_shared_in_the_next_round(caplog):
    api = FakeApi([tag(1, "Health", None), tag(2, "Health", TAGGER), tag(3, "Car", ADMIN)])
    sharer = sharer_for(api)
    sharer.check_all()
    del api.store["tags"][1]  # merged by hand in Paperless
    with caplog.at_level(logging.INFO):
        sharer.check_all()
    assert api.tags[2]["owner"] is None
    assert next(r for r in caplog.records if r.message == "tags shared").ids == [2]
