"""The re-specified Phase 7 gate.

The gate it replaces -- `get_throttled` at `0x0` for 72 h -- was an assertion about a
heatsink, and it was already false twenty minutes after a cold boot on the hardware we
have. The risk in replacing an unreachable gate is putting an unfalsifiable one in its
place, so most of these tests are about whether each criterion can actually *fail*.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from cindraleads.acceptance import (
    HEARTBEAT_GAP_SECONDS,
    LEADS_PER_DAY_TARGET,
    assess_run,
    render_markdown,
)
from cindraleads.metrics import HEARTBEAT_METRIC, HEARTBEAT_UNITS, OPTIONAL_UNITS
from cindraleads.models import to_iso, utcnow


def _beat(store: Any, unit: str, *, ago_minutes: float, **detail: Any) -> None:
    """A heartbeat at a chosen point in the past.

    Written straight to `metrics` because `record_heartbeat` always stamps *now*, and
    every question here is about a window rather than an instant. Compact separators:
    the lookup is a LIKE against this blob and a space after the colon never matches.
    """
    at = to_iso(utcnow() - timedelta(minutes=ago_minutes))
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO metrics (name, value, labels, recorded_at) VALUES (?,?,?,?)",
            (
                HEARTBEAT_METRIC,
                1.0,
                json.dumps({"unit": unit, "ok": True, **detail}, separators=(",", ":")),
                at,
            ),
        )


def _healthy_run(store: Any, *, hours: float = 72.0, temp_c: float = 60.0) -> None:
    """A worker reporting every minute for the window, plus every timer alive."""
    for minute in range(int(hours * 60), 0, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="nominal",
            temp_c=temp_c,
            throttled_now=False,
        )
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)


def _dispatch(store: Any, n: int, *, tier: str = "B", ago_hours: float = 1.0) -> None:
    at = to_iso(utcnow() - timedelta(hours=ago_hours))
    with store.tx() as conn:
        for i in range(n):
            lead = f"lead-{tier}-{ago_hours}-{i}"
            conn.execute(
                "INSERT INTO dispatch_log (dispatch_id, lead_id, channel, tier, score, "
                "idempotency_key, dispatched_at) VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, lead, "warm", tier, 60, uuid.uuid4().hex, at),
            )


# ------------------------------------------------------------------ throughput


def test_a_run_that_produced_enough_leads_passes_throughput(store: Any) -> None:
    _healthy_run(store)
    for day in range(3):
        _dispatch(store, LEADS_PER_DAY_TARGET + 2, ago_hours=6 + day * 24)

    report = assess_run(store, hours=72)

    assert report.leads_per_day >= LEADS_PER_DAY_TARGET
    assert report.criteria["throughput"] is True


def test_tier_c_does_not_count_toward_the_target(store: Any) -> None:
    """The gate is A+B per day. Counting the digest backlog would let a run pass on
    volume the target was never about."""
    _healthy_run(store)
    _dispatch(store, 200, tier="C", ago_hours=6)

    report = assess_run(store, hours=72)

    assert report.dispatched_ab == 0
    assert report.criteria["throughput"] is False


# ---------------------------------------------------------------- job integrity


def test_a_dead_lettered_job_fails_the_run(store: Any) -> None:
    """The one unrecoverable failure in the system is a unit of work that disappears."""
    _healthy_run(store)
    _dispatch(store, 60, ago_hours=6)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO dead_letter (job_id, kind, payload, attempts, last_error, died_at) "
            "VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, "extract.candidate", "{}", 5, "boom", to_iso(utcnow())),
        )

    report = assess_run(store, hours=72)

    assert report.dead_lettered == 1
    assert report.criteria["no_job_lost"] is False
    assert report.passed is False


def test_a_dead_letter_from_before_the_window_is_not_charged_to_this_run(store: Any) -> None:
    _healthy_run(store)
    _dispatch(store, 60, ago_hours=6)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO dead_letter (job_id, kind, payload, attempts, last_error, died_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                "extract.candidate",
                "{}",
                5,
                "old",
                to_iso(utcnow() - timedelta(days=9)),
            ),
        )

    assert assess_run(store, hours=72).criteria["no_job_lost"] is True


# -------------------------------------------------------------------- liveness


def test_a_gap_in_the_worker_record_is_caught(store: Any) -> None:
    """A worker dead for six hours and then back leaves a queue that looks exactly like
    one that was merely idle. The gap is the only evidence."""
    for minute in list(range(4320, 2000, -1)) + list(range(1500, 0, -1)):
        _beat(store, "worker", ago_minutes=minute, source_mtime=1_000.0)
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)

    report = assess_run(store, hours=72)

    assert report.worker_gaps
    assert report.criteria["no_silent_unit"] is False


def test_a_timer_that_never_ran_in_the_window_is_named(store: Any) -> None:
    _healthy_run(store)
    with store.tx() as conn:
        conn.execute('DELETE FROM metrics WHERE labels LIKE \'%"unit":"digest"%\'')

    report = assess_run(store, hours=72)

    assert "digest" in report.silent_units
    assert report.criteria["no_silent_unit"] is False


def test_an_absent_optional_unit_is_not_a_silent_unit(store: Any) -> None:
    """Declining the feedback bot is a supported configuration and must not fail a run
    that was otherwise clean."""
    _healthy_run(store)

    report = assess_run(store, hours=72)

    assert not any(unit in OPTIONAL_UNITS for unit in report.silent_units)


def test_a_worker_restarted_onto_a_new_build_mid_run_is_flagged(store: Any) -> None:
    """A build change inside the window means the report describes two systems averaged
    together, which is not a measurement of either."""
    for minute in range(4320, 2160, -1):
        _beat(store, "worker", ago_minutes=minute, source_mtime=1_000.0)
    for minute in range(2160, 0, -1):
        _beat(store, "worker", ago_minutes=minute, source_mtime=2_000.0)
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)

    report = assess_run(store, hours=72)

    assert report.builds_seen == 2
    assert report.criteria["one_build_throughout"] is False


# ------------------------------------------------------- heat: reported, not judged


def test_the_governor_engaging_does_not_fail_the_run(store: Any) -> None:
    """The whole point of re-specifying the gate. Pausing inference under heat is the
    design working -- jobs retry rather than dying -- and the old `0x0` gate failed the
    run for it."""
    for minute in range(4320, 1000, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="nominal",
            temp_c=62.0,
            throttled_now=False,
        )
    for minute in range(1000, 400, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="hot",
            temp_c=79.0,
            throttled_now=True,
        )
    for minute in range(400, 0, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="nominal",
            temp_c=64.0,
            throttled_now=False,
        )
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)
    _dispatch(store, 60, ago_hours=6)

    report = assess_run(store, hours=72)

    assert report.thermal.engaged is True
    assert report.thermal.recovered is True
    assert report.thermal.peak_temp_c == 79.0
    assert report.thermal.throttled_samples == 600
    assert report.criteria["governor_recovered"] is True
    assert report.passed is True


def test_a_governor_that_never_came_back_does_fail(store: Any) -> None:
    """Pausing under heat is correct; staying paused for the rest of the run is the
    system quietly stopping while reporting itself alive."""
    for minute in range(4320, 2000, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="nominal",
            temp_c=60.0,
            throttled_now=False,
        )
    for minute in range(2000, 0, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="throttled",
            temp_c=85.0,
            throttled_now=True,
        )
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)

    report = assess_run(store, hours=72)

    assert report.thermal.recovered is False
    assert report.criteria["governor_recovered"] is False
    assert report.passed is False


def test_minutes_are_credited_by_interval_not_by_sample_count(store: Any) -> None:
    _healthy_run(store, hours=2)

    thermal = assess_run(store, hours=72).thermal

    # 120 samples a minute apart: 119 intervals credited, the last not extrapolated.
    assert thermal.minutes_by_state["nominal"] == 119.0


def test_a_gap_is_not_credited_to_whatever_state_preceded_it(store: Any) -> None:
    """Crediting the interval across an outage would report hours of `nominal` for a
    window in which nothing was running at all."""
    _beat(
        store,
        "worker",
        ago_minutes=4000,
        source_mtime=1_000.0,
        thermal_state="nominal",
        temp_c=60.0,
    )
    _beat(
        store,
        "worker",
        ago_minutes=100,
        source_mtime=1_000.0,
        thermal_state="nominal",
        temp_c=60.0,
    )

    thermal = assess_run(store, hours=72).thermal

    assert thermal.minutes_by_state.get("nominal", 0.0) == 0.0
    assert HEARTBEAT_GAP_SECONDS < (4000 - 100) * 60  # the gap really was one


def test_an_unmeasured_run_reports_unmeasured_and_never_passes_on_it(store: Any) -> None:
    """A box with no sensor must not report as a cool one. `None` is not `True`."""
    for minute in range(4320, 0, -1):
        _beat(store, "worker", ago_minutes=minute, source_mtime=1_000.0)
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)
    _dispatch(store, 60, ago_hours=6)

    report = assess_run(store, hours=72)

    assert report.thermal.measured is False
    assert report.criteria["governor_recovered"] is None
    assert report.passed is False, "an unmeasurable criterion must not pass the run"
    assert "not evidence the box stayed cool" in render_markdown(report)


# ---------------------------------------------------------------------- render


def test_the_report_grades_the_software_and_only_reports_the_heat(store: Any) -> None:
    _healthy_run(store, temp_c=81.0)
    _dispatch(store, 60, ago_hours=6)

    body = render_markdown(assess_run(store, hours=72))

    assert "PASSED" in body
    assert "peak **81.0 C**" in body
    assert "Heat is reported and never graded" in body


def test_an_empty_window_does_not_claim_success(store: Any) -> None:
    report = assess_run(store, hours=72)

    assert report.passed is False
    assert "NOT PASSED" in render_markdown(report)


def test_an_unmeasured_run_does_not_claim_the_governor_never_engaged(store: Any) -> None:
    """ "Never engaged" is a claim about a cool run. With no samples we have not earned
    it, and printing it beside `n/a` invites reading the row as reassurance."""
    for minute in range(4320, 0, -1):
        _beat(store, "worker", ago_minutes=minute, source_mtime=1_000.0)

    body = render_markdown(assess_run(store, hours=72))

    assert "not measured" in body
    assert "governor never engaged" not in body
