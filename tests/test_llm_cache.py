from __future__ import annotations

from scanbutler.llm_cache import CachedChat


class Counting:
    llm_model = "model-a"

    def __init__(self):
        self.calls = 0

    def chat_json(self, system, user, schema, name):
        self.calls += 1
        return {"n": self.calls}


def test_cache_hits_until_prompt_or_model_changes(tmp_path):
    backend = Counting()
    chat = CachedChat(backend, tmp_path)

    assert chat.chat_json("sys", "user", {}, "x") == {"n": 1}
    assert chat.chat_json("sys", "user", {}, "x") == {"n": 1}
    assert CachedChat(backend, tmp_path).chat_json("sys", "user", {}, "x") == {"n": 1}
    assert chat.chat_json("sys v2", "user", {}, "x") == {"n": 2}

    backend.llm_model = "model-b"
    assert CachedChat(backend, tmp_path).chat_json("sys", "user", {}, "x") == {"n": 3}
