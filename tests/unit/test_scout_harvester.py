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
    # The cached template is gone from the batch. Deliberately not asserted by count:
    # there are more templates than the per-run plan ceiling, so skipping one simply
    # lets the next-highest-weighted template take the freed slot -- which is the
    # intended behaviour and would make a count-based assertion read as a regression.
    assert not any(p.template_id == target.template_id for p in after)
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


# ------------------------------------------------- discovery quality (Phase 7 follow-up)


def test_company_shaped_templates_outrank_project_shaped_ones():
    """The reweighting, as an assertion rather than a comment.

    The first corpus reached 148 companies at 82% T1_AI_SHIP -- a tic-tac-toe game, a
    world clock, a personal blog -- because unfiltered Show HN sat at weight 95 and the
    HN hiring thread at 72. With a 12-plan budget per run the project sources were
    consuming it before the company sources were reached.
    """
    from cindraleads.agents.scout import Scout
    from cindraleads.sources.registry import SourceRegistry

    templates = {t.id: t for t in Scout.from_config(SourceRegistry.from_config()).templates}

    # A hit here implies payroll or investors.
    #
    # `hn_who_is_hiring` briefly left this list, and putting it back is the point of
    # `comments: true`. The invariant asks what a *hit* implies, not what the source is
    # about -- and while a hit was the thread's own news.ycombinator.com URL it implied
    # nothing, which is why 19 hits produced 0 candidates. Now a hit is a comment naming
    # a company, so the premise and the mechanism finally agree.
    company_shaped = ("hn_who_is_hiring", "hn_hiring_ai_roles", "hn_funding")
    # A hit here implies someone shipped something, which anyone can do.
    project_shaped = ("hn_show_ai", "hn_ai_agent")

    worst_company = min(templates[t].weight for t in company_shaped)
    best_project = max(templates[t].weight for t in project_shaped)
    assert worst_company > best_project, (
        "a template that only proves someone shipped a thing outranks one that proves "
        "a company has payroll"
    )


def test_every_template_id_is_unique():
    """Ids are the provenance key written to `companies.discovered_by`. A duplicate
    would silently merge two templates' yield and make the report lie."""
    from cindraleads.agents.scout import Scout
    from cindraleads.sources.registry import SourceRegistry

    ids = [t.id for t in Scout.from_config(SourceRegistry.from_config()).templates]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_a_plan_carries_the_template_that_made_it():
    """Without this there is no way to tell which query found the funded healthtech
    and which found the tic-tac-toe game, and every rewrite is a guess."""
    from cindraleads.agents.scout import Scout
    from cindraleads.sources.registry import SourceRegistry

    plans = Scout.from_config(SourceRegistry.from_config()).plan(limit=5)
    assert plans
    assert all(p.template_id for p in plans), [p.query for p in plans if not p.template_id]


# --------------------------------------------------- thread comments (Phase 7 follow-up)


def test_a_comment_url_is_the_company_not_the_platform():
    """The extraction that makes the hiring thread usable at all.

    Real comments are HTML fragments: a hiring post links its own site, and mentions
    its ATS board, its Twitter, or a Show HN somewhere further down. First-wins over
    hrefs before bare text is what keeps the company's domain ahead of those.
    """
    from cindraleads.sources.clients import company_url_in

    assert company_url_in('We are hiring! <a href="https://acme.io/careers">apply</a>') == (
        "https://acme.io/careers"
    )
    # The platform is skipped and the company behind it found instead.
    assert (
        company_url_in(
            'Acme | Remote | <a href="https://boards.greenhouse.io/acme">jobs</a> '
            '<a href="https://acme.io">acme.io</a>'
        )
        == "https://acme.io"
    )
    # Bare text, which plenty of comments use.
    assert company_url_in("Acme (acme.io) is hiring, see https://acme.io/jobs") == (
        "https://acme.io/jobs"
    )
    # Trailing punctuation is not part of the URL.
    assert company_url_in("see https://acme.io/jobs.") == "https://acme.io/jobs"


def test_a_comment_with_no_company_url_is_skipped_not_guessed():
    """Replies, "how do I apply" questions, and pitches with the address in an image
    all name no domain. Inferring one would be exactly the unsourced claim the evidence
    rule forbids -- and rule 2 says no evidence, no lead."""
    from cindraleads.sources.clients import company_url_in

    assert company_url_in("Does anyone know if this role is still open?") is None
    assert company_url_in('<a href="https://news.ycombinator.com/item?id=1">see above</a>') is None
    assert company_url_in("") is None


