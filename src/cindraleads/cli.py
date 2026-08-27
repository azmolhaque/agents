"""The ``cindra`` CLI.

Phase 0 shipped the skeleton plus the durability drill; Phase 2 adds ``harvest`` and
teaches ``work`` to run pipeline stages. Later phases hang their subcommands off the
same app: ``replay``, ``lead show``, ``suppress``, ``budget``, ``dispatch-test``,
``benchmark-models``, ``precision-report``, ``erase-subject``.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
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
from cindraleads.agents import (
    DISPATCH_KIND,
    ENRICH_KIND,
    EXTRACT_KIND,
    HARVEST_KIND,
    RESOLVE_KIND,
    SCORE_KIND,
    enqueue_stale_scores,
    enqueue_unenriched,
    enqueue_unextracted,
)
from cindraleads.config import settings
from cindraleads.dedupe import canonical_domain
from cindraleads.errors import CindraError, LeaseLost
from cindraleads.logging import configure_logging, get_logger
from cindraleads.metrics import record_heartbeat, source_mtime
from cindraleads.models import Job, StageResult, to_iso, utcnow
from cindraleads.queue import JobQueue
from cindraleads.runtime import Runtime
from cindraleads.sdnotify import Watchdog, notify_ready, notify_stopping
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

# sysexits.h EX_CONFIG. Paired with `RestartPreventExitStatus=78` in the units, so a
# process that cannot possibly succeed -- no token, no optional extra -- stops instead
# of restart-looping and burying the reason in a scrolling journal.
EX_CONFIG = 78

# How often the worker records that it is alive. Well under the 15 minute silence
# budget in `HEARTBEAT_UNITS`, and far above the 50 ms poll interval.
WORKER_HEARTBEAT_SECONDS = 60.0

# Read once, at import, so it describes the code this process actually loaded rather
# than whatever is on disk by the time a heartbeat fires.
_RUNNING_SOURCE_MTIME = source_mtime()

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


def _open_store(*, migrate: bool = False) -> Store:
    """Open the database, optionally bringing the schema up to date first.

    `migrate=True` for the unattended entry points -- `pipeline` and `work` -- because
    a systemd timer has nobody to read an error message. A `git pull` that ships a
    migration would otherwise leave the pipeline dead until a human noticed, and the
    symptom would be "no such column" from whichever query touched the new field
    first, which is a long way from the cause.

    Migrations are additive and each runs in its own transaction, so applying them
    here cannot leave a half-migrated database.
    """
    cfg = settings()
    cfg.ensure_dirs()
    store = Store()
    # Python 3.13 warns about an unclosed sqlite3 connection at interpreter shutdown.
    # A CLI process exiting would otherwise print a ResourceWarning after its output.
    atexit.register(store.close)
    if migrate:
        applied = store.migrate()
        if applied:
            log.info("schema_migrated", applied=applied)
            typer.echo(f"applied pending migration(s): {', '.join(applied)}")
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

    The Phase 8 gateway bot writes the same rows from Discord reactions, through the
    same function -- so correcting yourself costs one row here exactly as it does
    there. Before that was shared, the CLI inserted unconditionally and a `good`
    followed by a `bad` left both.
    """
    from cindraleads.feedback import VERDICTS, record_verdict

    if verdict not in VERDICTS:
        raise typer.BadParameter(f"verdict must be one of {sorted(VERDICTS)}")
    store = _open_store()
    record_verdict(
        store,
        lead_id=lead_id,
        verdict=verdict,
        source="cli",
        actor=os.environ.get("USER"),
        note=note,
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


@queue_app.command("release")
def queue_release(
    kind: Annotated[str, typer.Option(help="Only this kind. Empty = all kinds.")] = "",
) -> None:
    """Make deferred jobs runnable now.

    A job held back by a stage — the Extractor defers a candidate that hit the
    per-domain budget — waits out its delay. That is right in normal running and
    wrong after a fix has landed that changes what the job will do, since otherwise
    you wait out a delay for an answer that is already known to have changed.
    """
    store = _open_store()
    now = to_iso(utcnow())
    sql = (
        "UPDATE jobs SET available_at = ?, updated_at = ? "
        "WHERE status='pending' AND available_at > ?"
    )
    params: list[Any] = [now, now, now]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    with store.tx() as conn:
        released = conn.execute(sql, params).rowcount
    typer.echo(f"released {released} deferred job(s)")


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
    # Self-migrates like every other unattended entry point. This one runs hourly under
    # `cindraleads-harvest.timer`, so a `git pull` that ships a migration would stop
    # discovery dead with nobody reading the error -- and the symptom would surface as
    # "no new companies", days later, a long way from the cause.
    store = _open_store(migrate=True)

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
            record_heartbeat(store, "harvest", planned=len(plans), enqueued=new)
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
    pending = store.pending_migrations()
    if pending:
        # Reported, not applied: `status` is a read-only command and a surprise schema
        # change is not what someone asking for a summary wants. Everything below still
        # works, because it only reads columns that predate the drift.
        typer.echo(
            f"schema is behind: {', '.join(pending)} not applied. Run `cindra db migrate`.\n"
        )
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

    # Why work is not moving, which the counts above cannot show. A queue that looks
    # idle has three very different causes and they need different responses: jobs
    # held back until later, jobs that exhausted their retries, and jobs genuinely
    # ready to run. Reporting only "pending" conflates the first with the third.
    now = to_iso(utcnow())
    waiting = count("SELECT COUNT(*) FROM jobs WHERE status='pending' AND available_at > ?", now)
    ready = count("SELECT COUNT(*) FROM jobs WHERE status='pending' AND available_at <= ?", now)
    dead = count("SELECT COUNT(*) FROM jobs WHERE status IN ('dead','dead_letter')")
    failed = count("SELECT COUNT(*) FROM jobs WHERE status='failed'")
    typer.echo("\nwork")
    typer.echo(f"  {'ready now':>14}: {ready}")
    typer.echo(f"  {'deferred':>14}: {waiting}")
    typer.echo(f"  {'failed':>14}: {failed}")
    typer.echo(f"  {'dead':>14}: {dead}")
    if waiting:
        row = conn.execute(
            "SELECT MIN(available_at) AS soonest FROM jobs WHERE status='pending' "
            "AND available_at > ?",
            (now,),
        ).fetchone()
        typer.echo(f"  next deferred job runs at {row['soonest']}")
    if dead or failed:
        typer.echo("\nrecent errors")
        for row in conn.execute(
            "SELECT kind, last_error FROM jobs WHERE last_error IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 5"
        ):
            typer.echo(f"  {row['kind']}: {str(row['last_error'])[:110]}")


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
    store = _open_store(migrate=True)

    async def _run() -> None:
        async with Runtime(store=store, config=cfg) as runtime:
            plans = runtime.scout.plan(limit=limit or None, can_spend=runtime.can_spend)
            _ids, new = runtime.harvester.enqueue_plans(plans)
            typer.echo(f"planned {len(plans)}, {new} new harvest job(s)")

            # Reconcile before running the stages. Scoring driven only by the resolve
            # event cannot pick up companies resolved before the Scorer existed, nor
            # re-score one whose triggers moved since its last lead.
            fresh = enqueue_unenriched(store, runtime.queue)
            if fresh:
                typer.echo(f"queued {fresh} company/companies for enrichment")
            stale = enqueue_stale_scores(store, runtime.queue)
            if stale:
                typer.echo(f"queued {stale} company/companies for (re)scoring")
            record_heartbeat(store, "reconcile", queued_enrich=fresh, queued_score=stale)

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


def _trigger_for_tier_a(report: Any, cfg: Any) -> float:
    """What the trigger component would have to reach for Tier A, all else at its mean.

    Solves the weighted sum for `trigger`, holding every other component at the corpus
    mean and reachability at a perfect 100. Answers "is Tier A reachable by enriching
    harder" with arithmetic rather than with another week of scraping work.
    """
    floor_a = float(cfg.tiers.get("A", 75))
    others = sum(
        report.component_means.get(name, 0.0) * weight
        for name, weight in cfg.components.items()
        if name not in ("trigger", "reachability")
    )
    others += 100.0 * cfg.components.get("reachability", 0.0)
    weight = cfg.components.get("trigger", 0.0)
    return (floor_a - others) / weight if weight else float("inf")


@app.command()
def explain(
    near_misses: Annotated[int, typer.Option(help="How many marginal leads to list.")] = 10,
) -> None:
    """Why the corpus scores the way it does. Reads only; changes nothing.

    A wall of REJECTs is either bad calibration or a weak corpus, and the tier counts
    cannot tell those apart -- they look identical. The counterfactual can: if lifting
    one penalty moves forty leads over a tier line, that penalty is miscalibrated for
    how this corpus forms; if the component means are floor-level, the answer is
    upstream in discovery and no tuning will help.
    """
    from cindraleads.diagnose import diagnose
    from cindraleads.scoring import ScoringConfig

    store = _open_store()
    scoring_cfg = ScoringConfig.load()
    report = diagnose(store, config=scoring_cfg, near_miss_limit=near_misses)

    if not report.total:
        typer.echo("no leads scored yet")
        return

    typer.echo(f"{report.total} lead(s) scored")
    if not report.is_current:
        # Loud, and first. `cindra reconcile` only enqueues, so running `explain`
        # straight after a calibration change reads the corpus exactly as the old
        # rules left it -- and every number below describes a config that is no
        # longer running.
        typer.echo(
            f"\n  !! {report.stale_calibration} of {report.total} lead(s) were scored by a "
            f"DIFFERENT calibration than the one\n"
            f"     running now. Everything below describes the old rules. The worker is "
            f"re-scoring\n"
            f"     them (~18 s each); re-run this once `cindra status` shows the score "
            f"queue drained."
        )
    typer.echo("\ntiers")
    for tier in ("A", "B", "C", "REJECT"):
        now = report.tiers.get(tier, 0)
        lifted = report.tiers_unpenalised.get(tier, 0)
        arrow = f"   ->{lifted:>5} with no penalties at all" if lifted != now else ""
        typer.echo(f"  {tier:>8}: {now:>5}{arrow}")
    typer.echo(f"  {'sendable':>8}: {report.dispatchable} (anything above REJECT)")

    # The ceiling on enrichment, stated before anyone spends another week on it. If
    # Tier A is still zero when every lead is handed a perfect contact, contacts are
    # not what is holding it back and no amount of scraping will move it.
    ceiling_a = report.tiers_with_contact.get("A", 0)
    if report.total:
        typer.echo(
            "\n  with a perfect contact for every lead: "
            + ", ".join(
                f"{tier} {report.tiers_with_contact.get(tier, 0)}"
                for tier in ("A", "B", "C", "REJECT")
            )
        )
        if ceiling_a == 0:
            typer.echo(
                "  Tier A stays at 0 even then -- reachability is not the constraint.\n"
                f"  At the current means it needs trigger >= "
                f"{_trigger_for_tier_a(report, scoring_cfg):.0f}/100 against an actual "
                f"mean of {report.component_means.get('trigger', 0):.0f}. That is a "
                f"discovery problem: the corpus does not contain companies with strong\n"
                "  enough news, and no scoring or enrichment change reaches it."
            )

    typer.echo("\ncomponents (mean of 100, and how many leads score zero)")
    for name, mean in sorted(report.component_means.items(), key=lambda kv: kv[1]):
        weight = scoring_cfg.components.get(name, 0.0)
        zeros = report.component_zero_counts.get(name, 0)
        typer.echo(f"  {name:>14}: {mean:5.1f}   weight {weight:>4.0%}   zero on {zeros} lead(s)")

    typer.echo("\npenalties (how often, what it costs, and who it holds back)")
    if not report.penalty_counts:
        typer.echo("  none applied")
    for name, count in sorted(report.penalty_counts.items(), key=lambda kv: -kv[1]):
        share = 100.0 * count / report.total
        promoted = report.promoted_by_lifting.get(name, 0)
        typer.echo(
            f"  {name:>16}: {count:>4} lead(s) ({share:4.0f}%)  "
            f"lifting it alone promotes {promoted}"
        )

    if report.penalty_counts.get("single_source"):
        typer.echo("\nevidence breadth (what a lead actually rests on)")
        typer.echo(
            f"  {report.corroborated:>4} lead(s) cite 2+ distinct sources across all their "
            f"live triggers"
        )
        typer.echo(f"  {report.penalised_but_corroborated:>4} of those carry single_source anyway")
        if report.penalised_but_corroborated:
            # Zero is the expected reading now that the rule counts every trigger.
            # Anything else is either an un-rescored corpus or a regression, and those
            # need different responses -- so say which is more likely rather than
            # leaving the number to be read as a standing indictment of the rule.
            cause = (
                "they have not been re-scored yet"
                if not report.is_current
                else "the rule may have regressed -- it should count every trigger's sources"
            )
            typer.echo(f"       ({cause})")
            typer.echo(
                f"  {report.promoted_if_corroboration_counted:>4} would change tier once they are"
            )

    if report.by_template:
        typer.echo("\ndiscovery yield by icp.yaml template")
        typer.echo(f"  {'template':<24} {'found':>6} {'sendable':>9} {'hit rate':>9} {'mean':>6}")
        for row in report.by_template:
            typer.echo(
                f"  {row.template_id[:24]:<24} {row.companies:>6} {row.sendable:>9} "
                f"{row.hit_rate:>8.0%} {row.mean_score:>6.1f}"
            )
        unknown = next((r for r in report.by_template if r.template_id == "(unknown)"), None)
        if unknown and unknown.companies:
            typer.echo(
                f"  ({unknown.companies} found before provenance was recorded; they will "
                f"not be attributed retroactively)"
            )

    if report.by_harvest:
        # Before this table a template returning nothing but platform URLs was invisible
        # -- it produces no company, so it has no `discovered_by` row and never appears
        # above. Two were doing that at weights 98 and 94, spending SerpAPI credits
        # hourly for zero candidates.
        typer.echo("\nharvest yield by template (last 7 days, worst first)")
        typer.echo(f"  {'template':<24} {'runs':>5} {'hits':>6} {'candidates':>11} {'dropped':>8}")
        for harvested in report.by_harvest:
            flag = "  <-- returns nothing usable" if harvested.is_barren else ""
            typer.echo(
                f"  {harvested.template_id[:24]:<24} {harvested.runs:>5} "
                f"{harvested.hits:>6} {harvested.candidates:>11} "
                f"{harvested.dropped_platform:>8}{flag}"
            )
        barren = [r.template_id for r in report.by_harvest if r.is_barren]
        if barren:
            typer.echo(
                f"\n  {len(barren)} template(s) found hits and produced no candidate: "
                f"{', '.join(barren)}."
                f"\n  Every hit was a platform URL with no company site behind it. Lower "
                f"the weight in icp.yaml, add a site: filter, or retire them -- they cost "
                f"credits and a plan slot on every run."
            )

    typer.echo(f"\nclosest to the Tier C floor of {report.floor:.0f}")
    for lead, gap, blocker in report.near_misses:
        typer.echo(
            f"  {lead.score:>3}  {gap:5.1f} short  {lead.display_name[:26]:<26} "
            f"{lead.domain[:24]:<24} {blocker}"
        )

    typer.echo(
        "\nRead it this way: if 'lifting it alone promotes' is large, the penalty is\n"
        "miscalibrated for this corpus. If the component means are floor-level, the\n"
        "problem is upstream in discovery and no penalty tuning will reach it."
    )


@app.command()
def reconcile(
    force: Annotated[
        bool,
        typer.Option(
            help="Re-queue enrichment and scoring even where a job already ran. See the docstring."
        ),
    ] = False,
) -> None:
    """Queue the work the event flow missed. Enqueue only -- drains nothing.

    Under systemd the worker is a long-running service and the timers only ever add
    to the queue; a timer that also drained would race the worker for the same jobs.
    The lease makes that safe rather than correct, and two processes loading the same
    model on a 16 GB Pi is not something to rely on being merely wasteful.

    Both reconcilers ask "what state is inconsistent", not "what happened", so this
    heals a stage added later, a restore from backup, and a crash between stages --
    none of which any event would replay.

    `--force` re-queues **enrichment and scoring** even for companies whose job already
    ran. Needed after a worker drained jobs while executing a stale build: those jobs
    completed without doing their work, and their dedupe keys now block the retry. It is
    equally needed after the Enricher gets *better* -- `enriched_at` records that we
    looked, not what we could see at the time, so teaching it to read `mailto:`
    attributes reached none of the companies already marked enriched.

    The data cannot tell a job that worked from one that did not, so this is the human
    override. It is not the default: rescoring costs ~18 s of Pi inference per company,
    and re-enrichment spends real fetches against the 6-per-domain daily budget.
    """
    cfg = settings()
    cfg.ensure_dirs()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=True)
    store = _open_store(migrate=True)

    async def _run() -> None:
        async with Runtime(store=store, config=cfg) as runtime:
            # Extraction first, and it is the one that recovers *lost* work rather
            # than merely late work: a candidate whose extract job died never became a
            # company, so no other reconciler here can see it.
            stranded = enqueue_unextracted(store, runtime.queue)
            fresh = enqueue_unenriched(store, runtime.queue, force=force)
            stale = enqueue_stale_scores(store, runtime.queue, force=force)
        typer.echo(
            f"queued {stranded} for extraction, {fresh} for enrichment, "
            f"{stale} for (re)scoring" + (" (forced past dedupe)" if force else "")
        )
        record_heartbeat(
            store,
            "reconcile",
            queued_extract=stranded,
            queued_enrich=fresh,
            queued_score=stale,
        )

    asyncio.run(_run())


