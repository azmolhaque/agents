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
from pydantic import SecretStr

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


# ------------------------------------------------ the wait the ceiling did not cover


async def test_a_long_retry_after_is_not_waited_out(store, monkeypatch):
    """What `enrich.company exceeded 900s and was cancelled` actually was.

    `_backoff` is capped at `backoff_max_seconds` because there is a length of time
    past which we would rather fail a fetch than hold a worker. The `Retry-After`
    branch stepped straight around that cap and slept for whatever the remote asked
    for, so one rate-limited page could hold a stage open until `MAX_STAGE_SECONDS`
    cancelled it -- failing a whole company, and charging an attempt against the
    dead-letter ceiling, over one slow host.

    Same shape as every other bound this project has had to add: the ceiling existed
    and one code path was not subject to it.
    """
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("cindraleads.sources.http.asyncio.sleep", _record)

    def limited(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(429, headers={"retry-after": "3600"}, text="slow down")

    client = make_client(store, Recorder())
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(limited))

    with pytest.raises(httpx.HTTPError):
        await client.fetch("site", "https://x.io/limited")

    ceiling = REGISTRY.defaults.backoff_max_seconds
    assert all(wait <= ceiling for wait in slept), f"slept past the ceiling: {slept}"
    await client.aclose()


async def test_a_retry_after_we_will_not_honour_ends_the_retries(store, monkeypatch):
    """Past the ceiling we stop asking rather than ask sooner.

    Clamping the wait down to the ceiling would be the other obvious fix and it is the
    wrong one: the server named a number, and retrying inside it is exactly the
    hammering the header exists to prevent. Every caller of a fetch here already treats
    a dead source as a missing field rather than a dead company.
    """

    async def _instant(seconds: float) -> None:
        return None

    monkeypatch.setattr("cindraleads.sources.http.asyncio.sleep", _instant)
    calls: list[str] = []

    def limited(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        calls.append(str(request.url))
        return httpx.Response(429, headers={"retry-after": "3600"}, text="slow down")

    client = make_client(store, Recorder())
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(limited))

    with pytest.raises(httpx.HTTPError):
        await client.fetch("site", "https://x.io/limited")

    assert len(calls) == 1, f"asked again inside a window we refused to wait for: {calls}"
    await client.aclose()


async def test_a_nonsense_retry_after_is_ignored_rather_than_slept_on(store):
    """A header is whatever the remote chose to send. `nan` parses as a float and
    compares False against every ceiling, so it would slip past the bound above and
    reach `asyncio.sleep` -- which is not a wait, it is a hang."""
    from cindraleads.sources.http import _retry_after

    for raw in ("nan", "inf", "-30", "tomorrow", "Wed, 21 Oct 2026 07:28:00 GMT"):
        response = httpx.Response(429, headers={"retry-after": raw})
        assert _retry_after(response) is None, raw

    assert _retry_after(httpx.Response(429, headers={"retry-after": "12"})) == 12.0


# ------------------------------------------------------------------ credentials
#
# `auth_env` existed on two GitHub sources, `GITHUB_TOKEN` existed in `.env.example`,
# `Settings.github_token` existed and was in the redaction list, and `GitHubClient`
# took a `token` argument -- and no request this project ever made carried one. Every
# hop of the chain was built except the last, so GitHub search ran at 60 requests an
# hour for the life of the project. Eighth instance of built-wired-never-connected,
# after `digest_pages`, `extend_lease`, `open_roles`, `discovered_by`, `full_name`,
# the heartbeat `exiting` flag and `_facts`.


AUTH_REGISTRY = SourceRegistry.from_dict(
    {
        "sources": [
            {
                "id": "bearer_src",
                "legality_class": "licensed_api",
                "auth_env": "GITHUB_TOKEN",
                "auth_scheme": "bearer",
                "cache_ttl_hours": 24,
            },
            {
                "id": "query_src",
                "legality_class": "licensed_api",
                "auth_env": "SERPAPI_KEY",
                "cache_ttl_hours": 24,
            },
        ],
        "defaults": {"retries": 1, "backoff_base_seconds": 0.001},
    }
)


