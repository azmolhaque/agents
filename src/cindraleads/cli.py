"""The ``cindra`` CLI.

Phase 0 shipped the skeleton plus the durability drill; Phase 2 adds ``harvest`` and
teaches ``work`` to run pipeline stages. Later phases hang their subcommands off the
same app: ``replay``, ``lead show``, ``suppress``, ``budget``, ``dispatch-test``,
``benchmark-models``, ``precision-report``, ``erase-subject``.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import signal
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, Protocol

import typer

from cindraleads import PIPELINE_VERSION, __version__
from cindraleads.agents import EXTRACT_KIND, HARVEST_KIND, RESOLVE_KIND
from cindraleads.config import settings
from cindraleads.errors import CindraError, LeaseLost
from cindraleads.logging import configure_logging, get_logger
from cindraleads.models import Job, StageResult, to_iso, utcnow
from cindraleads.queue import JobQueue
from cindraleads.runtime import Runtime
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


class Stage(Protocol):
    """A two-phase stage. See `Harvester` for why the split exists.

    `prepare` may do network I/O and must not write. `commit` writes, inside a
    transaction the worker opens and also completes the job in.
    """

    async def prepare(self, job: Job) -> Any: ...

    def commit(self, job: Job, outcome: Any, conn: sqlite3.Connection) -> StageResult: ...


class _StageFailed(CindraError):
    """Raised inside the transaction so an unsuccessful stage rolls its writes back."""


def _open_store() -> Store:
    cfg = settings()
    cfg.ensure_dirs()
    store = Store()
    # Python 3.13 warns about an unclosed sqlite3 connection at interpreter shutdown.
    # A CLI process exiting would otherwise print a ResourceWarning after its output.
    atexit.register(store.close)
    return store


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


# ------------------------------------------------------------------- harvesting


@app.command()
def harvest(
    limit: Annotated[int, typer.Option(help="Max plans. 0 = the configured ceiling.")] = 0,
    dry_run: Annotated[bool, typer.Option(help="Print the batch, enqueue nothing.")] = False,
) -> None:
    """Plan a batch of discovery queries and enqueue them.

    Planning and execution are deliberately separate commands. The Scout's output is a
    durable job per query, so a power cut between planning and fetching costs the
    planning, not the batch — and `--dry-run` lets you read what a credit would be
    spent on before spending it.
    """
    cfg = settings()
    cfg.ensure_dirs()
    store = _open_store()

    async def _run() -> None:
        async with Runtime(store=store, config=cfg) as runtime:
            plans = runtime.scout.plan(
                limit=limit or None,
                can_spend=runtime.can_spend,
            )
            for plan in plans:
                cost = runtime.registry.get(plan.engine).cost_units
                marker = f"{cost} credit(s)" if cost else "free"
                typer.echo(f"  [{marker:>10}] {plan.engine:<22} {plan.query or '(no query)'}")
            if dry_run:
                typer.echo(f"dry run: {len(plans)} plan(s), nothing enqueued")
                return
            ids, new = runtime.harvester.enqueue_plans(plans)
            # Report new vs deduped separately. Printing len(ids) called a run that
            # queued nothing "enqueued 12 harvest job(s)", which made a second run
            # look like it had worked when it had in fact been deduped away.
            skipped = len(ids) - new
            typer.echo(
                f"enqueued {new} new harvest job(s)"
                + (f", {skipped} already queued or run this cache window" if skipped else "")
            )

    asyncio.run(_run())


@app.command()
def status() -> None:
    """What the pipeline has actually produced.

    `queue status` answers "is work moving?"; this answers "did any of it turn into a
    prospect?". They are different questions, and a queue that drains cleanly while
    producing zero companies is exactly the failure worth being able to see at a
    glance.
    """
    store = _open_store()
    conn = store.conn

    def count(sql: str, *params: Any) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    typer.echo("candidates")
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM candidates GROUP BY status ORDER BY n DESC"
    ):
        typer.echo(f"  {row['status']!s:>14}: {row['n']}")

    typer.echo("\nentities")
    typer.echo(f"  {'companies':>14}: {count('SELECT COUNT(*) FROM companies')}")
    typer.echo(f"  {'evidence':>14}: {count('SELECT COUNT(*) FROM evidence')}")
    live = count(
        "SELECT COUNT(*) FROM triggers WHERE active = 1 AND decays_at > ?", to_iso(utcnow())
    )
    typer.echo(f"  {'live triggers':>14}: {live}")
    typer.echo(f"  {'quarantined':>14}: {count('SELECT COUNT(*) FROM quarantine')}")

    triggers = list(
        conn.execute(
            "SELECT code, COUNT(*) AS n FROM triggers WHERE active = 1 AND decays_at > ? "
            "GROUP BY code ORDER BY n DESC",
            (to_iso(utcnow()),),
        )
    )
    if triggers:
        typer.echo("\ntriggers by code")
        for row in triggers:
            typer.echo(f"  {row['code']!s:>20}: {row['n']}")

    # A company with no live trigger is fit without news, which the master prompt is
    # explicit is not a lead. Surfacing the split stops "500 companies" reading as
    # success when none of them has a reason to be contacted.
    leadable = count(
        "SELECT COUNT(DISTINCT canonical_domain) FROM triggers WHERE active = 1 AND decays_at > ?",
        to_iso(utcnow()),
    )
    typer.echo(f"\ncompanies with >=1 live trigger: {leadable}")


@app.command()
def pipeline(
    limit: Annotated[int, typer.Option(help="Max plans. 0 = the configured ceiling.")] = 0,
    max_jobs: Annotated[int, typer.Option(help="Stop after N jobs. 0 = drain.")] = 0,
) -> None:
    """Harvest, extract and resolve in one pass.

    The same three stages `work` runs, in one command, for a timer or a manual run.
    Ordering is deliberate rather than incidental: each stage drains fully before the
    next starts, so a batch of ~64 s extractions is never interleaved with harvest
    fetches competing for the same worker.
    """
    cfg = settings()
    cfg.ensure_dirs()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=True)
    store = _open_store()

    async def _run() -> None:
        async with Runtime(store=store, config=cfg) as runtime:
            plans = runtime.scout.plan(limit=limit or None, can_spend=runtime.can_spend)
            _ids, new = runtime.harvester.enqueue_plans(plans)
            typer.echo(f"planned {len(plans)}, {new} new harvest job(s)")

            for kind in PIPELINE_KINDS:
                stages = _stages_for([kind], runtime)
                done = await _work_loop(
                    store,
                    stages,
                    kinds=[kind],
                    worker_id=f"{os.uname().nodename}:{os.getpid()}",
                    lease=600,  # an extraction is ~64 s; a short lease would expire mid-page
                    max_jobs=max_jobs,
                    idle_exit=True,
                    drain_inflight=False,
                    poll_ms=50,
                    work_ms=0,
                )
                typer.echo(f"{kind}: {done} job(s)")

    asyncio.run(_run())


# -------------------------------------------------------------------- the worker

_shutdown = False


def _request_shutdown(_signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True


class _SyncStage:
    """Adapts a plain `Handler` to the two-phase protocol.

    A handler with no network work has nothing to prepare, so the whole thing runs in
    `commit`. This exists so the worker loop has exactly one code path — the durability
    drill must exercise the same loop the pipeline runs, or it proves nothing about it.
    """

    def __init__(self, kind: str, handler: Handler) -> None:
        self.kind = kind
        self.handler = handler

    async def prepare(self, job: Job) -> Any:
        return None

    def commit(self, job: Job, outcome: Any, conn: sqlite3.Connection) -> StageResult:
        self.handler(job, conn)
        return StageResult(ok=True, stage=self.kind, job_id=job.job_id)


def _stages_for(kinds: list[str], runtime: Runtime | None) -> dict[str, Stage]:
    """Which stages this worker can run.

    Built per-invocation rather than as a module-level table because the pipeline
    stages need a live `Runtime` (and therefore a running event loop), while the
    selftest handler needs nothing. A worker asked only for selftest work never
    constructs an HTTP client.
    """
    stages: dict[str, Stage] = {
        kind: _SyncStage(kind, handler) for kind, handler in HANDLERS.items() if kind in kinds
    }
    if runtime is None:
        return stages
    available: dict[str, Stage] = {
        HARVEST_KIND: runtime.harvester,
        EXTRACT_KIND: runtime.extractor,
        RESOLVE_KIND: runtime.resolver,
    }
    stages.update({kind: stage for kind, stage in available.items() if kind in kinds})
    return stages


PIPELINE_KINDS = (HARVEST_KIND, EXTRACT_KIND, RESOLVE_KIND)


def _needs_runtime(kinds: list[str]) -> bool:
    return any(kind in kinds for kind in PIPELINE_KINDS)


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

    Claims jobs, runs the stage and completes the job **inside a single transaction**.
    SIGTERM sets a flag and the loop finishes its current job; SIGKILL is uncatchable
    by design, which is exactly what the drill exercises.
    """
    cfg = settings()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=False)
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    wanted = [k.strip() for k in kinds.split(",") if k.strip()]
    store = _open_store()

    async def _main() -> int:
        if not _needs_runtime(wanted):
            return await _work_loop(store, _stages_for(wanted, None), **_opts())
        async with Runtime(store=store, config=cfg) as runtime:
            return await _work_loop(store, _stages_for(wanted, runtime), **_opts())

    def _opts() -> dict[str, Any]:
        return {
            "kinds": wanted,
            "worker_id": worker_id or f"{os.uname().nodename}:{os.getpid()}",
            "lease": lease,
            "max_jobs": max_jobs,
            "idle_exit": idle_exit,
            "drain_inflight": drain_inflight,
            "poll_ms": poll_ms,
            "work_ms": work_ms,
        }

    typer.echo(f"processed {asyncio.run(_main())}")