@app.command()
def suppress(
    domain: Annotated[str, typer.Argument(help="Domain to suppress. Omit with --list.")] = "",
    reason: Annotated[str, typer.Option(help="Why, for whoever reads this in a month.")] = "",
    remove: Annotated[bool, typer.Option(help="Un-suppress instead.")] = False,
    show: Annotated[bool, typer.Option("--list", help="Print the list.")] = False,
) -> None:
    """Never contact this domain again.

    Both ends of this were built in Phase 0 and nothing ever wrote to the table. The
    Scout reads it at *plan* time, so a suppressed company stops costing SerpAPI credits
    and ~64 s of extraction before anyone decides to reject it; the ComplianceGate reads
    it again at dispatch and vetoes. Only the writer was missing.

    The case it exists for is the one a rule cannot catch. `under_employee_ceiling`
    deliberately does not veto when `employee_band` is unknown -- silence is not
    evidence of size, and vetoing on it would reject every startup with a terse landing
    page -- so PagerDuty and JetBrains reach Tier B and sit at the top of the call list.
    They are not mis-scored; they are simply not prospects, and that is a judgement.
    """
    store = _open_store()
    if show:
        rows = store.conn.execute(
            "SELECT value, reason, created_at FROM suppression_list "
            "WHERE kind = 'domain' ORDER BY created_at DESC"
        ).fetchall()
        if not rows:
            typer.echo("nothing suppressed")
            return
        for row in rows:
            typer.echo(f"{row['value']:<32} {row['reason'] or ''}")
        return

    target = canonical_domain(domain) or domain.strip().lower()
    if not target:
        raise typer.BadParameter("need a domain, or --list")

    with store.tx() as conn:
        if remove:
            cur = conn.execute(
                "DELETE FROM suppression_list WHERE kind = 'domain' AND value = ?", (target,)
            )
            typer.echo(f"{'un-suppressed' if cur.rowcount else 'was not suppressed'} {target}")
            return
        conn.execute(
            "INSERT OR REPLACE INTO suppression_list (entry_id, kind, value, reason, "
            "created_at) VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex[:16], "domain", target, reason or None, to_iso(utcnow())),
        )
    typer.echo(f"suppressed {target}")


