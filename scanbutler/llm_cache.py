"""Disk cache for schema-constrained chat answers.

The key covers model, prompts and schema, so changing any of them asks the
model again, while a run that died halfway (rate limit, network, restart)
resumes without paying for answers it already has.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class CachedChat:
    def __init__(self, backend, cache_dir: Path) -> None:
        self._backend = backend
        self._dir = cache_dir
        self._model = getattr(backend, "llm_model", "")

    def chat_json(self, system: str, user: str, schema: dict, name: str) -> dict:
        key = hashlib.sha256(
            json.dumps([self._model, name, system, user, schema], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        path = self._dir / f"{name}-{key[:24]}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        answer = self._backend.chat_json(system, user, schema, name)
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return answer
