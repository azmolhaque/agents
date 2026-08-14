"""The ``cindra`` CLI.

Phase 0 ships the skeleton plus the durability drill. Later phases hang their
subcommands off the same app: ``harvest``, ``replay``, ``lead show``, ``suppress``,
``budget``, ``dispatch-test``, ``benchmark-models``, ``precision-report``,
``erase-subject``.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import typer

from cindraleads import PIPELINE_VERSION, __version__
from cindraleads.config import settings
from cindraleads.errors import CindraError, LeaseLost
from cindraleads.logging import configure_logging, get_logger
from cindraleads.models import Job, to_iso, utcnow
from cindraleads.queue import JobQueue
from cindraleads.store import Store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CindraLeads control CLI.")
db_app = typer.Typer(no_args_is_help=True, help="Database and migrations.")
queue_app = typer.Typer(no_args_is_help=True, help="Durable job queue.")
selftest_app = typer.Typer(no_args_is_help=True, help="Durability drills (Phase 0 gate).")
app.add_typer(db_app, name="db")
app.add_typer(queue_app, name="queue")
app.add_typer(selftest_app, name="selftest")

log = get_logger()

SELFTEST_KIND = "selftest.sideeffect"

# A stage handler receives the claimed job and the *open* transaction. It must do all
# of its writing through that connection so the work and the completion commit as one.
Handler = Callable[[Job, sqlite3.Connection], None]


def _open_store() -> Store:
    cfg = settings()
    cfg.ensure_dirs()
    return Store()


def _selftest_handler(job: Job, conn: sqlite3.Connection) -> None:
    """Insert exactly one row per job.

    ``job_id`` is the PRIMARY KEY, so a second insert for the same job raises
    IntegrityError rather than passing silently. That is the assertion: if the queue
    ever delivered at-least-once semantics where it promised exactly-once, this table
    would refuse the write.
    """
    conn.execute(
        "INSERT INTO selftest_side_effects (job_id, n, worker_id, created_at) VALUES (?,?,?,?)",
        (job.job_id, int(job.payload.get("n", 0)), job.worker_id, to_iso(utcnow())),
    )


HANDLERS: dict[str, Handler] = {SELFTEST_KIND: _selftest_handler}


def _create_selftest_table(store: Store) -> None:
    with store.tx() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS selftest_side_effects ("
            " job_id TEXT PRIMARY KEY, n INTEGER NOT NULL,"
            " worker_id TEXT, created_at TEXT NOT NULL)"
        )


# ---------------------------------------------------------------------- top level


@app.command()
def version() -> None:
    """Print versions."""
    typer.echo(f"cindraleads {__version__} (pipeline {PIPELINE_VERSION})")


@app.command()
def feedback(
    lead_id: Annotated[str, typer.Argument(help="Lead id from the card footer.")],
    verdict: Annotated[str, typer.Argument(help="good | bad | contacted | not_interested")],
    note: Annotated[str, typer.Option(help="Free-text reason.")] = "",
) -> None:
    """Record feedback on a dispatched lead.

    The Phase 8 gateway bot writes the same rows from Discord reactions; this is the
    manual path, and the seam the reaction handler is tested against.
    """
    allowed = {"good", "bad", "contacted", "not_interested"}
    if verdict not in allowed:
        raise typer.BadParameter(f"verdict must be one of {sorted(allowed)}")
    store = _open_store()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO feedback (feedback_id, lead_id, verdict, source, actor, note, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                lead_id,
                verdict,
                "cli",
                os.environ.get("USER"),
                note,
                to_iso(utcnow()),
            ),
        )
    typer.echo(f"recorded {verdict} for {lead_id}")


# ---------------------------------------------------------------------------- db


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations."""
    store = _open_store()
    applied = store.migrate()
    if applied:
        typer.echo(f"applied: {', '.join(applied)}")
    else:
        typer.echo("already up to date")


