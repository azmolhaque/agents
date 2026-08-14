"""PHASE 0 ACCEPTANCE GATE.

    Enqueue 100 jobs, kill -9 the worker mid-run, restart, all 100 complete
    exactly once.

This is the test the whole "durable SQLite queue instead of Redis" argument exists to
pass. It spawns real subprocesses and sends real SIGKILLs — nothing is simulated,
because the failure mode being defended against (power loss on a Pi) cannot be
faithfully mocked.

Exactly-once is asserted three ways at the end:
  * 100 side-effect rows, one per job
  * every ``job_id`` distinct (a duplicate would have raised IntegrityError anyway,
    since it is the PRIMARY KEY)
  * every payload ``n`` from 0..99 present, so nothing was skipped
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JOB_COUNT = 100

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _env(db: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "DB_PATH": str(db),
            "LOG_DIR": str(db.parent / "log"),
            "CACHE_DIR": str(db.parent / "cache"),
            "ENVIRONMENT": "test",
        }
    )
    return env


def _cindra(*args: str, db: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cindraleads.cli", *args],
        cwd=REPO_ROOT,
        env=_env(db),
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def _spawn_worker(db: Path, worker_id: str, *, work_ms: int = 6) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cindraleads.cli",
            "work",
            "--kinds",
            "selftest.sideeffect",
            "--worker-id",
            worker_id,
            "--lease",
            "2",
            "--work-ms",
            str(work_ms),
            "--no-idle-exit",
        ],
        cwd=REPO_ROOT,
        env=_env(db),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _reap(worker: subprocess.Popen[str], *, timeout: int = 10) -> None:
    """Wait and release the pipes. Leaked fds surface as ResourceWarnings, which
    this suite treats as errors."""
    try:
        worker.wait(timeout=timeout)
    finally:
        for stream in (worker.stdout, worker.stderr):
            if stream is not None:
                stream.close()


def _done_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0])
    finally:
        conn.close()


def _side_effect_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM selftest_side_effects").fetchone()[0])
    finally:
        conn.close()


def test_100_jobs_survive_repeated_sigkill_and_complete_exactly_once(tmp_path: Path):
    db = tmp_path / "crash.db"

    _cindra("selftest", "prepare", "--count", str(JOB_COUNT), db=db)
    assert _done_count(db) == 0

    # --- kill the worker three times, mid-transaction, while it grinds ---------
    for round_number in range(3):
        worker = _spawn_worker(db, f"victim-{round_number}")
        deadline = time.monotonic() + 15
        start_count = _done_count(db)
        # Let it make real progress before we pull the plug.
        while time.monotonic() < deadline and _done_count(db) < start_count + 8:
            if worker.poll() is not None:
                break
            time.sleep(0.05)

        if worker.poll() is None:
            os.kill(worker.pid, signal.SIGKILL)
        _reap(worker)
        assert worker.returncode in (-signal.SIGKILL, -9, 0), worker.returncode

        # A SIGKILL cannot corrupt the invariant even before recovery runs:
        # no job may have produced two side effects.
        assert _side_effect_count(db) == _done_count(db), (
            "a side effect committed without its job being marked done, or vice versa"
        )
        if _done_count(db) >= JOB_COUNT:
            break

    # --- restart clean and let it drain ---------------------------------------
    # --drain-inflight: the killed workers left jobs holding leases that have not
    # expired yet. Those are recoverable, just not *yet* claimable, so the survivor
    # has to outwait them rather than declaring the queue empty.
    survivor = _cindra(
        "work",
        "--kinds",
        "selftest.sideeffect",
        "--worker-id",
        "survivor",
        "--lease",
        "30",
        "--work-ms",
        "0",
        "--drain-inflight",
        db=db,
        check=False,
    )
    assert survivor.returncode == 0, survivor.stderr

    # --- the gate --------------------------------------------------------------
    verify = _cindra("selftest", "verify", "--expect", str(JOB_COUNT), db=db, check=False)
    assert verify.returncode == 0, f"{verify.stdout}\n{verify.stderr}"

    conn = sqlite3.connect(db)
    try:
        total, distinct_jobs, distinct_n = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT job_id), COUNT(DISTINCT n) FROM selftest_side_effects"
        ).fetchone()
        assert total == JOB_COUNT
        assert distinct_jobs == JOB_COUNT, "a job ran twice"
        assert distinct_n == JOB_COUNT, "a job never ran"
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status != 'done'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 0
    finally:
        conn.close()


def test_two_workers_racing_never_double_process(tmp_path: Path):
    """Concurrency, not crashes: BEGIN IMMEDIATE must serialize the claim so a job
    is handed to exactly one worker even under contention."""
    db = tmp_path / "race.db"
    _cindra("selftest", "prepare", "--count", "40", db=db)

    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cindraleads.cli",
                "work",
                "--kinds",
                "selftest.sideeffect",
                "--worker-id",
                f"racer-{i}",
                "--lease",
                "30",
                "--work-ms",
                "2",
                "--drain-inflight",
            ],
            cwd=REPO_ROOT,
            env=_env(db),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for i in range(4)
    ]
    for worker in workers:
        _reap(worker, timeout=180)

    verify = _cindra("selftest", "verify", "--expect", "40", db=db, check=False)
    assert verify.returncode == 0, f"{verify.stdout}\n{verify.stderr}"


def test_worker_reclaims_orphans_left_by_a_previous_boot(tmp_path: Path):
    """On boot the pipeline resumes mid-flight work. Simulate the aftermath of a
    power cut: rows stuck in_flight with a lease in the past, and no live worker."""
    db = tmp_path / "orphans.db"
    _cindra("selftest", "prepare", "--count", "10", db=db)

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE jobs SET status='in_flight', worker_id='ghost', "
        "lease_expires_at='2000-01-01T00:00:00.000Z'"
    )
    conn.commit()
    conn.close()

    result = _cindra("work", "--kinds", "selftest.sideeffect", "--worker-id", "boot", db=db)
    assert "processed 10" in result.stdout
    verify = _cindra("selftest", "verify", "--expect", "10", db=db, check=False)
    assert verify.returncode == 0, f"{verify.stdout}\n{verify.stderr}"
