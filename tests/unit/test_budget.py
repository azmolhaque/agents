"""Rationed-resource guards.

The behaviours that matter: the cap survives a restart, exhaustion degrades rather
than crashes, and two workers cannot both spend the last credit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cindraleads.budget import BudgetGuard
from cindraleads.errors import BudgetExhausted
from cindraleads.store import Store

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


@pytest.fixture
def guard(store):  # type: ignore[no-untyped-def]
    return BudgetGuard(store, "serpapi", cap=100, safety_fraction=0.85)


# ------------------------------------------------------------------ spending


def test_spends_accumulate(guard: BudgetGuard):
    assert guard.used() == 0
    guard.try_spend(3)
    guard.try_spend(2)
    assert guard.used() == 5


def test_stops_at_the_soft_cap_not_the_hard_cap(guard: BudgetGuard):
    """85% leaves headroom for something urgent later in the day."""
    allowed = sum(1 for _ in range(200) if guard.try_spend(1))
    assert allowed == 85
    assert guard.used() == 85
    assert guard.soft_cap == 85.0


def test_exhaustion_returns_false_rather_than_raising(guard: BudgetGuard):
    """The pipeline must degrade, never crash, when a budget runs out."""
    while guard.try_spend(1):
        pass
    assert guard.try_spend(1) is False


def test_spend_raises_for_callers_that_want_an_assertion(guard: BudgetGuard):
    while guard.try_spend(1):
        pass
    with pytest.raises(BudgetExhausted, match="serpapi"):
        guard.spend(1)


def test_a_single_oversized_request_is_refused_not_partially_filled(guard: BudgetGuard):
    assert guard.try_spend(500) is False
    assert guard.used() == 0


def test_check_and_write_are_one_transaction(guard: BudgetGuard, store):
    """Two workers racing must not both see room for the last credit. The read and
    the write happen inside one transaction, so the second serializes behind it."""
    guard.try_spend(84)
    with store.tx() as conn:
        assert guard.try_spend(1, conn=conn) is True
        assert guard.try_spend(1, conn=conn) is False
    assert guard.used() == 85


# ------------------------------------------------------------- persistence


def test_the_budget_survives_a_restart(tmp_path: Path):
    """A guard that resets when the worker crashes is not a guard, and on a Pi the
    worker will crash."""
    db = tmp_path / "budget.db"
    first = Store(db, migrations_dir=MIGRATIONS)
    first.migrate()
    BudgetGuard(first, "serpapi", cap=100).try_spend(40)
    first.close()

    second = Store(db, migrations_dir=MIGRATIONS)
    reopened = BudgetGuard(second, "serpapi", cap=100)
    assert reopened.used() == 40
    second.close()


def test_providers_are_isolated(store):
    serp = BudgetGuard(store, "serpapi", cap=100)
    cloud = BudgetGuard(store, "anthropic", cap=100)
    serp.try_spend(50)
    assert serp.used() == 50
    assert cloud.used() == 0


# ----------------------------------------------------------- rolling window


def test_usage_outside_the_window_no_longer_counts(store):
    """The bucket refills continuously. An hourly harvest against a naive daily
    counter is spent by mid-morning; a rolling window spreads the same quota."""
    guard = BudgetGuard(store, "serpapi", cap=100, window_hours=24)
    guard.try_spend(50)
    assert guard.used() == 50

    # A row stamped outside the window is invisible to the current balance.
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO api_budget (budget_id, provider, day, units_used, units_cap, "
            "usd_spent, updated_at) VALUES ('old','serpapi','2000-01-01T00',999,100,0,'t')"
        )
    assert guard.used() == 50


def test_purge_removes_only_expired_rows(store):
    guard = BudgetGuard(store, "serpapi", cap=100)
    guard.try_spend(10)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO api_budget (budget_id, provider, day, units_used, units_cap, "
            "usd_spent, updated_at) VALUES ('old','serpapi','2000-01-01T00',5,100,0,'t')"
        )
    assert guard.purge_expired() == 1
    assert guard.used() == 10


# ------------------------------------------------------------------- quota


def test_cap_is_adopted_from_the_provider_not_hardcoded(guard: BudgetGuard):
    """sources.yaml sets discover_quota_at_startup precisely so the cap is the
    account's real plan rather than a guess that silently diverges."""
    guard.set_cap(250)
    assert guard.cap == 250
    assert guard.soft_cap == pytest.approx(212.5)


def test_a_nonsense_cap_is_rejected(guard: BudgetGuard):
    with pytest.raises(ValueError, match="must be positive"):
        guard.set_cap(0)


# -------------------------------------------------------------- cloud usd


def test_cloud_budget_has_no_headroom_fraction(store):
    """For SerpAPI the soft cap leaves room for urgent work. For money, the
    configured cap IS the hard stop."""
    cloud = BudgetGuard.for_cloud(store, daily_usd_cap=0.50)
    assert cloud.safety_fraction == 1.0
    assert cloud.soft_cap == pytest.approx(0.50)

    assert cloud.try_spend(0.40, usd=0.40) is True
    assert cloud.can_spend(0.20) is False
    assert cloud.try_spend(0.10, usd=0.10) is True
    assert cloud.usd_spent() == pytest.approx(0.50)


def test_cloud_exhaustion_is_the_signal_the_llm_ladder_reads(store):
    """StructuredLLM takes can_escalate=guard.can_spend; False degrades to
    local-only with a warning rather than raising."""
    cloud = BudgetGuard.for_cloud(store, daily_usd_cap=0.50)
    cloud.try_spend(0.50, usd=0.50)
    assert cloud.can_spend(0.01) is False


def test_status_is_printable_for_ops(guard: BudgetGuard):
    guard.try_spend(42)
    status = guard.status()
    assert status.used == 42
    assert status.remaining == 58
    assert status.fraction_used == pytest.approx(0.42)
    assert status.exhausted is False
    assert "serpapi" in str(status)
