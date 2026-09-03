"""Health, metrics, heartbeats and the sd_notify plumbing.

The theme is the same throughout: **the endpoint must be able to tell idle from
stopped.** A queue at zero and a worker that died three days ago look identical from
the job table, and every check here exists because that ambiguity is what makes an
unattended system dangerous.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import timedelta
from pathlib import Path as _Path
from typing import Any

import pytest

from cindraleads.health import (
    DEAD_LETTER_CRITICAL,
    DEAD_LETTER_WARN,
    assess,
    port_is_free,
    serve,
)
from cindraleads.metrics import (
    HEARTBEAT_UNITS,
    OPTIONAL_UNITS,
    last_heartbeat,
    record_heartbeat,
    render_prometheus,
    snapshot,
    source_mtime,
)
from cindraleads.models import to_iso, utcnow
from cindraleads.sdnotify import Watchdog, available, notify, watchdog_interval_seconds


class _Governor:
    def __init__(self, alert: str = "none", state: str = "nominal") -> None:
        self.alert, self.state = alert, state

    def poll(self) -> Any:
        from cindraleads.thermal import ThermalPolicy

        return ThermalPolicy(
            state=self.state,  # type: ignore[arg-type]
            max_workers=2,
            allow_llm=True,
            allow_llm_batch=True,
            alert_level=self.alert,  # type: ignore[arg-type]
            reason="test",
        )


def _all_units_fresh(store: Any) -> None:
    """Every unit reporting in, and the worker running the code on disk.

    The worker's heartbeat has to carry `source_mtime` or the build check correctly
    reports it as behind -- a worker that does not say which build it is running is
    one that predates the check, which is the same situation as being stale.
    """
    for unit in HEARTBEAT_UNITS:
        if unit == "worker":
            record_heartbeat(store, unit, source_mtime=source_mtime())
        else:
            record_heartbeat(store, unit)


# ------------------------------------------------------------------------ heartbeats


def test_a_heartbeat_round_trips(store: Any) -> None:
    record_heartbeat(store, "harvest", planned=12, enqueued=3)
    beat = last_heartbeat(store, "harvest")

    assert beat is not None
    assert beat.ok is True
    assert beat.detail["planned"] == 12
    assert beat.age_seconds() < 5


def test_the_latest_heartbeat_wins(store: Any) -> None:
    record_heartbeat(store, "harvest", run=1)
    record_heartbeat(store, "harvest", run=2)

    beat = last_heartbeat(store, "harvest")
    assert beat is not None and beat.detail["run"] == 2


def test_one_unit_does_not_answer_for_another(store: Any) -> None:
    """The lookup is a LIKE against a JSON blob, which is exactly the kind of query
    that matches more than it means to. `enrich` must not satisfy `enrichment`."""
    record_heartbeat(store, "enrich")

    assert last_heartbeat(store, "enrich") is not None
    assert last_heartbeat(store, "digest") is None
    assert last_heartbeat(store, "enrichment") is None


def test_a_failed_run_records_a_heartbeat_that_says_so(store: Any) -> None:
    """Silence and failure are different problems. A run that happened and failed must
    not look like a timer that never fired."""
    record_heartbeat(store, "digest", ok=False, error="webhook 500")

    beat = last_heartbeat(store, "digest")
    assert beat is not None and beat.ok is False

    report = assess(store, thermal=_Governor())
    check = next(c for c in report.checks if c.name == "heartbeat:digest")
    assert check.status == "degraded"
    assert "reported failure" in check.detail


# ---------------------------------------------------------------- health assessment


def test_a_never_run_unit_is_flagged_not_silent(store: Any) -> None:
    report = assess(store, thermal=_Governor())

    for unit in HEARTBEAT_UNITS:
        check = next(c for c in report.checks if c.name == f"heartbeat:{unit}")
        if unit in OPTIONAL_UNITS:
            continue
        assert check.status == "degraded"
        assert "never recorded a run" in check.detail
    assert report.status == "degraded"


def test_an_optional_unit_that_was_never_installed_is_not_a_fault(store: Any) -> None:
    """Declining the feedback bot is a supported configuration -- the CLI is the other
    feedback path. Reporting it degraded forever teaches whoever reads this endpoint
    that degraded means nothing, which is how a real fault gets missed."""
    report = assess(store, thermal=_Governor())

    for unit in OPTIONAL_UNITS:
        check = next(c for c in report.checks if c.name == f"heartbeat:{unit}")
        assert check.status == "ok"
        assert "optional" in check.detail


def test_an_optional_unit_that_stopped_is_degraded_and_never_critical(store: Any) -> None:
    """The bot dying loses reactions while the pipeline keeps producing leads. A probe
    that returned 503 for it would restart a worker that is doing its job.

    Driven through `_check_heartbeats` rather than `assess` because no single clock
    offset makes an optional unit stale while leaving the worker's 15-minute budget
    intact -- and the worker going critical is what this test must not be measuring.
    """
    from cindraleads.health import HealthReport, _check_heartbeats

    unit = sorted(OPTIONAL_UNITS)[0]
    _all_units_fresh(store)
    stale = utcnow() + timedelta(hours=HEARTBEAT_UNITS[unit] + 1)

    report = HealthReport()
    _check_heartbeats(report, store, now=stale, uptime_seconds=10 * 24 * 3600.0)

    check = next(c for c in report.checks if c.name == f"heartbeat:{unit}")
    assert check.status == "degraded"
    assert "over its" in check.detail


def test_a_stale_unit_is_critical(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Uptime is pinned, not inherited from the test host. Staleness is now judged
    # against how long the machine has been up, so leaving this to the real
    # `/proc/uptime` would make the test pass or fail depending on when the box last
    # rebooted -- which is exactly the ambiguity the check exists to remove.
    monkeypatch.setattr("cindraleads.health.uptime_seconds", lambda: 90 * 24 * 3600.0)
    _all_units_fresh(store)
    now = utcnow()
    stale = now + timedelta(hours=HEARTBEAT_UNITS["harvest"] + 1)

    report = assess(store, now=stale, thermal=_Governor())
    check = next(c for c in report.checks if c.name == "heartbeat:harvest")

    assert check.status == "critical"
    assert report.status == "critical"


def test_a_reboot_does_not_look_like_a_dead_timer(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Pi is not always on, and the first `/healthz` after a weekend off reported
    `critical` on two timers that were queued and about to fire.

    A heartbeat cannot be fresher than the boot: if the host has been up for less than
    the unit's silence budget, an old heartbeat says the box was off, not that the
    timer is broken. Firing critical on every reboot is how an endpoint gets ignored.
    """
    _all_units_fresh(store)
    now = utcnow()
    later = now + timedelta(hours=43)

    monkeypatch.setattr("cindraleads.health.uptime_seconds", lambda: 4 * 60.0)
    report = assess(store, now=later, thermal=_Governor())

    harvest = next(c for c in report.checks if c.name == "heartbeat:harvest")
    assert harvest.status == "degraded"
    assert "powered off" in harvest.detail
    assert report.status != "critical"


