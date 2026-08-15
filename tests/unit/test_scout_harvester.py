"""Scout planning and the Harvester stage."""

from __future__ import annotations

import json
import typing
from pathlib import Path

import httpx
import pytest

from cindraleads.agents import HARVEST_KIND, Harvester, Scout
from cindraleads.config import settings
from cindraleads.models import QueryPlan, TriggerCode
from cindraleads.queue import JobQueue
from cindraleads.sources import DocumentCache, EgressClient, SourceBreakers, SourceRegistry
from cindraleads.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"
CONFIG_DIR = REPO_ROOT / "config"

# Triggers that legitimately have no DISCOVERY template, with the reason. Anything
# not listed here must be findable for free.
ENRICHMENT_ONLY: dict[str, str] = {
    "T7_SURFACE_SPRAWL": "crt.sh on a known domain; nothing to discover from",
    "T8_HYGIENE_GAP": "DNS lookup on a known domain",
    "T0_INBOUND": "our own inbox, Phase 6",
}
# Triggers with no free equivalent, where spending a credit is the right call.
PAID_JUSTIFIED: dict[str, str] = {
    "T9_MARKETPLACE": "no free source exists for someone actively shopping for a pentest",
}


@pytest.fixture
def shipped_registry() -> SourceRegistry:
    cfg = settings()
    object.__setattr__(cfg, "config_dir", CONFIG_DIR)
    return SourceRegistry.from_config(cfg)


@pytest.fixture
def scout(shipped_registry: SourceRegistry) -> Scout:
    cfg = settings()
    object.__setattr__(cfg, "config_dir", CONFIG_DIR)
    return Scout.from_config(shipped_registry, config=cfg)


# --------------------------------------------------------------- scout config


def test_templates_load_and_reference_real_sources(scout: Scout):
    assert len(scout.templates) >= 9
    for template in scout.templates:
        assert scout.registry.get(template.engine)


def test_a_template_pointing_at_an_unknown_source_is_fatal(shipped_registry, tmp_path: Path):
    """A typo in `engine` would silently stop finding a whole class of trigger."""
    from cindraleads.errors import ConfigError

    (tmp_path / "icp.yaml").write_text(
        "query_templates:\n  - id: t\n    engine: not_a_source\n    query: x\n"
    )
    cfg = settings()
    object.__setattr__(cfg, "config_dir", tmp_path)
    with pytest.raises(ConfigError, match="unknown source"):
        Scout.from_config(shipped_registry, config=cfg)


# ------------------------------------------------------------------- coverage


def test_every_trigger_is_free_reachable_or_explicitly_excused(scout: Scout):
    """Guards decision 7.

    A coverage audit found T2/T3/T4/T6/T12 reachable only through a rationed source,
    which quietly undercut the whole free-first argument. This makes that state
    impossible to reach again by accident: a trigger either has a free discovery
    template, or it is listed above with a reason.
    """
    coverage = scout.coverage()
    costed_engines = {s.id for s in scout.registry.sources.values() if s.cost_units}

    problems: list[str] = []
    for trigger in typing.get_args(TriggerCode):
        if trigger in ENRICHMENT_ONLY or trigger in PAID_JUSTIFIED:
            continue
        template_ids = coverage.get(trigger, [])
        free = [
            tid
            for tid in template_ids
            if next(t.engine for t in scout.templates if t.id == tid) not in costed_engines
        ]
        if not free:
            problems.append(f"{trigger}: no free discovery template (paid: {template_ids})")
    assert not problems, "\n".join(problems)


def test_the_excuse_lists_do_not_rot(scout: Scout):
    """If a free template later covers an excused trigger, the excuse should go."""
    coverage = scout.coverage()
    costed = {s.id for s in scout.registry.sources.values() if s.cost_units}
    for trigger in ENRICHMENT_ONLY:
        free = [
            tid
            for tid in coverage.get(trigger, [])
            if next(t.engine for t in scout.templates if t.id == tid) not in costed
        ]
        assert not free, f"{trigger} now has free coverage {free}; remove it from ENRICHMENT_ONLY"


