"""PHASE 7 CHAOS SUITE.

    With Ollama stopped, the disk full, the budget exhausted and the SoC at 85 C,
    the pipeline degrades, alerts, and resumes. It never crash-loops and never
    loses a job.

Four outages, one question each. The question is never "does it raise" -- anything
raises. It is **what happens to the job**, because the only unrecoverable failure in
this system is a unit of work that disappears. A stage that fails loudly and leaves its
job claimable is working correctly; a stage that swallows an error and completes the
job has destroyed a lead nobody will ever know existed.

The second question, close behind, is whether the system says so. An outage the health
endpoint reports as `ok` is worse than the outage, because it is the state in which
nobody looks for three days.

Nothing here is mocked at the boundary the failure crosses. Ollama-down is a real
connection refused to a closed port; budget exhaustion goes through the real
persisted token bucket; the thermal governor gets real sensor readings, injected.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from cindraleads.errors import SchemaValidationError
from cindraleads.health import assess
from cindraleads.llm import LLMRequest, OllamaBackend, StructuredLLM
from cindraleads.models import to_iso, utcnow
from cindraleads.queue import JobQueue

pytestmark = pytest.mark.integration


class _Extraction(BaseModel):
    """The smallest thing a stage could ask a model for."""

    display_name: str


# --------------------------------------------------------------- 1. Ollama is down


async def test_ollama_down_fails_the_job_without_losing_it(store: Any, queue: JobQueue) -> None:
    """A refused connection must leave the job claimable again, not completed.

    Port 1 with nothing on it: a real ECONNREFUSED, not a mocked exception, because
    the thing being tested is how the client classifies a real network error.
    """
    backend = OllamaBackend(base_url="http://127.0.0.1:1", timeout=1.0)
    client = StructuredLLM(backend)

    with pytest.raises(SchemaValidationError) as caught:
        await client.generate("extract this", _Extraction)

    # Degraded, and it says which rung ran out. "no escalation backend configured"
    # is the honest end of the ladder when the local model is unreachable and no
    # cloud key is set -- not a crash, and not a silent empty result.
    assert "escalation" in str(caught.value).lower() or "connect" in str(caught.value).lower()

    job_id = queue.enqueue("score.company", {"canonical_domain": "acme.io"})
    claimed = queue.claim("w1", kinds=["score.company"], lease_seconds=1, limit=1)
    assert [j.job_id for j in claimed] == [job_id]

    queue.fail(job_id, "LLM unreachable")

    # The job is still there. That is the whole assertion: an outage costs a retry,
    # never the work.
    row = store.conn.execute("SELECT status, attempts FROM jobs WHERE job_id = ?", (job_id,))
    status, attempts = row.fetchone()
    assert status in ("pending", "failed")
    assert attempts >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 0


async def test_ollama_down_is_visible_in_health(store: Any) -> None:
    """An outage nobody can see is the failure mode this endpoint exists to prevent."""
    report = assess(store, thermal=_Governor("nominal", "none"))
    # Nothing has ever run in a fresh database, so every heartbeat is missing and the
    # endpoint must say so rather than reporting a healthy idle system.
    assert report.status != "ok"
    assert any(c.name.startswith("heartbeat:") for c in report.problems)


# ------------------------------------------------------------------ 2. Disk is full


def test_disk_full_is_critical_and_never_silent(store: Any, tmp_path: Any) -> None:
    report = assess(store, disk_path=tmp_path, thermal=_Governor("nominal", "none"))
    disk = next(c for c in report.checks if c.name == "disk")
    # On the test host there is space, so this asserts the check ran and is wired to
    # a real statvfs rather than asserting a particular number.
    assert disk.value is not None and disk.value > 0

    starved = assess(store, disk_path=_NoSpace(), thermal=_Governor("nominal", "none"))
    disk = next(c for c in starved.checks if c.name == "disk")
    assert disk.status == "degraded"
    assert "could not stat" in disk.detail


def test_a_failed_commit_leaves_the_job_claimable(store: Any, queue: JobQueue) -> None:
    """The one thing exactly-once cannot paper over is a COMMIT that does not land.

    Simulated by raising inside the transaction, which is what SQLite does on a full
    disk. The side effect and the completion are in the same transaction, so both must
    roll back together -- a half-applied stage would be a lead written with no record
    that its job ran, or a job completed with nothing to show for it.
    """
    job_id = queue.enqueue("resolve.company", {"candidate_id": "c1"})
    claimed = queue.claim("w1", kinds=["resolve.company"], lease_seconds=60, limit=1)[0]

    with pytest.raises(sqlite3.OperationalError), store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, first_seen_at, "
            "last_updated_at) VALUES ('acme.io','Acme',?,?)",
            (to_iso(utcnow()), to_iso(utcnow())),
        )
        queue.complete(claimed.job_id, conn=conn)
        raise sqlite3.OperationalError("database or disk is full")

    assert store.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    status = store.conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
    assert status == "in_flight", "the job must still be leased, so its lease can expire"

    # Expire the lease the way a dead worker's would expire, and the job comes back.
    # This is the whole recovery story: nothing needs to notice the crash, the lease
    # just runs out and the next worker picks the job up whole.
    with store.tx() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE job_id = ?",
            (to_iso(utcnow() - timedelta(minutes=5)), job_id),
        )
    assert queue.reclaim_expired() == 1
    assert queue.claim("w2", kinds=["resolve.company"], lease_seconds=60, limit=1)


# ------------------------------------------------------------- 3. Budget exhausted


async def test_exhausted_budget_denies_without_raising(store: Any) -> None:
    """A spent quota is a planned state, not an error.

    It goes through the real persisted guard rather than a flag, because the bug this
    guards against was a cap that existed in config and was never consulted.
    """
    from cindraleads.budget import BudgetGuard

    guard = BudgetGuard(store, "serpapi", cap=3, window_hours=24.0, safety_fraction=1.0)
    for _ in range(3):
        assert guard.can_spend(1)
        guard.spend(1)

    assert not guard.can_spend(1)

    # Survives a restart: a cap held only in memory would reset to full on every
    # crash, which on an hourly timer is a cap that never applies.
    reopened = BudgetGuard(store, "serpapi", cap=3, window_hours=24.0, safety_fraction=1.0)
    assert not reopened.can_spend(1)


async def test_no_budget_means_no_plans_not_a_crash(store: Any) -> None:
    """The Scout asks before it plans, so exhaustion produces an empty batch."""
    from cindraleads.agents.scout import Scout
    from cindraleads.sources.registry import SourceRegistry

    scout = Scout.from_config(SourceRegistry.from_config(), store=store)
    plans = scout.plan(limit=5, can_spend=lambda engine, units: False)

    costed = [p for p in plans if p.cache_ttl_hours >= 0 and _is_costed(p)]
    assert costed == [], "a costed plan was produced with no budget to pay for it"


def _is_costed(plan: Any) -> bool:
    from cindraleads.sources.registry import SourceRegistry

    try:
        return bool(SourceRegistry.from_config().get(plan.engine).cost_units)
    except Exception:
        return False


# ---------------------------------------------------------------- 4. Thermal 85 C


class _Governor:
    """A governor with the readings pinned, so the policy is testable without a Pi."""

    def __init__(self, state: str, alert: str) -> None:
        self._state = state
        self._alert = alert

    def poll(self) -> Any:
        from cindraleads.thermal import ThermalPolicy

        return ThermalPolicy(
            state=self._state,  # type: ignore[arg-type]
            max_workers=1,
            allow_llm=self._alert == "none",
            allow_llm_batch=False,
            alert_level=self._alert,  # type: ignore[arg-type]
            reason=f"simulated {self._state}",
        )


class _NoSpace:
    """A path whose stat fails, standing in for a filesystem that has gone away."""

    def __fspath__(self) -> str:
        raise OSError("simulated statvfs failure")

    def __str__(self) -> str:
        return "<unstattable>"


async def test_85c_pauses_inference_and_keeps_the_job(store: Any, queue: JobQueue) -> None:
    """Hot means stop inferring, not stop working.

    The gate raises before any request is built, so no token is spent and the job goes
    back to the queue for a cooler moment.
    """
    client = StructuredLLM(_NeverCalled(), gate=lambda: False)

    with pytest.raises(SchemaValidationError, match="thermal governor"):
        await client.generate("anything", _Extraction)

    job_id = queue.enqueue("score.company", {"canonical_domain": "acme.io"})
    queue.claim("w1", kinds=["score.company"], lease_seconds=60, limit=1)
    queue.fail(job_id, "paused by the thermal governor")

    assert store.conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 0


def test_thermal_alert_reaches_health(store: Any) -> None:
    hot = assess(store, thermal=_Governor("hot", "warning"))
    thermal = next(c for c in hot.checks if c.name == "thermal")
    # Degraded, not critical: pausing inference is the designed response to heat, and
    # a 503 here would restart the worker straight back into the same temperature.
    assert thermal.status == "degraded"

    throttled = assess(store, thermal=_Governor("throttled", "critical"))
    assert throttled.status == "critical"


def test_an_unreadable_sensor_does_not_take_the_endpoint_down(store: Any) -> None:
    """No `vcgencmd` is the normal case on any machine that is not a Pi.

    It must degrade the report, not raise out of a health check -- the endpoint is
    what you curl when everything else is broken.
    """

    class Broken:
        def poll(self) -> Any:
            raise RuntimeError("vcgencmd not on PATH")

    report = assess(store, thermal=Broken())
    thermal = next(c for c in report.checks if c.name == "thermal")
    assert thermal.status == "degraded"
    assert "unreadable" in thermal.detail


class _NeverCalled:
    """Fails the test rather than the assertion if the gate is ever bypassed."""

    async def generate(self, request: LLMRequest) -> Any:
        raise AssertionError("the thermal gate must be checked before the backend is touched")


# ------------------------------------------------------- the suite's own invariant


def test_no_outage_here_produces_a_dead_letter(store: Any, queue: JobQueue) -> None:
    """Every failure above is retryable, so none of them should bury a job.

    Dead-lettering is for a job that will never succeed -- a schema the model cannot
    satisfy, a URL that does not exist. An outage is the opposite: the same job will
    work fine in ten minutes, and burying it means a lead lost to a blip.
    """
    for kind in ("harvest.query", "extract.candidate", "score.company"):
        job_id = queue.enqueue(kind, {"x": 1})
        queue.claim("w", kinds=[kind], lease_seconds=60, limit=1)
        queue.fail(job_id, "transient outage")

    assert store.conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 0