@app.command(name="worklist")
def worklist_cmd(
    limit: Annotated[int, typer.Option(help="How many leads to list.")] = 25,
    tier: Annotated[str, typer.Option(help="Comma-separated tiers to include.")] = "A,B",
    include_judged: Annotated[
        bool, typer.Option(help="Keep leads you have already judged.")
    ] = False,
) -> None:
    """The call list: reachable leads worth a personal email, and what to say.

    Everything else here answers "is this a lead?". This answers "what do I send before
    lunch?" -- one row per company, the best address, the angle the Scorer already
    wrote, and one URL that proves the trigger.

    Judged leads drop off by default, so the list shrinks as you work it.
    """
    from cindraleads.worklist import render_worklist, worklist

    store = _open_store()
    tiers = tuple(t.strip().upper() for t in tier.split(",") if t.strip())
    typer.echo(
        render_worklist(worklist(store, tiers=tiers, limit=limit, include_judged=include_judged))
    )


@app.command()
def digest(
    limit: Annotated[int, typer.Option(help="Max leads in one run.")] = 40,
    dry_run: Annotated[bool, typer.Option(help="Count the pages, post nothing.")] = False,
) -> None:
    """Post the Tier C backlog as a paginated digest.

    The per-lead stage only sends A and B, so this is the only route a Tier C lead has
    to Discord. It reconciles rather than draining a queue -- "leads below Tier B that
    `dispatch_log` has never seen" -- so a missed timer costs a day's delay, not a
    day's leads.
    """
    from cindraleads.agents.dispatcher import send_digest

    cfg = settings()
    cfg.ensure_dirs()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=True)
    store = _open_store(migrate=True)

    async def _run() -> None:
        async with Runtime(store=store, config=cfg) as runtime:
            report = await send_digest(runtime.dispatcher, limit=limit, dry_run=dry_run)

        typer.echo(f"pending below Tier B: {report.pending}")
        if report.skipped:
            typer.echo(f"skipped: {report.skipped}")
        elif dry_run:
            typer.echo(f"dry run: would post {report.pages} page(s)")
        else:
            typer.echo(f"sent {report.sent} lead(s) in {report.pages} page(s)")
        if report.error:
            typer.echo(f"stopped early: {report.error}")
        if not dry_run:
            record_heartbeat(
                store, "digest", ok=not report.error, sent=report.sent, pending=report.pending
            )

    asyncio.run(_run())


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Localhost port for /healthz and /metrics.")] = 9109,
) -> None:
    """Run the health and metrics endpoint until stopped.

    Its own process on purpose. Folding it into the worker would mean the endpoint
    dies with the thing it is there to report on, which is the one moment you need it.
    """
    from cindraleads.health import DEFAULT_HOST
    from cindraleads.health import serve as serve_health

    cfg = settings()
    cfg.ensure_dirs()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=False)
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    store = _open_store(migrate=True)
    server = serve_health(store, config=cfg, port=port)
    typer.echo(f"listening on http://{DEFAULT_HOST}:{port}/  (healthz, metrics)")
    notify_ready(f"health endpoint on {port}")
    try:
        while not _shutdown:
            time.sleep(0.5)
    finally:
        notify_stopping()
        server.shutdown()


