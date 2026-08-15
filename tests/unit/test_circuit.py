"""Circuit breaker behaviour.

The requirement being defended: the pipeline must never crash-loop because one
source is down. Time is injected, so nothing here sleeps.
"""

from __future__ import annotations

import pytest

from cindraleads.sources.circuit import CircuitBreaker, CircuitOpen, SourceBreakers


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker("serpapi", failure_threshold=3, open_seconds=900.0, clock=clock)


# ------------------------------------------------------------------- closed


def test_starts_closed_and_allows(breaker: CircuitBreaker):
    assert breaker.state == "closed"
    assert breaker.allow() is True
    breaker.check()  # does not raise


def test_failures_below_the_threshold_keep_it_closed(breaker: CircuitBreaker):
    breaker.record_failure("500")
    breaker.record_failure("500")
    assert breaker.state == "closed"
    assert breaker.allow() is True


def test_a_success_resets_the_consecutive_counter(breaker: CircuitBreaker):
    """The threshold is CONSECUTIVE failures. Two failures, a success, then two more
    is a flaky source, not a dead one, and must not trip."""
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"


# --------------------------------------------------------------------- open


def test_three_consecutive_failures_open_the_circuit(breaker: CircuitBreaker):
    for _ in range(3):
        breaker.record_failure("connection refused")
    assert breaker.state == "open"
    assert breaker.allow() is False


def test_open_circuit_fails_fast_without_calling(breaker: CircuitBreaker):
    for _ in range(3):
        breaker.record_failure()
    with pytest.raises(CircuitOpen) as exc:
        breaker.check()
    assert exc.value.source_id == "serpapi"
    assert exc.value.retry_after_seconds > 0


def test_retry_after_counts_down(breaker: CircuitBreaker, clock: FakeClock):
    for _ in range(3):
        breaker.record_failure()
    assert breaker.retry_after() == pytest.approx(900.0)
    clock.advance(300)
    assert breaker.retry_after() == pytest.approx(600.0)


# ---------------------------------------------------------------- half open


def test_cooldown_expiry_moves_to_half_open(breaker: CircuitBreaker, clock: FakeClock):
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "open"
    clock.advance(900)
    assert breaker.state == "half_open"


def test_half_open_admits_exactly_one_probe(breaker: CircuitBreaker, clock: FakeClock):
    """A naive breaker lets the whole backlog through the instant the cooldown
    expires, so a still-dead source is hammered and takes N timeouts to re-open."""
    for _ in range(3):
        breaker.record_failure()
    clock.advance(900)

    assert breaker.allow() is True, "first caller probes"
    assert breaker.allow() is False, "everyone else still fails fast"
    assert breaker.allow() is False


def test_a_successful_probe_closes_the_circuit(breaker: CircuitBreaker, clock: FakeClock):
    for _ in range(3):
        breaker.record_failure()
    clock.advance(900)
    breaker.allow()
    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.allow() is True
    assert breaker.allow() is True, "no longer limited to one probe"


def test_a_failed_probe_reopens_for_a_full_cooldown(breaker: CircuitBreaker, clock: FakeClock):
    """Re-opening for a fraction of the cooldown would let a dead source be probed
    repeatedly in quick succession."""
    for _ in range(3):
        breaker.record_failure()
    clock.advance(900)
    breaker.allow()
    breaker.record_failure("still dead")

    assert breaker.state == "open"
    assert breaker.retry_after() == pytest.approx(900.0)


def test_probe_slot_is_released_after_the_outcome(breaker: CircuitBreaker, clock: FakeClock):
    """If the slot leaked, a crashed probe would wedge the breaker shut forever."""
    for _ in range(3):
        breaker.record_failure()
    clock.advance(900)
    breaker.allow()
    breaker.record_failure()
    clock.advance(900)
    assert breaker.allow() is True, "a new probe must be admitted after the next cooldown"


# ------------------------------------------------------------------- isolation


def test_one_dead_source_does_not_affect_another(clock: FakeClock):
    """The whole point: SerpAPI being down must not stop crt.sh."""
    breakers = SourceBreakers(failure_threshold=3, open_seconds=900.0, clock=clock)
    dead = breakers.for_source("serpapi")
    healthy = breakers.for_source("crtsh")

    for _ in range(3):
        dead.record_failure()

    assert dead.state == "open"
    assert healthy.state == "closed"
    assert healthy.allow() is True
    assert breakers.open_circuits() == ["serpapi"]


def test_breakers_are_created_once_per_source(clock: FakeClock):
    breakers = SourceBreakers(clock=clock)
    assert breakers.for_source("a") is breakers.for_source("a")
    assert breakers.for_source("a") is not breakers.for_source("b")


# --------------------------------------------------------------------- ops


def test_manual_reset_closes_the_circuit(breaker: CircuitBreaker):
    for _ in range(3):
        breaker.record_failure()
    breaker.reset()
    assert breaker.state == "closed"
    assert breaker.allow() is True


def test_snapshot_reports_what_ops_needs(breaker: CircuitBreaker):
    breaker.record_success()
    breaker.record_failure("boom")
    snap = breaker.snapshot()
    assert snap["source_id"] == "serpapi"
    assert snap["state"] == "closed"
    assert snap["total_successes"] == 1
    assert snap["total_failures"] == 1
    assert snap["consecutive_failures"] == 1


def test_rejections_are_counted_separately_from_failures(breaker: CircuitBreaker):
    """A rejected call is not evidence the source is broken — we never asked it."""
    for _ in range(3):
        breaker.record_failure()
    for _ in range(5):
        with pytest.raises(CircuitOpen):
            breaker.check()
    snap = breaker.snapshot()
    assert snap["total_rejected"] == 5
    assert snap["total_failures"] == 3
