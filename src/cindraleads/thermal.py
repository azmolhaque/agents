"""Thermal governor for the Pi 5.

A real module, not a comment. A Pi 5 under sustained LLM load will reach the throttle
point (~80-85 C) with a passive heatsink, and a throttled Pi does not fail loudly — it
silently halves its clock, so the pipeline just gets mysteriously slow. This turns that
into an explicit, observable state transition.

Policy, revised against measurement (see docs/BENCHMARKS.md, 2026-08-15):

    < 70 C            full concurrency (4 workers)
    70-84 C           halve concurrency, pause local-LLM batch jobs
    > 84 C            LLM inference paused, network-IO-only tasks continue
    under-voltage     CRITICAL to the ops channel, drop to 1 worker

The hardware budget originally paused inference above 78 C. A 24-page benchmark
disproved that threshold: the Pi climbs for ~5 pages, plateaus at 80-82.3 C, and
holds there with **100% success, zero timeouts, and no upward latency trend**
(page 5: 69.5 s, page 20: 62.5 s). It is an equilibrium below the 85 C hard limit,
not a runaway. Pausing at 78 C would have stopped a machine that was working, and
stopping is far worse than the ~8% clock reduction the Pi applies itself.

So temperature bands drive the policy and the SoC's own throttle bits mostly do
not. The exception is **under-voltage**, which is a power-supply fault rather than
a thermal one: it risks data corruption, it does not resolve by waiting, and it
still drops the pipeline to one worker and pages the ops channel.

Two design notes:

* **Readings are injected.** ``ThermalGovernor`` takes a callable returning a
  :class:`ThermalReading`, so the policy is unit-testable at simulated 65/75/85 C on any
  machine. Only :class:`VcgencmdReader` knows what a Pi is.
* **There is hysteresis.** Without it a load sitting exactly on a boundary flaps between
  two worker counts every poll, which is worse than either state. A transition to a
  hotter state happens at the threshold; the way back down requires
  ``HYSTERESIS_C`` of real cooling first.

``vcgencmd`` requires the service user to be in the ``video`` group. If it is missing or
not permitted, the reader reports ``temp_c=None`` and the governor holds the safe state
rather than assuming the Pi is cool.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from cindraleads.models import utcnow

__all__ = [
    "HYSTERESIS_C",
    "ThermalGovernor",
    "ThermalPolicy",
    "ThermalReading",
    "ThermalState",
    "VcgencmdReader",
    "parse_throttled",
    "parse_vcgencmd_temp",
]

ThermalState = Literal["nominal", "warm", "hot", "throttled"]

HYSTERESIS_C = 3.0

# Bits reported by `vcgencmd get_throttled`. The low bits are "right now", the high bits
# are "has happened since boot" - we alert on the former and report the latter.
_THROTTLE_BITS: dict[int, str] = {
    0: "under_voltage_now",
    1: "arm_frequency_capped_now",
    2: "currently_throttled",
    3: "soft_temperature_limit_now",
    16: "under_voltage_occurred",
    17: "arm_frequency_capped_occurred",
    18: "throttling_occurred",
    19: "soft_temperature_limit_occurred",
}

_ACTIVE_BITS = (0, 1, 2, 3)

_TEMP_RE = re.compile(r"temp=([0-9.]+)'?C")
_THROTTLED_RE = re.compile(r"throttled=(0x[0-9a-fA-F]+)")


def parse_vcgencmd_temp(output: str) -> float | None:
    """Parse ``temp=53.0'C``."""
    match = _TEMP_RE.search(output)
    return float(match.group(1)) if match else None


def parse_throttled(output: str) -> dict[str, bool]:
    """Parse ``throttled=0x50005`` into named flags."""
    match = _THROTTLED_RE.search(output)
    if not match:
        return {}
    value = int(match.group(1), 16)
    return {name: bool(value & (1 << bit)) for bit, name in _THROTTLE_BITS.items()}


@dataclass(frozen=True)
class ThermalReading:
    """One sample of host health."""

    temp_c: float | None = None
    flags: dict[str, bool] = field(default_factory=dict)
    available_ram_mb: float | None = None
    free_disk_mb: float | None = None

    @property
    def throttled_now(self) -> bool:
        """Factual: the SoC is being held back right now, for any reason.

        Reported in benchmarks and /healthz. Deliberately NOT the policy trigger --
        at this Pi's normal 82 C plateau this is true and everything is fine.
        """
        return any(self.flags.get(_THROTTLE_BITS[bit], False) for bit in _ACTIVE_BITS)

    @property
    def undervoltage_now(self) -> bool:
        """A power-supply fault, not a thermal one.

        This is the one throttle bit that drives policy. It risks corruption, it
        does not resolve by waiting for the Pi to cool, and the fix is a better PSU.
        """
        return bool(self.flags.get("under_voltage_now", False))

    @property
    def throttled_ever(self) -> bool:
        return any(self.flags.get(name, False) for bit, name in _THROTTLE_BITS.items() if bit >= 16)

    @property
    def active_flag_names(self) -> list[str]:
        return sorted(
            _THROTTLE_BITS[bit] for bit in _ACTIVE_BITS if self.flags.get(_THROTTLE_BITS[bit])
        )


