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


class FakeCgroup:
    """Stand-in for config.Path that serves chosen /sys/fs/cgroup files."""

    files: dict[str, str] = {}

    def __init__(self, path):
        self.path = str(path)

    def read_text(self):
        if self.path not in self.files:
            raise OSError(self.path)
        return self.files[self.path]


@pytest.mark.parametrize(
    ("affinity", "files", "cpus"),
    [
        (12, {"/sys/fs/cgroup/cpu.max": "max 100000"}, 12),  # no limit
        (12, {"/sys/fs/cgroup/cpu.max": "200000 100000"}, 2),  # --cpus=2
        (12, {"/sys/fs/cgroup/cpu.max": "150000 100000"}, 1),  # --cpus=1.5
        (3, {"/sys/fs/cgroup/cpu.max": "max 100000"}, 3),  # --cpuset-cpus=0-2
        (12, {"/sys/fs/cgroup/cpu.max": "50000 100000"}, 1),  # --cpus=0.5 still gets one job
        # cgroup v1
        (12, {"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "400000", "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"}, 4),
        (12, {"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1", "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"}, 12),
    ],
)
def test_available_cpus_respects_container_limits(monkeypatch, affinity, files, cpus):
    import stacksplit.config as config

    FakeCgroup.files = files
    monkeypatch.setattr(config, "Path", FakeCgroup)
    monkeypatch.setattr(config.os, "sched_getaffinity", lambda pid: set(range(affinity)))
    assert config.available_cpus() == cpus
