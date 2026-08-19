"""The worker loop driving two-phase stages.

The loop is where the exactly-once promise is actually kept, so the properties pinned
here are the ones a future refactor could quietly lose: follow-on jobs and the
completion share one COMMIT, a stage that fails leaves nothing behind, and the network
phase happens outside the write transaction.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from cindraleads.agents import EXTRACT_KIND, HARVEST_KIND, Harvester
from cindraleads.cli import HANDLERS, SELFTEST_KIND, _stages_for, _work_loop
from cindraleads.models import Job, QueryPlan, StageResult
from cindraleads.queue import JobQueue
from cindraleads.sources import DocumentCache, EgressClient, SourceBreakers, SourceRegistry
from cindraleads.store import Store

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"

OPTS: dict[str, Any] = {
    "worker_id": "w1",
    "lease": 30,
    "max_jobs": 0,
    "idle_exit": True,
    "drain_inflight": False,
    "poll_ms": 1,
    "work_ms": 0,
}


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = Store(tmp_path / "w.db", migrations_dir=MIGRATIONS)
    s.migrate()
    yield s
    s.close()


def make_harvester(store: Store, tmp_path: Path, *urls: str) -> Harvester:
    registry = SourceRegistry.from_dict(
        {
            "sources": [
                {"id": "hn_algolia", "legality_class": "licensed_api", "cache_ttl_hours": 1}
            ],
            "defaults": {"retries": 1, "backoff_base_seconds": 0.001},
        }
    )
    hits = [{"objectID": str(i), "title": f"t{i}", "url": u} for i, u in enumerate(urls)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"hits": hits}))

    egress = EgressClient(
        store=store,
        registry=registry,
        cache=DocumentCache(store, cache_dir=tmp_path / "cache"),
        breakers=SourceBreakers(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return Harvester(store=store, egress=egress, queue=JobQueue(store))


def candidate_count(store: Store) -> int:
    return int(store.conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"])


# ------------------------------------------------------------------- happy path


async def test_a_harvest_job_enqueues_its_extract_jobs_and_completes(store, tmp_path):
    harvester = make_harvester(store, tmp_path, "https://a.io", "https://b.io")
    queue = JobQueue(store)
    plan = QueryPlan(query="ai", engine="hn_algolia", targets=["T1_AI_SHIP"])
    with store.tx() as conn:
        queue.enqueue(HARVEST_KIND, plan.model_dump(mode="json"), conn=conn)

    processed = await _work_loop(store, {HARVEST_KIND: harvester}, kinds=[HARVEST_KIND], **OPTS)

    assert processed == 1
    assert candidate_count(store) == 2
    rows = store.conn.execute("SELECT kind, status FROM jobs ORDER BY kind").fetchall()
    kinds = [(r["kind"], r["status"]) for r in rows]
    assert (HARVEST_KIND, "done") in kinds
    assert kinds.count((EXTRACT_KIND, "pending")) == 2
    await harvester.egress.aclose()


async def test_the_worker_does_not_pick_up_the_jobs_it_just_created(store, tmp_path):
    """`--kinds harvest.query` must not silently start extracting.

    The extract stage does not exist yet; if the loop ran anything it was handed, a
    typo in --kinds would run the wrong stage instead of failing loudly.
    """
    harvester = make_harvester(store, tmp_path, "https://a.io")
    queue = JobQueue(store)
    with store.tx() as conn:
        queue.enqueue(
            HARVEST_KIND, QueryPlan(query="x", engine="hn_algolia").model_dump(), conn=conn
        )

    await _work_loop(store, {HARVEST_KIND: harvester}, kinds=[HARVEST_KIND], **OPTS)

    assert queue.stats()["pending"] == 1, "the extract job is queued, not run"
    await harvester.egress.aclose()


# ------------------------------------------------------------------ atomicity


class _WritesThenFails:
    """A stage that writes a row and then reports failure."""

    async def prepare(self, job: Job) -> Any:
        return None

    def commit(self, job: Job, outcome: Any, conn: sqlite3.Connection) -> StageResult:
        conn.execute(
            "INSERT INTO candidates (candidate_id, content_sha256, raw_payload, status, "
            "created_at) VALUES ('c1','','{}','new','2026-01-01T00:00:00Z')"
        )
        return StageResult(ok=False, stage="x", job_id=job.job_id, error="deliberate")


async def test_a_failing_stage_leaves_nothing_behind(store):
    """ok=False must roll the stage's own writes back.

    Otherwise a half-written candidate survives, the job is retried, and the URL
    dedupe treats the debris as work already done -- the exact failure the
    prepare/commit split was introduced to prevent.
    """
    queue = JobQueue(store)
    with store.tx() as conn:
        queue.enqueue("x.kind", {}, conn=conn)

    processed = await _work_loop(store, {"x.kind": _WritesThenFails()}, kinds=["x.kind"], **OPTS)

    assert processed == 0
    assert candidate_count(store) == 0, "the failed stage's write was rolled back"
    row = store.conn.execute("SELECT status, last_error FROM jobs").fetchone()
    assert row["status"] in ("pending", "failed", "dead")
    assert "deliberate" in (row["last_error"] or "")


class _WritesThenRaises:
    async def prepare(self, job: Job) -> Any:
        return None

    def commit(self, job: Job, outcome: Any, conn: sqlite3.Connection) -> StageResult:
        conn.execute(
            "INSERT INTO candidates (candidate_id, content_sha256, raw_payload, status, "
            "created_at) VALUES ('c2','','{}','new','2026-01-01T00:00:00Z')"
        )
        raise ValueError("boom mid-commit")


async def test_a_raising_stage_also_rolls_back(store):
    queue = JobQueue(store)
    with store.tx() as conn:
        queue.enqueue("x.kind", {}, conn=conn)

    await _work_loop(store, {"x.kind": _WritesThenRaises()}, kinds=["x.kind"], **OPTS)

    assert candidate_count(store) == 0


class _PreparesBadly:
    async def prepare(self, job: Job) -> Any:
        raise OSError("network is down")

    def commit(self, job: Job, outcome: Any, conn: sqlite3.Connection) -> StageResult:
        raise AssertionError("commit must not run after prepare failed")


async def test_a_failed_prepare_never_reaches_commit(store):
    queue = JobQueue(store)
    with store.tx() as conn:
        queue.enqueue("x.kind", {}, conn=conn)

    await _work_loop(store, {"x.kind": _PreparesBadly()}, kinds=["x.kind"], **OPTS)

    row = store.conn.execute("SELECT status, last_error FROM jobs").fetchone()
    assert "network is down" in (row["last_error"] or "")


# ------------------------------------------------------------------ dispatch


async def test_an_unregistered_kind_fails_the_job_rather_than_running_it(store):
    queue = JobQueue(store)
    with store.tx() as conn:
        queue.enqueue("nobody.handles.this", {}, conn=conn)

    await _work_loop(store, {}, kinds=["nobody.handles.this"], **OPTS)

    row = store.conn.execute("SELECT last_error FROM jobs").fetchone()
    assert "no handler registered" in (row["last_error"] or "")


def test_a_selftest_worker_builds_no_runtime():
    """The durability drill must not need an HTTP client, a source registry or an
    event loop's worth of pipeline machinery to run 100 trivial jobs."""
    stages = _stages_for([SELFTEST_KIND], None)
    assert set(stages) == {SELFTEST_KIND}
    assert HARVEST_KIND not in stages