# ------------------------------------------------------------------- planning


def test_free_templates_are_planned_before_rationed_ones(scout: Scout):
    """SerpAPI is ~7 queries/day. A free template that finds one good company beats a
    paid one that finds three, because the paid one cannot run again tomorrow."""
    plans = scout.plan()
    costs = [scout.registry.get(p.engine).cost_units for p in plans]
    first_paid = next((i for i, c in enumerate(costs) if c > 0), len(costs))
    assert all(c == 0 for c in costs[:first_paid])
    assert all(c > 0 for c in costs[first_paid:]), "free and paid must not interleave"


def test_rationed_plans_are_capped_per_run(scout: Scout):
    plans = scout.plan()
    paid = [p for p in plans if scout.registry.get(p.engine).cost_units]
    assert len(paid) <= scout.config.max_costed_plans_per_run


def test_an_exhausted_budget_produces_a_free_only_batch(scout: Scout):
    plans = scout.plan(can_spend=lambda _engine, _units: False)
    assert plans, "the run must still happen; free sources are unaffected"
    assert all(scout.registry.get(p.engine).cost_units == 0 for p in plans)


def test_the_limit_is_respected(scout: Scout):
    assert len(scout.plan(limit=3)) == 3


def test_a_cached_query_is_not_replanned(scout: Scout, rig, tmp_path: Path):
    """Planning a guaranteed cache hit wastes a slot in the batch on documents we
    already have.

    The key must be the one the *client* fetches under. This test previously wrote
    the cache entry with the Scout's own `cache_key_for(engine, query, params)`, which
    matched the Scout's own lookup and so passed — while production wrote under
    `(source_id, url, api_params)` and skip_if_cached never once fired.
    """
    harvester, store = rig(hn_payload())
    cache = DocumentCache(store, cache_dir=tmp_path / "cache")
    scout.cache = cache
    scout.key_for_plan = harvester.cache_key_for_plan

    before = scout.plan()
    target = next(p for p in before if p.engine in ("hn_algolia", "github_api"))
    key = scout.key_for_plan(target)
    assert key is not None
    cache.put(
        key,
        "cached body",
        url="https://x.io",
        source_id=target.engine,
        legality_class="licensed_api",
        ttl_hours=24,
    )
    after = scout.plan()
    assert len(after) == len(before) - 1
    assert not any(p.query == target.query and p.engine == target.engine for p in after)


def test_the_scout_key_matches_the_key_the_client_actually_fetches(rig):
    """A direct pin on the mismatch, independent of the planning path."""
    from cindraleads.sources.cache import cache_key_for
    from cindraleads.sources.clients import HackerNewsClient

    harvester, _ = rig(hn_payload())
    plan = QueryPlan(query="ai", engine="hn_algolia", params={"since_days": "30"})

    url, params = HackerNewsClient.request_for("ai", since_days=30, tags="story")

    assert harvester.cache_key_for_plan(plan) == cache_key_for("hn_algolia", url, params)


def test_an_engine_with_no_client_has_no_cache_key(rig):
    harvester, _ = rig(hn_payload())
    assert harvester.cache_key_for_plan(QueryPlan(query="x", engine="not_wired_yet")) is None


def test_every_planned_engine_has_a_client(scout: Scout, rig):
    """A plan the Harvester cannot execute becomes a no-op job.

    That is how T9_MARKETPLACE sat dead for a phase: `icp.yaml` planned two SerpAPI
    queries every run, the Harvester had no client for them, and each one completed
    successfully having done nothing at all. Silence, not an error.
    """
    harvester, _ = rig(hn_payload())
    unsupported = sorted({t.engine for t in scout.templates if not harvester.supports(t.engine)})
    assert not unsupported, f"icp.yaml plans engines the Harvester cannot run: {unsupported}"


# ------------------------------------------------------------------ harvester


