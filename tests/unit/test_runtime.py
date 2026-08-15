"""The runtime factory: one store, one HTTP client, one set of budget guards.

A second `EgressClient` would come with its own empty in-flight map and its own budget
guards, so two of them in one process means duplicate requests and a cap applied twice
independently. These tests pin the wiring that keeps there being exactly one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cindraleads.config import settings
from cindraleads.runtime import Runtime
from cindraleads.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"


@pytest.fixture
def rt(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "r.db", migrations_dir=MIGRATIONS)
    store.migrate()
    cfg = settings()
    object.__setattr__(cfg, "config_dir", REPO_ROOT / "config")
    object.__setattr__(cfg, "cache_dir", tmp_path / "cache")
    yield Runtime(store=store, config=cfg)
    store.close()


async def test_the_shipped_config_assembles(rt):
    async with rt as runtime:
        assert runtime.scout.templates
        assert runtime.harvester.egress is runtime.egress
        assert runtime.harvester.queue is runtime.queue


async def test_the_http_client_is_closed_on_exit(rt):
    async with rt as runtime:
        client = runtime._http
        assert client is not None
    assert client.is_closed, "sockets outlive the runtime otherwise"


async def test_the_serpapi_guard_is_built_from_config(rt):
    async with rt as runtime:
        guard = runtime.egress.budgets["serpapi"]
        assert guard.cap == 8, "config/sources.yaml budget.serpapi.daily_cap"
        assert guard.safety_fraction == 0.85


async def test_spending_on_one_serpapi_source_constrains_the_others(rt):
    """One account, one quota.

    Before `budget_key`, the guard was looked up under each source's `auth_env` and
    no guard was ever built, so every costed fetch was uncapped. Four independent
    caps would be the same bug in a politer costume.
    """
    async with rt as runtime:
        guard = runtime.egress.budgets["serpapi"]
        assert runtime.can_spend("serpapi_jobs", 1)

        while guard.can_spend(1):
            guard.spend(1)

        assert not runtime.can_spend("serpapi_jobs", 1)
        assert not runtime.can_spend("serpapi_marketplace", 1), "shares the same account"
        assert runtime.can_spend("hn_algolia", 1), "free sources are unaffected"


async def test_an_exhausted_quota_still_produces_a_free_batch(rt):
    """The Pi must keep working on the day the credits run out."""
    async with rt as runtime:
        guard = runtime.egress.budgets["serpapi"]
        while guard.can_spend(1):
            guard.spend(1)

        plans = runtime.scout.plan(can_spend=runtime.can_spend)

        assert plans
        assert all(runtime.registry.get(p.engine).cost_units == 0 for p in plans)


async def test_the_scout_is_given_the_harvesters_key_function(rt):
    """Without this wiring `skip_if_cached` is dead code: the Scout compares a key
    that nothing ever writes, reports skipped_cached=0 forever, and re-plans queries
    whose answers are already sitting in the cache."""
    async with rt as runtime:
        assert runtime.scout.key_for_plan == runtime.harvester.cache_key_for_plan

        plan = next(p for p in runtime.scout.plan() if p.engine == "hn_algolia")
        assert runtime.scout.key_for_plan(plan) is not None


async def test_a_cached_answer_is_not_replanned_end_to_end(rt):
    """The Phase 2 gate, at the planning layer: a second run must not re-plan work
    whose answer is still fresh."""
    async with rt as runtime:
        before = runtime.scout.plan()
        target = next(p for p in before if p.engine == "hn_algolia")
        key = runtime.harvester.cache_key_for_plan(target)
        assert key is not None
        runtime.cache.put(
            key,
            "cached body",
            url="https://hn.algolia.com/api/v1/search_by_date",
            source_id="hn_algolia",
            legality_class="licensed_api",
            ttl_hours=6,
        )

        after = runtime.scout.plan()

        assert len(after) == len(before) - 1
        assert not any(p.query == target.query and p.engine == target.engine for p in after)
