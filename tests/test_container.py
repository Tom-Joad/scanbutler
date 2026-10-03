from __future__ import annotations

import os
import tempfile

from scanbutler import config
from scanbutler.__main__ import prepare_tmpdir


def test_work_dir_is_config_inside_the_container(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    data = tmp_path / "data"
    assert config.default_work_dir(data) == data / "work"  # outside the container
    (tmp_path / "config").mkdir()
    assert config.default_work_dir(data) == tmp_path / "config"


def test_work_dir_setting_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "elsewhere"))
    assert config.Settings.from_env().work_dir == tmp_path / "elsewhere"


def test_temp_files_go_to_the_work_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", None)
    prepare_tmpdir(tmp_path / "work")
    assert os.environ["TMPDIR"] == str(tmp_path / "work" / "tmp")
    assert tempfile.gettempdir() == str(tmp_path / "work" / "tmp")


def test_unusable_tmpdir_falls_back_to_the_work_folder(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("TMPDIR", str(blocker / "tmp"))  # below a file: can't be created
    monkeypatch.setattr(tempfile, "tempdir", None)
    prepare_tmpdir(tmp_path / "work")
    assert os.environ["TMPDIR"] == str(tmp_path / "work" / "tmp")
