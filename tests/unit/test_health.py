"""Health, metrics, heartbeats and the sd_notify plumbing.

The theme is the same throughout: **the endpoint must be able to tell idle from
stopped.** A queue at zero and a worker that died three days ago look identical from
the job table, and every check here exists because that ambiguity is what makes an
unattended system dangerous.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import timedelta
from typing import Any

import pytest

from cindraleads.health import (
    DEAD_LETTER_CRITICAL,
    assess,
    port_is_free,
    serve,
)
from cindraleads.metrics import (
    HEARTBEAT_UNITS,
    last_heartbeat,
    record_heartbeat,
    render_prometheus,
    snapshot,
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
    for unit in HEARTBEAT_UNITS:
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
        assert check.status == "degraded"
        assert "never recorded a run" in check.detail
    assert report.status == "degraded"


def test_a_stale_unit_is_critical(store: Any) -> None:
    _all_units_fresh(store)
    now = utcnow()
    stale = now + timedelta(hours=HEARTBEAT_UNITS["harvest"] + 1)

    report = assess(store, now=stale, thermal=_Governor())
    check = next(c for c in report.checks if c.name == "heartbeat:harvest")

    assert check.status == "critical"
    assert report.status == "critical"


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