@pytest.fixture
def auth_egress(tmp_path: Path):  # type: ignore[no-untyped-def]
    """An egress over a throwaway store, closed on the way out.

    A fixture rather than a plain helper because the helper leaked: three tests each
    opened a `Store` and none closed it, so three `sqlite3.Connection` objects were
    finalized whenever the garbage collector got to them. `-W error` turns that
    `ResourceWarning` into a `PytestUnraisableExceptionWarning` charged to **whichever
    test happens to be running at the time**, which was neither the leak nor a real
    failure -- the suite reported a green assertion as a red test. Teardown also runs
    when an assertion fails, which a close at the end of the test body does not.
    """
    from cindraleads.sources import DocumentCache, SourceBreakers
    from cindraleads.store import Store

    stores: list[Store] = []

    def build(tokens: dict[str, str], seen: list[dict[str, str]]) -> EgressClient:
        store = Store(tmp_path / f"auth{len(stores)}.db", migrations_dir=MIGRATIONS)
        store.migrate()
        stores.append(store)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.headers))
            return httpx.Response(200, text="{}")

        return EgressClient(
            store=store,
            registry=AUTH_REGISTRY,
            cache=DocumentCache(store, cache_dir=tmp_path / "cache"),
            breakers=SourceBreakers(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            auth_tokens=tokens,
        )

    yield build
    for store in stores:
        store.close()


async def test_a_bearer_source_sends_its_token(auth_egress):
    seen: list[dict[str, str]] = []
    egress = auth_egress({"GITHUB_TOKEN": "ghp_secret"}, seen)
    await egress.fetch("bearer_src", "https://api.example.com/a")
    assert seen[0].get("authorization") == "Bearer ghp_secret"


async def test_a_query_scheme_source_sends_no_header(auth_egress):
    """SerpAPI puts its key in `secret_params` and must keep doing so. A blanket
    "attach every auth_env as a header" would send the key a second way, on the wire,
    for no benefit -- and a credential travelling by two routes is two things to get
    wrong rather than one."""
    seen: list[dict[str, str]] = []
    egress = auth_egress({"SERPAPI_KEY": "sk_secret"}, seen)
    await egress.fetch("query_src", "https://api.example.com/b")
    assert "authorization" not in {k.lower() for k in seen[0]}


async def test_a_missing_token_still_fetches(auth_egress):
    """Unauthenticated GitHub works; it is slower, not broken. Refusing to fetch would
    turn a rate-limit downgrade into a dead source on any box without a token, which is
    every dev checkout."""
    seen: list[dict[str, str]] = []
    egress = auth_egress({}, seen)
    result = await egress.fetch("bearer_src", "https://api.example.com/c")
    assert result.body == "{}"
    assert "authorization" not in {k.lower() for k in seen[0]}


async def test_rotating_the_token_does_not_invalidate_the_cache(store, tmp_path):
    """Handled exactly like `secret_params`, for exactly that reason: a credential in
    the key means rotating it silently re-fetches every document cached under the old
    one, and puts the credential in a column nothing redacts.

    Asserted through `cached`, not by comparing a key to itself -- what matters is that
    the second client, with a different token, reads what the first one wrote.
    """
    cache = DocumentCache(store, cache_dir=tmp_path / "cache")
    fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetches.append(str(request.url))
        return httpx.Response(200, text="{}")

    def client_with(token: str) -> EgressClient:
        return EgressClient(
            store=store,
            registry=AUTH_REGISTRY,
            cache=cache,
            breakers=SourceBreakers(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            auth_tokens={"GITHUB_TOKEN": token},
        )

    first = await client_with("old").fetch("bearer_src", "https://api.example.com/d")
    second = await client_with("rotated").fetch("bearer_src", "https://api.example.com/d")

    assert first.cached is False
    assert second.cached is True, "a rotated token must not orphan the cached document"
    assert len(fetches) == 1


def test_a_bearer_source_with_no_auth_env_is_fatal():
    """There is no credential for it to send, so the declaration is a typo. Fatal for
    the same reason an unclassified source is: a silently unauthenticated source is a
    rate limit nobody can see."""
    from cindraleads.errors import ConfigError

    with pytest.raises(ConfigError, match="no auth_env"):
        SourceRegistry.from_dict(
            {"sources": [{"id": "x", "legality_class": "licensed_api", "auth_scheme": "bearer"}]}
        )


def test_every_bearer_source_has_a_settings_field_to_read():
    """The check the original defect had nowhere to live.

    `auth_tokens_for` resolves `auth_env: GITHUB_TOKEN` to `Settings.github_token` by
    convention, so a source declaring a credential the settings object has no field for
    would quietly get no header -- the same silence as before, one layer further in.
    This drives the real resolver against the real registry rather than restating
    either, which is the difference between this and the tests that passed while
    `discovered_by` was NULL for every company ever recorded.
    """
    from cindraleads.runtime import auth_tokens_for

    cfg = _shipped_settings()
    registry = SourceRegistry.from_config(cfg)
    bearer = [s for s in registry.sources.values() if s.auth_scheme == "bearer"]
    assert bearer, "sources.yaml declares no bearer source; this test has stopped testing"

    for source in bearer:
        assert source.auth_env is not None
        assert hasattr(cfg, source.auth_env.lower()), (
            f"source {source.id!r} declares auth_env={source.auth_env!r} and "
            f"Settings has no {source.auth_env.lower()!r} field to read it from"
        )

    object.__setattr__(cfg, "github_token", SecretStr("ghp_t"))
    assert auth_tokens_for(registry, cfg)["GITHUB_TOKEN"] == "ghp_t"