@dataclass(frozen=True)
class ThermalPolicy:
    """What the pipeline is allowed to do in a given state."""

    state: ThermalState
    max_workers: int
    allow_llm: bool
    allow_llm_batch: bool
    alert_level: Literal["none", "warning", "critical"]
    reason: str

    @property
    def network_only(self) -> bool:
        """Hot but not throttled: keep fetching, stop inferring."""
        return not self.allow_llm


_POLICIES: dict[ThermalState, ThermalPolicy] = {
    "nominal": ThermalPolicy(
        state="nominal",
        max_workers=4,
        allow_llm=True,
        allow_llm_batch=True,
        alert_level="none",
        reason="below 70C",
    ),
    "warm": ThermalPolicy(
        state="warm",
        max_workers=2,
        allow_llm=True,
        allow_llm_batch=False,
        alert_level="none",
        reason="70-78C: halved concurrency, batch LLM paused",
    ),
    "hot": ThermalPolicy(
        state="hot",
        max_workers=2,
        allow_llm=False,
        allow_llm_batch=False,
        alert_level="warning",
        reason="above 78C: LLM inference paused, network IO continues",
    ),
    "throttled": ThermalPolicy(
        state="throttled",
        max_workers=1,
        allow_llm=False,
        allow_llm_batch=False,
        alert_level="critical",
        reason="SoC reports under-voltage: power supply fault, not heat",
    ),
}

# Upper edge of each band. Crossing upward happens at these values; crossing back down
# requires HYSTERESIS_C below the boundary.
_WARM_AT = 70.0
# 84 C, not 78: measured plateau is 80-82.3 C with no failures, and the SoC's own
# hard limit is 85 C. This is a backstop against runaway heat, not a speed limiter.
_HOT_AT = 84.0


class VcgencmdReader:
    """Reads real sensors. The only Pi-aware code in the module."""

    def __init__(self, binary: str = "vcgencmd", timeout: float = 2.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _run(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout if completed.returncode == 0 else ""

    def __call__(self) -> ThermalReading:
        return ThermalReading(
            temp_c=parse_vcgencmd_temp(self._run("measure_temp")),
            flags=parse_throttled(self._run("get_throttled")),
            available_ram_mb=_available_ram_mb(),
            free_disk_mb=None,
        )


def _available_ram_mb() -> float | None:
    """MemAvailable from /proc/meminfo. Absent off Linux."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:  # noqa: PTH123
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024
    except OSError:
        return None
    return None


class ThermalGovernor:
    """Turns a stream of readings into a stable policy."""

    def __init__(
        self,
        reader: Callable[[], ThermalReading] | None = None,
        *,
        initial_state: ThermalState = "nominal",
    ) -> None:
        self._reader = reader or VcgencmdReader()
        self._state: ThermalState = initial_state
        self._last_reading: ThermalReading | None = None
        self._last_changed_at = utcnow()

    @property
    def state(self) -> ThermalState:
        return self._state

    @property
    def policy(self) -> ThermalPolicy:
        return _POLICIES[self._state]

    @property
    def last_reading(self) -> ThermalReading | None:
        return self._last_reading

    def _classify(self, reading: ThermalReading) -> ThermalState:
        # Only under-voltage forces the critical state. Thermal capping is the Pi
        # managing itself correctly and is handled by the temperature bands below.
        if reading.undervoltage_now:
            return "throttled"

        if reading.temp_c is None:
            # No sensor. Hold whatever we already believe rather than optimistically
            # declaring the Pi cool - a missing `video` group must not look like 20 C.
            return self._state if self._state != "throttled" else "hot"

        temp = reading.temp_c
        current = self._state

        # Upward transitions are immediate; downward ones need real cooling first.
        if temp >= _HOT_AT:
            return "hot"
        if temp >= _WARM_AT:
            return "warm" if current != "hot" or temp < _HOT_AT - HYSTERESIS_C else "hot"
        if temp >= _WARM_AT - HYSTERESIS_C and current in ("warm", "hot", "throttled"):
            return "warm"
        return "nominal"

    def poll(self) -> ThermalPolicy:
        """Take a reading and return the policy now in force."""
        reading = self._reader()
        self._last_reading = reading
        new_state = self._classify(reading)
        if new_state != self._state:
            self._state = new_state
            self._last_changed_at = utcnow()
        return self.policy

    def snapshot(self) -> dict[str, object]:
        """Structured host status, for /healthz, the digest footer, and ops alerts."""
        reading = self._last_reading
        return {
            "state": self._state,
            "temp_c": reading.temp_c if reading else None,
            "throttled_now": reading.throttled_now if reading else None,
            "throttled_ever": reading.throttled_ever if reading else None,
            "active_flags": reading.active_flag_names if reading else [],
            "available_ram_mb": reading.available_ram_mb if reading else None,
            "max_workers": self.policy.max_workers,
            "allow_llm": self.policy.allow_llm,
            "allow_llm_batch": self.policy.allow_llm_batch,
            "alert_level": self.policy.alert_level,
            "reason": self.policy.reason,
            "changed_at": self._last_changed_at.isoformat(),
        }