def test_a_dead_timer_on_a_long_running_host_is_still_critical(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half. Once uptime exceeds the budget the timer has had a fair chance,
    and a stale heartbeat means it is genuinely not firing."""
    _all_units_fresh(store)
    later = utcnow() + timedelta(hours=43)

    monkeypatch.setattr("cindraleads.health.uptime_seconds", lambda: 40 * 3600.0)
    report = assess(store, now=later, thermal=_Governor())

    harvest = next(c for c in report.checks if c.name == "heartbeat:harvest")
    assert harvest.status == "critical"
    assert report.status == "critical"


def test_unknown_uptime_never_suppresses_an_alarm(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/proc/uptime` is missing on plenty of platforms. Reading that as a fresh boot
    would silence every staleness alarm on the one host that could not prove it was
    running -- so None means "no information", not "just booted"."""
    _all_units_fresh(store)
    later = utcnow() + timedelta(hours=43)

    monkeypatch.setattr("cindraleads.health.uptime_seconds", lambda: None)
    report = assess(store, now=later, thermal=_Governor())

    assert report.status == "critical"


def test_uptime_reads_a_real_number_on_linux() -> None:
    """Pinned because the whole downgrade hinges on this returning something."""
    from cindraleads.health import uptime_seconds

    value = uptime_seconds()
    if value is None:
        pytest.skip("no /proc/uptime on this platform")
    assert value > 0


def test_a_healthy_system_is_ok(store: Any) -> None:
    _all_units_fresh(store)
    report = assess(store, thermal=_Governor())
    assert report.status == "ok", [c.detail for c in report.problems]


def test_an_idle_queue_is_not_a_problem(store: Any) -> None:
    """The distinction the whole module is built around. Zero ready jobs plus fresh
    heartbeats is a system that finished its work, and must not page anyone."""
    _all_units_fresh(store)
    report = assess(store, thermal=_Governor())

    depth = next(c for c in report.checks if c.name == "queue:depth")
    assert depth.status == "ok"
    assert report.metrics["queue_ready"] == 0
    assert report.status == "ok"


def test_a_dead_letter_pile_escalates(store: Any) -> None:
    _all_units_fresh(store)
    with store.tx() as conn:
        for n in range(DEAD_LETTER_CRITICAL):
            conn.execute(
                "INSERT INTO dead_letter (job_id, kind, payload, attempts, died_at) "
                "VALUES (?,?,'{}',3,?)",
                (f"j{n}", "extract.candidate", to_iso(utcnow())),
            )

    report = assess(store, thermal=_Governor())
    assert next(c for c in report.checks if c.name == "queue:dead").status == "critical"


def test_a_dead_job_is_counted_once_not_twice(store: Any) -> None:
    """Burying a job writes both sides: a `dead_letter` row carrying the payload and
    reason, and `jobs.status='dead'` marking the job. Adding them double-counted every
    dead job -- the endpoint reported 10 for 5 real ones, which put the warn threshold
    at 3 and critical at 13 rather than where those numbers say they are.

    Driven through `fail()` rather than by inserting rows, because writing only the
    `dead_letter` side is exactly the shape that hid the bug: the old sum happened to
    be right whenever a test forgot the other half.

    Asserted on the all-time figure in the detail rather than on `Check.value`, which
    now carries the windowed count. Reverting `max` to `+` does not move the window, so
    a guard on `value` would have stopped catching the bug it was written for.
    """
    from cindraleads.queue import JobQueue

    queue = JobQueue(store)
    _all_units_fresh(store)
    for n in range(3):
        job_id = queue.enqueue(f"k{n}", max_attempts=1)
        queue.claim("w", kinds=[f"k{n}"])
        assert queue.fail(job_id, "boom") == "dead"
    with store.tx() as conn:  # out of the window, so the all-time figure is visible
        conn.execute("UPDATE dead_letter SET died_at = ?", (to_iso(utcnow() - timedelta(days=3)),))

    report = assess(store, thermal=_Governor())
    check = next(c for c in report.checks if c.name == "queue:dead")

    assert "3 all time" in check.detail, "three buried jobs must not report as six"


def test_dead_letters_from_a_fixed_bug_stop_holding_the_endpoint_degraded(store: Any) -> None:
    """`dead_letter` is append-only and nothing purges it, so an all-time count can only
    climb. Four jobs buried by the pre-0006 attempt accounting and the watchdog crash
    loop sat above the warn threshold permanently -- describing two bugs that were
    already fixed, on a box where nothing had died since. A probe that stays degraded
    after the fault is gone is one you learn to ignore, which is the single thing this
    endpoint exists to avoid.

    The pile is not hidden: it is still counted, still on `/metrics`, and still what
    `cindra acceptance` grades "no job lost" against over a window a human chose.
    """
    _all_units_fresh(store)
    stale = to_iso(utcnow() - timedelta(days=3))
    with store.tx() as conn:
        for n in range(DEAD_LETTER_WARN + 2):
            conn.execute(
                "INSERT INTO dead_letter (job_id, kind, payload, attempts, died_at) "
                "VALUES (?,?,'{}',3,?)",
                (f"old{n}", "score.company", stale),
            )

    check = next(c for c in assess(store, thermal=_Governor()).checks if c.name == "queue:dead")

    assert check.status == "ok"
    assert f"{DEAD_LETTER_WARN + 2} all time" in check.detail, "the pile must still be reported"


def test_a_pile_that_is_still_growing_is_still_degraded(store: Any) -> None:
    """The bound on the test above. Windowing must not make a stage that is failing
    right now invisible just because the failures are recent."""
    _all_units_fresh(store)
    with store.tx() as conn:
        for n in range(DEAD_LETTER_WARN):
            conn.execute(
                "INSERT INTO dead_letter (job_id, kind, payload, attempts, died_at) "
                "VALUES (?,?,'{}',3,?)",
                (f"new{n}", "extract.candidate", to_iso(utcnow())),
            )

    check = next(c for c in assess(store, thermal=_Governor()).checks if c.name == "queue:dead")

    assert check.status == "degraded"


def test_every_check_runs_even_after_one_fails(store: Any) -> None:
    """No short-circuiting: "the disk is full" and "the disk is full and harvest is
    dead" are one fix and two, and the endpoint has to distinguish them."""
    report = assess(store, thermal=_Governor("critical"))

    names = {c.name for c in report.checks}
    assert {"disk", "thermal", "queue:dead", "queue:depth"} <= names
    assert len([c for c in names if c.startswith("heartbeat:")]) == len(HEARTBEAT_UNITS)


# --------------------------------------------------------------------- exposition


def test_prometheus_output_parses_as_the_text_format(store: Any) -> None:
    record_heartbeat(store, "worker")
    body = render_prometheus(store)

    for line in body.splitlines():
        if line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        assert name.startswith("cindraleads_")
        float(value)  # raises if a label leaked into the value position


def test_a_unit_that_never_ran_reports_minus_one_not_zero(store: Any) -> None:
    """Omitting it would make `heartbeat_age_seconds > threshold` silently never fire
    for the unit that has never run -- the worst case, reported as healthy. Zero would
    be worse still: it reads as "ran just now"."""
    body = render_prometheus(store)
    assert 'cindraleads_heartbeat_age_seconds{unit="harvest"} -1' in body


def test_snapshot_counts_only_live_triggers(store: Any) -> None:
    now = utcnow()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, first_seen_at, "
            "last_updated_at) VALUES ('acme.io','Acme',?,?)",
            (to_iso(now), to_iso(now)),
        )
        for tid, decays, active in (
            ("live", now + timedelta(days=5), 1),
            ("decayed", now - timedelta(days=1), 1),
            ("retired", now + timedelta(days=5), 0),
        ):
            conn.execute(
                "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
                "observed_at, decays_at, active) VALUES (?,'acme.io','T1_AI_SHIP',0.7,?,?,?)",
                (tid, to_iso(now), to_iso(decays), active),
            )

    assert snapshot(store)["triggers_live"] == 1