@db_app.command("status")
def db_status() -> None:
    """Show applied migrations and tables."""
    store = _open_store()
    typer.echo(f"database: {store.db_path}")
    typer.echo(f"applied:  {', '.join(store.applied_migrations()) or '(none)'}")
    typer.echo(f"tables:   {len(store.table_names())}")


@db_app.command("schema-dump")
def db_schema_dump(
    out: Annotated[Path, typer.Option(help="Where to write the reference dump.")] = Path(
        "db/schema.sql"
    ),
) -> None:
    """Regenerate db/schema.sql from the migrated database."""
    store = _open_store()
    store.migrate()
    target = settings().resolve(out)
    header = (
        "-- GENERATED by `cindra db schema-dump` (make schema). Do not edit.\n"
        "-- Source of truth is db/migrations/. This file is a reference dump only.\n\n"
    )
    target.write_text(header + store.dump_schema(), encoding="utf-8")
    typer.echo(f"wrote {target}")


@db_app.command("backup")
def db_backup(
    destination: Annotated[Path, typer.Argument(help="Backup file path.")],
) -> None:
    """Online backup of the live database."""
    store = _open_store()
    store.backup_to(settings().resolve(destination))
    typer.echo(f"backed up to {destination}")


# ------------------------------------------------------------------------- queue


@queue_app.command("status")
def queue_status() -> None:
    """Counts by status."""
    store = _open_store()
    stats = JobQueue(store).stats()
    for key in sorted(stats):
        typer.echo(f"{key:>12}: {stats[key]}")


@queue_app.command("reclaim")
def queue_reclaim() -> None:
    """Return expired in-flight jobs to the pool."""
    store = _open_store()
    typer.echo(f"reclaimed {JobQueue(store).reclaim_expired()}")


@queue_app.command("enqueue")
def queue_enqueue(
    kind: Annotated[str, typer.Option(help="Job kind.")],
    payload: Annotated[str, typer.Option(help="JSON payload.")] = "{}",
    count: Annotated[int, typer.Option(help="How many copies.")] = 1,
) -> None:
    """Enqueue jobs by hand."""
    store = _open_store()
    queue = JobQueue(store)
    parsed: dict[str, Any] = json.loads(payload)
    with store.tx() as conn:
        ids = [queue.enqueue(kind, dict(parsed, n=i), conn=conn) for i in range(count)]
    typer.echo(f"enqueued {len(ids)} job(s) of kind {kind}")


# ---------------------------------------------------------------------- selftest


@selftest_app.command("prepare")
def selftest_prepare(
    count: Annotated[int, typer.Option(help="Jobs to enqueue.")] = 100,
) -> None:
    """Create the side-effect table and enqueue N jobs."""
    store = _open_store()
    store.migrate()
    _create_selftest_table(store)
    queue = JobQueue(store)
    with store.tx() as conn:
        for i in range(count):
            queue.enqueue(SELFTEST_KIND, {"n": i}, dedupe_key=f"selftest:{i}", conn=conn)
    typer.echo(f"prepared {count} jobs")


