"""Exception hierarchy.

Everything the pipeline raises deliberately descends from :class:`CindraError`, so a
stage runner can distinguish "our code decided to stop" from "something unexpected
blew up" without catching bare ``Exception``.

``PassiveOnlyViolation`` deliberately does NOT live here — it lands in ``passive.py``
in Phase 4 alongside the enforcement code and the forbidden-binary denylist, so the
rule and its exception cannot drift apart.
"""

from __future__ import annotations

__all__ = [
    "BudgetExhausted",
    "CindraError",
    "ConfigError",
    "JobNotFound",
    "LeaseLost",
    "MigrationError",
    "QueueError",
    "SchemaValidationError",
    "StoreError",
]


class CindraError(Exception):
    """Base class for every deliberate failure in the pipeline."""


class ConfigError(CindraError):
    """Configuration is missing, malformed, or internally inconsistent."""


class StoreError(CindraError):
    """SQLite refused to do something we expected to work."""


class MigrationError(StoreError):
    """A migration is missing, misnamed, or failed to apply."""


class QueueError(CindraError):
    """The durable job queue was asked for something impossible."""


class JobNotFound(QueueError):
    """No job with that id exists."""


class LeaseLost(QueueError):
    """A worker tried to finish a job whose lease it no longer holds.

    This is the signal that a job was reclaimed underneath us — most likely because
    the worker stalled past ``lease_expires_at`` and another worker picked the job
    up. The correct response is to abandon the work, never to force the write.
    """


class SchemaValidationError(CindraError):
    """An LLM response failed JSON Schema or Pydantic validation.

    Phase 1 wires this into the retry -> escalate -> dead-letter ladder.
    """


class BudgetExhausted(CindraError):
    """A rationed resource (SerpAPI credits, cloud USD) hit its cap.

    Callers degrade; they never crash. See PLAN.md Phase 2 and the daily USD cap.
    """