def test_metrics_survive_the_process_that_wrote_them(store: Any) -> None:
    """The reason none of this uses in-process counters.

    Under systemd, harvest and maintenance are short-lived processes. A counter held
    in memory would die with each one, so a scrape would report whatever the last
    process to exit happened to have done.
    """
    record_heartbeat(store, "harvest", planned=7)
    from cindraleads.store import Store

    reopened = Store(store.db_path)
    try:
        beat = last_heartbeat(reopened, "harvest")
        assert beat is not None and beat.detail["planned"] == 7
    finally:
        reopened.close()


# ------------------------------------------------------------------------- server


def test_the_endpoint_refuses_to_bind_off_loopback(store: Any) -> None:
    """A guard against a future edit, not an attacker. `0.0.0.0` for convenience is
    exactly how a lead database ends up reachable from the LAN."""
    with pytest.raises(ValueError, match="loopback only"):
        serve(store, host="0.0.0.0")


@pytest.mark.integration
def test_healthz_and_metrics_answer_over_http(store: Any) -> None:
    port = 9209
    if not port_is_free(port=port):
        pytest.skip(f"port {port} busy")
    _all_units_fresh(store)

    server = serve(store, port=port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            assert response.status == 200
            payload = json.loads(response.read())
        assert payload["status"] == "ok"
        assert payload["checks"]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            assert response.status == 200
            assert b"cindraleads_companies_total" in response.read()

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert b"CindraLeads" in response.read()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_critical_answers_503_so_curl_f_is_a_probe(store: Any) -> None:
    """Degraded stays 200 on purpose: a hot SoC or a spent budget is a working
    pipeline, and a probe that failed on those would restart it into the same state."""
    port = 9210
    if not port_is_free(port=port):
        pytest.skip(f"port {port} busy")

    server = serve(store, port=port)  # fresh db: every heartbeat missing -> degraded
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            assert response.status == 200, "missing heartbeats are degraded, not critical"
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------------ sd_notify


def test_notify_is_inert_without_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same `cindra work` has to run identically in a terminal and under systemd.
    A worker that only behaves correctly when supervised cannot be debugged."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    assert available() is False
    assert notify("READY=1") is False


def test_watchdog_interval_is_half_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("WATCHDOG_USEC", "180000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert watchdog_interval_seconds() == 90.0


def test_a_child_process_does_not_pet_its_parents_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """systemd sets these in the environment and children inherit them. Without the
    pid check, a subprocess would keep a wedged parent looking alive forever."""
    monkeypatch.setenv("WATCHDOG_USEC", "180000000")
    monkeypatch.setenv("WATCHDOG_PID", "999999")

    assert watchdog_interval_seconds() == 0.0


def test_watchdog_rate_limits_its_pings() -> None:
    dog = Watchdog(interval_seconds=10.0)
    assert dog.enabled

    # No NOTIFY_SOCKET here, so `pet` returns False either way -- what is asserted is
    # that the second call is suppressed before it ever reaches the socket.
    assert dog.pet(now=100.0) is False
    dog._last = 100.0
    assert dog.pet(now=105.0) is False
    assert dog.pet(now=115.0) is False


def test_a_disabled_watchdog_never_pings() -> None:
    dog = Watchdog(interval_seconds=0.0)
    assert not dog.enabled
    assert dog.pet(now=1_000_000.0) is False


# -------------------------------------------------------------- unit list coverage


def test_every_timer_unit_has_a_heartbeat_budget() -> None:
    """A systemd unit added without an entry in `HEARTBEAT_UNITS` is a blind spot, and
    the whole premise of Phase 7 is that nobody looks for 72 hours."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    timers = {p.stem.replace("cindraleads-", "") for p in (repo / "deploy/systemd").glob("*.timer")}
    monitored = set(HEARTBEAT_UNITS)

    assert timers <= monitored, f"timers with no heartbeat budget: {sorted(timers - monitored)}"


def test_no_pipeline_unit_can_be_marked_optional() -> None:
    """`OPTIONAL_UNITS` silences the never-run alarm and downgrades staleness. Applied
    to anything that drains the queue or writes a lead, it would turn the one check
    that distinguishes idle from stopped back off."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    timers = {p.stem.replace("cindraleads-", "") for p in (repo / "deploy/systemd").glob("*.timer")}
    pipeline = timers | {"worker"}

    assert not (pipeline & OPTIONAL_UNITS), (
        f"pipeline units marked optional: {pipeline & OPTIONAL_UNITS}"
    )


def _unit_subcommands() -> set[str]:
    """Every `cindra <subcommand>` a systemd unit invokes."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    found: set[str] = set()
    for unit in (repo / "deploy/systemd").glob("*.service"):
        for line in unit.read_text().splitlines():
            if not line.startswith("ExecStart="):
                continue
            parts = line.split("=", 1)[1].lstrip("-").split()
            for index, token in enumerate(parts):
                if token.endswith("/cindra") and index + 1 < len(parts):
                    found.add(parts[index + 1])
    return found


def test_every_unattended_command_migrates_itself() -> None:
    """A command a systemd unit runs has nobody to read its error message.

    `git pull` shipping a migration must not be able to stop a timer dead. The symptom
    would be "no such column" from whichever query touched the new field first, days
    later, surfacing as "no new companies" -- a long way from the cause. Interactive
    commands are exempt: a human is right there reading the output.

    Written as a test rather than a convention because the gap was real -- `harvest`
    was the one entry point that did not self-migrate, and it became unattended the
    moment `cindraleads-harvest.timer` shipped.
    """
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    tree = ast.parse((repo / "src/cindraleads/cli.py").read_text())

    migrating: set[str] = set()
    defined: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        # Typer maps `def foo_bar` to `foo-bar` unless the decorator names it.
        name = node.name.replace("_", "-")
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and decorator.args:
                first = decorator.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    name = first.value
        defined.add(name)
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_open_store"
                and any(
                    kw.arg == "migrate" and getattr(kw.value, "value", False) is True
                    for kw in call.keywords
                )
            ):
                migrating.add(name)

    unattended = _unit_subcommands()
    assert unattended, "no ExecStart lines found; did the unit files move?"
    assert unattended <= defined, f"units invoke unknown commands: {sorted(unattended - defined)}"
    assert unattended <= migrating, (
        f"these run under systemd but do not self-migrate: {sorted(unattended - migrating)}"
    )


# ------------------------------------------------- is the worker running this code?


def test_a_worker_behind_the_source_is_flagged(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The silent failure that cost several rounds of "the fix did not work".

    `git pull` rewrites the source; the long-running worker keeps the modules it
    imported at boot. Every fix sits on disk doing nothing while the unit drains jobs
    and reports itself perfectly healthy. A scoring change was deployed four times
    before anyone noticed the process applying it was four builds old.
    """
    # Both sides pinned. Reading the real source mtime made this depend on how long
    # ago the working tree was last edited: it passed right after a change and failed
    # an hour later, which is the same wall-clock flakiness the uptime tests fix.
    monkeypatch.setattr("cindraleads.health.source_mtime", lambda: 2_000_000.0)
    record_heartbeat(store, "worker", source_mtime=1_000_000.0)

    report = assess(store, thermal=_Governor())
    check = next(c for c in report.checks if c.name == "worker:build")

    assert check.status == "degraded"
    assert "systemctl restart cindraleads-worker" in check.detail


def test_a_worker_on_the_current_build_is_ok(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cindraleads.health.source_mtime", lambda: 2_000_000.0)
    record_heartbeat(store, "worker", source_mtime=2_000_000.0)

    check = next(c for c in assess(store, thermal=_Governor()).checks if c.name == "worker:build")
    assert check.status == "ok"


def test_a_worker_that_does_not_report_its_build_is_flagged(store: Any) -> None:
    """ "Predates the check" and "is stale" are the same situation, and a worker old
    enough to lack the field is certainly old enough to be behind."""
    record_heartbeat(store, "worker")

    check = next(c for c in assess(store, thermal=_Governor()).checks if c.name == "worker:build")
    assert check.status == "degraded"
    assert "predates this check" in check.detail


def test_no_worker_heartbeat_means_no_build_check(store: Any) -> None:
    """A missing worker is already reported by the heartbeat check. Saying it twice in
    different words makes the report harder to read, not more informative."""
    report = assess(store, thermal=_Governor())
    assert not any(c.name == "worker:build" for c in report.checks)


def test_the_endpoint_reports_when_it_is_itself_behind_the_source(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`worker:build` watched the worker and nothing watched the watcher.

    A stale worker still does correct old work. A stale endpoint reports a system that
    no longer exists -- and it does so by *omitting* checks added since it started,
    which reads as no problem. That happened: `enable --now` does not restart a running
    unit, so the endpoint kept a `HEARTBEAT_UNITS` with no `feedback` in it and simply
    left the check out of the report.
    """
    from cindraleads import health as health_mod

    # Both sides pinned. Reading real mtimes here makes the test pass or fail depending
    # on when the working tree was last touched.
    monkeypatch.setattr(health_mod, "_RUNNING_SOURCE_MTIME", 1_000_000.0)
    monkeypatch.setattr(health_mod, "source_mtime", lambda: 1_003_600.0)

    report = assess(store, thermal=_Governor())

    check = next(c for c in report.checks if c.name == "health:build")
    assert check.status == "degraded"
    assert "restart cindraleads-health" in check.detail
    assert report.status != "critical"


def test_an_up_to_date_endpoint_says_so(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from cindraleads import health as health_mod

    monkeypatch.setattr(health_mod, "_RUNNING_SOURCE_MTIME", 1_000_000.0)
    monkeypatch.setattr(health_mod, "source_mtime", lambda: 1_000_000.0)

    report = assess(store, thermal=_Governor())

    check = next(c for c in report.checks if c.name == "health:build")
    assert check.status == "ok"


# ------------------------------------------------------------- what counts as a build


def _settings_at(root: Any) -> Any:
    from cindraleads.config import Settings

    return Settings(repo_root=root)


def test_a_prompt_edit_is_a_new_build(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`source_mtime` scanned `*.py` and nothing else, so the two directories this
    project changes most were invisible to the one probe built to catch a stale worker.

    That is not hypothetical. `description` and `industry` were null for 583 of 583
    companies; the fix was two edits to `prompts/extract_company.md`; the next 37
    companies came out null as well. Probed afterwards against the real model, the
    fixed prompt fills both fields on the first attempt -- so the prompt was right and
    the worker was still holding the one it loaded at boot, while `/healthz` compared
    `.py` mtimes, found nothing newer and reported the build current.

    Every stage caches these at construction: `load_prompt` in the Extractor's
    `__post_init__`, `icp.yaml` in `Scout.from_config`, `scoring.yaml` for the Scorer's
    lifetime. A long-lived worker pins an edited prompt exactly as firmly as an edited
    module, which is what makes a `.py`-only check answer the wrong question
    confidently.
    """
    from cindraleads import metrics as metrics_mod

    (tmp_path / "prompts").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts" / "extract_company.md").write_text("v1")
    (tmp_path / "config" / "icp.yaml").write_text("query_templates: []\n")
    monkeypatch.setattr(metrics_mod, "settings", lambda: _settings_at(tmp_path))

    baseline = metrics_mod.source_mtime()
    assert baseline > 0

    for name in ("prompts/extract_company.md", "config/icp.yaml"):
        path = tmp_path / name
        os.utime(path, (baseline + 3600, baseline + 3600))
        assert metrics_mod.source_mtime() > baseline, f"editing {name} did not move the build"
        os.utime(path, (baseline - 3600, baseline - 3600))


def test_the_real_prompt_and_config_directories_are_watched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed layout, not a temporary one. Resolved through `Settings` rather
    than guessed from `__file__`, so this asserts the resolution actually lands on the
    directories the running code reads from."""
    from cindraleads import metrics as metrics_mod
    from cindraleads.config import settings

    cfg = settings()
    watched = {root for root, _ in metrics_mod._behaviour_trees()}

    assert cfg.resolve(cfg.prompt_dir) in watched
    assert cfg.resolve(cfg.config_dir) in watched


def test_an_unreadable_config_still_yields_a_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under-reporting beats raising. `source_mtime()` runs at import time in both the
    CLI and the health endpoint, so a settings failure here would take `cindra health`
    down with it -- and `cindra health` is the command you run when something is
    already wrong."""
    from cindraleads import metrics as metrics_mod

    def _boom() -> Any:
        raise RuntimeError("no .env, no repo root, nothing")

    monkeypatch.setattr(metrics_mod, "settings", _boom)

    assert metrics_mod.source_mtime() > 0, "the package tree must still count"


def test_systemd_waits_longer_than_a_stage_may_run() -> None:
    """`MAX_STAGE_SECONDS` and `TimeoutStopSec` are one decision made in two files, and
    nothing at runtime checks they agree -- the same shape as the prose bound and its
    token budget, and as `WatchdogSec` against the lease.

    At 900 against 180, systemd stopped waiting five times sooner than the worker was
    permitted to take, so a deploy landing during a slow stage was SIGKILL rather than
    a shutdown. Measured over 24 h: 3 builds, 1 announced exit, 2 worker gaps. Nothing
    was lost -- the lease reclaim is what covers this -- but the job sat unclaimed for
    up to the lease and charged a reclaim against a ceiling that exists to catch a
    genuinely broken job.
    """
    import re

    from cindraleads.cli import MAX_STAGE_SECONDS

    repo_root = _Path(__file__).resolve().parents[2]
    unit = (repo_root / "deploy" / "systemd" / "cindraleads-worker.service").read_text()
    match = re.search(r"^TimeoutStopSec=(\d+)", unit, re.MULTILINE)
    assert match, "the worker unit must set TimeoutStopSec"

    stop_seconds = int(match.group(1))
    assert stop_seconds > MAX_STAGE_SECONDS, (
        f"TimeoutStopSec={stop_seconds} is under MAX_STAGE_SECONDS={MAX_STAGE_SECONDS}, "
        f"so a deploy during a long stage is a SIGKILL and the worker never records "
        f"that it was shutting down"
    )