@app.command()
def maintain(
    dry_run: Annotated[bool, typer.Option(help="Report what would change, change nothing.")] = (
        False
    ),
    no_network: Annotated[bool, typer.Option(help="Skip the evidence resample.")] = False,
) -> None:
    """The nightly backward-looking pass.

    Retire triggers a tightened rule would no longer write, flip decayed ones, re-check
    a sample of evidence URLs, purge past retention, and sweep the cache. Companies
    whose trigger set changed are queued for re-scoring, because nothing else notices:
    the score reconciler keys on trigger timestamps, and a retirement moves none of them.
    """
    from cindraleads.maintenance import MaintenanceConfig, run_maintenance

    cfg = settings()
    cfg.ensure_dirs()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=True)
    store = _open_store(migrate=True)
    mcfg = MaintenanceConfig.load(cfg)

    async def _run() -> None:
        async with Runtime(store=store, config=cfg) as runtime:
            report = await run_maintenance(
                store,
                queue=runtime.queue,
                egress=None if no_network else runtime.egress,
                cache=runtime.cache,
                config=mcfg,
                dry_run=dry_run,
            )

        if dry_run:
            typer.echo("dry run -- nothing was changed\n")
        typer.echo("triggers")
        typer.echo(f"  {'superseded':>14}: {report.superseded}")
        for code, count in sorted(report.superseded_codes.items()):
            typer.echo(f"  {'':>14}  {code}: {count}")
        typer.echo(f"  {'decayed':>14}: {report.decayed}")
        typer.echo(f"  {'unevidenced':>14}: {report.unevidenced}")

        typer.echo("\nevidence")
        if no_network:
            typer.echo("  resample skipped (--no-network)")
        elif dry_run:
            # Not "re-checked": nothing was fetched, and retirement has not run either,
            # so the live-trigger pool this sampled from is still the pre-retirement one.
            typer.echo(f"  {'would sample':>14}: {report.evidence_sampled} (upper bound)")
        else:
            inconclusive = report.evidence_sampled - report.evidence_checked
            typer.echo(f"  {'sampled':>14}: {report.evidence_sampled}")
            typer.echo(f"  {'answered':>14}: {report.evidence_checked}")
            typer.echo(f"  {'inconclusive':>14}: {inconclusive}")
            typer.echo(f"  {'found dead':>14}: {report.evidence_dead}")

        typer.echo("\nretention")
        for table, count in sorted(report.purged.items()):
            typer.echo(f"  {table:>14}: {count}")
        typer.echo(f"  {'cache rows':>14}: {report.cache_rows}")
        typer.echo(f"  {'cache files':>14}: {report.cache_files}")

        typer.echo(f"\nqueued {report.rescored} company/companies for re-scoring")
        if not report.changed:
            typer.echo("nothing needed changing")
        if not dry_run:
            record_heartbeat(
                store, "maintenance", superseded=report.superseded, rescored=report.rescored
            )

    asyncio.run(_run())