@selftest_app.command("verify")
def selftest_verify(
    expect: Annotated[int, typer.Option(help="Expected job count.")] = 100,
) -> None:
    """Assert every job ran exactly once."""
    store = _open_store()
    rows = store.conn.execute(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT job_id) AS distinct_ids, "
        "COUNT(DISTINCT n) AS distinct_n FROM selftest_side_effects"
    ).fetchone()
    stats = JobQueue(store).stats()
    total, distinct_ids, distinct_n = (
        int(rows["total"]),
        int(rows["distinct_ids"]),
        int(rows["distinct_n"]),
    )
    typer.echo(f"side effects: {total} rows, {distinct_ids} distinct jobs, {distinct_n} distinct n")
    typer.echo(f"queue: {stats}")
    problems: list[str] = []
    if total != expect:
        problems.append(f"expected {expect} side effects, found {total}")
    if distinct_ids != total:
        problems.append(f"duplicate side effects: {total} rows for {distinct_ids} jobs")
    if distinct_n != expect:
        problems.append(f"expected {expect} distinct payloads, found {distinct_n}")
    if stats["done"] != expect:
        problems.append(f"expected {expect} done jobs, found {stats['done']}")
    if problems:
        for problem in problems:
            typer.echo(f"FAIL: {problem}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"OK: {expect} jobs completed exactly once")


# -------------------------------------------------------------------- the worker

_shutdown = False


def _request_shutdown(_signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True


@app.command()
def work(
    kinds: Annotated[str, typer.Option(help="Comma-separated job kinds.")] = SELFTEST_KIND,
    worker_id: Annotated[str, typer.Option(help="Worker identity.")] = "",
    lease: Annotated[int, typer.Option(help="Lease seconds.")] = 30,
    max_jobs: Annotated[int, typer.Option(help="Stop after N jobs. 0 = forever.")] = 0,
    idle_exit: Annotated[bool, typer.Option(help="Exit when the queue drains.")] = True,
    drain_inflight: Annotated[
        bool,
        typer.Option(help="With --idle-exit, keep waiting while jobs are still leased elsewhere."),
    ] = False,
    poll_ms: Annotated[int, typer.Option(help="Sleep between empty polls.")] = 50,
    work_ms: Annotated[int, typer.Option(help="Simulated work inside the tx.")] = 0,
) -> None:
    """Run the worker loop.

    Claims jobs, runs the handler and completes the job **inside a single
    transaction**. SIGTERM sets a flag and the loop finishes its current job;
    SIGKILL is uncatchable by design, which is exactly what the drill exercises.
    """
    cfg = settings()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=False)
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    wid = worker_id or f"{os.uname().nodename}:{os.getpid()}"
    wanted = [k.strip() for k in kinds.split(",") if k.strip()]
    store = _open_store()
    queue = JobQueue(store)
    queue.reclaim_expired()

    processed = 0
    while not _shutdown:
        if max_jobs and processed >= max_jobs:
            break
        try:
            claimed = queue.claim(wid, kinds=wanted, lease_seconds=lease, limit=1)
        except sqlite3.OperationalError as exc:  # database locked under contention
            log.warning("claim_contended", error=str(exc), worker_id=wid)
            time.sleep(poll_ms / 1000)
            continue

        if not claimed:
            if queue.reclaim_expired() == 0:
                # Nothing claimable right now. There may still be jobs leased by a
                # worker that was killed: those only become claimable once their
                # lease expires, so an unconditional exit here would strand them.
                # --drain-inflight is the "finish the whole queue" mode the
                # durability drill and one-shot timers want.
                if idle_exit and not (drain_inflight and queue.stats()["in_flight"]):
                    break
                time.sleep(poll_ms / 1000)
            continue

        job = claimed[0]
        handler = HANDLERS.get(job.kind)
        if handler is None:
            queue.fail(job.job_id, f"no handler registered for kind {job.kind!r}")
            continue

        started = time.monotonic()
        try:
            # THE critical section. Side effect and completion, one COMMIT.
            with store.tx() as conn:
                handler(job, conn)
                if work_ms:
                    time.sleep(work_ms / 1000)
                queue.complete(job.job_id, conn=conn)
        except LeaseLost as exc:
            log.warning("lease_lost", job_id=job.job_id, error=str(exc))
            continue
        except (CindraError, sqlite3.Error, ValueError) as exc:
            queue.fail(job.job_id, f"{type(exc).__name__}: {exc}")
            log.error("job_failed", job_id=job.job_id, stage=job.kind, error=str(exc))
            continue

        processed += 1
        log.info(
            "job_done",
            job_id=job.job_id,
            stage=job.kind,
            worker_id=wid,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    log.info("worker_exit", worker_id=wid, processed=processed)
    typer.echo(f"processed {processed}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
