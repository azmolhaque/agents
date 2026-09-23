"""The re-specified Phase 7 gate.

The gate it replaces -- `get_throttled` at `0x0` for 72 h -- was an assertion about a
heatsink, and it was already false twenty minutes after a cold boot on the hardware we
have. The risk in replacing an unreachable gate is putting an unfalsifiable one in its
place, so most of these tests are about whether each criterion can actually *fail*.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from cindraleads.acceptance import (
    HEARTBEAT_GAP_SECONDS,
    LEADS_PER_DAY_TARGET,
    assess_run,
    render_markdown,
)
from cindraleads.metrics import HEARTBEAT_METRIC, HEARTBEAT_UNITS, OPTIONAL_UNITS
from cindraleads.models import to_iso, utcnow

LONG_UPTIME = 90 * 24 * 3600.0


@pytest.fixture(autouse=True)
def _box_has_been_up_a_while(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """State the box's uptime instead of reading the machine running the tests.

    `_silent_units` exempts a unit the box has not been up long enough to have run,
    so without this every silence verdict here would depend on how long the laptop or
    the CI runner happened to have been switched on -- green on a box up for a week,
    red on one booted this morning, with nothing in the diff to explain it. That is
    the health test polling the real SoC and the tests pinned to literal dates, for
    the third time: **green locally is a claim about the laptop.**
    """
    monkeypatch.setattr("cindraleads.acceptance.uptime_seconds", lambda: LONG_UPTIME)
    yield


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


def test_a_timer_that_has_not_run_within_its_own_budget_is_named(store: Any) -> None:
    _healthy_run(store)
    with store.tx() as conn:
        conn.execute('DELETE FROM metrics WHERE labels LIKE \'%"unit":"digest"%\'')

    report = assess_run(store, hours=72)

    assert "digest" in report.silent_units
    assert report.criteria["no_silent_unit"] is False


def test_a_daily_timer_is_not_silent_just_because_the_window_is_short(store: Any) -> None:
    """The defect this criterion shipped with, and it failed a healthy box.

    `digest` fires daily at 08:30 and `maintenance` at 03:20, while the check asked
    whether a unit beat inside the *operator's* window. A six-hour evening window
    therefore failed on a system where every timer was enabled, loaded and running --
    read on the Pi 2026-09-23 as `FAIL no_silent_unit ... silent: digest`, on a box
    that had posted its digest that morning.

    Same family as `get_throttled == 0x0` asserting a heatsink and `throughput`
    dividing by wall-clock: **a criterion a correct system cannot satisfy is grading
    something other than the software.** The budget it should have used was already in
    `HEARTBEAT_UNITS` and already read by `/healthz`.
    """
    _healthy_run(store, hours=6)
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        # Every timer alive on its own cadence, and the daily pair last ran this
        # morning -- well before a window that covers only the evening.
        _beat(store, unit, ago_minutes=30 if HEARTBEAT_UNITS[unit] <= 12 else 9 * 60)

    report = assess_run(store, hours=6)

    assert report.silent_units == []
    assert report.unjudged_units == []
    assert report.criteria["no_silent_unit"] is True


def test_a_timer_the_box_has_not_been_up_long_enough_to_run_is_not_yet_judged(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown, which keeps the criterion off green rather than excusing it.

    A box up for ten minutes has not given a 36-hour timer its turn, so "digest has
    not beaten" says nothing about the timer. Reporting that as a pass would be the
    `get_throttled` mistake in the other direction -- a green earned by not looking --
    so it reports `n/a`, the same rule `throughput` follows under
    `MIN_THROUGHPUT_HOURS`.
    """
    monkeypatch.setattr("cindraleads.acceptance.uptime_seconds", lambda: 600.0)
    _healthy_run(store, hours=6)
    # A box booted ten minutes ago has no beat from either daily timer yet, which is
    # what `_healthy_run` would otherwise supply.
    with store.tx() as conn:
        conn.execute(
            'DELETE FROM metrics WHERE labels LIKE \'%"unit":"digest"%\' '
            'OR labels LIKE \'%"unit":"maintenance"%\''
        )

    report = assess_run(store, hours=6)

    assert set(report.unjudged_units) == {"digest", "maintenance"}
    assert report.silent_units == []
    assert report.criteria["no_silent_unit"] is None
    assert report.passed is False


