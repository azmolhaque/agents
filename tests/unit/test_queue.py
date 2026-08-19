"""Durable queue semantics."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from cindraleads.errors import LeaseLost
from cindraleads.models import to_iso, utcnow


def test_enqueue_and_claim_roundtrip(queue):
    job_id = queue.enqueue("stage.extract", {"url": "https://x.io"})
    claimed = queue.claim("w1", kinds=["stage.extract"])
    assert len(claimed) == 1
    job = claimed[0]
    assert job.job_id == job_id
    assert job.status == "in_flight"
    # A claim is not an attempt. `attempts` means "times a stage ran and failed", and
    # nothing has run yet.
    assert job.attempts == 0
    assert job.payload == {"url": "https://x.io"}
    assert job.worker_id == "w1"


def test_dedupe_key_makes_enqueue_idempotent(queue):
    first = queue.enqueue("k", {"a": 1}, dedupe_key="same")
    second = queue.enqueue("k", {"a": 2}, dedupe_key="same")
    assert first == second
    assert queue.stats()["pending"] == 1


def test_a_job_is_only_ever_claimed_once(queue):
    queue.enqueue("k")
    assert len(queue.claim("w1", kinds=["k"])) == 1
    assert queue.claim("w2", kinds=["k"]) == []


def test_claim_respects_kind_filter_and_priority(queue):
    queue.enqueue("other")
    queue.enqueue("wanted", {"p": "low"}, priority=200)
    queue.enqueue("wanted", {"p": "high"}, priority=1)
    claimed = queue.claim("w1", kinds=["wanted"], limit=2)
    assert [j.payload["p"] for j in claimed] == ["high", "low"]


def test_complete_requires_an_active_lease(queue):
    job_id = queue.enqueue("k")
    with pytest.raises(LeaseLost):
        queue.complete(job_id)  # never claimed
    queue.claim("w1", kinds=["k"])
    queue.complete(job_id)
    assert queue.stats()["done"] == 1
    with pytest.raises(LeaseLost):
        queue.complete(job_id)  # already done


def test_fail_retries_with_backoff_then_dead_letters(queue):
    job_id = queue.enqueue("k", max_attempts=2)
    queue.claim("w1", kinds=["k"])
    assert queue.fail(job_id, "boom") == "pending"

    # Backoff puts it in the future, so it is not immediately claimable.
    assert queue.claim("w1", kinds=["k"]) == []

    with queue.store.tx() as conn:
        conn.execute("UPDATE jobs SET available_at=? WHERE job_id=?", (to_iso(utcnow()), job_id))
    queue.claim("w1", kinds=["k"])
    assert queue.fail(job_id, "boom again") == "dead"
    assert queue.stats()["dead_letter"] == 1


def _orphan(queue, job_id):
    """Expire a claimed job's lease, as a killed worker would."""
    past = to_iso(utcnow() - timedelta(seconds=5))
    with queue.store.tx() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=? WHERE job_id=?", (past, job_id))


def test_reclaim_expired_returns_orphans_to_the_pool(queue):
    """The crash-recovery mechanism, in miniature: a worker vanished mid-job."""
    job_id = queue.enqueue("k")
    queue.claim("w1", kinds=["k"], lease_seconds=60)
    assert queue.claim("w2", kinds=["k"]) == []

    _orphan(queue, job_id)

    assert queue.reclaim_expired() == 1
    recovered = queue.claim("w2", kinds=["k"])
    assert len(recovered) == 1
    # The interruption is charged to `reclaims`; the stage never ran, so `attempts`
    # stays at zero. Charging both to one counter is what let three deploys bury a
    # `score.company` job that had never failed.
    assert recovered[0].reclaims == 1
    assert recovered[0].attempts == 0


def test_an_interruption_never_counts_toward_the_failure_ceiling(queue):
    """A job orphaned more times than `max_attempts` must still be alive.

    This is the defect, stated directly: with `attempts` charged at claim time, three
    worker restarts during a slow LLM call dead-lettered a job whose stage had not once
    reported an error.
    """
    job_id = queue.enqueue("k", max_attempts=3)

    for _ in range(5):
        queue.claim("w1", kinds=["k"])
        _orphan(queue, job_id)
        queue.reclaim_expired()

    job = queue.get(job_id)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.reclaims == 5
    assert queue.stats()["dead_letter"] == 0


def test_reclaim_still_dead_letters_a_job_that_keeps_killing_its_worker(queue):
    """The protection the claim-time increment used to provide, kept.

    A job that reliably wedges or kills whatever picks it up must not retry forever.
    The ceiling is just higher and separate, because the evidence is weaker: three
    stage failures say the job is broken, three interruptions say we deployed.
    """
    job_id = queue.enqueue("k", max_reclaims=2)

    for _ in range(2):
        queue.claim("w1", kinds=["k"])
        _orphan(queue, job_id)
        queue.reclaim_expired()

    assert queue.get(job_id).status == "dead"
    assert queue.stats()["dead_letter"] == 1


def test_the_dead_letter_reason_names_the_counter_that_buried_it(queue):
    """ "lease expired past max_attempts" sent us hunting a stage error that had never
    happened. The reason has to distinguish "it failed" from "we kept killing it"."""
    job_id = queue.enqueue("k", max_reclaims=1)
    queue.claim("w1", kinds=["k"])
    _orphan(queue, job_id)
    queue.reclaim_expired()

    reason = queue.store.conn.execute(
        "SELECT last_error FROM dead_letter WHERE job_id=?", (job_id,)
    ).fetchone()["last_error"]
    assert "without ever failing" in reason


def test_extend_lease_keeps_a_slow_job_safe(queue):
    job_id = queue.enqueue("k")
    queue.claim("w1", kinds=["k"], lease_seconds=1)
    queue.extend_lease(job_id, seconds=600)
    assert queue.reclaim_expired() == 0


def test_side_effect_and_completion_roll_back_together(queue, store):
    """The exactly-once invariant, asserted directly.

    If the transaction aborts, the side effect must vanish *and* the job must remain
    claimable. A queue that committed one without the other would silently drop or
    duplicate work under power loss.
    """
    store.conn.execute("CREATE TABLE fx (job_id TEXT PRIMARY KEY)")
    job_id = queue.enqueue("k")
    queue.claim("w1", kinds=["k"])

    with pytest.raises(RuntimeError), store.tx() as conn:
        conn.execute("INSERT INTO fx VALUES (?)", (job_id,))
        queue.complete(job_id, conn=conn)
        raise RuntimeError("power cut")

    assert store.conn.execute("SELECT COUNT(*) FROM fx").fetchone()[0] == 0
    assert queue.get(job_id).status == "in_flight"


def test_purge_done_leaves_live_jobs_alone(queue):
    done_id = queue.enqueue("k")
    queue.claim("w1", kinds=["k"])
    queue.complete(done_id)
    queue.enqueue("k")
    assert queue.purge_done(older_than_seconds=-1) == 1
    assert queue.stats()["pending"] == 1


def test_duplicate_side_effect_is_rejected_by_the_schema(queue, store):
    """The selftest table's PRIMARY KEY is the assertion that makes the Phase 0
    drill meaningful — a second insert must raise, not pass quietly."""
    store.conn.execute("CREATE TABLE fx (job_id TEXT PRIMARY KEY)")
    store.conn.execute("INSERT INTO fx VALUES ('j1')")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO fx VALUES ('j1')")
