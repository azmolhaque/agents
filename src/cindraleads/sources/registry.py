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
from typing import Any

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.errors import ConfigError
from cindraleads.models import LegalityClass

__all__ = ["FetchDefaults", "PublicWebPolicy", "Source", "SourceRegistry"]

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

    PLAN.md 2.5 (approved): 6 requests per domain per rolling 24 h, >= 3 s apart. The
    master prompt's "<=2 per day" could not coexist with its own five-path fetch list.
    """

    fetch_budget_per_domain_24h: int = 6
    min_interval_seconds: float = 3.0
    respect_robots: bool = True
    obey_crawl_delay: bool = True
    follow_cross_origin_redirects: bool = False
    paths: tuple[str, ...] = ("/",)


@dataclass(frozen=True)
class Source:
    id: str
    legality_class: LegalityClass
    enabled: bool = True
    base_url: str | None = None
    auth_env: str | None = None
    cache_ttl_hours: int = 24
    cost_units: int = 0
    requires_contact_ua: bool = False
    notes: str = ""

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
            sources[str(source_id)] = Source(
                id=str(source_id),
                legality_class=klass,
                enabled=bool(entry.get("enabled", True)),
                base_url=entry.get("base_url"),
                auth_env=entry.get("auth_env"),
                cache_ttl_hours=int(entry.get("cache_ttl_hours", 24)),
                cost_units=int(entry.get("cost_units", 0)),
                requires_contact_ua=bool(entry.get("requires_contact_ua", False)),
                notes=str(entry.get("notes", "")),
            )

        defaults = FetchDefaults(**_subset(data.get("defaults") or {}, FetchDefaults))
        policy_raw = dict(data.get("public_web_policy") or {})
        if "paths" in policy_raw:
            policy_raw["paths"] = tuple(policy_raw["paths"])
        policy = PublicWebPolicy(**_subset(policy_raw, PublicWebPolicy))

        return cls(
            sources=sources,
            defaults=defaults,
            public_web=policy,
            budget=dict(data.get("budget") or {}),
        )

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
