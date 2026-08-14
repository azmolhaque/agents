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
    assert job.attempts == 1
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


def test_reclaim_expired_returns_orphans_to_the_pool(queue):
    """The crash-recovery mechanism, in miniature: a worker vanished mid-job."""
    job_id = queue.enqueue("k")
    queue.claim("w1", kinds=["k"], lease_seconds=60)
    assert queue.claim("w2", kinds=["k"]) == []

    past = to_iso(utcnow() - timedelta(seconds=5))
    with queue.store.tx() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=? WHERE job_id=?", (past, job_id))

    assert queue.reclaim_expired() == 1
    recovered = queue.claim("w2", kinds=["k"])
    assert len(recovered) == 1
    assert recovered[0].attempts == 2


def test_reclaim_dead_letters_a_job_past_max_attempts(queue):
    job_id = queue.enqueue("k", max_attempts=1)
    queue.claim("w1", kinds=["k"])
    past = to_iso(utcnow() - timedelta(seconds=5))
    with queue.store.tx() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=? WHERE job_id=?", (past, job_id))
    assert queue.reclaim_expired() == 1
    assert queue.get(job_id).status == "dead"
    assert queue.stats()["dead_letter"] == 1


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
