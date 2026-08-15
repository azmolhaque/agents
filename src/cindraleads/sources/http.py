"""The single egress chokepoint. Nothing else in this system makes a network request.

That is the most important architectural rule in the project, and it is here for one
reason: the passive-only guarantee has to be *auditable*. With one function, a reviewer
checks one place. With egress scattered across ten stages, the rule gets re-argued —
and quietly re-decided — every time someone adds a source.

Every fetch passes the same gauntlet, in this order:

    1. source enabled?          a disabled source is a decision, not an obstacle
    2. cache hit?               free, and the politest possible outcome
    3. already in flight?       collapse duplicates rather than pay twice
    4. circuit closed?          fail fast on a source we know is down
    5. budget available?        rationed credits, checked before spending
    6. public_web extras:       robots.txt, per-domain 24h budget, min interval
    7. request                  real UA, timeout, jittered retry
    8. record outcome           breaker + cache + provenance

Order is deliberate. Cache before circuit means a cached answer is served even while a
source is down. Circuit before budget means a dead source cannot burn credits. Robots
before request means we never fetch a page we were asked not to.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
import urllib.robotparser
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from cindraleads.budget import BudgetGuard
from cindraleads.errors import CindraError
from cindraleads.logging import get_logger
from cindraleads.models import LegalityClass, from_iso, to_iso, utcnow
from cindraleads.sources.cache import CachedDocument, DocumentCache, cache_key_for
from cindraleads.sources.circuit import SourceBreakers
from cindraleads.sources.registry import FetchDefaults, SourceRegistry
from cindraleads.store import Store

__all__ = ["EgressClient", "FetchDenied", "FetchResult"]

log = get_logger("cindraleads.egress")


class FetchDenied(CindraError):
    """A policy said no before any request was made.

    robots.txt, the per-domain budget, or the minimum interval. Distinct from a
    failure: nothing broke, we simply are not allowed to ask right now, so it must not
    count against the source's circuit breaker.
    """

    def __init__(self, reason: str, url: str) -> None:
        self.reason = reason
        self.url = url
        super().__init__(f"{reason}: {url}")


@dataclass(frozen=True)
class FetchResult:
    body: str
    url: str
    source_id: str
    legality_class: LegalityClass
    content_sha256: str
    cached: bool
    status_code: int | None = None
    cost_units: int = 0
    elapsed_ms: int = 0


@dataclass
class EgressClient:
    store: Store
    registry: SourceRegistry
    cache: DocumentCache | None = None
    breakers: SourceBreakers | None = None
    client: httpx.AsyncClient | None = None
    budgets: dict[str, BudgetGuard] = field(default_factory=dict)
    _robots: dict[str, urllib.robotparser.RobotFileParser] = field(default_factory=dict)
    _inflight: dict[str, asyncio.Future[FetchResult]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cache is None:
            self.cache = DocumentCache(self.store)
        if self.breakers is None:
            self.breakers = SourceBreakers(
                failure_threshold=self.registry.defaults.circuit_failure_threshold,
                open_seconds=self.registry.defaults.circuit_open_seconds,
            )
        # Register every configured allowance up front. Building guards lazily at the
        # first spend meant `budgets.get(provider)` returned None on the very first
        # call of the day and the cap did not apply to it.
        for provider, spec in self.registry.budget.items():
            if provider in self.budgets or not isinstance(spec, dict):
                continue
            self.budgets[provider] = BudgetGuard(
                self.store,
                provider,
                cap=float(spec.get("daily_cap", 0)),
                window_hours=float(spec.get("refill_window_hours", 24.0)),
                safety_fraction=float(spec.get("safety_fraction", 1.0)),
            )

    # ------------------------------------------------------------- budgets

    def budget_for(self, provider: str, *, cap: float, safety: float = 1.0) -> BudgetGuard:
        guard = self.budgets.get(provider)
        if guard is None:
            guard = BudgetGuard(
                self.store, provider, cap=cap, window_hours=24.0, safety_fraction=safety
            )
            self.budgets[provider] = guard
        return guard

    def _domain_budget(self, host: str) -> BudgetGuard:
        """Per-domain politeness, reusing the same persisted mechanism as API credits.

        6 fetches per rolling 24 h with no safety fraction: the number IS the limit,
        not a target to leave headroom under.
        """
        return self.budget_for(
            f"domain:{host}",
            cap=float(self.registry.public_web.fetch_budget_per_domain_24h),
            safety=1.0,
        )

    def seconds_since_last_fetch(self, host: str) -> float | None:
        row = self.store.conn.execute(
            "SELECT MAX(fetched_at) AS last FROM domain_fetch_log WHERE host = ?", (host,)
        ).fetchone()
        if row is None or row["last"] is None:
            return None
        return (utcnow() - from_iso(str(row["last"]))).total_seconds()

    # -------------------------------------------------------------- robots

    async def robots_allows(self, url: str) -> bool:
        """Parse and obey robots.txt, per origin, cached for the process lifetime."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            try:
                client = await self._http()
                response = await client.get(f"{origin}/robots.txt", timeout=10.0)
                parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            except httpx.HTTPError as exc:
                # An unreachable robots.txt means "allowed" by the standard, but say so
                # out loud rather than silently assuming permission.
                log.info("robots_unreachable", origin=origin, error=str(exc))
                parser.parse([])
            self._robots[origin] = parser
        return parser.can_fetch(self.registry.defaults.user_agent, url)

    # --------------------------------------------------------------- fetch

    async def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=self.registry.defaults.timeout_seconds,
                headers={"User-Agent": self.registry.defaults.user_agent},
                # Cross-origin redirects escape both the per-domain budget and the
                # robots decision already made for the origin we intended to visit.
                follow_redirects=self.registry.public_web.follow_cross_origin_redirects,
            )
        return self.client

    async def fetch(
        self,
        source_id: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        ttl_hours: float | None = None,
        allow_stale_on_open_circuit: bool = True,
    ) -> FetchResult:
        source = self.registry.require_enabled(source_id)  # (1)
        assert self.cache is not None and self.breakers is not None
        key = cache_key_for(source_id, url, params)
        ttl = ttl_hours if ttl_hours is not None else float(source.cache_ttl_hours)

        cached = self.cache.get(key)  # (2)
        if cached is not None:
            return _from_cache(cached, cost_units=0)

        existing = self._inflight.get(key)  # (3)
        if existing is not None:
            # Two workers asking the same question simultaneously must cost one credit,
            # not two. The second awaits the first rather than issuing its own request.
            log.debug("egress_inflight_collapsed", source_id=source_id, url=url)
            return await asyncio.shield(existing)

        future: asyncio.Future[FetchResult] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await self._fetch_uncached(
                source_id, url, key, params, ttl, allow_stale_on_open_circuit
            )
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)
            # A future nobody awaited would otherwise log "exception never retrieved".
            if future.done() and future.exception() is not None:
                future.exception()

    async def _fetch_uncached(
        self,
        source_id: str,
        url: str,
        key: str,
        params: dict[str, str] | None,
        ttl: float,
        allow_stale: bool,
    ) -> FetchResult:
        source = self.registry.get(source_id)
        assert self.cache is not None and self.breakers is not None
        breaker = self.breakers.for_source(source_id)

        if not breaker.allow():  # (4)
            stale = self.cache.get(key, allow_stale=True) if allow_stale else None
            if stale is not None:
                # A stale answer beats no answer while a source is down.
                log.info("egress_served_stale", source_id=source_id, url=url)
                return _from_cache(stale, cost_units=0)
            breaker.total_rejected += 1
            raise FetchDenied(f"circuit open for {source_id}", url)

        if source.cost_units:  # (5)
            provider = source.budget_provider
            guard = self.budgets.get(provider)
            if guard is None:
                # Unreachable through `from_dict`, which rejects this at load. Kept
                # because a hand-built registry must not spend an uncapped credit.
                raise FetchDenied(f"no budget configured for {provider}", url)
            if not guard.try_spend(source.cost_units):
                raise FetchDenied(f"{provider} budget exhausted", url)

        host = urlparse(url).netloc
        if source.is_public_web:  # (6)
            await self._enforce_public_web_policy(url, host)

        started = time.monotonic()
        try:
            body, status, content_type = await self._request(url, params)
        except (httpx.HTTPError, OSError) as exc:
            breaker.record_failure(f"{type(exc).__name__}: {exc}")
            log.warning("egress_failed", source_id=source_id, url=url, error=str(exc))
            raise
        breaker.record_success()  # (8)

        if source.is_public_web:
            self._record_domain_fetch(host, url, status)

        self.cache.put(
            key,
            body,
            url=url,
            source_id=source_id,
            legality_class=source.legality_class,
            ttl_hours=ttl,
            content_type=content_type,
            status_code=status,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        log.info(
            "egress_fetched",
            source_id=source_id,
            url=url,
            status=status,
            bytes=len(body),
            duration_ms=elapsed,
            cost_units=source.cost_units,
        )

        return FetchResult(
            body=body,
            url=url,
            source_id=source_id,
            legality_class=source.legality_class,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            cached=False,
            status_code=status,
            cost_units=source.cost_units,
            elapsed_ms=elapsed,
        )

    async def _enforce_public_web_policy(self, url: str, host: str) -> None:
        policy = self.registry.public_web
        if policy.respect_robots and not await self.robots_allows(url):
            raise FetchDenied("robots.txt disallows", url)

        if not self._domain_budget(host).try_spend(1):
            raise FetchDenied(
                f"per-domain budget spent ({policy.fetch_budget_per_domain_24h}/24h)", url
            )

        elapsed = self.seconds_since_last_fetch(host)
        if elapsed is not None and elapsed < policy.min_interval_seconds:
            await asyncio.sleep(policy.min_interval_seconds - elapsed)

    def _record_domain_fetch(self, host: str, url: str, status: int | None) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO domain_fetch_log (fetch_id, host, url, status, fetched_at) "
                "VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, host, url, status, to_iso(utcnow())),
            )

    async def _request(
        self, url: str, params: dict[str, str] | None
    ) -> tuple[str, int, str | None]:
        """One request, with jittered exponential backoff on transient failures."""
        defaults = self.registry.defaults
        client = await self._http()
        last: Exception | None = None

        for attempt in range(defaults.retries):
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                last = exc
            else:
                status = response.status_code
                # Retryable: rate limits and server-side faults. NOT 4xx -- a 404 or a
                # 403 will say exactly the same thing next time, and retrying it just
                # spends the budget and annoys the server.
                if status == 429 or 500 <= status < 600:
                    last = httpx.HTTPStatusError(
                        f"status {status}", request=response.request, response=response
                    )
                    if attempt < defaults.retries - 1:
                        # Obey Retry-After when the server bothered to send one;
                        # guessing is how you get rate-limited harder.
                        retry_after = _retry_after(response)
                        wait = (
                            retry_after if retry_after is not None else _backoff(attempt, defaults)
                        )
                        log.info("egress_backoff", url=url, status=status, wait_s=round(wait, 3))
                        await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                body = response.text[: defaults.max_bytes]
                return body, status, response.headers.get("content-type")

            if attempt < defaults.retries - 1:
                await asyncio.sleep(_backoff(attempt, defaults))

        raise last if last is not None else httpx.HTTPError(f"no response for {url}")

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


def _backoff(attempt: int, defaults: FetchDefaults) -> float:
    """Exponential with full jitter. Without jitter, N workers that failed together
    retry together and re-create the thundering herd they just caused."""
    base = defaults.backoff_base_seconds
    ceiling = defaults.backoff_max_seconds
    return random.uniform(0, min(ceiling, base * (2**attempt)))


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _from_cache(doc: CachedDocument, *, cost_units: int) -> FetchResult:
    return FetchResult(
        body=doc.body,
        url=doc.url,
        source_id=doc.source_id,
        legality_class=doc.legality_class,
        content_sha256=doc.content_sha256,
        cached=True,
        status_code=doc.status_code,
        cost_units=cost_units,
    )
