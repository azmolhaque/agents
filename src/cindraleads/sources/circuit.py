"""Per-source circuit breaker.

The rule this exists to satisfy: **the pipeline must never crash-loop because one
source is down.** Without a breaker, a dead source is retried by every job that
touches it, each paying a full timeout, until the queue is nothing but doomed work.

Three states, the standard shape:

    closed     normal. Failures counted; N consecutive failures open the circuit.
    open       fail fast without making the call. After a cooldown, go half-open.
    half_open  let exactly ONE request through as a probe. Success closes the
               circuit; failure re-opens it for another full cooldown.

Two details that matter more than they look:

* **A success in ``closed`` resets the counter to zero.** The threshold is
  *consecutive* failures. Three scattered failures across a healthy day are noise;
  three in a row is a broken source.
* **``half_open`` admits one caller, not all of them.** A naive implementation lets
  every waiting request through the instant the cooldown expires, so a still-dead
  source gets hammered by the whole backlog and takes N timeouts to re-open.

Time is injected so the tests do not sleep.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from cindraleads.errors import CindraError
from cindraleads.logging import get_logger

__all__ = ["CircuitBreaker", "CircuitOpen", "CircuitState", "SourceBreakers"]

log = get_logger("cindraleads.circuit")

CircuitState = Literal["closed", "open", "half_open"]


class CircuitOpen(CindraError):
    """The circuit is open; the call was not attempted.

    Distinct from a source failure on purpose. The caller should reschedule rather
    than count this as evidence the source is broken — we never asked it.
    """

    def __init__(self, source_id: str, retry_after_seconds: float) -> None:
        self.source_id = source_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"circuit open for {source_id!r}; retry in {retry_after_seconds:.0f}s")


@dataclass
class CircuitBreaker:
    """One source's breaker."""

    source_id: str
    failure_threshold: int = 3
    open_seconds: float = 900.0
    clock: Callable[[], float] = time.monotonic

    _state: CircuitState = "closed"
    _consecutive_failures: int = 0
    _opened_at: float = 0.0
    _probe_in_flight: bool = False
    total_failures: int = 0
    total_successes: int = 0
    total_rejected: int = 0

    @property
    def state(self) -> CircuitState:
        """Current state, after accounting for an elapsed cooldown."""
        if self._state == "open" and self._cooldown_elapsed():
            return "half_open"
        return self._state

    def _cooldown_elapsed(self) -> bool:
        return (self.clock() - self._opened_at) >= self.open_seconds

    def retry_after(self) -> float:
        if self._state != "open":
            return 0.0
        return max(0.0, self.open_seconds - (self.clock() - self._opened_at))

    # ------------------------------------------------------------- decisions

    def allow(self) -> bool:
        """May a call proceed right now? Admits exactly one probe in half-open."""
        current = self.state
        if current == "closed":
            return True
        if current == "half_open":
            if self._probe_in_flight:
                # Another caller is already probing. Everyone else keeps failing
                # fast rather than stampeding a source that may still be dead.
                return False
            self._state = "half_open"
            self._probe_in_flight = True
            return True
        return False

    def check(self) -> None:
        """``allow()`` as an assertion. Raises :class:`CircuitOpen` if shut."""
        if not self.allow():
            self.total_rejected += 1
            raise CircuitOpen(self.source_id, self.retry_after())

    # -------------------------------------------------------------- outcomes

    def record_success(self) -> None:
        self.total_successes += 1
        was = self._state
        self._consecutive_failures = 0
        self._probe_in_flight = False
        self._state = "closed"
        if was != "closed":
            log.info("circuit_closed", source_id=self.source_id, previous_state=was)

    def record_failure(self, error: str = "") -> None:
        self.total_failures += 1
        self._probe_in_flight = False

        if self._state == "half_open" or (self._state == "open" and self._cooldown_elapsed()):
            # The probe failed. Full cooldown again rather than an immediate retry.
            self._open(error, reason="probe_failed")
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open(error, reason="threshold_reached")

    def _open(self, error: str, *, reason: str) -> None:
        self._state = "open"
        self._opened_at = self.clock()
        self._probe_in_flight = False
        log.warning(
            "circuit_opened",
            source_id=self.source_id,
            reason=reason,
            consecutive_failures=self._consecutive_failures,
            open_seconds=self.open_seconds,
            error=error[:200],
        )

    def reset(self) -> None:
        """Manual close, for the ops CLI. Does not clear the lifetime counters."""
        self._state = "closed"
        self._consecutive_failures = 0
        self._probe_in_flight = False

    def snapshot(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "state": self.state,
            "consecutive_failures": self._consecutive_failures,
            "retry_after_seconds": round(self.retry_after(), 1),
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_rejected": self.total_rejected,
        }


@dataclass
class SourceBreakers:
    """One breaker per source, created on first use."""

    failure_threshold: int = 3
    open_seconds: float = 900.0
    clock: Callable[[], float] = time.monotonic
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def for_source(self, source_id: str) -> CircuitBreaker:
        breaker = self._breakers.get(source_id)
        if breaker is None:
            breaker = CircuitBreaker(
                source_id=source_id,
                failure_threshold=self.failure_threshold,
                open_seconds=self.open_seconds,
                clock=self.clock,
            )
            self._breakers[source_id] = breaker
        return breaker

    def open_circuits(self) -> list[str]:
        return sorted(sid for sid, b in self._breakers.items() if b.state != "closed")

    def snapshot(self) -> list[dict[str, object]]:
        return [b.snapshot() for b in sorted(self._breakers.values(), key=lambda b: b.source_id)]