@pytest.fixture
def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "h.db", migrations_dir=MIGRATIONS)
    store.migrate()
    registry = SourceRegistry.from_dict(
        {
            "sources": [
                {"id": "hn_algolia", "legality_class": "licensed_api", "cache_ttl_hours": 1},
                {"id": "github_api", "legality_class": "licensed_api", "cache_ttl_hours": 1},
            ],
            "defaults": {"retries": 1, "backoff_base_seconds": 0.001},
        }
    )

    def build(handler):  # type: ignore[no-untyped-def]
        egress = EgressClient(
            store=store,
            registry=registry,
            cache=DocumentCache(store, cache_dir=tmp_path / "cache"),
            breakers=SourceBreakers(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return Harvester(store=store, egress=egress, queue=JobQueue(store)), store

    yield build
    store.close()


def hn_payload(*urls: str):  # type: ignore[no-untyped-def]
    hits = [{"objectID": str(i), "title": f"t{i}", "url": u} for i, u in enumerate(urls)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"hits": hits}))

    return handler


async def test_a_harvest_produces_extract_jobs(rig):
    harvester, _ = rig(hn_payload("https://a.io", "https://b.io"))
    plan = QueryPlan(query="ai", engine="hn_algolia", targets=["T1_AI_SHIP"])
    job = _job(plan)

    result = await harvester.run(job)
    assert result.ok
    assert len(result.follow_on) == 2
    assert all(kind == "extract.candidate" for kind, _ in result.follow_on)
    assert {p["url"] for _, p in result.follow_on} == {"https://a.io", "https://b.io"}


async def test_a_url_already_seen_is_not_extracted_twice(rig):
    """Two templates legitimately surface the same Show HN post. Deduplicating here
    rather than after extraction saves ~64 s of Pi inference per duplicate."""
    harvester, _ = rig(hn_payload("https://dupe.io"))
    plan = QueryPlan(query="ai", engine="hn_algolia")

    first = await harvester.run(_job(plan))
    second = await harvester.run(_job(plan))

    assert len(first.follow_on) == 1
    assert second.follow_on == [], "the second sighting produces no new work"


async def test_a_dead_source_does_not_fail_the_stage(rig):
    """One broken source must not take the batch down with it."""

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    harvester, _ = rig(broken)
    result = await harvester.run(_job(QueryPlan(query="x", engine="hn_algolia")))
    assert result.ok, "the stage completes; the source simply returned nothing"
    assert result.follow_on == []


async def test_an_unsupported_engine_is_skipped_not_fatal(rig):
    harvester, _ = rig(hn_payload())
    result = await harvester.run(_job(QueryPlan(query="x", engine="serpapi_search")))
    assert result.ok
    assert result.follow_on == []


async def test_a_malformed_payload_fails_cleanly(rig):
    from cindraleads.models import Job

    harvester, _ = rig(hn_payload())
    job = Job(job_id="j", kind=HARVEST_KIND, payload={"not": "a plan"})
    result = await harvester.run(job)
    assert result.ok is False
    assert "bad QueryPlan" in (result.error or "")


def test_enqueueing_the_same_plan_twice_inside_the_window_is_one_job(rig):
    harvester, store = rig(hn_payload())
    plan = QueryPlan(query="ai agents", engine="hn_algolia", params={"since_days": "30"})

    first_ids, first_new = harvester.enqueue_plans([plan])
    second_ids, second_new = harvester.enqueue_plans([plan])

    assert first_ids == second_ids, "the dedupe key is the plan's identity"
    assert (first_new, second_new) == (1, 0), "the second call queued nothing new"
    assert JobQueue(store).stats()["pending"] == 1


