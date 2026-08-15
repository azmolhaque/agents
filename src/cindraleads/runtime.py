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
from pydantic import SecretStr

from cindraleads.agents import Dispatcher, Extractor, Harvester, Resolver, Scorer, Scout
from cindraleads.budget import BudgetGuard
from cindraleads.compliance import ComplianceGate
from cindraleads.config import Settings, settings
from cindraleads.discord import DiscordWebhook
from cindraleads.llm import ModelRegistry, OllamaBackend, StructuredLLM
from cindraleads.logging import get_logger
from cindraleads.queue import JobQueue
from cindraleads.sources import DocumentCache, EgressClient, SourceRegistry
from cindraleads.store import Store
from cindraleads.thermal import ThermalGovernor

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
    llm: StructuredLLM = field(init=False)
    extractor: Extractor = field(init=False)
    resolver: Resolver = field(init=False)
    scorer: Scorer = field(init=False)
    dispatcher: Dispatcher = field(init=False)
    cloud_budget: BudgetGuard = field(init=False)
    governor: ThermalGovernor = field(init=False)
    _http: httpx.AsyncClient | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.registry = SourceRegistry.from_config(self.config)
        self.cache = DocumentCache(self.store, cache_dir=self.config.resolve(self.config.cache_dir))
        self.queue = JobQueue(self.store)
        self.cloud_budget = BudgetGuard.for_cloud(
            self.store, daily_usd_cap=self.config.daily_cloud_usd_cap
        )
        self.governor = ThermalGovernor()

    def thermal_gate(self) -> bool:
        """Whether inference is allowed right now.

        Polled per call rather than cached: a batch of 150 pages runs for hours, and a
        governor sampled once at startup would happily keep inferring into an 85 C
        shutdown. A reader that is unavailable (no `vcgencmd`, wrong group) reports
        nominal, so a dev box is never gated.
        """
        return bool(self.governor.poll().allow_llm)

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
        self.harvester = Harvester(
            store=self.store,
            egress=self.egress,
            queue=self.queue,
            serpapi_key=_secret(self.config.serpapi_key),
        )
        self.llm = StructuredLLM(
            OllamaBackend(timeout=self.config.ollama_timeout_seconds),
            registry=ModelRegistry.from_config(self.config),
            # The cloud tier is a rationed escalation, not a fallback: the guard is
            # consulted per call so an exhausted day degrades to local-only instead of
            # quietly spending past the cap.
            can_escalate=lambda: self.cloud_budget.can_spend(0.01),
            gate=self.thermal_gate,
        )
        self.extractor = Extractor(
            store=self.store, egress=self.egress, llm=self.llm, config=self.config
        )
        self.resolver = Resolver(store=self.store)
        self.scorer = Scorer(
            store=self.store,
            llm=self.llm,
            config=self.config,
            gate=ComplianceGate.from_config(self.config),
        )
        self.dispatcher = Dispatcher(
            store=self.store,
            # Its own client: a Discord 429 must not stall prospect fetches, and a
            # slow prospect must not hold a connection Discord is waiting on.
            webhook=DiscordWebhook(client=httpx.AsyncClient(timeout=20.0)),
            config=self.config,
        )
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
        await self.dispatcher.webhook.client.aclose()

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


def _secret(value: SecretStr | None) -> str | None:
    """Unwrap a SecretStr exactly at the point of use.

    Kept wrapped everywhere else so an accidental repr or f-string prints asterisks
    rather than the key. This is the second line of defence; the redaction processor
    in `logging.py` is the first.
    """
    return value.get_secret_value() if value is not None else None
