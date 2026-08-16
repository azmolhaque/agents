"""The single egress chokepoint.

Everything here runs against httpx.MockTransport — no network, no Pi. What is being
pinned is the *order* of the gauntlet, because each ordering choice has a consequence
that is invisible until it bites in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from cindraleads.budget import BudgetGuard
from cindraleads.sources import (
    DocumentCache,
    EgressClient,
    FetchDenied,
    SourceBreakers,
    SourceRegistry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"


def _shipped_settings():  # type: ignore[no-untyped-def]
    from cindraleads.config import settings

    cfg = settings()
    object.__setattr__(cfg, "config_dir", REPO_ROOT / "config")
    return cfg


REGISTRY = SourceRegistry.from_dict(
    {
        "sources": [
            {
                "id": "api",
                "legality_class": "licensed_api",
                "cost_units": 1,
                "auth_env": "SERPAPI_KEY",
                "cache_ttl_hours": 24,
            },
            {"id": "site", "legality_class": "public_web", "cache_ttl_hours": 24},
            {"id": "off", "legality_class": "public_web", "enabled": False},
        ],
        # A costed source must name a configured allowance or the registry refuses to
        # load it -- otherwise the guard lookup misses and the credit is uncapped.
        "budget": {"api": {"daily_cap": 100, "safety_fraction": 1.0}},
        "defaults": {"retries": 2, "backoff_base_seconds": 0.001, "backoff_max_seconds": 0.002},
        "public_web_policy": {
            "fetch_budget_per_domain_24h": 6,
            "min_interval_seconds": 0.0,  # kept at 0 so tests do not sleep
            "respect_robots": True,
        },
    }
)


class Recorder:
    """A MockTransport handler that counts requests and can be told to fail."""

    def __init__(self, *, robots: str = "User-agent: *\nAllow: /", fail_times: int = 0) -> None:
        self.robots = robots
        self.fail_times = fail_times
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=self.robots)
        self.calls.append(url)
        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=f"<html>body for {url}</html>")

    @property
    def content_calls(self) -> int:
        return len(self.calls)


def make_client(store, recorder: Recorder, **kwargs) -> EgressClient:  # type: ignore[no-untyped-def]
    return EgressClient(
        store=store,
        registry=REGISTRY,
        cache=DocumentCache(store, cache_dir=Path(store.db_path).parent / "cache"),
        breakers=SourceBreakers(failure_threshold=3, open_seconds=900.0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        **kwargs,
    )


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    from cindraleads.store import Store

    s = Store(tmp_path / "egress.db", migrations_dir=MIGRATIONS)
    s.migrate()
    yield s
    s.close()


# ------------------------------------------------------------------- basics


async def test_a_fetch_returns_the_body_and_hashes_it(store):
    client = make_client(store, Recorder())
    result = await client.fetch("api", "https://x.io/a")
    assert "body for" in result.body
    assert result.cached is False
    assert len(result.content_sha256) == 64
    await client.aclose()


async def test_a_disabled_source_cannot_be_fetched(store):
    from cindraleads.errors import ConfigError

    client = make_client(store, Recorder())
    with pytest.raises(ConfigError, match="disabled"):
        await client.fetch("off", "https://x.io/")
    await client.aclose()


# -------------------------------------------------------------------- cache


async def test_the_second_identical_request_costs_nothing(store):
    """The Phase 2 gate: a repeated run must spend zero credits."""
    recorder = Recorder()
    client = make_client(store, recorder)

    first = await client.fetch("api", "https://x.io/a")
    second = await client.fetch("api", "https://x.io/a")

    assert first.cached is False and first.cost_units == 1
    assert second.cached is True and second.cost_units == 0
    assert recorder.content_calls == 1, "the network was hit once, not twice"
    assert second.body == first.body
    await client.aclose()


async def test_different_params_are_different_cache_entries(store):
    recorder = Recorder()
    client = make_client(store, recorder)
    await client.fetch("api", "https://x.io/s", params={"q": "a"})
    await client.fetch("api", "https://x.io/s", params={"q": "b"})
    assert recorder.content_calls == 2
    await client.aclose()


async def test_identical_in_flight_requests_are_collapsed(store):
    """Two workers asking the same question at the same moment must cost one credit.
    Without this the cache cannot help — neither has written to it yet."""
    recorder = Recorder()
    client = make_client(store, recorder)

    results = await asyncio.gather(*(client.fetch("api", "https://x.io/same") for _ in range(5)))

    assert recorder.content_calls == 1, "five callers, one request"
    assert all(r.body == results[0].body for r in results)
    await client.aclose()


# ------------------------------------------------------------------ robots


async def test_robots_disallow_blocks_the_fetch(store):
    recorder = Recorder(robots="User-agent: *\nDisallow: /private")
    client = make_client(store, recorder)

    with pytest.raises(FetchDenied, match="robots"):
        await client.fetch("site", "https://x.io/private/page")
    assert recorder.content_calls == 0, "denied before any request was made"
    await client.aclose()


async def test_robots_allow_permits_the_fetch(store):
    recorder = Recorder(robots="User-agent: *\nDisallow: /private")
    client = make_client(store, recorder)
    result = await client.fetch("site", "https://x.io/public")
    assert result.cached is False
    await client.aclose()


async def test_licensed_api_sources_skip_robots(store):
    """robots.txt governs crawling a site, not calling an API we are entitled to use."""
    recorder = Recorder(robots="User-agent: *\nDisallow: /")
    client = make_client(store, recorder)
    result = await client.fetch("api", "https://serpapi.com/search")
    assert result.cached is False
    await client.aclose()


# ----------------------------------------------------------- domain budget


async def test_per_domain_budget_is_enforced_and_persisted(store):
    """PLAN.md 2.5, approved: 6 per domain per rolling 24 h."""
    recorder = Recorder()
    client = make_client(store, recorder)

    for i in range(6):
        await client.fetch("site", f"https://x.io/page{i}")
    assert recorder.content_calls == 6

    with pytest.raises(FetchDenied, match="per-domain budget"):
        await client.fetch("site", "https://x.io/page7")
    await client.aclose()


async def test_the_domain_budget_is_per_host(store):
    recorder = Recorder()
    client = make_client(store, recorder)
    for i in range(6):
        await client.fetch("site", f"https://a.io/p{i}")
    # A different host has its own allowance.
    result = await client.fetch("site", "https://b.io/p")
    assert result.cached is False
    await client.aclose()


async def test_the_domain_budget_survives_a_new_client(store):
    """A restart must not hand a prospect six more requests."""
    recorder = Recorder()
    first = make_client(store, recorder)
    for i in range(6):
        await first.fetch("site", f"https://x.io/p{i}")
    await first.aclose()

    second = make_client(store, Recorder())
    with pytest.raises(FetchDenied, match="per-domain budget"):
        await second.fetch("site", "https://x.io/after-restart")
    await second.aclose()


# ---------------------------------------------------------- circuit + budget


async def test_repeated_failures_open_the_circuit_and_then_fail_fast(store):
    recorder = Recorder(fail_times=99)
    client = make_client(store, recorder)

    for _ in range(3):
        with pytest.raises(httpx.HTTPError):
            await client.fetch("api", "https://x.io/broken")

    before = recorder.content_calls
    with pytest.raises(FetchDenied, match="circuit open"):
        await client.fetch("api", "https://x.io/another")
    assert recorder.content_calls == before, "an open circuit makes no request at all"
    await client.aclose()


async def test_an_open_circuit_still_serves_a_stale_cache_entry(store):
    """A stale answer beats no answer while a source is down."""
    recorder = Recorder()
    client = make_client(store, recorder)
    await client.fetch("api", "https://x.io/cached", ttl_hours=0.0001)

    breaker = client.breakers.for_source("api")
    for _ in range(3):
        breaker.record_failure()

    await asyncio.sleep(0.5)  # let the TTL lapse
    result = await client.fetch("api", "https://x.io/cached")
    assert result.cached is True
    await client.aclose()


async def test_an_exhausted_budget_prevents_the_request(store):
    recorder = Recorder()
    client = make_client(store, recorder)
    # The provider key is the source's budget_provider. An earlier version of this
    # test registered the guard under the source's `auth_env` instead, which is not
    # what the fetch path looks up -- so it passed while the real cap did nothing.
    client.budgets["api"] = BudgetGuard(store, "api", cap=2, safety_fraction=1.0)

    await client.fetch("api", "https://x.io/1")
    await client.fetch("api", "https://x.io/2")
    with pytest.raises(FetchDenied, match="budget exhausted"):
        await client.fetch("api", "https://x.io/3")
    assert recorder.content_calls == 2
    await client.aclose()


async def test_the_configured_cap_applies_without_anyone_registering_a_guard(store):
    """The regression that mattered.

    Nothing in the system built a BudgetGuard from `sources.yaml: budget`, so
    `budgets.get(provider)` was always None and the `if guard is not None` check
    turned every costed fetch into a free one. On the Pi that is invisible until
    the monthly SerpAPI quota is gone.
    """
    registry = SourceRegistry.from_dict(
        {
            "sources": [
                {"id": "api", "legality_class": "licensed_api", "cost_units": 1},
            ],
            "budget": {"api": {"daily_cap": 1, "safety_fraction": 1.0}},
            "defaults": {"retries": 1, "backoff_base_seconds": 0.001},
        }
    )
    recorder = Recorder()
    client = EgressClient(
        store=store,
        registry=registry,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )
    await client.fetch("api", "https://x.io/1")
    with pytest.raises(FetchDenied, match="budget exhausted"):
        await client.fetch("api", "https://x.io/2")
    await client.aclose()


async def test_the_four_serpapi_sources_share_one_quota():
    """They are one account. Four separate caps would be four times the spend."""
    shipped = SourceRegistry.from_config(_shipped_settings())
    serp = [s for s in shipped.sources.values() if s.id.startswith("serpapi_")]
    assert len(serp) == 4
    assert {s.budget_provider for s in serp} == {"serpapi"}


async def test_a_costed_source_with_no_allowance_will_not_load():
    from cindraleads.errors import ConfigError

    with pytest.raises(ConfigError, match="budget 'api' is not configured"):
        SourceRegistry.from_dict(
            {"sources": [{"id": "api", "legality_class": "licensed_api", "cost_units": 1}]}
        )


async def test_a_transient_failure_is_retried_then_succeeds(store):
    recorder = Recorder(fail_times=1)
    client = make_client(store, recorder)
    result = await client.fetch("api", "https://x.io/flaky")
    assert result.cached is False
    assert recorder.content_calls == 2, "one failure, one retry"
    await client.aclose()


async def test_a_client_error_is_not_retried(store):
    """A 404 says the same thing next time. Retrying it spends budget and annoys
    the server for nothing — only 429 and 5xx are transient."""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        calls.append(str(request.url))
        return httpx.Response(404, text="nope")

    client = EgressClient(
        store=store,
        registry=REGISTRY,
        cache=DocumentCache(store, cache_dir=Path(store.db_path).parent / "cache"),
        breakers=SourceBreakers(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch("api", "https://x.io/missing")
    assert len(calls) == 1, "a 404 must not be retried"
    await client.aclose()


async def test_a_denied_fetch_does_not_count_against_the_circuit(store):
    """robots said no; nothing broke. Counting policy denials as failures would open
    the breaker on a perfectly healthy source."""
    recorder = Recorder(robots="User-agent: *\nDisallow: /")
    client = make_client(store, recorder)

    for i in range(5):
        with pytest.raises(FetchDenied):
            await client.fetch("site", f"https://x.io/p{i}")

    assert client.breakers.for_source("site").state == "closed"
    await client.aclose()


async def test_a_404_does_not_open_the_circuit(store):
    """The bug that would have starved enrichment.

    The Enricher checks five standard paths per company and most sites have three of
    them. Three 404s on ONE company opened the breaker for `company_site` and every
    other company then failed fast for the full 900 s window.
    """

    def missing(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(404, text="not found")

    client = make_client(store, Recorder())
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(missing))
    breaker = client.breakers.for_source("site")

    for i in range(5):
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch("site", f"https://x.io/missing{i}")

    assert breaker.allow(), "a missing page is not evidence the source is down"
    await client.aclose()


async def test_a_500_still_opens_the_circuit(store):
    """The breaker must still do its job for failures that are about the source."""
    recorder = Recorder(fail_times=99)
    client = make_client(store, recorder)
    breaker = client.breakers.for_source("api")

    # Exactly the threshold. A fourth call would raise FetchDenied from the now-open
    # breaker rather than the HTTP error, which is the behaviour being asserted.
    for i in range(3):
        with pytest.raises(httpx.HTTPError):
            await client.fetch("api", f"https://x.io/boom{i}")

    assert not breaker.allow()
    await client.aclose()


async def test_a_429_still_counts_against_the_source(store):
    """Rate limiting is the source telling us to back off — the one 4xx that is."""

    def limited(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(429, json={"retry_after": 0})

    client = make_client(store, Recorder())
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(limited))
    breaker = client.breakers.for_source("site")

    for i in range(3):
        with pytest.raises(httpx.HTTPError):
            await client.fetch("site", f"https://x.io/slow{i}")

    assert not breaker.allow()
    await client.aclose()