def test_every_sync_handler_is_reachable_through_the_stage_adapter():
    """A handler added to HANDLERS but not exposed by _stages_for would be dead code
    that fails at runtime as 'no handler registered'."""
    stages = _stages_for(list(HANDLERS), None)
    assert set(stages) == set(HANDLERS)


# ------------------------------------------------- a slow stage keeps its own lease


class _SlowStage:
    """A stage whose `prepare` outlives the lease, as an LLM call on this Pi does."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def prepare(self, job: Job) -> Any:
        import asyncio

        await asyncio.sleep(self.seconds)
        return {"ok": True}

    def commit(self, job: Job, outcome: Any, conn: sqlite3.Connection) -> StageResult:
        return StageResult(ok=True, stage=job.kind, job_id=job.job_id)


async def test_a_stage_slower_than_its_lease_is_not_reclaimed_out_from_under_itself(
    store: Store,
) -> None:
    """Decode on the Pi is 3.7 tok/s, so a scoring stage routinely outlives a fixed
    lease. Before the worker renewed it, the job was reclaimed *while still being
    worked on*: the original worker then hit `LeaseLost` at commit and threw away work
    it had actually finished, and the reclaim charged the job an attempt for it.
    """
    queue = JobQueue(store)
    job_id = queue.enqueue("slow.kind")

    opts = {**OPTS, "lease": 1}  # renewals fall due every ~5 s floor, well inside it
    processed = await _work_loop(store, {"slow.kind": _SlowStage(0.3)}, kinds=["slow.kind"], **opts)

    assert processed == 1
    job = queue.get(job_id)
    assert job is not None and job.status == "done"
    assert job.reclaims == 0


async def test_a_stage_that_never_returns_is_cancelled_rather_than_renewed_forever(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound on the fix. Renewing the lease and petting the watchdog while a stage
    runs would otherwise mean a stage wedged in a socket read with no timeout keeps
    both alive indefinitely -- defeating the two mechanisms that exist to catch exactly
    that. Past the deadline the job fails honestly instead.
    """
    from cindraleads import cli

    monkeypatch.setattr(cli, "MAX_STAGE_SECONDS", 0.2)
    queue = JobQueue(store)
    job_id = queue.enqueue("slow.kind", max_attempts=1)

    processed = await _work_loop(
        store, {"slow.kind": _SlowStage(30.0)}, kinds=["slow.kind"], **{**OPTS, "lease": 1}
    )

    assert processed == 0
    job = queue.get(job_id)
    assert job is not None and job.status == "dead"
    reason = store.conn.execute(
        "SELECT last_error FROM dead_letter WHERE job_id=?", (job_id,)
    ).fetchone()["last_error"]
    assert "exceeded" in reason and "cancelled" in reason


