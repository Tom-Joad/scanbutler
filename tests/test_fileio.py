from __future__ import annotations

import json
from pathlib import Path

import pytest

from scanbutler import fileio
from scanbutler.pipeline import _read_ink


def test_write_json_replaces_the_file(tmp_path):
    path = tmp_path / "state.json"
    fileio.write_json(path, {"a": 1})
    fileio.write_json(path, {"a": 2}, indent=1)
    assert json.loads(path.read_text()) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_failure_mid_write_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    fileio.write_json(path, {"job": "paid"})

    def full_disk(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", full_disk)
    with pytest.raises(OSError):
        fileio.write_json(path, {"job": "other"})
    monkeypatch.undo()

    assert json.loads(path.read_text()) == {"job": "paid"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_an_unreadable_ink_file_is_measured_again(tmp_path):
    path = tmp_path / "ink.json"
    assert _read_ink(path) is None
    path.write_text("[0.1, 0.", encoding="utf-8")  # cut off
    assert _read_ink(path) is None
    path.write_text("[0.1, 0.2]", encoding="utf-8")
    assert _read_ink(path) == [0.1, 0.2]
