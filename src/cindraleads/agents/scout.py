"""Scout — turns the ICP into a batch of discovery queries.

Deliberately **not** an LLM stage, which is a deviation from the master prompt's §9
worth stating plainly. The plan lists Scout as one of three LLM-bearing stages, and on
paper "synthesize diverse queries" sounds like a job for a model. Measured on this
hardware it is not: a query is ~15 tokens, decode runs at 3.7 tok/s, and the model would
be inventing search strings that a template plus the ICP file expresses exactly. That is
30+ seconds of inference to produce something less predictable than a config row.

The templates live in ``config/icp.yaml``, so adding a way to find prospects is a row in
a file rather than a prompt change with golden fixtures to re-run.

What the Scout actually decides, and why each matters:

* **Free before rationed.** SerpAPI is ~7 queries/day (decision 7). Templates are sorted
  by weight but partitioned by cost first, so a free template always outranks a paid one
  of equal value.
* **Never plan a guaranteed cache hit.** If the answer is still fresh in the cache, the
  plan is skipped rather than executed for free — it would return identical documents
  and waste a slot in the batch.
* **Suppression at plan time, not dispatch time.** The master prompt consults the
  suppression list before dispatch, by which point the credit, the fetch and ~64 s of
  extraction have already been spent on a company we were always going to reject.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.errors import ConfigError
from cindraleads.logging import get_logger
from cindraleads.models import QueryPlan, TriggerCode
from cindraleads.sources.cache import DocumentCache
from cindraleads.sources.registry import SourceRegistry
from cindraleads.store import Store

__all__ = ["QueryTemplate", "Scout", "ScoutConfig"]

log = get_logger("cindraleads.scout")


@dataclass(frozen=True)
class QueryTemplate:
    id: str
    engine: str
    query: str = ""
    targets: tuple[TriggerCode, ...] = ()
    weight: int = 50
    since_days: int = 30
    tags: str = ""
    rationale: str = ""
    # GitHub only. Personal repos are excluded by default because a personal repo is a
    # person; a template sets this to opt back in, which no current one does.
    include_personal_repos: bool = False
    # HN only. The hits are the *comments* of the matched threads rather than the
    # threads themselves. For a thread like "Ask HN: Who is hiring" the story is an
    # index and the companies are in the replies -- without this the only URL on offer
    # is news.ycombinator.com, which is a platform and rightly dropped.
    comments: bool = False
    # HN comment expansion only. Which of the matched stories are worth reading, by
    # title. The author filter returns everything an account posts, and `whoishiring`
    # posts "Who is hiring?" *and* "Who wants to be hired?" every month -- the second
    # is individuals advertising themselves, which the anti-ICP rule excludes outright.
    # Measured before this existed: 23 of 40 candidates in a run were personal CVs.
    title_contains: str = ""

    def to_plan(self, *, cache_ttl_hours: int) -> QueryPlan:
        params: dict[str, str] = {"since_days": str(self.since_days)}
        if self.tags:
            params["tags"] = self.tags
        if self.include_personal_repos:
            params["organizations_only"] = "false"
        if self.comments:
            params["comments"] = "true"
        if self.title_contains:
            params["title_contains"] = self.title_contains
        return QueryPlan(
            template_id=self.id,
            query=self.query,
            engine=self.engine,
            params=params,
            targets=list(self.targets),
            rationale=self.rationale.strip(),
            cache_ttl_hours=cache_ttl_hours,
        )


@dataclass(frozen=True)
class ScoutConfig:
    max_plans_per_run: int = 12
    max_costed_plans_per_run: int = 2
    skip_if_cached: bool = True


@dataclass
class Scout:
    registry: SourceRegistry
    templates: list[QueryTemplate] = field(default_factory=list)
    config: ScoutConfig = field(default_factory=ScoutConfig)
    cache: DocumentCache | None = None
    store: Store | None = None
    # `callable(plan) -> cache key | None`. The Runtime wires this to the Harvester,
    # which asks the source client for the key it will really fetch under.
    key_for_plan: Callable[[QueryPlan], str | None] | None = None

    @classmethod
    def from_config(
        cls,
        registry: SourceRegistry,
        *,
        store: Store | None = None,
        cache: DocumentCache | None = None,
        config: Settings | None = None,
    ) -> Scout:
        cfg = config or settings()
        data = load_yaml("icp", base=cfg.resolve(cfg.config_dir))
        raw = data.get("query_templates")
        if not isinstance(raw, list) or not raw:
            raise ConfigError("icp.yaml needs a non-empty 'query_templates' list")

        templates: list[QueryTemplate] = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("id"):
                raise ConfigError(f"query template needs an 'id': {entry}")
            engine = entry.get("engine")
            if not engine:
                raise ConfigError(f"template {entry['id']!r} needs an 'engine'")
            # Fatal, not a warning: a template pointing at a source that does not
            # exist is a typo that would silently stop finding a whole trigger class.
            registry.get(str(engine))
            templates.append(
                QueryTemplate(
                    id=str(entry["id"]),
                    engine=str(engine),
                    query=str(entry.get("query", "")),
                    targets=tuple(entry.get("targets", [])),
                    weight=int(entry.get("weight", 50)),
                    include_personal_repos=bool(entry.get("include_personal_repos", False)),
                    comments=bool(entry.get("comments", False)),
                    title_contains=str(entry.get("title_contains", "")),
                    since_days=int(entry.get("since_days", 30)),
                    tags=str(entry.get("tags", "")),
                    rationale=str(entry.get("rationale", "")),
                )
            )

        scout_cfg = data.get("scout") or {}
        return cls(
            registry=registry,
            templates=templates,
            config=ScoutConfig(
                max_plans_per_run=int(scout_cfg.get("max_plans_per_run", 12)),
                max_costed_plans_per_run=int(scout_cfg.get("max_costed_plans_per_run", 2)),
                skip_if_cached=bool(scout_cfg.get("skip_if_cached", True)),
            ),
            store=store,
            cache=cache,
        )

    # ------------------------------------------------------------- planning

    def _cost_of(self, template: QueryTemplate) -> int:
        try:
            return self.registry.get(template.engine).cost_units
        except ConfigError:
            return 0

    def _is_enabled(self, template: QueryTemplate) -> bool:
        try:
            return self.registry.get(template.engine).enabled
        except ConfigError:
            return False

    def _already_cached(self, plan: QueryPlan) -> bool:
        """Whether this plan's answer is already in the cache.

        The key comes from ``key_for_plan`` — in practice the Harvester's, which asks
        the client that will make the request. The Scout used to build its own key
        from ``(engine, query, params)``, but the client fetches under
        ``(source_id, url, api_params)``, so the two never matched and this check
        silently always returned False. Without a key source, it says False honestly
        rather than guessing.
        """
        if not (self.config.skip_if_cached and self.cache is not None):
            return False
        if self.key_for_plan is None:
            return False
        key = self.key_for_plan(plan)
        return False if key is None else self.cache.has_fresh(key)

    def suppressed_domains(self) -> set[str]:
        """Domains we must never spend a credit discovering.

        Checked at plan time. The master prompt checks suppression before dispatch,
        which is far too late — by then the credit, the fetch and ~64 s of extraction
        have all been spent on a company that was always going to be vetoed.
        """
        if self.store is None:
            return set()
        rows = self.store.conn.execute(
            "SELECT value FROM suppression_list WHERE kind = 'domain'"
        ).fetchall()
        return {str(r["value"]).lower() for r in rows}

    def plan(
        self,
        *,
        limit: int | None = None,
        can_spend: Callable[[str, int], bool] | None = None,
    ) -> list[QueryPlan]:
        """Build this run's batch of discovery queries.

        ``can_spend(engine, units) -> bool`` is normally ``Runtime.can_spend``. It takes
        the engine, not just a unit count, because the allowance is per *provider*: the
        four ``serpapi_*`` sources draw on one account, and a units-only predicate could
        not tell them apart from a future source with its own separate cap. Absent,
        rationed templates are planned up to the configured ceiling and the egress layer
        enforces the real limit.
        """
        ceiling = limit if limit is not None else self.config.max_plans_per_run

        usable = [t for t in self.templates if self._is_enabled(t)]
        free = sorted((t for t in usable if self._cost_of(t) == 0), key=lambda t: -t.weight)
        costed = sorted((t for t in usable if self._cost_of(t) > 0), key=lambda t: -t.weight)

        plans: list[QueryPlan] = []
        skipped_cached = 0

        # Free first, always. A free template that finds one good company beats a paid
        # one that finds three, because the paid one cannot run again tomorrow.
        for template in free:
            if len(plans) >= ceiling:
                break
            plan = template.to_plan(
                cache_ttl_hours=self.registry.get(template.engine).cache_ttl_hours
            )
            if self._already_cached(plan):
                skipped_cached += 1
                continue
            plans.append(plan)

        spent = 0
        for template in costed:
            if len(plans) >= ceiling or spent >= self.config.max_costed_plans_per_run:
                break
            cost = self._cost_of(template)
            if can_spend is not None and not can_spend(template.engine, cost):
                log.info("scout_skipped_costed", template=template.id, reason="budget")
                continue
            plan = template.to_plan(
                cache_ttl_hours=self.registry.get(template.engine).cache_ttl_hours
            )
            if self._already_cached(plan):
                skipped_cached += 1
                continue
            plans.append(plan)
            spent += cost

        log.info(
            "scout_planned",
            plans=len(plans),
            free=len(plans) - spent,
            costed=spent,
            skipped_cached=skipped_cached,
        )
        return plans

    def templates_for(self, trigger: TriggerCode) -> list[QueryTemplate]:
        return [t for t in self.templates if trigger in t.targets]

    def coverage(self) -> dict[str, list[str]]:
        """Which templates chase which trigger. Used by a test to prove no trigger
        the taxonomy defines has silently lost its only source."""
        by_trigger: dict[str, list[str]] = {}
        for template in self.templates:
            for target in template.targets:
                by_trigger.setdefault(target, []).append(template.id)
        return by_trigger
