from __future__ import annotations

import pytest

from stacksplit.config import Settings, auto_jobs

GB = 2**30


@pytest.mark.parametrize(
    ("memory_gb", "cpus", "jobs"),
    [(2, 12, 1), (4, 12, 4), (5, 12, 5), (8, 12, 9), (64, 12, 12), (4, 2, 2), (1, 12, 1)],
)
def test_jobs_follow_memory_then_cpus(memory_gb, cpus, jobs):
    assert auto_jobs(memory_gb * GB, cpus) == jobs


def test_unknown_memory_stays_modest():
    assert auto_jobs(None, 12) == 4


def test_jobs_are_shared_between_inputs(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("OCRMYPDF_JOBS", "6")
    settings = Settings.from_env()
    assert {p.name: settings.jobs_for(p) for p in settings.profiles} == {"stacks": 5, "scanner": 1}

    monkeypatch.setenv("PAPERLESS_URL", "http://p:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    settings = Settings.from_env()
    assert {p.name: settings.jobs_for(p) for p in settings.profiles} == {"stacks": 4, "scanner": 1, "paperless": 1}
    assert sum(settings.jobs_for(p) for p in settings.profiles) == 6

    monkeypatch.setenv("STACKS_ENABLED", "false")
    settings = Settings.from_env()
    assert {p.name: settings.jobs_for(p) for p in settings.profiles} == {"scanner": 3, "paperless": 3}


def test_jobs_setting_accepts_auto(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("OCRMYPDF_JOBS", "auto")
    assert Settings.from_env().ocrmypdf_jobs >= 1


def test_budget_makes_runs_wait_instead_of_overcommitting():
    import threading
    import time

    from stacksplit.pdfops import JobBudget

    budget = JobBudget(2)
    running, peak, lock = [0], [0], threading.Lock()

    def run(jobs):
        with budget.reserve(jobs) as granted:
            with lock:
                running[0] += granted
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= granted

    threads = [threading.Thread(target=run, args=(n,)) for n in (2, 1, 1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert peak[0] <= 2  # never more jobs at once than the budget
    assert not any(t.is_alive() for t in threads)  # and a request above the budget still runs (capped)
