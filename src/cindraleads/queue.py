"""Durable, lease-based job queue on SQLite.

Why not Redis/Celery/Prefect: power loss on a Pi is a *when*. Every stage transition
is a row update inside a transaction. On boot the worker reclaims mid-flight work
with ``status='in_flight' AND lease_expires_at < now``. One file, atomic, no broker.

## How exactly-once actually works

A lease queue on its own gives *at-least-once*: a worker can finish its side effect
and then die before marking the job done, and the next worker redoes it.

The property the Phase 0 gate demands is stronger, and it comes from one rule:

    **The side effect and the completion commit in the same transaction.**

    with store.tx() as conn:
        conn.execute("INSERT INTO results ...")   # the work
        queue.complete(job.job_id, conn=conn)     # the bookkeeping

Killed before COMMIT, both roll back and the lease expires — the job is retried with
nothing half-done. Killed after COMMIT, both are durable and the job is never handed
out again. There is no window in between, which is why every mutating method here
takes an optional ``conn``: a caller must be able to enlist in *their* transaction
rather than being forced into one of ours.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from cindraleads.errors import LeaseLost
from cindraleads.models import Job, from_iso, to_iso, utcnow
from cindraleads.store import Store

__all__ = ["JobQueue"]

_COLUMNS = (
    "job_id, kind, payload, status, priority, attempts, max_attempts, "
    "reclaims, max_reclaims, dedupe_key, "
    "worker_id, lease_expires_at, available_at, last_error, created_at, updated_at"
)


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        payload=json.loads(row["payload"]),
        status=row["status"],
        priority=int(row["priority"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        reclaims=int(row["reclaims"]),
        max_reclaims=int(row["max_reclaims"]),
        dedupe_key=row["dedupe_key"],
        worker_id=row["worker_id"],
        lease_expires_at=from_iso(row["lease_expires_at"]) if row["lease_expires_at"] else None,
        available_at=from_iso(row["available_at"]),
        last_error=row["last_error"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


class JobQueue:
    def __init__(self, store: Store) -> None:
        self.store = store

    @contextmanager
    def _scope(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """Use the caller's transaction if given one, otherwise open our own."""
        if conn is not None:
            yield conn
        else:
            with self.store.tx() as owned:
                yield owned

    # ---------------------------------------------------------------- enqueue

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        priority: int = 100,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
        max_reclaims: int = 10,
        delay_seconds: float = 0.0,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Add a job. With a ``dedupe_key``, re-enqueueing is a no-op that returns
        the existing job id — enqueueing the same logical work twice must never
        produce two jobs."""
        now = utcnow()
        job_id = uuid.uuid4().hex
        available_at = now + timedelta(seconds=delay_seconds)
        with self._scope(conn) as active:
            if dedupe_key is not None:
                existing = active.execute(
                    "SELECT job_id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if existing is not None:
                    return str(existing["job_id"])
            active.execute(
                "INSERT INTO jobs (job_id, kind, payload, status, priority, attempts, "
                "max_attempts, max_reclaims, dedupe_key, available_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    kind,
                    json.dumps(payload or {}, separators=(",", ":")),
                    priority,
                    max_attempts,
                    max_reclaims,
                    dedupe_key,
                    to_iso(available_at),
                    to_iso(now),
                    to_iso(now),
                ),
            )
        return job_id

    def enqueue_many(
        self,
        kind: str,
        payloads: Sequence[dict[str, Any]],
        *,
        priority: int = 100,
        conn: sqlite3.Connection | None = None,
    ) -> list[str]:
        with self._scope(conn) as active:
            return [
                self.enqueue(kind, payload, priority=priority, conn=active) for payload in payloads
            ]

    # ------------------------------------------------------------------ claim

    def claim(
        self,
        worker_id: str,
        *,
        kinds: Sequence[str] | None = None,
        lease_seconds: int = 60,
        limit: int = 1,
        conn: sqlite3.Connection | None = None,
    ) -> list[Job]:
        """Atomically take up to ``limit`` pending jobs.

        The UPDATE ... RETURNING runs under BEGIN IMMEDIATE, so concurrent workers
        serialize on the write lock and a job can only ever be handed to one of them.
        """
        now = utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        params: list[Any] = [worker_id, to_iso(lease_until), to_iso(now), to_iso(now)]
        kind_clause = ""
        if kinds:
            kind_clause = f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        params.append(limit)

        # Note what is *not* here: `attempts=attempts+1`. A claim is not an attempt.
        # It used to be, because a SIGKILL'd worker never reaches `fail()` and the
        # attempt would otherwise go uncounted -- but that made an interruption
        # indistinguishable from a stage error, and three deploys during a slow LLM
        # call dead-lettered a job that had never failed. `reclaim_expired` now charges
        # those to `reclaims`, so every claim still ends in exactly one of done,
        # attempts+1 or reclaims+1 and nothing escapes the accounting.
        sql = (
            "UPDATE jobs SET status='in_flight', worker_id=?, lease_expires_at=?, "
            "updated_at=? "
            "WHERE job_id IN ("
            "  SELECT job_id FROM jobs"
            "  WHERE status='pending' AND available_at <= ?"
            + kind_clause
            + "  ORDER BY priority ASC, created_at ASC"
            "  LIMIT ?"
            f") RETURNING {_COLUMNS}"
        )
        with self._scope(conn) as active:
            rows = active.execute(sql, params).fetchall()
        # The subquery's ORDER BY decides *which* jobs are claimed, but RETURNING
        # emits them in whatever order the UPDATE happened to touch rows. Re-sort so
        # a caller draining a batch still handles the most urgent job first.
        jobs = [_row_to_job(row) for row in rows]
        jobs.sort(key=lambda j: (j.priority, j.created_at))
        return jobs

    # --------------------------------------------------------------- finalize

    def complete(self, job_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """Mark done. Call this inside the same transaction as the side effect.

        Raises :class:`LeaseLost` if the job was not in flight — that means another
        worker reclaimed it while we were slow, and our write must not land.
        """
        now = to_iso(utcnow())
        with self._scope(conn) as active:
            cursor = active.execute(
                "UPDATE jobs SET status='done', worker_id=NULL, lease_expires_at=NULL, "
                "updated_at=? WHERE job_id=? AND status='in_flight'",
                (now, job_id),
            )
            if cursor.rowcount == 0:
                raise LeaseLost(f"job {job_id} is no longer in flight; refusing to complete")

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        retry_in_seconds: float | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Record a failure — the stage ran and errored. Returns the resulting status.

        This is the *only* place ``attempts`` is charged. Past ``max_attempts`` the job
        moves to ``dead_letter`` rather than retrying forever: one broken source must
        never turn into a crash loop.
        """
        now = utcnow()
        with self._scope(conn) as active:
            row = active.execute(
                "SELECT attempts, max_attempts, kind, payload FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise LeaseLost(f"job {job_id} does not exist")

            # Charged here rather than at claim time, so the count means "times a stage
            # ran and failed". The ceiling behaviour is unchanged: three failures still
            # bury the job, because the increment moved and the comparison moved with it.
            attempts = int(row["attempts"]) + 1
            if attempts >= int(row["max_attempts"]):
                active.execute(
                    "INSERT OR REPLACE INTO dead_letter "
                    "(job_id, kind, payload, attempts, last_error, died_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, row["kind"], row["payload"], attempts, error[:2000], to_iso(now)),
                )
                active.execute(
                    "UPDATE jobs SET status='dead', attempts=?, worker_id=NULL, "
                    "lease_expires_at=NULL, last_error=?, updated_at=? WHERE job_id=?",
                    (attempts, error[:2000], to_iso(now), job_id),
                )
                return "dead"

            backoff = retry_in_seconds if retry_in_seconds is not None else 2**attempts
            active.execute(
                "UPDATE jobs SET status='pending', attempts=?, worker_id=NULL, "
                "lease_expires_at=NULL, last_error=?, available_at=?, updated_at=? "
                "WHERE job_id=?",
                (
                    attempts,
                    error[:2000],
                    to_iso(now + timedelta(seconds=backoff)),
                    to_iso(now),
                    job_id,
                ),
            )
            return "pending"

    # ---------------------------------------------------------------- recovery

    def reclaim_expired(self, *, conn: sqlite3.Connection | None = None) -> int:
        """Return orphaned jobs to the pool. This is the whole crash-recovery story.

        A worker that was ``kill -9``'d leaves its jobs ``in_flight`` with a lease in
        the past. Nothing else has to happen for the system to heal: the next call
        here makes them claimable again, and because their side effects never
        committed, redoing them is safe.
        """
        now = to_iso(utcnow())
        reclaimed = 0
        with self._scope(conn) as active:
            expired = active.execute(
                "SELECT job_id, kind, payload, attempts, reclaims, max_reclaims, last_error "
                "FROM jobs WHERE status='in_flight' AND lease_expires_at < ?",
                (now,),
            ).fetchall()
            for row in expired:
                job_id = str(row["job_id"])
                # `reclaims`, not `attempts`. The stage never reported anything, so we
                # have no evidence about the job -- only that we lost the worker holding
                # it. The ceiling is higher for exactly that reason: three stage failures
                # say the job is broken, three interruptions say we deployed three times.
                # It is still a ceiling, because a job that reliably wedges or kills the
                # worker must not retry forever, which is what the claim-time increment
                # used to protect against.
                reclaims = int(row["reclaims"]) + 1
                if reclaims >= int(row["max_reclaims"]):
                    active.execute(
                        "INSERT OR REPLACE INTO dead_letter "
                        "(job_id, kind, payload, attempts, last_error, died_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            job_id,
                            row["kind"],
                            row["payload"],
                            int(row["attempts"]),
                            # Names the counter that buried it. "lease expired past
                            # max_attempts" sent us looking for a stage error that had
                            # never happened.
                            f"orphaned {reclaims}x without ever failing; "
                            f"last stage error: {row['last_error'] or 'none'}",
                            now,
                        ),
                    )
                    active.execute(
                        "UPDATE jobs SET status='dead', reclaims=?, worker_id=NULL, "
                        "lease_expires_at=NULL, updated_at=? WHERE job_id=?",
                        (reclaims, now, job_id),
                    )
                else:
                    active.execute(
                        "UPDATE jobs SET status='pending', reclaims=?, worker_id=NULL, "
                        "lease_expires_at=NULL, available_at=?, updated_at=? WHERE job_id=?",
                        (reclaims, now, now, job_id),
                    )
                reclaimed += 1
        return reclaimed

    def extend_lease(
        self, job_id: str, *, seconds: int = 60, conn: sqlite3.Connection | None = None
    ) -> None:
        """Heartbeat for a stage that legitimately runs long (a slow LLM batch)."""
        until = to_iso(utcnow() + timedelta(seconds=seconds))
        with self._scope(conn) as active:
            cursor = active.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=? "
                "WHERE job_id=? AND status='in_flight'",
                (until, to_iso(utcnow()), job_id),
            )
            if cursor.rowcount == 0:
                raise LeaseLost(f"job {job_id} is no longer in flight; lease not extended")

    # ------------------------------------------------------------------ reads

    def get(self, job_id: str) -> Job | None:
        row = self.store.conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row is not None else None

    def stats(self) -> dict[str, int]:
        rows = self.store.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        for status in ("pending", "in_flight", "done", "failed", "dead"):
            counts.setdefault(status, 0)
        dead = self.store.conn.execute("SELECT COUNT(*) AS n FROM dead_letter").fetchone()
        counts["dead_letter"] = int(dead["n"])
        return counts

    def purge_done(self, *, older_than_seconds: float = 86400) -> int:
        cutoff = to_iso(utcnow() - timedelta(seconds=older_than_seconds))
        with self.store.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM jobs WHERE status='done' AND updated_at < ?", (cutoff,)
            )
            return int(cursor.rowcount)
