"""Heartbeats and Prometheus exposition, both read out of SQLite.

**Every number here is computed from the database at scrape time, not accumulated in
process memory.** That is the one design decision in this module and it follows from
how Phase 7 runs the system: the worker is long-lived, but harvest, enrichment, the
digest and maintenance are separate short-lived processes started by systemd timers.
An in-process counter in any of them would see only its own slice of the work and then
exit, so `cindraleads_jobs_total` would report whatever the last process to die
happened to have done. Querying the shared database is the only way a single scrape
describes the whole system, and it has the pleasant side effect that metrics survive a
restart and a crash without a persistence layer of their own.

The cost is that these are gauges over current state rather than monotonic counters,
so `rate()` does not apply to most of them. That is the right trade for a single-node
system whose real questions are "is anything stuck" and "when did harvest last run",
not "what is the p99 request rate".

`prometheus_client` is deliberately not used. The text format is a dozen lines to
generate, and a metrics endpoint that fails to start because an optional extra is
missing is worse than no metrics endpoint -- this is the code you reach for when
something is already wrong.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cindraleads.logging import get_logger
from cindraleads.models import to_iso, utcnow
from cindraleads.store import Store

__all__ = [
    "HEARTBEAT_UNITS",
    "OPTIONAL_UNITS",
    "Heartbeat",
    "heartbeats",
    "last_heartbeat",
    "record_heartbeat",
    "render_prometheus",
    "snapshot",
    "source_mtime",
]

log = get_logger("cindraleads.metrics")

HEARTBEAT_METRIC = "heartbeat"

# The units whose silence means something. A unit missing from this tuple is not
# monitored, so adding a timer without adding it here buys a blind spot: the whole
# point of Phase 7 is that 72 hours pass without anyone looking, and a stage that
# stopped running three days ago must be visible when someone finally does.
#
# `max_silence_hours` is how long absence is normal, generously: harvest runs hourly
# but a thermal pause or an exhausted budget can legitimately skip several, so the
# alarm is set well past "unusual" and at "something is wrong".
# The name must match the systemd unit's, minus the `cindraleads-` prefix --
# `test_every_timer_unit_has_a_heartbeat_budget` asserts it, and it caught this exact
# mistake: renaming `enrich.timer` to `reconcile.timer` left the heartbeat under the
# old key, so a reconciler that stopped running would have gone unnoticed while the
# endpoint reported a unit nothing writes.
HEARTBEAT_UNITS: dict[str, float] = {
    "worker": 0.25,
    "harvest": 6.0,
    "reconcile": 12.0,
    "digest": 36.0,
    "maintenance": 36.0,
    "feedback": 1.0,
}

# Units that are a choice rather than part of the pipeline. Never having run one is
# normal and reported as such; having run it and then stopped is still a fault.
#
# The feedback bot is the only one: it needs a bot token and a guild invite, and a Pi
# running without it is fully functional with the CLI as the feedback path. Without
# this distinction every install that declined the bot would sit permanently degraded,
# which is precisely how an endpoint gets ignored -- and then a real fault with it.
#
# Nothing that drains the queue or writes a lead may be listed here.
OPTIONAL_UNITS: frozenset[str] = frozenset({"feedback"})

# How far back "is anything dying" looks. A day covers a full cycle of every timer, so
# a stage that is systematically failing shows up inside one window while yesterday's
# fixed bug drops out of it.
DEAD_LETTER_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Heartbeat:
    unit: str
    at: datetime
    ok: bool
    detail: dict[str, Any]

    def age_seconds(self, *, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.at).total_seconds()


def record_heartbeat(
    store: Store,
    unit: str,
    *,
    ok: bool = True,
    conn: sqlite3.Connection | None = None,
    **detail: Any,
) -> None:
    """Record that `unit` reached the end of a run.

    Written at the *end* deliberately. A heartbeat at the start would keep ticking
    while a stage failed every single time, which is precisely the state this is meant
    to make visible.
    """
    labels = json.dumps({"unit": unit, "ok": ok, **detail}, separators=(",", ":"), default=str)
    row = (HEARTBEAT_METRIC, 1.0 if ok else 0.0, labels, to_iso(utcnow()))
    statement = "INSERT INTO metrics (name, value, labels, recorded_at) VALUES (?,?,?,?)"
    if conn is not None:
        conn.execute(statement, row)
    else:
        with store.tx() as own:
            own.execute(statement, row)
    log.debug("heartbeat", unit=unit, ok=ok, **detail)


def source_mtime() -> float:
    """Newest modification time across the package source, or 0 if unreadable.

    Used to catch a long-running worker still executing the code it imported at boot.
    `git pull` rewrites these files; the running process keeps its old modules, so
    every fix sits on disk doing nothing until the unit restarts -- silently, while
    the worker looks perfectly healthy and drains jobs the whole time.

    mtime rather than a git sha: no subprocess (the passive-only rule forbids shelling
    out from the package), no dependency on a `.git` directory that a deployed copy may
    not have, and it catches an edited file as readily as a pulled one.
    """
    root = Path(__file__).resolve().parent
    newest = 0.0
    try:
        for path in root.rglob("*.py"):
            newest = max(newest, path.stat().st_mtime)
    except OSError:
        return 0.0
    return newest


def last_heartbeat(store: Store, unit: str) -> Heartbeat | None:
    row = store.conn.execute(
        "SELECT value, labels, recorded_at FROM metrics WHERE name = ? "
        "AND labels LIKE ? ORDER BY recorded_at DESC LIMIT 1",
        (HEARTBEAT_METRIC, f'%"unit":"{unit}"%'),
    ).fetchone()
    if row is None:
        return None
    try:
        detail = json.loads(str(row["labels"]))
    except ValueError:
        detail = {}
    return Heartbeat(
        unit=unit,
        at=datetime.fromisoformat(str(row["recorded_at"])),
        ok=bool(row["value"]),
        detail=detail,
    )


def heartbeats(store: Store) -> dict[str, Heartbeat | None]:
    return {unit: last_heartbeat(store, unit) for unit in HEARTBEAT_UNITS}


# ------------------------------------------------------------------------- snapshot


def snapshot(store: Store, *, now: datetime | None = None) -> dict[str, float]:
    """Every gauge, as a flat name -> value mapping.

    Shared by `/metrics`, `/healthz` and `cindra status` so the three can never
    disagree about what the system is doing -- three code paths computing "how many
    leads are live" three ways is how a dashboard ends up contradicting the CLI.
    """
    at = now or utcnow()
    stamp = to_iso(at)
    conn = store.conn

    def count(sql: str, *params: Any) -> float:
        row = conn.execute(sql, params).fetchone()
        return float(row[0] if row else 0)

    values: dict[str, float] = {
        "companies_total": count("SELECT COUNT(*) FROM companies"),
        "companies_enriched": count("SELECT COUNT(*) FROM companies WHERE enriched_at IS NOT NULL"),
        "candidates_pending": count("SELECT COUNT(*) FROM candidates WHERE status = 'new'"),
        "triggers_live": count(
            "SELECT COUNT(*) FROM triggers WHERE active = 1 AND decays_at > ?", stamp
        ),
        "evidence_total": count("SELECT COUNT(*) FROM evidence"),
        "evidence_dead": count("SELECT COUNT(*) FROM evidence WHERE reachable = 0"),
        "contacts_total": count("SELECT COUNT(*) FROM contacts"),
        "leads_total": count("SELECT COUNT(*) FROM leads"),
        "dispatches_total": count("SELECT COUNT(*) FROM dispatch_log"),
        "queue_ready": count(
            "SELECT COUNT(*) FROM jobs WHERE status = 'pending' AND available_at <= ?", stamp
        ),
        "queue_deferred": count(
            "SELECT COUNT(*) FROM jobs WHERE status = 'pending' AND available_at > ?", stamp
        ),
        "queue_in_flight": count("SELECT COUNT(*) FROM jobs WHERE status = 'in_flight'"),
        "queue_failed": count("SELECT COUNT(*) FROM jobs WHERE status = 'failed'"),
        "queue_dead": count("SELECT COUNT(*) FROM jobs WHERE status IN ('dead','dead_letter')"),
        "dead_letter_total": count("SELECT COUNT(*) FROM dead_letter"),
        # The same pile, asked about in the present tense. `dead_letter` is append-only
        # and nothing purges it, so the total answers "has anything ever died" and
        # cannot answer "is anything dying". Four jobs buried by two bugs that are now
        # fixed held `/healthz` at degraded indefinitely, and no amount of healthy
        # running would have cleared it. Both are exported: the total is the record
        # `cindra acceptance` grades against, the window is what a probe should read.
        "dead_letter_recent": count(
            "SELECT COUNT(*) FROM dead_letter WHERE died_at > ?",
            to_iso(at - DEAD_LETTER_WINDOW),
        ),
    }

    for tier in ("A", "B", "C", "REJECT"):
        values[f"leads_tier_{tier.lower()}"] = count(
            "SELECT COUNT(*) FROM leads WHERE tier = ?", tier
        )

    # The Phase 7 acceptance number: >= 15 Tier A+B per day, sustained. Counting
    # dispatches rather than leads, because a lead that scored well and never reached
    # Discord did not do its job.
    day_ago = to_iso(at - timedelta(days=1))
    values["dispatches_24h"] = count(
        "SELECT COUNT(*) FROM dispatch_log WHERE dispatched_at > ?", day_ago
    )
    values["dispatches_ab_24h"] = count(
        "SELECT COUNT(*) FROM dispatch_log WHERE dispatched_at > ? AND tier IN ('A','B')",
        day_ago,
    )
    return values


def render_prometheus(store: Store, *, now: datetime | None = None) -> str:
    """Prometheus text exposition format 0.0.4."""
    at = now or utcnow()
    lines: list[str] = []

    for name, value in sorted(snapshot(store, now=at).items()):
        metric = f"cindraleads_{name}"
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {value:g}")

    lines.append("# HELP cindraleads_heartbeat_age_seconds Time since a unit last finished a run.")
    lines.append("# TYPE cindraleads_heartbeat_age_seconds gauge")
    for unit, beat in heartbeats(store).items():
        # -1, not 0 and not omitted. Omitting it makes an alert on "too old" silently
        # never fire for a unit that has never run, which is the worst case of all.
        age = beat.age_seconds(now=at) if beat else -1.0
        lines.append(f'cindraleads_heartbeat_age_seconds{{unit="{unit}"}} {age:.0f}')

    lines.append("# TYPE cindraleads_heartbeat_ok gauge")
    for unit, beat in heartbeats(store).items():
        lines.append(f'cindraleads_heartbeat_ok{{unit="{unit}"}} {1 if beat and beat.ok else 0}')

    return "\n".join(lines) + "\n"
