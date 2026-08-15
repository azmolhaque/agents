"""Assembles the pipeline's long-lived objects, once, in one place.

Every stage needs the same handful of collaborators — the store, the source registry,
the egress client, the queue. Constructing them ad hoc at each call site is how a second
`httpx.AsyncClient` (with its own connection pool and no shared in-flight map) or a
second `EgressClient` (with its own, empty, budget table) quietly appears. Either would
break a guarantee the egress chokepoint is supposed to provide.

So: one `Runtime` per process. It is an async context manager because the HTTP client
owns sockets and must be closed on the way out, and because the client should be created
inside the running loop rather than bound to whichever loop happened to exist at import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType

import httpx

from cindraleads.agents import Harvester, Scout
from cindraleads.config import Settings, settings
from cindraleads.logging import get_logger
from cindraleads.queue import JobQueue
from cindraleads.sources import DocumentCache, EgressClient, SourceRegistry
from cindraleads.store import Store

__all__ = ["Runtime"]

log = get_logger("cindraleads.runtime")


@dataclass
class Runtime:
    store: Store
    config: Settings = field(default_factory=settings)
    registry: SourceRegistry = field(init=False)
    cache: DocumentCache = field(init=False)
    egress: EgressClient = field(init=False)
    queue: JobQueue = field(init=False)
    scout: Scout = field(init=False)
    harvester: Harvester = field(init=False)
    _http: httpx.AsyncClient | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.registry = SourceRegistry.from_config(self.config)
        self.cache = DocumentCache(self.store, cache_dir=self.config.resolve(self.config.cache_dir))
        self.queue = JobQueue(self.store)

    async def __aenter__(self) -> Runtime:
        self._http = httpx.AsyncClient(
            timeout=self.registry.defaults.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.registry.defaults.user_agent},
        )
        self.egress = EgressClient(
            store=self.store,
            registry=self.registry,
            cache=self.cache,
            client=self._http,
        )
        self.scout = Scout.from_config(
            self.registry, store=self.store, cache=self.cache, config=self.config
        )
        self.harvester = Harvester(store=self.store, egress=self.egress, queue=self.queue)
        # The Harvester owns the clients, so it is the only thing that can say what
        # key a plan will be fetched under. Without this the Scout's skip_if_cached
        # compares a key nothing ever writes.
        self.scout.key_for_plan = self.harvester.cache_key_for_plan
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ budget

    def can_spend(self, engine: str, units: int) -> bool:
        """Whether `engine` may spend `units` right now, without spending them.

        Keyed on the source's budget provider rather than on the source id: the four
        `serpapi_*` sources draw on one account, and four independent caps would be
        four times the intended spend.
        """
        source = self.registry.get(engine)
        guard = self.egress.budgets.get(source.budget_provider)
        return True if guard is None else guard.can_spend(units)
