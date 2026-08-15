"""Thermal governor policy.

The whole point of injecting readings is that the policy can be tested at 85 C on a
machine that will never reach 85 C.
"""

from __future__ import annotations

import pytest

from cindraleads.thermal import (
    ThermalGovernor,
    ThermalReading,
    parse_throttled,
    parse_vcgencmd_temp,
)


def gov(*temps: float, initial: str = "nominal") -> ThermalGovernor:
    """A governor fed a fixed sequence of temperatures."""
    readings = iter([ThermalReading(temp_c=t) for t in temps])
    return ThermalGovernor(lambda: next(readings), initial_state=initial)  # type: ignore[arg-type]


# ------------------------------------------------------------------- parsing


def test_parse_temp():
    assert parse_vcgencmd_temp("temp=53.0'C\n") == 53.0
    assert parse_vcgencmd_temp("temp=81.2'C") == 81.2
    assert parse_vcgencmd_temp("") is None
    assert parse_vcgencmd_temp("garbage") is None


def test_parse_throttled_all_clear():
    flags = parse_throttled("throttled=0x0")
    assert flags["currently_throttled"] is False
    assert flags["throttling_occurred"] is False


def test_parse_throttled_decodes_active_and_historic_bits():
    # 0x50005 = under-voltage now + throttled now + both "occurred since boot".
    flags = parse_throttled("throttled=0x50005")
    assert flags["under_voltage_now"] is True
    assert flags["currently_throttled"] is True
    assert flags["under_voltage_occurred"] is True
    assert flags["throttling_occurred"] is True
    assert flags["arm_frequency_capped_now"] is False


def test_historic_flags_alone_do_not_mean_throttled_now():
    """0x50000 is 'this happened earlier'. Treating it as current would permanently
    pin the Pi to one worker after a single power blip."""
    reading = ThermalReading(temp_c=45.0, flags=parse_throttled("throttled=0x50000"))
    assert reading.throttled_now is False
    assert reading.throttled_ever is True


# -------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    ("temp", "state", "workers", "allow_llm", "allow_batch"),
    [
        (45.0, "nominal", 4, True, True),
        (69.9, "nominal", 4, True, True),
        (70.0, "warm", 2, True, False),
        (75.0, "warm", 2, True, False),
        (78.0, "hot", 2, False, False),
        (85.0, "hot", 2, False, False),
    ],
)
def test_policy_bands(temp, state, workers, allow_llm, allow_batch):
    policy = gov(temp).poll()
    assert policy.state == state
    assert policy.max_workers == workers
    assert policy.allow_llm is allow_llm
    assert policy.allow_llm_batch is allow_batch


def test_hot_still_permits_network_io():
    """Above 78 C we stop inferring but keep fetching — network work is nearly free
    thermally, and stopping it would starve the queue for no benefit."""
    policy = gov(80.0).poll()
    assert policy.allow_llm is False
    assert policy.network_only is True
    assert policy.max_workers >= 1


def test_throttle_flag_overrides_a_cool_reading():
    """If the SoC says it is being held back, believe it regardless of temperature —
    under-voltage throttles at room temperature and would otherwise go unnoticed."""
    governor = ThermalGovernor(
        lambda: ThermalReading(temp_c=42.0, flags=parse_throttled("throttled=0x4"))
    )
    policy = governor.poll()
    assert policy.state == "throttled"
    assert policy.max_workers == 1
    assert policy.alert_level == "critical"
    assert policy.allow_llm is False


# ---------------------------------------------------------------- hysteresis


def test_cooling_below_a_boundary_does_not_immediately_relax():
    """69.5 C right after 71 C must not snap back to 4 workers; without hysteresis a
    load sitting on the boundary flaps every poll."""
    governor = gov(71.0, 69.5)
    assert governor.poll().state == "warm"
    assert governor.poll().state == "warm"


def test_real_cooling_does_relax():
    governor = gov(71.0, 60.0)
    assert governor.poll().state == "warm"
    assert governor.poll().state == "nominal"


def test_heating_is_immediate_not_hysteretic():
    """Cooling is cautious; heating is not. Waiting to react to a rising SoC is how
    you get a throttle event."""
    governor = gov(50.0, 78.5)
    assert governor.poll().state == "nominal"
    assert governor.poll().state == "hot"


# ------------------------------------------------------------ missing sensor


def test_missing_sensor_holds_state_rather_than_assuming_cool():
    """vcgencmd needs the `video` group. If it is missing, a governor that defaulted
    to 'nominal' would run 4 workers on a Pi it cannot see the temperature of."""
    governor = gov(80.0)
    assert governor.poll().state == "hot"
    blind = ThermalGovernor(lambda: ThermalReading(temp_c=None), initial_state="hot")
    assert blind.poll().state == "hot"
    assert blind.policy.allow_llm is False


def test_blind_governor_starting_nominal_stays_conservative_after_throttle():
    readings = iter(
        [
            ThermalReading(temp_c=42.0, flags=parse_throttled("throttled=0x4")),
            ThermalReading(temp_c=None),
        ]
    )
    governor = ThermalGovernor(lambda: next(readings))
    assert governor.poll().state == "throttled"
    # Sensor vanishes right after a throttle: degrade to 'hot', never to 'nominal'.
    assert governor.poll().state == "hot"


# -------------------------------------------------------------------- output


def test_snapshot_is_serializable_and_complete():
    governor = ThermalGovernor(
        lambda: ThermalReading(
            temp_c=72.5, flags=parse_throttled("throttled=0x50000"), available_ram_mb=9000.0
        )
    )
    governor.poll()
    snap = governor.snapshot()
    assert snap["state"] == "warm"
    assert snap["temp_c"] == 72.5
    assert snap["throttled_now"] is False
    assert snap["throttled_ever"] is True
    assert snap["max_workers"] == 2
    assert snap["available_ram_mb"] == 9000.0
    assert isinstance(snap["reason"], str)
