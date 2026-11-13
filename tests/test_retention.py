from __future__ import annotations

import os
import time
from pathlib import PurePosixPath

from scanbutler import retention
from scanbutler.config import Settings
from scanbutler.pipeline import process_stack

from .conftest import FakeBackend, make_pdf
from .test_paperless import paperless_settings  # noqa: F401

DAY = 86400


def age(work, days):
    old = time.time() - days * DAY
    for path in (work, *work.rglob("*")):
        os.utime(path, (old, old))


def finished(root, name, days, folder=""):
    work = root / folder / name if folder else root / name
    work.mkdir(parents=True)
    (work / "plan.json").write_text("{}")
    (work / "review.md").write_text("# Review")
    (work / "searchable.pdf").write_bytes(b"x" * 2048)
    age(work, days)
    return work


def test_only_finished_folders_past_the_retention_go(settings):
    root = settings.work_dir / "scanner"
    old = finished(root, "old-1234", 40, folder="Household/2026")
    recent = finished(root, "recent-5678", 5)
    unfinished = root / "failed-9999"  # no review.md: failed or interrupted
    unfinished.mkdir(parents=True)
    (unfinished / "plan.json").write_text("{}")
    age(unfinished, 400)

    removed, freed = retention.clean(settings)

    assert (removed, freed > 2000) == (1, True)
    assert not old.exists()
    assert not (root / "Household").exists()  # emptied sub-folders go too
    assert recent.exists() and unfinished.exists()


def test_a_folder_in_use_is_kept(settings):
    work = finished(settings.work_dir / "stacks", "busy-0001", 90)
    with retention.in_use(work):
        assert retention.clean(settings) == (0, 0)
    assert retention.clean(settings)[0] == 1


def test_zero_switches_cleanup_off(settings, monkeypatch):
    work = finished(settings.work_dir / "stacks", "old-0001", 900)
    monkeypatch.setenv("WORK_RETENTION_DAYS", "0")
    assert retention.clean(Settings.from_env()) == (0, 0)
    assert work.exists()


def test_paperless_ledger_and_unrelated_folders_are_never_touched(settings, monkeypatch):
    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test")
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    settings = Settings.from_env()
    ledger = settings.work_dir / "paperless" / "uploaded.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{}")
    stray = settings.work_dir / "paperless" / "x"  # even if it looked finished
    stray.mkdir()
    (stray / "plan.json").write_text("{}")
    (stray / "review.md").write_text("")
    for path in (ledger, stray):
        age(path.parent, 400)

    assert retention.clean(settings) == (0, 0)
    assert ledger.exists() and stray.exists()


def test_a_processed_file_counts_as_finished(settings, tmp_path):
    scanner = settings.profile("scanner")
    src = tmp_path / "scan.pdf"
    make_pdf(src, 2)
    backend = FakeBackend(["Blutbild dated report with enough text", "second page with enough text as well"])
    work = process_stack(src, PurePosixPath(""), settings, backend, scanner)

    assert retention.clean(settings) == (0, 0)  # fresh
    age(work, 31)
    assert retention.clean(settings)[0] == 1
    assert not work.exists()
    assert list(scanner.output.glob("*.pdf"))  # outputs are not part of it


def test_failed_paperless_work_folders_go_but_not_the_ledger_or_waiting_uploads(paperless_settings):
    root = paperless_settings.work_dir / "paperless"
    failed = finished(root, "failed-0001", 40)
    (failed / "review.md").unlink()
    (failed / "plan.json").unlink()
    age(failed, 40)
    waiting = finished(root, "waiting-0002", 40)
    (waiting / "paperless_task.json").write_text("{}")
    age(waiting, 40)
    young = finished(root, "young-0003", 3)
    (root / "uploaded.json").write_text("{}")
    age(root / "uploaded.json", 400)

    removed, _ = retention.clean(paperless_settings)

    assert removed == 1
    assert not failed.exists()
    assert waiting.exists() and young.exists() and (root / "uploaded.json").exists()


def test_old_temp_leftovers_go_recent_ones_stay(settings):
    tmp = settings.work_dir / "tmp"
    stale, fresh = tmp / "ocrmypdf.io.abc", tmp / "ocrmypdf.io.live"
    for folder in (stale, fresh):
        folder.mkdir(parents=True)
        (folder / "page.png").write_bytes(b"x" * 100)
    age(stale, 5)
    (tmp / "old-file.tmp").write_bytes(b"x")
    age(tmp / "old-file.tmp", 5)

    retention.clean(settings)

    assert not stale.exists() and not (tmp / "old-file.tmp").exists()
    assert fresh.exists()
