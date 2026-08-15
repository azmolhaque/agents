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

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"

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
    guard = BudgetGuard(store, "SERPAPI_KEY", cap=2, safety_fraction=1.0)
    client.budgets["SERPAPI_KEY"] = guard

    await client.fetch("api", "https://x.io/1")
    await client.fetch("api", "https://x.io/2")
    with pytest.raises(FetchDenied, match="budget exhausted"):
        await client.fetch("api", "https://x.io/3")
    assert recorder.content_calls == 2
    await client.aclose()


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
