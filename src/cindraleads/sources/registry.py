"""The source registry: what may be fetched, and under which legality class.

Adding a source is a row in ``config/sources.yaml``, never a code change. This module
loads that file and answers one question for the egress layer: *is this request allowed,
and under what rules?*

The legality class is enforcement, not documentation. Phase 4's ``passive.py`` will hang
the forbidden-action denylist off the same boundary, so a source with no declared class
cannot be fetched at all — the failure mode for a misconfigured source is "refuses to
run", never "runs unclassified".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cindraleads.config import (
    FETCH_BUDGET_PER_DOMAIN_24H,
    Settings,
    load_yaml,
    settings,
)
from cindraleads.errors import ConfigError
from cindraleads.models import LegalityClass

__all__ = ["FetchDefaults", "PublicWebPolicy", "Source", "SourceRegistry", "SourceRole"]

_VALID_CLASSES: frozenset[str] = frozenset(
    {"public_record", "public_web", "licensed_api", "first_party"}
)


@dataclass(frozen=True)
class FetchDefaults:
    user_agent: str = "CindrasecLeadsBot/0.1 (+https://cindrasec.com/bot)"
    timeout_seconds: float = 30.0
    max_bytes: int = 900_000
    retries: int = 3
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    circuit_failure_threshold: int = 3
    circuit_open_seconds: float = 900.0


@dataclass(frozen=True)
class PublicWebPolicy:
    """Politeness rules for a prospect's own site.

    PLAN.md 2.5 (approved): one request per path we ask for, per domain per rolling
    24 h, >= 3 s apart. The master prompt's "<=2 per day" could not coexist with its
    own five-path fetch list, and 6 could not coexist with the nine-path list it grew
    into -- so the default is **derived from that list** rather than written down a
    third time. A registry built without the key used to get 6 while the shipped
    config said 9, which is how a test rig exercised a configuration we do not ship.
    """

    fetch_budget_per_domain_24h: int = FETCH_BUDGET_PER_DOMAIN_24H
    min_interval_seconds: float = 3.0
    respect_robots: bool = True
    obey_crawl_delay: bool = True
    follow_cross_origin_redirects: bool = False
    paths: tuple[str, ...] = ("/",)


SourceRole = Literal["discovery", "enrichment"]


@dataclass(frozen=True)
class Source:
    id: str
    legality_class: LegalityClass
    # PLAN.md decision 7: discovery finds companies we do not know; enrichment
    # deepens ones we do. Enrichment is almost entirely free, which is what makes a
    # 250-search monthly quota workable.
    role: SourceRole = "enrichment"
    enabled: bool = True
    base_url: str | None = None
    auth_env: str | None = None
    # How the credential named by `auth_env` travels. `query` means the caller puts it
    # in `secret_params` itself, which is what the four SerpAPI sources do; `bearer`
    # means the egress attaches `Authorization: Bearer <token>` and no call site has to
    # remember to.
    #
    # It defaults to `query` so this is not a behaviour change for anything that
    # already worked -- but `query` is also the value that has no enforcement behind
    # it, so declaring `auth_env` alone still buys nothing. That is exactly how
    # `GITHUB_TOKEN` came to exist in `.env.example`, in `Settings`, in the redaction
    # list and in two `auth_env` lines while **no request ever carried it**: every hop
    # of the chain was built except the last, so every GitHub call this project has
    # ever made went out unauthenticated at 60 requests an hour.
    auth_scheme: Literal["query", "bearer"] = "query"
    cache_ttl_hours: int = 24
    cost_units: int = 0
    requires_contact_ua: bool = False
    notes: str = ""
    # Which allowance this source spends from. Four serpapi_* sources share one
    # SerpAPI quota, so the budget is a property of the *provider*, not the source.
    # Defaults to the source id, which is right for a source with a private cap.
    budget_key: str | None = None

    @property
    def budget_provider(self) -> str:
        return self.budget_key or self.id

    @property
    def is_public_web(self) -> bool:
        return self.legality_class == "public_web"

    @property
    def needs_auth(self) -> bool:
        return self.auth_env is not None


@dataclass
class SourceRegistry:
    sources: dict[str, Source] = field(default_factory=dict)
    defaults: FetchDefaults = field(default_factory=FetchDefaults)
    public_web: PublicWebPolicy = field(default_factory=PublicWebPolicy)
    budget: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ load

    @classmethod
    def from_config(cls, config: Settings | None = None) -> SourceRegistry:
        cfg = config or settings()
        data = load_yaml("sources", base=cfg.resolve(cfg.config_dir))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRegistry:
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ConfigError("sources.yaml needs a non-empty 'sources' list")

        sources: dict[str, Source] = {}
        for entry in raw_sources:
            if not isinstance(entry, dict):
                raise ConfigError(f"each source must be a mapping, got {type(entry).__name__}")
            source_id = entry.get("id")
            if not source_id:
                raise ConfigError(f"source is missing an 'id': {entry}")
            klass = entry.get("legality_class")
            if klass not in _VALID_CLASSES:
                # Deliberately fatal. An unclassified source must not be fetchable,
                # because the class is what the passive-only boundary checks.
                raise ConfigError(
                    f"source {source_id!r} has legality_class={klass!r}; "
                    f"must be one of {sorted(_VALID_CLASSES)}"
                )
            if source_id in sources:
                raise ConfigError(f"duplicate source id {source_id!r}")
            role = entry.get("role", "enrichment")
            if role not in ("discovery", "enrichment"):
                raise ConfigError(
                    f"source {source_id!r} has role={role!r}; must be discovery or enrichment"
                )
            scheme = entry.get("auth_scheme", "query")
            if scheme not in ("query", "bearer"):
                raise ConfigError(
                    f"source {source_id!r} has auth_scheme={scheme!r}; must be query or bearer"
                )
            if scheme != "query" and not entry.get("auth_env"):
                raise ConfigError(
                    f"source {source_id!r} declares auth_scheme={scheme!r} with no auth_env; "
                    "there is no credential for it to send"
                )
            sources[str(source_id)] = Source(
                id=str(source_id),
                legality_class=klass,
                role=role,
                enabled=bool(entry.get("enabled", True)),
                base_url=entry.get("base_url"),
                auth_env=entry.get("auth_env"),
                auth_scheme=scheme,
                cache_ttl_hours=int(entry.get("cache_ttl_hours", 24)),
                cost_units=int(entry.get("cost_units", 0)),
                requires_contact_ua=bool(entry.get("requires_contact_ua", False)),
                notes=str(entry.get("notes", "")),
                budget_key=(str(entry["budget_key"]) if entry.get("budget_key") else None),
            )

        defaults = FetchDefaults(**_subset(data.get("defaults") or {}, FetchDefaults))
        policy_raw = dict(data.get("public_web_policy") or {})
        if "paths" in policy_raw:
            policy_raw["paths"] = tuple(policy_raw["paths"])
        policy = PublicWebPolicy(**_subset(policy_raw, PublicWebPolicy))

        budget = dict(data.get("budget") or {})
        # Fail closed. A source that costs credits but names no allowance would be
        # fetched without any cap at all -- the egress layer can only enforce a guard
        # that exists. That failure is invisible until the monthly quota is gone, so
        # it is caught here, at load, instead of at 2am on the Pi.
        for source in sources.values():
            if source.enabled and source.cost_units and source.budget_provider not in budget:
                raise ConfigError(
                    f"source {source.id!r} costs {source.cost_units} unit(s) but "
                    f"budget {source.budget_provider!r} is not configured; "
                    f"known budgets: {sorted(budget)}"
                )

        return cls(sources=sources, defaults=defaults, public_web=policy, budget=budget)

    # ----------------------------------------------------------------- query

    def get(self, source_id: str) -> Source:
        try:
            return self.sources[source_id]
        except KeyError:
            raise ConfigError(
                f"unknown source {source_id!r}; known: {sorted(self.sources)}"
            ) from None

    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources.values() if s.enabled]

    def by_class(self, legality_class: LegalityClass) -> list[Source]:
        return [s for s in self.sources.values() if s.legality_class == legality_class]

    def by_role(self, role: SourceRole, *, enabled_only: bool = True) -> list[Source]:
        return [
            s for s in self.sources.values() if s.role == role and (s.enabled or not enabled_only)
        ]

    def free_sources(self) -> list[Source]:
        """Sources that cost no rationed credit. The Scout should exhaust these first."""
        return [s for s in self.sources.values() if s.enabled and s.cost_units == 0]

    def require_enabled(self, source_id: str) -> Source:
        """Fetch-time check. A disabled source is a config decision, not an error to
        route around, so this raises rather than returning None."""
        source = self.get(source_id)
        if not source.enabled:
            raise ConfigError(f"source {source_id!r} is disabled in sources.yaml")
        return source


def _subset(raw: dict[str, Any], cls: type) -> dict[str, Any]:
    """Keep only keys the dataclass declares.

    Unknown keys in YAML are ignored rather than fatal: a future field documented in
    the file before the code supports it should not stop the pipeline booting.
    """
    allowed = set(getattr(cls, "__dataclass_fields__", {}))
    return {k: v for k, v in raw.items() if k in allowed}