def test_the_same_plan_runs_again_once_its_cache_window_lapses():
    """The bug this exists to prevent.

    `JobQueue.enqueue` matches a dedupe key across *every* job, completed ones
    included. With a key of just (engine, query, params), a query that had run once
    could never run again — the first harvest worked and every later one deduped onto
    the finished jobs and did nothing. The hourly timer would have harvested once at
    boot and idled forever, with a queue that looked perfectly healthy.
    """
    from datetime import UTC, datetime, timedelta

    plan = QueryPlan(query="ai agents", engine="hn_algolia", cache_ttl_hours=6)
    # A fixed instant on a 6 h boundary. Using utcnow() made this flaky: "now" and
    # "now + 1 h" land in different buckets whenever the clock is inside an hour of
    # one, so the test failed roughly one run in six.
    start = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)

    same_window = Harvester.dedupe_key_for(plan, now=start + timedelta(hours=1))
    next_window = Harvester.dedupe_key_for(plan, now=start + timedelta(hours=7))

    assert Harvester.dedupe_key_for(plan, now=start) == same_window
    assert next_window != same_window, "a lapsed cache window is genuinely new work"


def test_a_longer_ttl_holds_the_query_back_longer():
    """The bucket is the plan's own TTL, not a fixed interval: news is cached for
    12 h and marketplace listings for 12 h, but a repo search for 72 h."""
    from datetime import timedelta

    from cindraleads.models import utcnow

    now = utcnow()
    short = QueryPlan(query="q", engine="hn_algolia", cache_ttl_hours=1)
    long = QueryPlan(query="q", engine="hn_algolia", cache_ttl_hours=72)
    later = now + timedelta(hours=2)

    assert Harvester.dedupe_key_for(short, now=now) != Harvester.dedupe_key_for(short, now=later)
    assert Harvester.dedupe_key_for(long, now=now) == Harvester.dedupe_key_for(long, now=later)


def _job(plan: QueryPlan):  # type: ignore[no-untyped-def]
    from cindraleads.models import Job

    return Job(job_id="job1", kind=HARVEST_KIND, payload=plan.model_dump(mode="json"))


# ------------------------------------------------------- what is worth extracting


def test_a_repo_with_a_homepage_is_extracted_at_the_company_site(rig):
    """GitHub's API hands us the company's own site next to the repo. Reading the
    landing page instead of a README is the difference between a company profile and
    a project description."""
    from cindraleads.agents.harvester import extraction_target
    from cindraleads.sources.clients import SourceHit

    hit = SourceHit(
        url="https://github.com/acme/agent",
        title="acme/agent",
        snippet="",
        source_id="github_api",
        raw={"homepage": "https://acme.io"},
    )
    assert extraction_target(hit) == "https://acme.io"


def test_a_bare_homepage_gets_a_scheme(rig):
    from cindraleads.agents.harvester import extraction_target
    from cindraleads.sources.clients import SourceHit

    hit = SourceHit(
        url="https://github.com/acme/agent",
        title="",
        snippet="",
        source_id="github_api",
        raw={"homepage": "acme.io"},
    )
    assert extraction_target(hit) == "https://acme.io"


def test_a_platform_url_with_no_company_site_is_not_worth_extracting(rig):
    """Measured on the first real Pi run: 13 of 51 resolutions were platform URLs the
    Resolver dropped, each having cost ~60 s of inference first. The Harvester can
    reach the same conclusion for free, and doing so also stops github.com consuming
    the 6-per-domain budget meant for a prospect's own infrastructure."""
    from cindraleads.agents.harvester import extraction_target
    from cindraleads.sources.clients import SourceHit

    for url in (
        "https://github.com/someone/sideproject",
        "https://news.ycombinator.com/item?id=1",
        "https://www.linkedin.com/company/acme",
    ):
        hit = SourceHit(url=url, title="", snippet="", source_id="hn_algolia", raw={})
        assert extraction_target(hit) is None, url


async def test_a_platform_hit_produces_no_extract_job(rig):
    harvester, store = rig(hn_payload("https://news.ycombinator.com/item?id=99"))
    result = await harvester.run(_job(QueryPlan(query="x", engine="hn_algolia")))
    assert result.ok
    assert result.follow_on == []
    assert store.conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"] == 0


def test_an_ordinary_company_url_is_unaffected(rig):
    from cindraleads.agents.harvester import extraction_target
    from cindraleads.sources.clients import SourceHit

    hit = SourceHit(url="https://acme.io/blog/x", title="", snippet="", source_id="x", raw={})
    assert extraction_target(hit) == "https://acme.io/blog/x"