def _thread_payload(comments: list[dict]):  # type: ignore[no-untyped-def]
    """A story search followed by that story's comments, on the two Algolia paths."""
    story = {
        "objectID": "42",
        "title": "Ask HN: Who is hiring? (August 2026)",
        "url": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "search_by_date" in str(request.url):
            return httpx.Response(200, text=json.dumps({"hits": [story]}))
        return httpx.Response(200, text=json.dumps({"hits": comments}))

    return handler


async def test_the_hiring_thread_yields_the_companies_in_its_comments(rig):
    """The defect this whole mechanism exists for: 19 hits, 16 platform drops, 0
    candidates, every run, on the strongest free company-shaped source there is.

    The thread's own URL must not survive into the hit list. Returning it alongside the
    comments would put news.ycombinator.com back in front of the drop rule and charge
    this template for it -- the exact number that made it look broken.
    """
    harvester, _ = rig(
        _thread_payload(
            [
                {
                    "objectID": "101",
                    "comment_text": 'Acme Health | Remote | <a href="https://acme.io">acme.io</a>',
                    "created_at": "2026-08-01T00:00:00Z",
                },
                {
                    "objectID": "102",
                    "comment_text": "Is this thread still active?",
                    "created_at": "2026-08-01T00:00:00Z",
                },
            ]
        )
    )
    plan = QueryPlan(
        query="Ask HN Who is hiring",
        engine="hn_algolia",
        params={"comments": "true"},
        targets=["T4_HIRING_AI_ONLY"],
    )

    result = await harvester.run(_job(plan))

    assert result.ok
    urls = {p["url"] for _, p in result.follow_on}
    assert urls == {"https://acme.io"}, "the company is extracted, the thread is not a hit"
    assert not any("ycombinator" in u for u in urls)


async def test_the_comment_keeps_the_hn_permalink_as_its_evidence(rig):
    """The company's page is what gets read; the comment is what we actually saw.

    Citing acme.io as the evidence for "they are hiring" would be a claim their landing
    page may not make. The permalink is dated, public and quotable, which is what rule 2
    asks of an evidence URL.
    """
    import json as _json

    harvester, store = rig(
        _thread_payload(
            [
                {
                    "objectID": "101",
                    "comment_text": 'Acme | <a href="https://acme.io">acme.io</a>',
                    "created_at": "2026-08-01T00:00:00Z",
                }
            ]
        )
    )
    plan = QueryPlan(query="Ask HN Who is hiring", engine="hn_algolia", params={"comments": "true"})

    await harvester.run(_job(plan))

    payload = _json.loads(store.conn.execute("SELECT raw_payload FROM candidates").fetchone()[0])
    assert "news.ycombinator.com/item?id=101" in _json.dumps(payload)


async def test_a_thread_without_the_flag_still_behaves_as_before(rig):
    """The expansion is opt-in per template. Every other HN template returns stories,
    and turning them all into comment readers would change what they mean."""
    harvester, _ = rig(_thread_payload([{"objectID": "101", "comment_text": "x"}]))
    plan = QueryPlan(query="ai agent", engine="hn_algolia")

    result = await harvester.run(_job(plan))

    # The story has no external url, so its own HN permalink is the hit -- a platform
    # URL, correctly dropped. Unchanged behaviour, asserted so the flag stays opt-in.
    assert result.follow_on == []


async def test_comment_expansion_is_bounded(rig):
    """A monthly hiring thread runs past 500 comments and each accepted one is ~64 s of
    decode. Unbounded, one harvest queues more than the Pi drains in a day and starves
    every other template behind it."""
    from cindraleads.agents.harvester import MAX_COMMENTS_PER_THREAD

    many = [
        {
            "objectID": str(i),
            "comment_text": f'Co{i} | <a href="https://co{i}.io">co{i}.io</a>',
            "created_at": "2026-08-01T00:00:00Z",
        }
        for i in range(MAX_COMMENTS_PER_THREAD + 25)
    ]
    harvester, _ = rig(_thread_payload(many))
    plan = QueryPlan(query="Ask HN Who is hiring", engine="hn_algolia", params={"comments": "true"})

    result = await harvester.run(_job(plan))

    assert len(result.follow_on) <= MAX_COMMENTS_PER_THREAD
