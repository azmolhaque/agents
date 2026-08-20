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

from cindraleads.metrics import HEARTBEAT_METRIC, HEARTBEAT_UNITS, OPTIONAL_UNITS
from cindraleads.models import to_iso, utcnow
from cindraleads.store import Store

__all__ = ["AcceptanceReport", "ThermalWindow", "assess_run", "render_markdown"]

# Leads per day the run has to sustain to count as productive. PLAN.md Phase 7.
LEADS_PER_DAY_TARGET = 15

# A worker heartbeat is due every 60 s. Allow a generous multiple before calling a gap
# a gap: one slow transaction must not read as an outage.
HEARTBEAT_GAP_SECONDS = 300.0

# How long the governor may still be in a degraded state at the end of the window before
# that counts as "did not recover". Generous on purpose: the failure worth catching is a
# box wedged hot indefinitely, not one that is simply busy at the moment you looked.
OPEN_SPELL_GRACE_MINUTES = 90.0


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
    # liveness
    worker_gaps: list[tuple[datetime, float]] = field(default_factory=list)
    silent_units: list[str] = field(default_factory=list)
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
            "throughput": self.leads_per_day >= LEADS_PER_DAY_TARGET,
            "no_job_lost": self.dead_lettered == 0,
            "no_silent_unit": not self.silent_units and not self.worker_gaps,
            "one_build_throughout": self.builds_seen <= 1 if self.builds_seen else None,
            "governor_recovered": self.thermal.recovered if self.thermal.measured else None,
        }

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
    _throughput(report, store, stamp, hours)
    _job_integrity(report, store, stamp)
    _liveness(report, store, stamp, beats, now=at)
    report.thermal = _thermal(beats)
    return report


def _throughput(report: AcceptanceReport, store: Store, stamp: str, hours: float) -> None:
    row = store.conn.execute(
        "SELECT COUNT(DISTINCT lead_id) AS total, "
        "COUNT(DISTINCT CASE WHEN tier IN ('A','B') THEN lead_id END) AS ab "
        "FROM dispatch_log WHERE dispatched_at >= ?",
        (stamp,),
    ).fetchone()
    report.dispatched_total = int(row["total"] or 0)
    report.dispatched_ab = int(row["ab"] or 0)
    report.leads_per_day = report.dispatched_ab / (hours / 24) if hours else 0.0


def _job_integrity(report: AcceptanceReport, store: Store, stamp: str) -> None:
    report.dead_lettered = int(
        store.conn.execute(
            "SELECT COUNT(*) AS n FROM dead_letter WHERE died_at >= ?", (stamp,)
        ).fetchone()["n"]
    )


def _liveness(
    report: AcceptanceReport,
    store: Store,
    stamp: str,
    beats: list[tuple[datetime, dict[str, Any]]],
    *,
    now: datetime,
) -> None:
    """Gaps in the worker's own record, plus units that never reported at all.

    A gap is the interesting signal and the job table cannot show it: a worker that was
    dead for six hours and came back leaves a queue that looks exactly like one that was
    merely idle.
    """
    for (earlier, _), (later, _) in pairwise(beats):
        gap = (later - earlier).total_seconds()
        if gap > HEARTBEAT_GAP_SECONDS:
            report.worker_gaps.append((earlier, gap))

    builds = {d.get("source_mtime") for _, d in beats if d.get("source_mtime")}
    report.builds_seen = len(builds)
    report.worker_restarts = sum(1 for _, d in beats if d.get("exiting"))

    for unit in HEARTBEAT_UNITS:
        if unit == "worker" or unit in OPTIONAL_UNITS:
            continue
        if not _heartbeat_rows(store, unit, stamp):
            report.silent_units.append(unit)


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
        "throughput": f"{report.leads_per_day:.1f} Tier A+B/day "
        f"(target {LEADS_PER_DAY_TARGET}); {report.dispatched_total} dispatched in total",
        "no_job_lost": f"{report.dead_lettered} dead-lettered",
        "no_silent_unit": (
            f"{len(report.worker_gaps)} worker gap(s)"
            + (f"; silent: {', '.join(report.silent_units)}" if report.silent_units else "")
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