@app.command()
def health() -> None:
    """Host state as the governor sees it.

    Exists because "inference is paused by the thermal governor" is not actionable on
    its own: heat, an under-voltage fault and a missing `vcgencmd` are three different
    problems with three different fixes, and the log line cannot tell them apart.
    """
    from cindraleads.thermal import ThermalGovernor, VcgencmdReader

    reader = VcgencmdReader()
    typer.echo(f"vcgencmd on PATH: {reader.available()}")
    if not reader.available():
        typer.echo(
            "  vcgencmd is missing, so temperature is unknown. The governor holds its\n"
            "  last state rather than assuming the Pi is cool."
        )

    governor = ThermalGovernor()
    policy = governor.poll()
    snapshot = governor.snapshot()
    for key in (
        "state",
        "temp_c",
        "throttled_now",
        "throttled_ever",
        "active_flags",
        "available_ram_mb",
        "max_workers",
        "allow_llm",
        "alert_level",
        "reason",
    ):
        typer.echo(f"  {key:>18}: {snapshot.get(key)}")

    if not policy.allow_llm:
        typer.echo(
            "\nLLM inference is PAUSED. Leads still score -- the arithmetic needs no\n"
            "model -- but outreach angles are deferred until this clears."
        )


@app.command("dispatch-test")
def dispatch_test(
    lead_id: Annotated[str, typer.Option(help="A specific lead. Empty = highest scoring.")] = "",
    dry_run: Annotated[bool, typer.Option(help="Print the embed, post nothing.")] = False,
) -> None:
    """Send one real card to Discord, to prove the wiring.

    Separate from the pipeline on purpose: when no card arrives, the question is
    whether the webhook is wrong or whether no lead qualified, and those need different
    fixes. This answers the first without waiting for the second.
    """
    cfg = settings()
    cfg.ensure_dirs()
    store = _open_store()

    async def _run() -> None:
        async with Runtime(store=store, config=cfg) as runtime:
            target = lead_id
            if not target:
                # Any tier, including REJECT. This proves the *wiring*, and it has to
                # answer "is the webhook right?" on a database where nothing yet
                # qualifies — which is exactly the state that prompts the question.
                row = store.conn.execute(
                    "SELECT lead_id, tier, score FROM leads ORDER BY score DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    typer.echo("no lead exists yet — run `cindra pipeline` first", err=True)
                    raise typer.Exit(code=1)
                target = str(row["lead_id"])
                typer.echo(f"using {target} (tier {row['tier']}, score {row['score']})")

            configured = sorted(runtime.dispatcher.webhooks)
            typer.echo(f"webhooks configured: {', '.join(configured) or '(none)'}")
            if not configured and not dry_run:
                typer.echo(
                    "No DISCORD_WEBHOOK_* is set in .env, so there is nowhere to post.",
                    err=True,
                )
                raise typer.Exit(code=1)

            from cindraleads.agents.dispatcher import build_card

            lead = runtime.dispatcher.read_lead(target)
            if lead is None:
                typer.echo(f"lead {target} not found", err=True)
                raise typer.Exit(code=1)
            embed = build_card(lead)

            if dry_run:
                typer.echo(json.dumps(embed, indent=2, ensure_ascii=False))
                return

            # Posted directly rather than through the stage: this is a wiring check, so
            # it must not write a dispatch_log row. Otherwise testing the webhook would
            # mark the lead as already sent and suppress the real card later.
            url = runtime.dispatcher.webhook_for("ops")
            assert url is not None
            result = await runtime.dispatcher.webhook.post(
                url, {"embeds": [embed], "username": "CindraLeads (test)"}
            )
            typer.echo("sent" if result.ok else f"failed: {result.error}")
            if not result.ok:
                raise typer.Exit(code=1)

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
        ENRICH_KIND: runtime.enricher,
        SCORE_KIND: runtime.scorer,
        DISPATCH_KIND: runtime.dispatcher,
    }
    stages.update({kind: stage for kind, stage in available.items() if kind in kinds})
    return stages


