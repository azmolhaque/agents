"""What to look at when nobody has looked for three days.

`/healthz` answers one question -- is this system still doing its job -- and it has to
answer it without a human present to interpret nuance. So every check returns one of
three states and says why in a sentence you could act on at 3am.

The distinction that matters most here is **stalled versus idle**. A queue with zero
ready jobs is the normal state of a healthy system that has finished its work; it is
also the state of a system whose harvest timer has not fired since Tuesday. The queue
depth cannot tell those apart, which is why the heartbeat checks exist: idle plus a
recent harvest heartbeat is healthy, idle plus a three-day-old one is not.

Everything binds to 127.0.0.1. The Pi has no business exposing this, and the passive
-only promise is about what we send outward -- but a lead database reachable from the
LAN would be its own kind of embarrassment.

Degraded is not failure. Ollama being down, the SerpAPI budget being spent and the SoC
being hot are all states the pipeline is designed to survive: it keeps the queue, stops
the part it cannot do, and resumes. Reporting those as critical would train whoever is
reading to ignore the endpoint, so they are `degraded` and only a stuck queue, a dead
letter pile or a silent unit is `critical`.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

from cindraleads.config import Settings, settings
from cindraleads.logging import get_logger
from cindraleads.metrics import (
    DEAD_LETTER_WINDOW,
    HEARTBEAT_UNITS,
    OPTIONAL_UNITS,
    heartbeats,
    render_prometheus,
    snapshot,
    source_mtime,
)
from cindraleads.models import utcnow
from cindraleads.store import Store

__all__ = [
    "Check",
    "HealthReport",
    "assess",
    "serve",
]

log = get_logger("cindraleads.health")

Status = Literal["ok", "degraded", "critical"]

DEFAULT_PORT = 9109
DEFAULT_HOST = "127.0.0.1"

# Free space below which the pipeline cannot be trusted to keep its promises. SQLite
# under WAL needs room for the journal, and a failed COMMIT mid-stage is the one thing
# the exactly-once design cannot paper over.
DISK_WARN_MB = 2048.0
DISK_CRITICAL_MB = 512.0

# A dead-letter pile is not an outage, but it is unattended work. One is noise; a dozen
# means a stage is systematically failing and nobody has noticed. Counted over
# `DEAD_LETTER_WINDOW` rather than all time -- see `_check_queue`.
DEAD_LETTER_WARN = 5
DEAD_LETTER_CRITICAL = 25
DEAD_LETTER_WINDOW_HOURS = int(DEAD_LETTER_WINDOW.total_seconds() // 3600)

_RANK: dict[Status, int] = {"ok": 0, "degraded": 1, "critical": 2}

# Read once, at import, so it describes the code *this process* loaded rather than
# whatever is on disk by the time a request arrives. Same reason the worker stamps its
# own on the heartbeat: a value read per-request would always equal the disk and the
# check would be a tautology that passes forever.
_RUNNING_SOURCE_MTIME = source_mtime()


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    value: float | None = None


@dataclass
class HealthReport:
    status: Status = "ok"
    checked_at: str = ""
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def add(self, check: Check) -> None:
        self.checks.append(check)
        if _RANK[check.status] > _RANK[self.status]:
            self.status = check.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "checks": [asdict(check) for check in self.checks],
            "metrics": self.metrics,
        }

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.status != "ok"]


def assess(
    store: Store,
    *,
    config: Settings | None = None,
    now: datetime | None = None,
    thermal: Any = None,
    disk_path: Path | None = None,
) -> HealthReport:
    """Every check, every time. No short-circuiting.

    A first failing check must not hide the four behind it: at 3am the difference
    between "the disk is full" and "the disk is full *and* the queue is stuck" is the
    difference between one fix and two.
    """
    cfg = config or settings()
    at = now or utcnow()
    report = HealthReport(checked_at=at.isoformat())
    report.metrics = snapshot(store, now=at)

    _check_heartbeats(report, store, now=at, uptime_seconds=uptime_seconds())
    _check_worker_build(report, store)
    _check_own_build(report)
    _check_queue(report)
    _check_disk(report, disk_path or cfg.resolve(cfg.db_path).parent)
    _check_thermal(report, thermal)

    return report


def uptime_seconds() -> float | None:
    """Seconds since boot, or None where that is not knowable.

    None on anything without `/proc/uptime`, which the caller must treat as "no
    information" rather than as zero -- reading a missing file as a fresh boot would
    suppress every staleness alarm on the one platform that could not prove otherwise.
    """
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _check_heartbeats(
    report: HealthReport, store: Store, *, now: datetime, uptime_seconds: float | None = None
) -> None:
    for unit, max_silence_hours in HEARTBEAT_UNITS.items():
        beat = heartbeats(store).get(unit)
        if beat is None:
            # Never run. On a fresh install that is expected, which is why it is
            # degraded rather than critical -- but it must not be silent, because it
            # is also what a timer that was never enabled looks like.
            #
            # An optional unit is the exception: declining the feedback bot is a
            # supported configuration, and reporting it degraded forever would teach
            # whoever reads this endpoint that degraded means nothing.
            if unit in OPTIONAL_UNITS:
                report.add(
                    Check(f"heartbeat:{unit}", "ok", f"{unit} is optional and not installed")
                )
                continue
            report.add(
                Check(
                    f"heartbeat:{unit}",
                    "degraded",
                    f"{unit} has never recorded a run; is its timer enabled?",
                )
            )
            continue

        age_hours = beat.age_seconds(now=now) / 3600
        # A heartbeat cannot be fresher than the boot. If the machine has been up for
        # less than this unit's silence budget, an old heartbeat says the box was off,
        # not that the timer is broken -- and the two need completely different
        # responses. Reporting critical here would fire on every single reboot and
        # train whoever is reading to ignore the endpoint.
        #
        # The window is the unit's own budget rather than a fixed grace period, so it
        # closes exactly when the timer has had a fair chance to fire: once uptime
        # exceeds the budget and the heartbeat is still old, the timer really is dead.
        booted_recently = uptime_seconds is not None and uptime_seconds < max_silence_hours * 3600
        if age_hours > max_silence_hours and booted_recently:
            up_hours = (uptime_seconds or 0) / 3600
            report.add(
                Check(
                    f"heartbeat:{unit}",
                    "degraded",
                    f"{unit} last ran {age_hours:.1f}h ago, but the host has only been up "
                    f"{up_hours:.1f}h -- it was powered off, and the timer has not "
                    f"fired yet",
                    value=age_hours,
                )
            )
        elif age_hours > max_silence_hours:
            # An optional unit going silent is worth saying and not worth a 503: the
            # feedback bot dying loses reactions while the pipeline keeps producing
            # leads, and a probe that failed on it would restart a healthy worker.
            report.add(
                Check(
                    f"heartbeat:{unit}",
                    "degraded" if unit in OPTIONAL_UNITS else "critical",
                    f"{unit} last ran {age_hours:.1f}h ago, over its {max_silence_hours}h limit",
                    value=age_hours,
                )
            )
        elif not beat.ok:
            report.add(
                Check(
                    f"heartbeat:{unit}",
                    "degraded",
                    f"{unit} ran {age_hours:.1f}h ago and reported failure",
                    value=age_hours,
                )
            )
        else:
            report.add(
                Check(f"heartbeat:{unit}", "ok", f"ran {age_hours:.1f}h ago", value=age_hours)
            )


def _check_worker_build(report: HealthReport, store: Store) -> None:
    """Is the worker running the code that is on disk?

    The failure this catches is completely silent. `git pull` rewrites the source; the
    long-running worker keeps the modules it imported at boot, so every fix sits on
    disk doing nothing while the unit drains jobs and reports itself healthy. It cost
    several rounds of "the fix did not work" before the cause was visible: a scoring
    change had been deployed four times and the process applying it was four builds old.

    Degraded, not critical: the worker is doing real work, just not the newest version
    of it. The remedy is one line and belongs in the detail.
    """
    beat = heartbeats(store).get("worker")
    if beat is None:
        return  # the heartbeat check already reports a missing worker

    running = beat.detail.get("source_mtime")
    if not isinstance(running, int | float) or not running:
        # An older worker that predates this field. Saying so is better than staying
        # quiet, because "predates the field" and "is stale" are the same situation.
        report.add(
            Check(
                "worker:build",
                "degraded",
                "worker does not report its build; it predates this check and is "
                "certainly behind. Restart: systemctl restart cindraleads-worker",
            )
        )
        return

    on_disk = source_mtime()
    if on_disk > running + 1.0:  # a second of slack for filesystem timestamp jitter
        behind = timedelta(seconds=int(on_disk - running))
        report.add(
            Check(
                "worker:build",
                "degraded",
                f"source on disk is {behind} newer than the running worker; it cannot "
                f"see those changes. Restart: systemctl restart cindraleads-worker",
                value=on_disk - running,
            )
        )
    else:
        report.add(Check("worker:build", "ok", "worker is running the code on disk"))


def _check_own_build(report: HealthReport) -> None:
    """Is the *reporter* running the code on disk?

    `worker:build` watched the worker and nothing watched the watcher, which is the
    worse of the two failures: a stale worker still does correct old work, while a
    stale health endpoint reports a system that no longer exists. It happened the day
    the feedback bot shipped -- `enable --now` does not restart an already-running
    unit, so the endpoint kept a `HEARTBEAT_UNITS` that had never heard of `feedback`
    and simply omitted the check. **A missing check reads as no problem**, which is the
    one failure mode this file exists to prevent.

    Degraded, never critical: the endpoint answering with old rules is still answering,
    and a probe that 503'd here would restart the process a human is mid-way through
    reading.
    """
    on_disk = source_mtime()
    if on_disk > _RUNNING_SOURCE_MTIME + 1.0:  # a second of slack for timestamp jitter
        behind = timedelta(seconds=int(on_disk - _RUNNING_SOURCE_MTIME))
        report.add(
            Check(
                "health:build",
                "degraded",
                f"source on disk is {behind} newer than this endpoint; checks added "
                f"since then are missing from this report entirely. Restart: "
                f"systemctl restart cindraleads-health",
                value=on_disk - _RUNNING_SOURCE_MTIME,
            )
        )
    else:
        report.add(Check("health:build", "ok", "endpoint is running the code on disk"))


def _check_queue(report: HealthReport) -> None:
    metrics = report.metrics
    # `max`, not `+`. Burying a job writes both sides -- a `dead_letter` row carrying
    # the payload and reason, and `jobs.status='dead'` marking the job -- so adding them
    # counted every dead job twice and the endpoint reported 10 for 5. That put the warn
    # threshold at 3 real jobs and critical at 13, which is not what those numbers say.
    #
    # `max` rather than picking one, because a divergence between the two is itself a
    # fault worth surfacing rather than hiding behind whichever side we happened to read.
    dead = int(max(metrics.get("queue_dead", 0), metrics.get("dead_letter_total", 0)))
    # Graded on the window, reported with the total. `dead_letter` is append-only and
    # nothing purges it, so an all-time count can only ever climb -- four jobs buried by
    # the pre-0006 attempt accounting and the watchdog crash loop sat above the warn
    # threshold permanently, describing two bugs that had already been fixed. A probe
    # that stays degraded after the fault is gone is one you learn to ignore, which is
    # the failure this endpoint exists to avoid. The total still appears in the message
    # and on `/metrics`, because lost work does not stop mattering after a day -- it is
    # `cindra acceptance` that grades it, over a window a human chose.
    recent = int(metrics.get("dead_letter_recent", 0))
    if recent >= DEAD_LETTER_CRITICAL:
        status: Status = "critical"
    elif recent >= DEAD_LETTER_WARN:
        status = "degraded"
    else:
        status = "ok"
    detail = f"{recent} job(s) past retry in {DEAD_LETTER_WINDOW_HOURS}h"
    if dead > recent:
        detail += f" ({dead} all time)"
    report.add(Check("queue:dead", status, detail, value=float(recent)))

    ready = int(metrics.get("queue_ready", 0))
    deferred = int(metrics.get("queue_deferred", 0))
    in_flight = int(metrics.get("queue_in_flight", 0))
    # Deliberately not an alarm on depth. A backlog on a Pi that does ~64 s per
    # extraction is the normal shape of a good harvest, not a fault; the heartbeat
    # checks are what catch a queue that is deep *and* not moving.
    report.add(
        Check(
            "queue:depth",
            "ok",
            f"{ready} ready, {deferred} deferred, {in_flight} in flight",
            value=float(ready),
        )
    )


def _check_disk(report: HealthReport, path: Path) -> None:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        report.add(Check("disk", "degraded", f"could not stat {path}: {exc}"))
        return

    free_mb = usage.free / (1024 * 1024)
    if free_mb < DISK_CRITICAL_MB:
        status: Status = "critical"
        detail = f"{free_mb:.0f} MB free: SQLite cannot be trusted to COMMIT"
    elif free_mb < DISK_WARN_MB:
        status = "degraded"
        detail = f"{free_mb:.0f} MB free: run the cache sweep"
    else:
        status = "ok"
        detail = f"{free_mb:.0f} MB free"
    report.add(Check("disk", status, detail, value=free_mb))


def _check_thermal(report: HealthReport, thermal: Any) -> None:
    governor = thermal
    if governor is None:
        from cindraleads.thermal import ThermalGovernor

        governor = ThermalGovernor()

    try:
        policy = governor.poll()
    except Exception as exc:  # a sensor read must never take the endpoint down
        report.add(Check("thermal", "degraded", f"governor unreadable: {type(exc).__name__}"))
        return

    # The governor's own alert levels, mapped straight across. Heat is `degraded`, not
    # `critical`: the pipeline is designed to pause inference and keep fetching, and a
    # probe that failed on a warm afternoon would restart the worker into the same heat.
    by_level: dict[str, Status] = {"none": "ok", "warning": "degraded", "critical": "critical"}
    status = by_level.get(policy.alert_level, "degraded")
    report.add(Check("thermal", status, f"{policy.state}: {policy.reason}"))


# ----------------------------------------------------------------------------- server


def _handler(store: Store, config: Settings) -> type[BaseHTTPRequestHandler]:
    @contextmanager
    def reader() -> Iterator[Store]:
        """A connection belonging to the thread that is about to use it.

        `ThreadingHTTPServer` serves each request on a new thread, and a sqlite3
        connection may only be used from the thread that opened it -- so handing the
        long-lived store to a handler makes every single request fail with
        "SQLite objects created in a thread can only be used in that same thread".

        Opening one per request rather than passing `check_same_thread=False`: these
        are read-only queries a few times a minute, so the open costs nothing, and
        sharing one connection across arbitrary threads would put the scrape path in
        contention with the worker's writes for no benefit.
        """
        own = Store(store.db_path, config=config)
        try:
            yield own
        finally:
            own.close()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, code: int, body: str, content_type: str) -> None:
            payload = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            route = self.path.split("?")[0].rstrip("/") or "/"
            try:
                if route == "/healthz":
                    with reader() as db:
                        report = assess(db, config=config)
                    # 503 on critical so a plain `curl -f` is a usable probe. Degraded
                    # stays 200: the pipeline is working, just not at full capability,
                    # and a restart loop triggered by a hot SoC would make it worse.
                    code = 503 if report.status == "critical" else 200
                    self._respond(code, json.dumps(report.to_dict(), indent=2), "application/json")
                elif route == "/metrics":
                    with reader() as db:
                        body = render_prometheus(db)
                    self._respond(200, body, "text/plain; version=0.0.4")
                elif route == "/":
                    with reader() as db:
                        page = _dashboard(db, config)
                    self._respond(200, page, "text/html; charset=utf-8")
                else:
                    self._respond(404, "not found\n", "text/plain")
            except Exception as exc:  # never let a scrape kill the server
                log.error("health_request_failed", route=route, error=str(exc))
                self._respond(500, f"{type(exc).__name__}\n", "text/plain")

        def log_message(self, fmt: str, *args: Any) -> None:
            # BaseHTTPRequestHandler writes to stderr, which under systemd means every
            # scrape lands in the journal. Route it to structlog at debug instead.
            log.debug("health_request", request=fmt % args)

    return Handler


def _dashboard(store: Store, config: Settings) -> str:
    """The read-only HTML view, on the endpoint that already exists.

    PLAN.md 20 ruled out a separate dashboard app. This is a table rendered from the
    same `assess()` the JSON uses, so it cannot drift from the machine-readable answer.
    """
    report = assess(store, config=config)
    colour = {"ok": "#2e7d32", "degraded": "#ef6c00", "critical": "#c62828"}
    rows = "".join(
        f'<tr><td>{c.name}</td><td style="color:{colour[c.status]}">{c.status}</td>'
        f"<td>{c.detail}</td></tr>"
        for c in report.checks
    )
    metrics = "".join(
        f"<tr><td>{name}</td><td>{value:g}</td></tr>"
        for name, value in sorted(report.metrics.items())
    )
    return (
        "<!doctype html><meta charset=utf-8><title>CindraLeads</title>"
        "<style>body{font:14px/1.5 system-ui;margin:2rem;max-width:60rem}"
        "table{border-collapse:collapse;width:100%;margin-bottom:2rem}"
        "td,th{border-bottom:1px solid #ddd;padding:.4rem .6rem;text-align:left}"
        "h1{font-size:1.2rem}</style>"
        f'<h1>CindraLeads &mdash; <span style="color:{colour[report.status]}">'
        f"{report.status}</span></h1>"
        f"<p>{report.checked_at}</p>"
        f"<table><tr><th>check</th><th>status</th><th>detail</th></tr>{rows}</table>"
        f"<table><tr><th>metric</th><th>value</th></tr>{metrics}</table>"
    )


def serve(
    store: Store,
    *,
    config: Settings | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Start the endpoint on a daemon thread and return the server.

    Refuses to bind anywhere but loopback. This is a guard against a future edit
    passing `0.0.0.0` for convenience rather than against an attacker -- but that edit
    is exactly how a lead database ends up on someone's LAN.
    """
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError(f"health endpoint binds loopback only, got {host!r}")

    cfg = config or settings()
    server = ThreadingHTTPServer((host, port), _handler(store, cfg))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    log.info("health_listening", host=host, port=port)
    return server


def port_is_free(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True