def test_unknown_uptime_buys_a_dead_timer_no_alibi(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive evidence, never absence of it -- the `no_job_lost` discipline.

    A platform with no `/proc/uptime` cannot show that a timer has not had its turn,
    and a missing file must not become a blanket excuse. Without this, the exemption
    would be widest on exactly the machines that can prove the least.
    """
    monkeypatch.setattr("cindraleads.acceptance.uptime_seconds", lambda: None)
    _healthy_run(store, hours=6)
    with store.tx() as conn:
        conn.execute('DELETE FROM metrics WHERE labels LIKE \'%"unit":"digest"%\'')

    report = assess_run(store, hours=6)

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


def test_a_governor_cycling_under_sustained_load_still_passes(store: Any) -> None:
    """The criterion as first written asked whether the *final* sample was nominal, and
    that made it useless: a box grinding through a queue is warm whenever you look at
    it. It failed a run doing exactly what it is supposed to do -- 400 jobs draining and
    the report saying "still degraded at the end of the window" about a governor that
    had cycled in and out of `warm` all evening.

    What matters is whether heat is passing or terminal. Cycling passes.
    """
    for minute in range(4320, 0, -1):
        # Alternating spells: warm for twenty minutes, nominal for ten, all night, and
        # warm at the final sample because the queue is still draining.
        hot = (minute // 20) % 2 == 0
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="warm" if hot else "nominal",
            temp_c=76.0 if hot else 64.0,
        )
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)
    _dispatch(store, 60, ago_hours=6)

    report = assess_run(store, hours=72)

    assert report.thermal.engaged is True
    assert report.thermal.recovered is True
    assert report.criteria["governor_recovered"] is True


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


def _dead(store: Any, last_error: str, *, kind: str = "extract.candidate") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO dead_letter (job_id, kind, payload, attempts, last_error, died_at) "
            "VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, kind, "{}", 3, last_error, to_iso(utcnow())),
        )


def test_an_unreachable_prospect_is_reported_but_not_graded(store: Any) -> None:
    """The job ran, reached the network, and the host did not answer after every
    retry. That is not work the system lost.

    Measured over the project's whole life: 12 of 26 dead letters were a prospect's own
    host timing out or refusing a connection -- ~0.4/day, evenly spread. Grading them
    fails the run on a background rate nobody can fix, which is the defect that retired
    the `get_throttled == 0x0` criterion. A gate that fails routinely for an unfixable
    reason is one you learn to ignore.
    """
    _healthy_run(store)
    _dispatch(store, 60, ago_hours=6)
    _dead(store, "_StageFailed: ReadTimeout: ")
    _dead(store, "_StageFailed: ConnectError: All connection attempts failed")

    report = assess_run(store, hours=72)

    assert report.dead_lettered == 2, "still counted"
    assert report.dead_unreachable == 2
    assert report.jobs_lost == 0
    assert report.criteria["no_job_lost"] is True


def test_an_unrecognised_failure_is_still_lost(store: Any) -> None:
    """The load-bearing half. This is an allow-list of remote-origin markers and never
    a deny-list, because the one thing that must not happen is a new failure mode
    quietly acquiring an excuse."""
    _healthy_run(store)
    _dispatch(store, 60, ago_hours=6)
    _dead(store, "_StageFailed: something nobody has seen before")

    report = assess_run(store, hours=72)

    assert report.jobs_lost == 1
    assert report.criteria["no_job_lost"] is False


def test_a_stage_failure_beside_an_unreachable_host_still_fails_the_run(store: Any) -> None:
    """The exemption narrows what is graded; it must not hide what sits next to it."""
    _healthy_run(store)
    _dispatch(store, 60, ago_hours=6)
    _dead(store, "_StageFailed: ReadTimeout: ")
    _dead(store, "lease expired past max_attempts", kind="score.company")

    report = assess_run(store, hours=72)

    assert (report.dead_lettered, report.dead_unreachable, report.jobs_lost) == (2, 1, 1)
    assert report.criteria["no_job_lost"] is False


def test_the_excused_count_is_printed(store: Any) -> None:
    """An exemption nobody can see is a weakened gate, and a rising unreachable count
    is how a broken uplink on this end would present."""
    from cindraleads.acceptance import render_markdown

    _healthy_run(store)
    _dispatch(store, 60, ago_hours=6)
    _dead(store, "_StageFailed: ReadTimeout: ")

    text = render_markdown(assess_run(store, hours=72))

    assert "unreachable host(s), not graded" in text


# ------------------------------------------- an announced stop is not a silence


def _run_with_break(store: Any, *, gap_minutes: float, announced: bool) -> None:
    """A worker beating every minute for 3 h with one interruption in the middle."""
    for minute in range(180, 90, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="nominal",
            temp_c=60.0,
            throttled_now=False,
            **({"exiting": True} if announced and minute == 91 else {}),
        )
    for minute in range(int(91 - gap_minutes), 0, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            source_mtime=1_000.0,
            thermal_state="nominal",
            temp_c=60.0,
            throttled_now=False,
        )
    for unit in HEARTBEAT_UNITS:
        if unit in ("worker", *OPTIONAL_UNITS):
            continue
        _beat(store, unit, ago_minutes=30)


def test_a_clean_restart_is_not_a_silent_unit(store: Any) -> None:
    """Raising `TimeoutStopSec` to 960 s so a deploy mid-stage is a shutdown rather
    than a SIGKILL means a *clean* stop legitimately takes up to 16 minutes -- three
    times `HEARTBEAT_GAP_SECONDS`. The fix for one gate started failing another: 4 clean
    exits and 2 worker gaps in one window, where gaps used to track the missing
    goodbyes exactly.

    The discriminator was already in the heartbeat: `exiting=True` is the last beat the
    worker writes, `worker_restarts` counts those, and the gap loop never looked at the
    flag sitting immediately before the gap.
    """
    _run_with_break(store, gap_minutes=14.0, announced=True)
    _dispatch(store, 60, ago_hours=1)

    report = assess_run(store, hours=3)

    assert report.worker_gaps == [], "the worker said goodbye"
    assert len(report.announced_gaps) == 1, "and it is still reported"
    assert report.criteria["no_silent_unit"] is True


def test_an_unannounced_gap_of_the_same_length_still_fails(store: Any) -> None:
    """The load-bearing half. What this criterion is for is a worker that died and came
    back, and that is indistinguishable from a deploy by duration alone."""
    _run_with_break(store, gap_minutes=14.0, announced=False)
    _dispatch(store, 60, ago_hours=1)

    report = assess_run(store, hours=3)

    assert len(report.worker_gaps) == 1
    assert report.announced_gaps == []
    assert report.criteria["no_silent_unit"] is False


def test_an_announced_exit_that_never_comes_back_is_still_an_outage(store: Any) -> None:
    """Bounded on purpose. A worker that said goodbye and stayed away for an hour is
    exactly the outage this criterion exists for -- the goodbye is not a blank cheque."""
    _run_with_break(store, gap_minutes=60.0, announced=True)
    _dispatch(store, 60, ago_hours=1)

    report = assess_run(store, hours=3)

    assert len(report.worker_gaps) == 1
    assert report.criteria["no_silent_unit"] is False


def test_the_announced_bound_is_the_one_a_stage_was_sized_for() -> None:
    """One decision, not two. What bounds an honest shutdown is how long the worker is
    permitted to take finishing a stage -- the same number `TimeoutStopSec` is checked
    against, rather than a third copy of it."""
    from cindraleads.acceptance import ANNOUNCED_STOP_SECONDS
    from cindraleads.config import MAX_STAGE_SECONDS

    assert ANNOUNCED_STOP_SECONDS > MAX_STAGE_SECONDS
    assert ANNOUNCED_STOP_SECONDS > HEARTBEAT_GAP_SECONDS


# ------------------------------------------------- the grid is not the software


def _beats_over(store: Any, *, from_min: int, to_min: int, worker_id: str) -> None:
    """A worker beating every minute across a span, under one boot identity."""
    for minute in range(from_min, to_min, -1):
        _beat(
            store,
            "worker",
            ago_minutes=minute,
            worker_id=worker_id,
            source_mtime=1_000.0,
            thermal_state="nominal",
            temp_c=60.0,
            throttled_now=False,
        )


def _timers_alive(store: Any) -> None:
    for unit in HEARTBEAT_UNITS:
        if unit not in ("worker", *OPTIONAL_UNITS):
            _beat(store, unit, ago_minutes=30)


def test_a_power_cut_is_not_a_silent_unit(store: Any) -> None:
    """The gate grades what the software controls, and the grid is not the software.

    This box loses power: 17 and 18 September have no heartbeat rows at all. Reading
    every gap as "the worker died on Tuesday" reports a fault on every outage, and a
    criterion that fails for a reason no code change can fix is one you learn to
    explain away -- which is exactly why `get_throttled == 0x0` was retired and why an
    unreachable prospect was taken out of `no_job_lost`.

    The discriminator is the boot token the heartbeat already carries: a gap the
    machine rebooted across is one it was switched off for.
    """
    _beats_over(store, from_min=600, to_min=420, worker_id="pi:aaaa1111:100")
    _beats_over(store, from_min=180, to_min=0, worker_id="pi:bbbb2222:200")
    _timers_alive(store)

    report = assess_run(store, hours=24)

    assert report.worker_gaps == [], "a reboot gap must not count as a silent unit"
    assert len(report.power_gaps) == 1
    assert report.criteria["no_silent_unit"] is True


def test_a_worker_that_died_without_rebooting_still_fails(store: Any) -> None:
    """The bound, and the reason the excuse is an allow-list.

    Same boot on both sides means the machine stayed up and the worker did not -- which
    is precisely the outage this criterion exists for. Without this the power-cut
    exemption would swallow every real death.
    """
    _beats_over(store, from_min=600, to_min=420, worker_id="pi:aaaa1111:100")
    _beats_over(store, from_min=180, to_min=0, worker_id="pi:aaaa1111:100")
    _timers_alive(store)

    report = assess_run(store, hours=24)

    assert report.power_gaps == []
    assert len(report.worker_gaps) == 1
    assert report.criteria["no_silent_unit"] is False


def test_a_gap_with_no_boot_token_is_not_excused(store: Any) -> None:
    """Positive evidence, never absence of it.

    A beat written before `worker_identity` carried a boot token yields None, and None
    must not buy an alibi -- the same allow-list discipline as the unreachable markers
    in `no_job_lost`, where an unrecognised error still counts as lost.
    """
    _beats_over(store, from_min=600, to_min=420, worker_id="pi:100")
    _beats_over(store, from_min=180, to_min=0, worker_id="pi:200")
    _timers_alive(store)

    report = assess_run(store, hours=24)

    assert report.power_gaps == []
    assert len(report.worker_gaps) == 1


def test_throughput_is_measured_over_running_time_not_wall_clock(store: Any) -> None:
    """Dividing by the window measured the electricity supply.

    A 72 h window covering 12.5 h of running reported 0.7/day for a worker that was
    managing ~11.5. The run either produced leads at a rate or it did not; how long the
    power was out is a separate fact, and it is printed separately.
    """
    _beats_over(store, from_min=720, to_min=0, worker_id="pi:aaaa1111:100")
    _timers_alive(store)
    _dispatch(store, 10, ago_hours=6)

    report = assess_run(store, hours=72)

    assert 11.5 <= report.covered_hours <= 12.5, report.covered_hours
    # 10 leads over ~12 h is ~20/day, not 10 / 3 days.
    assert report.leads_per_day > LEADS_PER_DAY_TARGET
    assert report.criteria["throughput"] is True


def test_a_run_too_short_to_judge_reports_n_a_rather_than_passing(store: Any) -> None:
    """A criterion that cannot be evaluated does not pass -- the same three-valued rule
    as a box with no thermal sensor.

    One lucky dispatch in a one-hour window is 24/day, which would let a run prove
    throughput by being too short to measure it. Short windows are the only ones this
    grid allows, so the report has to say which kind of short it was.
    """
    _beats_over(store, from_min=120, to_min=0, worker_id="pi:aaaa1111:100")
    _timers_alive(store)
    _dispatch(store, 5, ago_hours=1)

    report = assess_run(store, hours=4)

    assert report.covered_hours < 6.0
    assert report.criteria["throughput"] is None, "too little running time to judge"
    assert report.passed is False, "an unmeasurable criterion never passes the run"
    assert "too short to judge" in render_markdown(report)