# Order matters: `cindra pipeline` drains each fully before starting the next, so a
# batch of ~64 s extractions never interleaves with harvest fetches or Discord posts
# competing for the same worker.
PIPELINE_KINDS = (
    HARVEST_KIND,
    EXTRACT_KIND,
    RESOLVE_KIND,
    ENRICH_KIND,
    SCORE_KIND,
    DISPATCH_KIND,
)


def _needs_runtime(kinds: list[str]) -> bool:
    return any(kind in kinds for kind in PIPELINE_KINDS)


# ---------------------------------------------------------------------- feedback


@app.command("feedback-bot")
def feedback_bot() -> None:
    """Run the Discord gateway client that turns reactions into feedback rows.

    A long-lived process with no queue lease and no model. Killing it costs reactions
    and nothing else; `cindra feedback` remains the manual path.
    """
    from cindraleads.feedback.bot import run_bot

    cfg = settings()
    configure_logging(log_dir=cfg.resolve(cfg.log_dir), level=cfg.log_level, console=False)
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    store = _open_store(migrate=True)
    try:
        with contextlib.suppress(KeyboardInterrupt):  # the SIGINT path
            asyncio.run(run_bot(store))
    except CindraError as exc:
        # A missing token or a missing extra is a configuration error, not a crash, and
        # it must not print a traceback every 30 seconds forever. Exit 78 (EX_CONFIG);
        # the unit's `RestartPreventExitStatus=78` stops the restart loop on it, so
        # `systemctl status` shows one legible reason instead of a scrolling stack.
        typer.echo(f"cannot start the feedback bot: {exc}", err=True)
        raise typer.Exit(EX_CONFIG) from None