async def _work_loop(
    store: Store,
    stages: dict[str, Stage],
    *,
    kinds: list[str],
    worker_id: str,
    lease: int,
    max_jobs: int,
    idle_exit: bool,
    drain_inflight: bool,
    poll_ms: int,
    work_ms: int,
) -> int:
    queue = JobQueue(store)
    queue.reclaim_expired()

    processed = 0
    while not _shutdown:
        if max_jobs and processed >= max_jobs:
            break
        try:
            claimed = queue.claim(worker_id, kinds=kinds, lease_seconds=lease, limit=1)
        except sqlite3.OperationalError as exc:  # database locked under contention
            log.warning("claim_contended", error=str(exc), worker_id=worker_id)
            await asyncio.sleep(poll_ms / 1000)
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
                await asyncio.sleep(poll_ms / 1000)
            continue

        job = claimed[0]
        stage = stages.get(job.kind)
        if stage is None:
            queue.fail(job.job_id, f"no handler registered for kind {job.kind!r}")
            continue

        started = time.monotonic()
        try:
            # Phase 1, outside any transaction. A harvest fetch can take 30 s, and
            # BEGIN IMMEDIATE holds the single write lock for its whole duration --
            # doing the network work here keeps every other worker running.
            outcome = await stage.prepare(job)
        except (CindraError, OSError, ValueError) as exc:
            queue.fail(job.job_id, f"{type(exc).__name__}: {exc}")
            log.error("stage_prepare_failed", job_id=job.job_id, stage=job.kind, error=str(exc))
            continue

        try:
            # Phase 2. THE critical section: side effect, follow-on jobs and
            # completion, one COMMIT. A crash anywhere inside rolls back all three,
            # and the job is retried whole once its lease expires.
            with store.tx() as conn:
                result = stage.commit(job, outcome, conn)
                if not result.ok:
                    raise _StageFailed(result.error or "stage reported failure")
                for kind, payload in result.follow_on:
                    # A stage can ask for its follow-on to be held back — the Extractor
                    # does this when a candidate hit the per-domain budget and is early
                    # rather than finished. The key is stripped so it never reaches the
                    # stage that reads the payload.
                    body = {k: v for k, v in payload.items() if k != "_delay_seconds"}
                    queue.enqueue(
                        kind,
                        body,
                        delay_seconds=float(payload.get("_delay_seconds") or 0),
                        conn=conn,
                    )
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
            worker_id=worker_id,
            follow_on=len(result.follow_on),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    log.info("worker_exit", worker_id=worker_id, processed=processed)
    return processed


def main() -> None:
    app()


if __name__ == "__main__":
    main()