# ------------------------------------ the two deadlines, only one of which is ours


def test_the_renewal_interval_never_outlasts_the_watchdog() -> None:
    """The bug that crash-looped the worker twelve times.

    The interval was `lease / 3`. The unit runs `--lease 600`, so that is 200 seconds --
    and `WatchdogSec=180` means systemd sends SIGABRT at 180. Every stage slower than
    three minutes sat inside `asyncio.wait` for 200 s without petting and was killed by
    the watchdog while it was working correctly.

    Half the pet interval, not the whole of it: `Watchdog.pet()` rate-limits itself, so
    waking at exactly the interval lets jitter push a ping past its own gate, and the
    next chance is the deadline.
    """
    from cindraleads.cli import _renewal_interval
    from cindraleads.sdnotify import Watchdog

    watchdog = Watchdog(interval_seconds=90.0)  # WatchdogSec=180

    assert _renewal_interval(600, watchdog) == 45.0
    assert _renewal_interval(600, watchdog) < watchdog.interval


def test_a_short_lease_still_wins_over_the_watchdog_bound() -> None:
    """Whichever deadline is nearer decides. A 30 s lease must renew every 10 s even
    though the watchdog would tolerate 45."""
    from cindraleads.cli import _renewal_interval
    from cindraleads.sdnotify import Watchdog

    assert _renewal_interval(30, Watchdog(interval_seconds=90.0)) == 10.0


def test_without_a_watchdog_the_lease_alone_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run by hand or in a test there is no `WATCHDOG_USEC`, and inventing a bound
    would make the worker write to the queue three times as often for nothing."""
    from cindraleads.cli import _renewal_interval
    from cindraleads.sdnotify import Watchdog

    inert = Watchdog(interval_seconds=0.0)
    assert not inert.enabled
    assert _renewal_interval(600, inert) == 200.0


async def test_a_slow_stage_keeps_the_heartbeat_alive(store: Store) -> None:
    """A stage slower than `HEARTBEAT_GAP_SECONDS` must not read as a dead worker.

    The main loop writes the heartbeat every 60 s and does not get to run while a stage
    does, so a legitimately slow scoring job would appear in `cindra acceptance` as a
    gap in the worker's record -- the exact signal that means "the worker died and came
    back". The renewal loop writes one on every tick for the same reason it pets the
    watchdog.
    """
    from cindraleads.metrics import HEARTBEAT_METRIC

    queue = JobQueue(store)
    queue.enqueue("slow.kind")

    before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM metrics WHERE name = ?", (HEARTBEAT_METRIC,)
    ).fetchone()["n"]

    # lease 3 -> renewals every second, so a 2.5 s stage ticks at least twice
    await _work_loop(
        store, {"slow.kind": _SlowStage(2.5)}, kinds=["slow.kind"], **{**OPTS, "lease": 3}
    )

    after = store.conn.execute(
        "SELECT COUNT(*) AS n FROM metrics WHERE name = ?", (HEARTBEAT_METRIC,)
    ).fetchone()["n"]
    # start + at least one mid-stage tick + the exit beat
    assert after - before >= 3
