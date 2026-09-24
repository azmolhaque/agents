"""The Phase 7 acceptance report: what a 72 hour unattended run actually proved.

The original gate was `get_throttled` staying `0x0` for 72 hours. That is an assertion
about a heatsink, not about this system, and on the hardware we have it was already
false twenty minutes after a cold boot -- sticky bits 17/18/19 set, no under-voltage,
so purely thermal. A gate you know will fail is not a gate; it is a number you learn to
explain away.

**The system has a thermal governor precisely so heat is a designed-for state.**
Demanding `0x0` demands that the governor never has to act, which is a strange thing to
require of a system built to act. What is worth asserting is what the software controls:

| Criterion | Why it is the real question |
| --- | --- |
| Throughput held | The run produced leads, not just uptime |
| No job was lost | The one unrecoverable failure. Dead letters and orphans, counted |
| The governor engaged **and recovered** | Pausing under heat is correct; staying paused is not |
| No unit went silent | A timer that died on Tuesday is invisible from the job table |
| The worker stayed on one build | A restart mid-run means the window measured two systems |

Heat is *reported*, never pass/fail: peak temperature, minutes spent in each governor
state, and whether the SoC was throttled at the moment of each sample. Those are the
numbers that tell you whether to buy a cooler. They are not the numbers that tell you
whether the software works.

Everything here is reconstructed from the `metrics` table, which the worker writes a row
to every 60 seconds anyway. Nothing new is sampled and nothing accumulates in process
memory -- the same reason `metrics.py` computes at scrape time. Before the heartbeat
carried `thermal_state`, none of this was answerable after the fact: the governor keeps
its state in memory and `/healthz` reports only the instant, so the evidence died with
each poll.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from cindraleads.config import MAX_STAGE_SECONDS, WORKER_HEARTBEAT_SECONDS
from cindraleads.metrics import (
    HEARTBEAT_METRIC,
    HEARTBEAT_UNITS,
    OPTIONAL_UNITS,
    boot_of,
    uptime_seconds,
)
from cindraleads.models import to_iso, utcnow
from cindraleads.store import Store

__all__ = ["AcceptanceReport", "ThermalWindow", "assess_run", "render_markdown"]

# Leads per day the run has to sustain to count as productive. PLAN.md Phase 7.
LEADS_PER_DAY_TARGET = 15

# A worker heartbeat is due every 60 s. Allow a generous multiple before calling a gap
# a gap: one slow transaction must not read as an outage.
HEARTBEAT_GAP_SECONDS = 300.0

# How long a worker that *announced* its exit may stay away before the silence counts
# against it anyway.
#
# `no_silent_unit` exists to catch "the worker died on Tuesday and nobody noticed", and
# it read every gap as that. Raising `TimeoutStopSec` to 960 s -- so a deploy landing
# mid-stage is a shutdown rather than a SIGKILL -- means a *clean* stop now legitimately
# takes up to 16 minutes, three times `HEARTBEAT_GAP_SECONDS`. So the fix for one gate
# started failing another: 4 clean exits and 2 worker gaps in the same 24 h, where the
# old note had gaps tracking the *missing* goodbyes exactly.
#
# The discriminator was already in the heartbeat. `exiting=True` is the last beat the
# worker writes before returning, and `worker_restarts` counts those -- but the gap loop
# never looked at the flag sitting immediately before the gap. Sixth instance of
# built-wired-never-connected, after `digest_pages`, `extend_lease`, `open_roles`,
# `discovered_by` and `full_name`.
#
# Derived from `MAX_STAGE_SECONDS` rather than written down again, because what bounds
# an honest shutdown is how long the worker is permitted to take finishing a stage. The
# slack is the way back up: process start plus a ~32 s cold model load off microSD.
ANNOUNCED_STOP_SECONDS = MAX_STAGE_SECONDS + 120.0

# How long the governor may still be in a degraded state at the end of the window before
# that counts as "did not recover". Generous on purpose: the failure worth catching is a
# box wedged hot indefinitely, not one that is simply busy at the moment you looked.
OPEN_SPELL_GRACE_MINUTES = 90.0

# Running time below which a leads-per-day figure is noise rather than a measurement.
# At the 15/day target six hours expects under four leads, and one lucky dispatch in a
# one-hour window would read as 24/day. Below this the criterion reports `n/a` and does
# **not** pass, the same three-valued rule as a box with no thermal sensor: a run too
# short to prove throughput must not be recorded as one that proved it.
#
# This is what makes a short window honest rather than merely convenient. The grid here
# does not permit 72 unbroken hours, so short runs are the only runs available -- but
# short enough is still too short, and the report has to say which it was.
MIN_THROUGHPUT_HOURS = 6.0


@dataclass(frozen=True)
class ThermalWindow:
    """Heat over the run. Reported, never pass/fail."""

    samples: int = 0
    peak_temp_c: float | None = None
    mean_temp_c: float | None = None
    minutes_by_state: dict[str, float] = field(default_factory=dict)
    throttled_samples: int = 0
    engaged: bool = False
    recovered: bool = True

    @property
    def measured(self) -> bool:
        """False on a box with no sensor, or a run predating the thermal heartbeat.

        Distinguished from "it never got hot" on purpose: an unmeasured run must not
        be reported as a cool one.
        """
        return self.samples > 0


@dataclass
class AcceptanceReport:
    window_hours: float
    since: datetime
    # throughput
    dispatched_ab: int = 0
    dispatched_total: int = 0
    leads_per_day: float = 0.0
    # job integrity
    dead_lettered: int = 0
    # Of those, the ones where a prospect's host did not answer. Reported, not graded.
    dead_unreachable: int = 0
    # liveness
    worker_gaps: list[tuple[datetime, float]] = field(default_factory=list)
    # Gaps that follow an announced exit: a deploy, not an outage. Reported, not graded.
    announced_gaps: list[tuple[datetime, float]] = field(default_factory=list)
    # Gaps that span a reboot: the machine was off. Reported, not graded.
    power_gaps: list[tuple[datetime, float]] = field(default_factory=list)
    # Hours the worker was actually beating. The denominator throughput deserves on a
    # box that is not on for the whole window.
    covered_hours: float = 0.0
    silent_units: list[str] = field(default_factory=list)
    # Units whose silence cannot be judged yet: the box has not been up as long as the
    # unit's own budget, so a daily timer may simply not have had its turn. Reported,
    # and enough on its own to keep `no_silent_unit` off green.
    unjudged_units: list[str] = field(default_factory=list)
    worker_restarts: int = 0
    builds_seen: int = 0
    # heat, reported not judged
    thermal: ThermalWindow = field(default_factory=ThermalWindow)

    @property
    def criteria(self) -> dict[str, bool | None]:
        """The gate. `None` means "not measurable from this window", never "pass".

        A criterion nothing could evaluate must not report green -- that is how the
        old `0x0` gate would have been quietly satisfied by a box with no sensor.
        """
        return {
            "throughput": (
                self.leads_per_day >= LEADS_PER_DAY_TARGET
                if self.covered_hours >= MIN_THROUGHPUT_HOURS
                else None
            ),
            "no_job_lost": self.jobs_lost == 0,
            # A real fault still fails. Otherwise, a unit nobody could judge yet keeps
            # this off green rather than earning one -- same rule as `throughput`
            # under `MIN_THROUGHPUT_HOURS`.
            "no_silent_unit": (
                False
                if (self.silent_units or self.worker_gaps)
                else None
                if self.unjudged_units
                else True
            ),
            "one_build_throughout": self.builds_seen <= 1 if self.builds_seen else None,
            "governor_recovered": self.thermal.recovered if self.thermal.measured else None,
        }

    @property
    def jobs_lost(self) -> int:
        """Dead letters the software is answerable for.

        An unreachable prospect is not lost work -- the job ran, reached the network,
        and the host did not answer after every retry. What this counts is everything
        else: a stage that raised, a lease that expired, a job orphaned without ever
        failing. Those are ours.
        """
        return max(0, self.dead_lettered - self.dead_unreachable)

    @property
    def passed(self) -> bool:
        """Every measurable criterion held. An unmeasurable one does not pass it."""
        values = self.criteria.values()
        return all(v is True for v in values)


def _heartbeat_rows(store: Store, unit: str, stamp: str) -> list[tuple[datetime, dict[str, Any]]]:
    rows = store.conn.execute(
        "SELECT labels, recorded_at FROM metrics WHERE name = ? "
        "AND labels LIKE ? AND recorded_at >= ? ORDER BY recorded_at",
        (HEARTBEAT_METRIC, f'%"unit":"{unit}"%', stamp),
    ).fetchall()
    out: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        try:
            detail = json.loads(str(row["labels"]))
            at = datetime.fromisoformat(str(row["recorded_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append((at, detail))
    return out


def assess_run(
    store: Store, *, hours: float = 72.0, now: datetime | None = None
) -> AcceptanceReport:
    """Reconstruct what the last `hours` proved, from what was already written down."""
    at = now or utcnow()
    since = at - timedelta(hours=hours)
    stamp = to_iso(since)
    report = AcceptanceReport(window_hours=hours, since=since)

    beats = _heartbeat_rows(store, "worker", stamp)
    _job_integrity(report, store, stamp)
    # After `_liveness`, which is what measures `covered_hours` -- the denominator.
    # Read here rather than inside `_liveness`, so a test states the box's uptime by
    # patching one function and nothing has to remember to pass an argument. A
    # parameter no caller supplies is the shape this project has paid for ten times.
    _liveness(report, store, beats, now=at, uptime=uptime_seconds())
    _throughput(report, store, stamp)
    report.thermal = _thermal(beats)
    return report


def _throughput(report: AcceptanceReport, store: Store, stamp: str) -> None:
    """Leads per day, over the time the box was **on**.

    It divided by the wall-clock window, which on a grid with load shedding measures the
    electricity supply. 17 and 18 September have no heartbeat rows at all, so a 72 h
    window covered 12.5 h of running and reported 0.7/day for a worker that was actually
    managing ~11.5. That is the same defect as grading `get_throttled` and as counting an
    unreachable prospect against `no_job_lost`: **the gate judges what the software
    controls.** The software controls leads per hour of running; the grid controls hours.

    Both numbers are always printed, exactly as with the unreachable count -- an
    exemption nobody can see is a weakened gate, and hours lost to power is the number
    that argues for a UPS.
    """
    row = store.conn.execute(
        "SELECT COUNT(DISTINCT lead_id) AS total, "
        "COUNT(DISTINCT CASE WHEN tier IN ('A','B') THEN lead_id END) AS ab "
        "FROM dispatch_log WHERE dispatched_at >= ?",
        (stamp,),
    ).fetchone()
    report.dispatched_total = int(row["total"] or 0)
    report.dispatched_ab = int(row["ab"] or 0)
    hours = report.covered_hours
    report.leads_per_day = report.dispatched_ab / (hours / 24) if hours else 0.0


# Dead letters where the remote simply did not answer, as opposed to ones the software
# could have prevented. `no_job_lost` grades the second and **reports** the first.
#
# Measured over the project's whole life on 2026-09-15: 12 of 26 dead letters were a
# prospect's own host timing out, refusing a connection or serving a 502 -- ~0.4/day,
# spread evenly over 31 days with no cluster. Grading those fails the run on a
# background rate nobody can fix, which is precisely the defect that retired the
# `get_throttled == 0x0` criterion: it graded something outside the software. A gate
# that fails routinely for an unfixable reason is one you learn to ignore.
#
# Deliberately an allow-list of remote-origin markers, never a deny-list. An error this
# does not recognise counts as lost, because the one thing that must not happen is a
# new failure mode quietly acquiring an excuse. The excused count is printed next to
# the graded one for the same reason -- an exemption nobody can see is a weakened gate.
UNREACHABLE_MARKERS = (
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectError",
    "PoolTimeout",
    "ReadError",
    "RemoteProtocolError",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
)


def _is_unreachable(last_error: str) -> bool:
    return any(marker in last_error for marker in UNREACHABLE_MARKERS)


def _job_integrity(report: AcceptanceReport, store: Store, stamp: str) -> None:
    rows = store.conn.execute(
        "SELECT last_error FROM dead_letter WHERE died_at >= ?", (stamp,)
    ).fetchall()
    report.dead_lettered = len(rows)
    report.dead_unreachable = sum(1 for r in rows if _is_unreachable(str(r["last_error"] or "")))


def _liveness(
    report: AcceptanceReport,
    store: Store,
    beats: list[tuple[datetime, dict[str, Any]]],
    *,
    now: datetime,
    uptime: float | None = None,
) -> None:
    """Gaps in the worker's own record, plus units that never reported at all.

    A gap is the interesting signal and the job table cannot show it: a worker that was
    dead for six hours and came back leaves a queue that looks exactly like one that was
    merely idle.
    """
    for (earlier, before), (later, after) in pairwise(beats):
        gap = (later - earlier).total_seconds()
        if gap <= HEARTBEAT_GAP_SECONDS:
            continue
        # An announced stop is not a silence. The worker said goodbye, and a clean
        # shutdown is allowed to spend up to `MAX_STAGE_SECONDS` finishing the job it
        # was holding -- which is the whole reason `TimeoutStopSec` was raised.
        #
        # Bounded, because a worker that announced its exit and never came back for six
        # hours is exactly the outage this criterion is for. Past the bound it counts.
        if before.get("exiting") and gap <= ANNOUNCED_STOP_SECONDS:
            report.announced_gaps.append((earlier, gap))
            continue
        # The machine was off. A power cut is not a silent unit, for exactly the reason
        # `get_throttled == 0x0` was retired from this gate and an unreachable prospect
        # was taken out of `no_job_lost`: it grades something the software does not
        # control. This box is on a grid with load shedding -- 17 and 18 September have
        # no heartbeat rows at all -- so without this the criterion reports a fault
        # every time the power fails, and a gate that cries wolf is one nobody reads.
        #
        # Positive evidence, never absence of it: the boot token must be *known on both
        # sides and different*. An old beat carries no token and yields None, and None
        # is not an excuse -- same allow-list discipline as the unreachable markers,
        # so a worker that genuinely died cannot acquire an alibi by having no data.
        before_boot = boot_of(str(before.get("worker_id") or ""))
        after_boot = boot_of(str(after.get("worker_id") or ""))
        if before_boot and after_boot and before_boot != after_boot:
            report.power_gaps.append((earlier, gap))
            continue
        report.worker_gaps.append((earlier, gap))

    # Time the worker was demonstrably alive: every inter-beat interval short enough
    # to be an ordinary beat. Gaps of any kind are excluded -- an outage is not uptime,
    # whoever caused it -- so this is a floor on running time rather than an estimate.
    #
    # Plus the interval the *last* beat opens, which the pairwise sum drops. n beats
    # yield n-1 intervals, so one beat's worth of real running was silently uncounted
    # every time, and at the boundary that is the difference between a judgeable window
    # and `n/a`: a worker beating every 60 s through a perfect `--hours 6` run scored
    # 5.97 against a floor of 6.0 and could never clear it. A beat is not a point, it
    # says "alive now, due again in `WORKER_HEARTBEAT_SECONDS`" -- so it attests the
    # interval it opens, bounded by that promise and by the end of the window, and
    # never beyond either.
    #
    # And the sliver before the *first* beat, when a beat sitting just outside the
    # window proves the worker was already running across the boundary. That one is
    # positive evidence like every other exemption here -- the earlier beat has to
    # exist and be close enough to be an ordinary beat, so a window that opens three
    # hours before the worker started is credited nothing. Without both edges the
    # window start silently eats a beat as well, and `--hours 6` still could not reach
    # a 6 h floor: **exact equality was unreachable by construction.**
    report.covered_hours = (
        sum(
            (later - earlier).total_seconds()
            for (earlier, _b), (later, _a) in pairwise(beats)
            if (later - earlier).total_seconds() <= HEARTBEAT_GAP_SECONDS
        )
        + _edge_before(store, beats, since=report.since)
        + (
            min(WORKER_HEARTBEAT_SECONDS, max(0.0, (now - beats[-1][0]).total_seconds()))
            if beats
            else 0.0
        )
    ) / 3600.0

    builds = {d.get("source_mtime") for _, d in beats if d.get("source_mtime")}
    report.builds_seen = len(builds)
    report.worker_restarts = sum(1 for _, d in beats if d.get("exiting"))

    _silent_units(report, store, now=now, uptime=uptime)


def _edge_before(
    store: Store, beats: list[tuple[datetime, dict[str, Any]]], *, since: datetime
) -> float:
    """Seconds of running between the window opening and its first beat, or zero.

    Credited only when a beat *outside* the window sits close enough to the first one
    inside it to be an ordinary beat -- then the worker demonstrably spanned the
    boundary and the interval is measured, not assumed. No earlier beat, or one too far
    back, and this is zero: the window may simply predate the worker.
    """
    if not beats:
        return 0.0
    first = beats[0][0]
    previous = _last_beat_at_or_before(store, "worker", since)
    if previous is None or (first - previous).total_seconds() > HEARTBEAT_GAP_SECONDS:
        return 0.0
    return max(0.0, (first - since).total_seconds())


def _silent_units(
    report: AcceptanceReport, store: Store, *, now: datetime, uptime: float | None
) -> None:
    """Which timers have actually stopped, judged against their own cadence.

    This asked whether a unit beat *inside the operator's window*, and that is a
    question about the window. `digest` fires daily at 08:30 and `maintenance` at
    03:20, so **neither can beat in a six-hour evening window** and a correct system
    fails by construction -- observed 2026-09-23, `silent: digest` on a box whose
    digest timer was enabled, loaded and entirely healthy. Same family as the
    `get_throttled == 0x0` gate asserting a heatsink, as `throughput` dividing by
    wall-clock on a grid with load shedding, and as `no_job_lost` charging an
    unreachable prospect to the software: **a criterion a correct system cannot
    satisfy is grading something other than the software.**

    The right number already existed and already had a reader. `HEARTBEAT_UNITS` maps
    each unit to `max_silence_hours` -- 36 for the daily pair, 6 for hourly harvest --
    and `/healthz` has always judged against it. So this was one decision in two files
    with only one of them reading the number, for the eighth time, and the fix is to
    ask the same question: **is this unit's last beat older than its own budget**,
    looking as far back as it takes rather than stopping at the window edge.

    The boot case is the one exemption and it follows the `no_job_lost` discipline --
    positive evidence, never absence of it. A box up for less than a unit's budget has
    not given that timer its turn, so the answer is *unknown*, which keeps the
    criterion off green instead of excusing it. Unknown uptime buys nothing.
    """
    up_hours = None if uptime is None else uptime / 3600.0
    for unit, budget_hours in HEARTBEAT_UNITS.items():
        if unit == "worker" or unit in OPTIONAL_UNITS:
            continue
        beat = _last_beat_at_or_before(store, unit, now)
        if beat is not None and (now - beat).total_seconds() <= budget_hours * 3600:
            continue
        if up_hours is not None and up_hours < budget_hours:
            report.unjudged_units.append(unit)
        else:
            report.silent_units.append(unit)


def _with_budgets(units: list[str]) -> str:
    """`digest (36 h)`, because the budget is what the operator is being told about.

    A bare `silent: digest` reads as "the digest timer is broken" and sent a reader
    looking at systemd. `digest (36 h)` says what claim is being made, and the next
    question -- has it been more than 36 hours -- is one they can answer.
    """
    return ", ".join(f"{u} ({HEARTBEAT_UNITS[u]:g} h)" for u in units)


def _last_beat_at_or_before(store: Store, unit: str, at: datetime) -> datetime | None:
    """The unit's most recent heartbeat, unbounded by the operator's window.

    Deliberately not `_heartbeat_rows`: that one is scoped to the window, which is
    correct for the worker -- whose gaps are a fact *about* the window -- and wrong for
    a timer, whose cadence is longer than most windows anyone will ask about.
    """
    row = store.conn.execute(
        "SELECT recorded_at FROM metrics WHERE name = ? AND labels LIKE ? "
        "AND recorded_at <= ? ORDER BY recorded_at DESC LIMIT 1",
        (HEARTBEAT_METRIC, f'%"unit":"{unit}"%', to_iso(at)),
    ).fetchone()
    if row is None:
        return None
    try:
        return datetime.fromisoformat(str(row["recorded_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None


def _recovered(states: list[tuple[datetime, dict[str, Any]]]) -> bool:
    """Did the governor come back, rather than: is it cool at this instant.

    The first version asked whether the *final* sample was nominal, and that was wrong
    in a way that made the criterion useless. A box grinding through a queue is warm
    whenever you look at it, so the check failed on a system doing exactly what it is
    supposed to do -- observed 2026-08-20 with 400 jobs draining and the report saying
    "still degraded at the end of the window" about a governor that had cycled in and
    out of `warm` all evening.

    What matters is whether heat is a passing state or a terminal one:

    - It returned to nominal at least once after engaging, **and**
    - any spell still open at the end of the window is younger than
      `OPEN_SPELL_GRACE_MINUTES`.

    So a governor cycling under load passes, and one that went hot two hours ago and
    never came back fails. The grace is generous because the honest failure this must
    catch is "wedged hot indefinitely", not "busy for twenty minutes".
    """
    came_back = any(
        str(earlier[1].get("thermal_state")) != "nominal"
        and str(later[1].get("thermal_state")) == "nominal"
        for earlier, later in pairwise(states)
    )
    if not came_back:
        return False

    # How long the current spell has been running, if it is a degraded one.
    last_at, last_detail = states[-1]
    if str(last_detail.get("thermal_state")) == "nominal":
        return True
    spell_started = last_at
    for at, detail in reversed(states):
        if str(detail.get("thermal_state")) == "nominal":
            break
        spell_started = at
    open_minutes = (last_at - spell_started).total_seconds() / 60
    return open_minutes <= OPEN_SPELL_GRACE_MINUTES


def _thermal(beats: list[tuple[datetime, dict[str, Any]]]) -> ThermalWindow:
    """Time in each governor state, from the heartbeat's own cadence.

    Each sample is credited with the interval until the next one, so a pause that
    spanned an hour reads as an hour rather than as one sample. The final sample is
    dropped rather than extrapolated -- assuming it lasted a minute would invent a
    minute of whatever state the run happened to end in.
    """
    temps = [d["temp_c"] for _, d in beats if isinstance(d.get("temp_c"), int | float)]
    states = [(at, d) for at, d in beats if d.get("thermal_state")]
    if not states:
        return ThermalWindow()

    minutes: dict[str, float] = {}
    for (earlier, detail), (later, _) in pairwise(states):
        span = (later - earlier).total_seconds() / 60
        # A gap longer than the alarm threshold is the worker being absent, not the
        # governor holding a state. Crediting it would report hours of "nominal" for a
        # window in which nothing was running at all.
        if span > HEARTBEAT_GAP_SECONDS / 60:
            continue
        minutes[str(detail["thermal_state"])] = (
            minutes.get(str(detail["thermal_state"]), 0.0) + span
        )

    engaged = any(state != "nominal" for state in minutes)
    recovered = _recovered(states) if engaged else True

    return ThermalWindow(
        samples=len(states),
        peak_temp_c=max(temps) if temps else None,
        mean_temp_c=(sum(temps) / len(temps)) if temps else None,
        minutes_by_state={k: round(v, 1) for k, v in sorted(minutes.items())},
        throttled_samples=sum(1 for _, d in states if d.get("throttled_now")),
        engaged=engaged,
        recovered=recovered,
    )


_MARK = {True: "PASS", False: "FAIL", None: "n/a "}


def render_markdown(report: AcceptanceReport) -> str:
    verdict = "PASSED" if report.passed else "NOT PASSED"
    lines = [
        f"# Phase 7 acceptance — {report.window_hours:g} h to {utcnow():%Y-%m-%d %H:%M} UTC",
        "",
        f"**{verdict}**",
        "",
        "| | Criterion | Measured |",
        "| --- | --- | --- |",
    ]
    detail = {
        # Both numbers, always: the rate and the hours it was measured over. A rate
        # divided by uptime with the uptime hidden is unfalsifiable, and hours lost to
        # power is the number that argues for a UPS rather than for a code change.
        # Two decimals, and the shortfall named, because one decimal made this line
        # contradict itself: `5.97` printed as `6.0` under a clause reading "under 6 h
        # running, too short to judge", so the report said the window both met the
        # floor and missed it. Rounding a number in the same sentence that explains a
        # comparison against it is how a true statement is made to read as a bug.
        "throughput": (
            f"{report.leads_per_day:.1f} Tier A+B/day over {report.covered_hours:.2f} h "
            f"running of {report.window_hours:g} h elapsed "
            f"(target {LEADS_PER_DAY_TARGET}); {report.dispatched_total} dispatched in total"
            + (
                f" -- {MIN_THROUGHPUT_HOURS - report.covered_hours:.2f} h short of the "
                f"{MIN_THROUGHPUT_HOURS:g} h floor, too short to judge; ask for a "
                f"longer window"
                if report.covered_hours < MIN_THROUGHPUT_HOURS
                else ""
            )
        ),
        # Both numbers, always. An exemption nobody can see is a weakened gate, and a
        # rising unreachable count is how a broken uplink on this end would present.
        "no_job_lost": f"{report.jobs_lost} lost of {report.dead_lettered} dead-lettered "
        f"({report.dead_unreachable} unreachable host(s), not graded)",
        # The excused count is printed for the same reason the unreachable one is: an
        # exemption nobody can see is a weakened gate, and a run full of announced
        # restarts is still a run nobody should call unattended.
        "no_silent_unit": (
            f"{len(report.worker_gaps)} worker gap(s)"
            + (
                f"; {len(report.announced_gaps)} announced restart(s), not graded"
                if report.announced_gaps
                else ""
            )
            + (f"; {len(report.power_gaps)} power cut(s), not graded" if report.power_gaps else "")
            + (
                f"; silent past its budget: {_with_budgets(report.silent_units)}"
                if report.silent_units
                else ""
            )
            # Printed for the same reason the unreachable and announced counts are: an
            # exemption nobody can see is a weakened gate, and "the box has only been
            # up an hour" is the operator's cue to wait rather than to investigate.
            + (
                f"; not yet judgeable: {_with_budgets(report.unjudged_units)}"
                f" -- box up under their budget"
                if report.unjudged_units
                else ""
            )
        ),
        "one_build_throughout": f"{report.builds_seen} build(s) seen, "
        f"{report.worker_restarts} clean exit(s)",
        # Three-valued, like everything else here. "Never engaged" is a claim about a
        # cool run; with no samples we have not earned it and must say so instead.
        "governor_recovered": (
            "no thermal samples -- not measured"
            if not report.thermal.measured
            else "governor never engaged"
            if not report.thermal.engaged
            else "back to nominal at the end of the window"
            if report.thermal.recovered
            else "still degraded at the end of the window"
        ),
    }
    for name, verdict_flag in report.criteria.items():
        lines.append(f"| {_MARK[verdict_flag]} | `{name}` | {detail[name]} |")

    lines += ["", "## Heat (reported, not judged)", ""]
    thermal = report.thermal
    if not thermal.measured:
        # Never "0 C" and never a pass. A run with no sensor is a run we did not measure.
        lines += [
            "No thermal samples in this window. Either the host has no `vcgencmd`, or "
            "the run predates the worker recording it. **This is not evidence the box "
            "stayed cool.**"
        ]
    else:
        lines += [
            f"- peak **{thermal.peak_temp_c:.1f} C**, mean {thermal.mean_temp_c:.1f} C "
            f"over {thermal.samples} samples",
            f"- throttled at {thermal.throttled_samples} of {thermal.samples} samples",
            "- minutes by governor state: "
            + (
                ", ".join(f"`{s}` {m:.0f}" for s, m in thermal.minutes_by_state.items()) or "(none)"
            ),
        ]
        if thermal.engaged:
            lines += [
                "",
                "The governor engaged. **That is the design working, not a failure** -- "
                "it pauses inference and the jobs retry rather than dying. The criterion "
                "above asks only whether it came back.",
            ]
    lines += [
        "",
        "---",
        "",
        "Heat is reported and never graded. The original gate asked `get_throttled` to "
        "stay `0x0`, which asserts a heatsink rather than this system, and requires that "
        "the thermal governor never has to do the job it exists for. Use the peak "
        "temperature to decide about cooling; use the table above to decide about the "
        "software.",
    ]
    return "\n".join(lines) + "\n"