@app.command("precision-report")
def precision_report_cmd(
    days: Annotated[float, typer.Option(help="Window, in days.")] = 7.0,
    pending: Annotated[int, typer.Option(help="How many unjudged leads to list.")] = 10,
    write: Annotated[bool, typer.Option(help="Also write reports/precision_YYYY-WW.md.")] = False,
) -> None:
    """Precision over the leads someone actually judged, plus what is still unjudged.

    `judged` prints beside `precision` and never under it. 100% over two leads is not
    evidence of anything, and a report showing only the ratio reads as though it were.
    """
    from cindraleads.feedback import precision_report, unjudged_leads

    store = _open_store()
    report = precision_report(store, days=days)
    pending_rows = unjudged_leads(store, days=days, limit=pending) if pending else []

    lines = [
        f"# Precision, last {days:g} days",
        "",
        f"dispatched:  {report.dispatched}",
        f"judged:      {report.judged}  ({report.coverage:.0%} of dispatched)",
        f"good / bad:  {report.good} / {report.bad}",
        "precision:   "
        + (
            f"{report.precision:.0%}  (over {report.judged} judged leads)"
            if report.precision is not None
            else "n/a -- nothing judged yet"
        ),
    ]
    if report.by_tier:
        lines += ["", "by tier:"]
        lines += [
            f"  {tier}: {good} good / {bad} bad" for tier, (good, bad) in report.by_tier.items()
        ]
    if pending_rows:
        lines += ["", f"unjudged ({len(pending_rows)} of {report.dispatched - report.judged}):"]
        lines += [
            f"  {row['lead_id']}  {row['tier']}  {row['score']:>3}  {row['display_name']}"
            for row in pending_rows
        ]

    body = "\n".join(lines)
    typer.echo(body)

    if write:
        iso = utcnow().isocalendar()
        path = Path("reports") / f"precision_{iso.year}-W{iso.week:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")
        typer.echo(f"\nwrote {path}")


@app.command()
def acceptance(
    hours: Annotated[float, typer.Option(help="Window to assess.")] = 72.0,
    write: Annotated[bool, typer.Option(help="Also write reports/acceptance_<date>.md.")] = False,
) -> None:
    """What the last 72 h of unattended running actually proved.

    Heat is reported, never graded. The original gate asked `get_throttled` to stay
    `0x0`, which asserts a heatsink rather than this system -- and requires that the
    thermal governor never does the job it exists for.
    """
    from cindraleads.acceptance import assess_run, render_markdown

    store = _open_store()
    report = assess_run(store, hours=hours)
    body = render_markdown(report)
    typer.echo(body)

    if write:
        path = Path("reports") / f"acceptance_{utcnow():%Y%m%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        typer.echo(f"wrote {path}")
    raise typer.Exit(0 if report.passed else 1)


@app.command()
def critic(
    write: Annotated[bool, typer.Option(help="Also write reports/critic_YYYY-WW.md.")] = False,
) -> None:
    """Propose scoring and query-plan changes. Applies none of them.

    Every proposal is a diff you type yourself. That is not caution about bugs: a
    scoring change that applied itself would be one nobody read, measured against a
    corpus scored under the rules it just replaced.
    """
    from cindraleads.agents.critic import critique, render_markdown

    store = _open_store()
    body = render_markdown(critique(store))
    typer.echo(body)

    if write:
        iso = utcnow().isocalendar()
        path = Path("reports") / f"critic_{iso.year}-W{iso.week:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        typer.echo(f"wrote {path}")


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
    store = _open_store(migrate=True)

    async def _main() -> int:
        # READY only once the Runtime is built. Under `Type=notify` systemd holds
        # dependent units until this fires, so announcing it earlier would report the
        # worker up while Ollama and the egress client were still being wired.
        try:
            if not _needs_runtime(wanted):
                notify_ready(f"worker: {','.join(wanted)}")
                return await _work_loop(store, _stages_for(wanted, None), **_opts())
            async with Runtime(store=store, config=cfg) as runtime:
                notify_ready(f"worker: {','.join(wanted)}")
                return await _work_loop(
                    store, _stages_for(wanted, runtime), governor=runtime.governor, **_opts()
                )
        finally:
            notify_stopping()

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


# The longest a single stage may run before we stop believing in it. Generous against
# the measured worst case -- a 64 s p50 page, plus a thermal pause -- because the cost
# of cutting a slow job short is a lost lead and the cost of waiting is a slow lead.
MAX_STAGE_SECONDS = 900.0


def _renewal_interval(lease: int, watchdog: Watchdog) -> float:
    """How long to sleep between renewals while a stage runs.

    Two deadlines have to be respected and only one of them is ours. The lease is a
    third of `lease`, short enough that a renewal is never the last one before expiry.
    **The watchdog is the one that bites**, and it belongs to systemd: `WatchdogSec=180`
    means `watchdog.interval` is 90 s and the kill lands at 180.

    Getting this wrong crash-looped the worker twelve times. The interval was
    `lease / 3`, which at the unit's `--lease 600` is 200 seconds -- so on any stage
    slower than three minutes the loop sat inside `asyncio.wait` for 200 s, never petted,
    and systemd sent SIGABRT at 180. Every slow scoring job killed the process that was
    running it, which then looked like a thermal problem and a corpus problem rather
    than an arithmetic one.

    Half the pet interval, not the whole of it: `Watchdog.pet()` rate-limits itself, so
    waking at exactly 90 s means jitter can push a ping past its own gate and the next
    chance is 180 s away, which is the deadline.
    """
    bound = lease / 3
    if watchdog.enabled:
        bound = min(bound, watchdog.interval / 2)
    return max(1.0, bound)


