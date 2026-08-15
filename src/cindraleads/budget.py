"""Rationed-resource guards: SerpAPI credits and cloud USD.

Two budgets, one mechanism. Both are persisted in SQLite because both must survive a
restart — a guard that resets when the worker crashes is not a guard, and on a Pi the
worker will crash.

**Why a token bucket rather than a daily counter.** The harvest timer fires hourly. A
naive "N searches per day" counter is spent by mid-morning, so the afternoon and the
whole night harvest nothing, and any genuinely urgent query in the evening finds an
empty account. A bucket that refills continuously across a 24 h window spends the same
quota evenly and always leaves something for later.

**On exhaustion the pipeline degrades; it never crashes.** Callers get ``False`` from
:meth:`try_spend` (or :class:`BudgetExhausted` from :meth:`spend`) and fall back to
local-only or skip the source, having logged a warning to the ops channel.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from cindraleads.errors import BudgetExhausted
from cindraleads.logging import get_logger
from cindraleads.models import to_iso, utcnow
from cindraleads.store import Store

__all__ = ["BudgetGuard", "BudgetStatus"]

log = get_logger("cindraleads.budget")


@dataclass(frozen=True)
class BudgetStatus:
    provider: str
    used: float
    cap: float
    usd_spent: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.cap - self.used)

    @property
    def fraction_used(self) -> float:
        return (self.used / self.cap) if self.cap > 0 else 1.0

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def __str__(self) -> str:
        return (
            f"{self.provider}: {self.used:.2f}/{self.cap:.2f} "
            f"({self.fraction_used * 100:.0f}%), ${self.usd_spent:.4f} spent"
        )


class BudgetGuard:
    """A persisted, continuously-refilling allowance for one provider.

    The bucket is modelled as spend-per-window rather than as a literal token count:
    usage older than ``window_hours`` no longer counts against the cap. That is the
    same thing as a bucket refilling at ``cap / window``, and it needs one row and no
    background timer — which matters on a box that loses power.
    """

    def __init__(
        self,
        store: Store,
        provider: str,
        *,
        cap: float,
        window_hours: float = 24.0,
        safety_fraction: float = 0.85,
    ) -> None:
        self.store = store
        self.provider = provider
        self.cap = float(cap)
        self.window_hours = window_hours
        # Stop at 85% and warn, leaving headroom for anything urgent later in the day.
        self.safety_fraction = safety_fraction

    # ---------------------------------------------------------------- reads

    def _window_start(self, now: datetime | None = None) -> str:
        return to_iso((now or utcnow()) - timedelta(hours=self.window_hours))

    def used(self, *, conn: sqlite3.Connection | None = None) -> float:
        """Units spent inside the rolling window."""
        active = conn or self.store.conn
        row = active.execute(
            "SELECT COALESCE(SUM(units_used), 0) AS u FROM api_budget "
            "WHERE provider = ? AND day >= ?",
            (self.provider, self._window_start()),
        ).fetchone()
        return float(row["u"])

    def usd_spent(self, *, conn: sqlite3.Connection | None = None) -> float:
        active = conn or self.store.conn
        row = active.execute(
            "SELECT COALESCE(SUM(usd_spent), 0) AS s FROM api_budget "
            "WHERE provider = ? AND day >= ?",
            (self.provider, self._window_start()),
        ).fetchone()
        return float(row["s"])

    def status(self) -> BudgetStatus:
        return BudgetStatus(
            provider=self.provider,
            used=self.used(),
            cap=self.cap,
            usd_spent=self.usd_spent(),
        )

    @property
    def soft_cap(self) -> float:
        return self.cap * self.safety_fraction

    def would_exceed_soft_cap(self, units: float = 1.0) -> bool:
        return (self.used() + units) > self.soft_cap

    # --------------------------------------------------------------- spends

    def try_spend(
        self,
        units: float = 1.0,
        *,
        usd: float = 0.0,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """Reserve ``units`` if the soft cap allows. Returns whether it succeeded.

        The check and the write happen in one transaction: two workers racing must not
        both see room for the last credit. Spending against the *soft* cap is the point
        — the hard cap is a wall we should never reach.
        """
        now = utcnow()
        bucket = to_iso(now)[:13]  # hour-resolution rows keep the table small

        def _attempt(active: sqlite3.Connection) -> bool:
            current = self.used(conn=active)
            if current + units > self.soft_cap:
                log.warning(
                    "budget_soft_cap_reached",
                    provider=self.provider,
                    used=round(current, 3),
                    soft_cap=round(self.soft_cap, 3),
                    cap=self.cap,
                    requested=units,
                )
                return False
            active.execute(
                "INSERT INTO api_budget (budget_id, provider, day, units_used, units_cap, "
                "usd_spent, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(provider, day) DO UPDATE SET "
                "  units_used = units_used + excluded.units_used,"
                "  usd_spent  = usd_spent  + excluded.usd_spent,"
                "  updated_at = excluded.updated_at",
                (
                    f"{self.provider}:{bucket}",
                    self.provider,
                    bucket,
                    units,
                    self.cap,
                    usd,
                    to_iso(now),
                ),
            )
            return True

        if conn is not None:
            return _attempt(conn)
        with self.store.tx() as owned:
            return _attempt(owned)

    def spend(
        self, units: float = 1.0, *, usd: float = 0.0, conn: sqlite3.Connection | None = None
    ) -> None:
        """:meth:`try_spend` as an assertion."""
        if not self.try_spend(units, usd=usd, conn=conn):
            raise BudgetExhausted(
                f"{self.provider} budget exhausted: {self.used():.2f}/{self.soft_cap:.2f} "
                f"used in the last {self.window_hours:g}h"
            )

    def can_spend(self, units: float = 1.0) -> bool:
        """Check without reserving. Inherently racy — prefer ``try_spend``."""
        return not self.would_exceed_soft_cap(units)

    # ----------------------------------------------------------- maintenance

    def set_cap(self, cap: float) -> None:
        """Adopt the real quota, discovered from the provider at startup.

        sources.yaml sets ``discover_quota_at_startup: true`` precisely so this is not
        a hardcoded guess that silently diverges from the account's actual plan.
        """
        if cap <= 0:
            raise ValueError(f"cap must be positive, got {cap}")
        previous, self.cap = self.cap, float(cap)
        if previous != self.cap:
            log.info("budget_cap_updated", provider=self.provider, was=previous, now=self.cap)

    def purge_expired(self) -> int:
        """Drop rows outside the window. Called by the nightly maintenance job."""
        with self.store.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM api_budget WHERE provider = ? AND day < ?",
                (self.provider, self._window_start()),
            )
            return int(cursor.rowcount)

    @classmethod
    def for_cloud(cls, store: Store, *, daily_usd_cap: float) -> BudgetGuard:
        """The cloud escalation budget.

        safety_fraction is 1.0 here: the configured USD cap IS the hard stop, not a
        number to leave headroom under. Wire ``can_escalate=guard.can_spend`` into
        StructuredLLM and exhaustion degrades to local-only.
        """
        return cls(
            store,
            "anthropic",
            cap=daily_usd_cap,
            window_hours=24.0,
            safety_fraction=1.0,
        )