async def _prepare_renewing_lease(
    stage: Stage,
    job: Job,
    queue: JobQueue,
    *,
    lease: int,
    watchdog: Watchdog,
    on_tick: Callable[[], None] | None = None,
) -> Any:
    """Run `prepare()`, renewing the lease and petting the watchdog while it runs.

    Phase 0 built `extend_lease`, tested it, and nothing ever called it. The bill came
    due as dead-lettered `score.company` jobs: decode on this Pi is 3.7 tok/s, so an
    LLM stage can outlive any fixed lease, and once it does the job is reclaimed *while
    still being worked on* -- the original worker then hits `LeaseLost` at commit and
    throws away work it had actually finished.

    The watchdog is petted here for the same reason, and this is the part that needs a
    bound: a stage stuck in a socket read with no timeout would otherwise renew its
    lease and pet its watchdog forever, which is precisely the wedge both mechanisms
    exist to catch. `MAX_STAGE_SECONDS` is the line. Past it the stage is cancelled and
    the job fails honestly rather than being held open by a worker that will never
    finish it.
    """
    task = asyncio.ensure_future(stage.prepare(job))
    interval = _renewal_interval(lease, watchdog)
    deadline = time.monotonic() + MAX_STAGE_SECONDS

    while True:
        # Never wait past the deadline: sleeping a full interval first would let a
        # wedged stage run up to one renewal period beyond the bound it just broke.
        remaining = deadline - time.monotonic()
        done, _ = await asyncio.wait({task}, timeout=max(0.0, min(interval, remaining)))
        if done:
            return task.result()
        if time.monotonic() >= deadline:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise CindraError(
                f"stage {job.kind} exceeded {MAX_STAGE_SECONDS:.0f}s and was cancelled"
            )
        queue.extend_lease(job.job_id, seconds=lease)
        watchdog.pet()
        # The heartbeat too, for the same reason as the other two: the main loop writes
        # it every 60 s and does not get to run while a stage does. A stage legitimately
        # taking longer than `HEARTBEAT_GAP_SECONDS` would otherwise be reported by
        # `cindra acceptance` as a gap in the worker's record -- the signal that means
        # "the worker was dead and came back" -- for a worker that was working the whole
        # time.
        if on_tick is not None:
            on_tick()


def _thermal_detail(governor: Any) -> dict[str, Any]:
    """The three thermal fields worth carrying on a heartbeat, or nothing.

    Read from the governor's *last* reading rather than polling: `poll()` shells out to
    `vcgencmd`, and doing that on the heartbeat would add a subprocess a minute to
    measure something the thermal gate already sampled on every LLM call.

    Empty on a box with no sensor, so the field is absent rather than falsely `0` --
    "we did not look" and "it was cold" must not read the same, which is the same
    three-valued rule as `evidence.reachable`.
    """
    if governor is None:
        return {}
    try:
        reading = governor.last_reading
        if reading is None:
            return {}
        return {
            "thermal_state": governor.state,
            "temp_c": reading.temp_c,
            "throttled_now": reading.throttled_now,
        }
    except Exception:  # a sensor fault must never stop the heartbeat
        return {}


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
    governor: Any = None,
) -> int:
    queue = JobQueue(store)
    queue.reclaim_expired()

    # Petted at the top of every iteration, including the empty polls. That is the
    # point: a worker with nothing to do is healthy and must keep saying so, while one
    # wedged inside a stage stops petting and systemd restarts it. `Watchdog` is inert
    # unless systemd set WATCHDOG_USEC for this pid.
    watchdog = Watchdog()
    heartbeat_due = 0.0

    processed = 0

    def _beat() -> None:
        """The liveness record the health endpoint reads.

        Cheap, but not free -- one INSERT per loop on an empty queue polling every
        50 ms would be 20 writes a second against the same lock the stages need,
        which is why the caller rate-limits it.
        """
        record_heartbeat(
            store,
            "worker",
            worker_id=worker_id,
            processed=processed,
            # The build this process is *running*, captured once at import. If the
            # source on disk is newer, a `git pull` has landed that this worker
            # cannot see -- Python does not reload modules.
            source_mtime=_RUNNING_SOURCE_MTIME,
            # Heat, sampled onto a row that was being written anyway. The governor
            # keeps its state in memory and `/healthz` reports the instant, so
            # before this nothing survived a poll -- and "did the governor engage
            # over those 72 hours, and did it recover" was unanswerable the moment
            # the hour passed. One minute of resolution over three days is 4320
            # rows, which the retention purge already sweeps.
            **_thermal_detail(governor),
        )

    while not _shutdown:
        watchdog.pet()
        now = time.monotonic()

        if now >= heartbeat_due:
            _beat()
            heartbeat_due = now + WORKER_HEARTBEAT_SECONDS

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
            outcome = await _prepare_renewing_lease(
                stage, job, queue, lease=lease, watchdog=watchdog, on_tick=_beat
            )
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

    record_heartbeat(store, "worker", worker_id=worker_id, processed=processed, exiting=True)
    log.info("worker_exit", worker_id=worker_id, processed=processed)
    return processed


def main() -> None:
    app()


if __name__ == "__main__":
    main()
