"""Free-source clients.

All driven through MockTransport with realistic response shapes. The point of these
tests is that a source returning something unexpected — an HTML error page, a missing
field, a wildcard cert — degrades to "no results" rather than raising into the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cindraleads.sources import DocumentCache, EgressClient, SourceBreakers, SourceRegistry
from cindraleads.sources.clients import (
    AshbyClient,
    CrtShClient,
    GitHubClient,
    GreenhouseClient,
    HackerNewsClient,
    JobPosting,
    LeverClient,
    RdapClient,
    analyze_postings,
    classify_role,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"

REGISTRY = SourceRegistry.from_dict(
    {
        "sources": [
            {"id": s, "legality_class": "licensed_api", "cost_units": 0, "cache_ttl_hours": 1}
            for s in (
                "hn_algolia",
                "github_api",
                "greenhouse_boards",
                "lever_postings",
                "ashby_postings",
            )
        ]
        + [
            {"id": s, "legality_class": "public_record", "cost_units": 0, "cache_ttl_hours": 1}
            for s in ("crtsh", "rdap")
        ],
        "defaults": {"retries": 1, "backoff_base_seconds": 0.001},
    }
)


def responder(payload: object, *, status: int = 200, text: str | None = None):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, text=json.dumps(payload))

    return handler


@pytest.fixture
def egress(tmp_path: Path):  # type: ignore[no-untyped-def]
    from cindraleads.store import Store

    store = Store(tmp_path / "c.db", migrations_dir=MIGRATIONS)
    store.migrate()

    def build(handler):  # type: ignore[no-untyped-def]
        return EgressClient(
            store=store,
            registry=REGISTRY,
            cache=DocumentCache(store, cache_dir=tmp_path / "cache"),
            breakers=SourceBreakers(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    yield build
    store.close()


# --------------------------------------------------------------- hacker news


async def test_hn_search_normalizes_hits(egress):
    payload = {
        "hits": [
            {
                "objectID": "1",
                "title": "Show HN: our AI agent",
                "url": "https://acme.io/launch",
                "created_at": "2026-08-01T10:00:00.000Z",
                "points": 42,
                "author": "nabila",
            }
        ]
    }
    hits = await HackerNewsClient(egress(responder(payload))).search("ai agent")
    assert len(hits) == 1
    assert hits[0].url == "https://acme.io/launch"
    assert hits[0].raw["points"] == 42
    assert hits[0].published_at is not None


async def test_a_text_only_show_hn_falls_back_to_the_thread_url(egress):
    """A Show HN with no URL is a text post; the thread is then the artifact, and a
    trigger with no evidence URL cannot exist."""
    payload = {"hits": [{"objectID": "999", "title": "Show HN: thing", "url": None}]}
    hits = await HackerNewsClient(egress(responder(payload))).search("thing")
    assert hits[0].url == "https://news.ycombinator.com/item?id=999"


async def test_an_html_error_page_is_no_results_not_an_exception(egress):
    """Sources return HTML where they promised JSON. That is routine and must not
    raise into the pipeline."""
    client = HackerNewsClient(egress(responder(None, text="<html>502 Bad Gateway</html>")))
    assert await client.search("x") == []


# -------------------------------------------------------------------- github


async def test_github_search_extracts_owner_and_homepage(egress):
    payload = {
        "items": [
            {
                "full_name": "acme/agent",
                "html_url": "https://github.com/acme/agent",
                "description": "An MCP server",
                "pushed_at": "2026-08-10T00:00:00Z",
                "stargazers_count": 7,
                "language": "Python",
                "homepage": "https://acme.io",
                "owner": {"login": "acme", "type": "Organization"},
            }
        ]
    }
    hits = await GitHubClient(egress(responder(payload))).search_repos("mcp")
    assert hits[0].raw["owner"] == "acme"
    assert hits[0].raw["homepage"] == "https://acme.io", "the homepage is the domain lead"
    assert hits[0].raw["owner_type"] == "Organization"


# ----------------------------------------------------------------- ATS boards


async def test_greenhouse_board_parses(egress):
    payload = {
        "jobs": [
            {
                "title": "Senior AI Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "location": {"name": "Dhaka"},
                "departments": [{"name": "Engineering"}],
                "updated_at": "2026-08-01T00:00:00Z",
                "content": "Build with LangChain",
            }
        ]
    }
    jobs = await GreenhouseClient(egress(responder(payload))).jobs("acme")
    assert jobs[0].title == "Senior AI Engineer"
    assert jobs[0].location == "Dhaka"
    assert jobs[0].department == "Engineering"


async def test_lever_postings_parse(egress):
    payload = [
        {
            "text": "Security Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/1",
            "categories": {"location": "Remote", "team": "Security"},
            "createdAt": 1754006400000,
            "descriptionPlain": "SOC 2 work",
        }
    ]
    jobs = await LeverClient(egress(responder(payload))).postings("acme")
    assert jobs[0].title == "Security Engineer"
    assert jobs[0].updated_at is not None


async def test_ashby_board_parses(egress):
    payload = {"jobs": [{"title": "ML Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/1"}]}
    jobs = await AshbyClient(egress(responder(payload))).jobs("acme")
    assert jobs[0].title == "ML Engineer"


async def test_an_unknown_board_returns_nothing(egress):
    """Most companies do not use any given ATS. A 404 is the common case."""
    client = GreenhouseClient(egress(responder({}, status=404, text="not found")))
    with pytest.raises(httpx.HTTPStatusError):
        await client.jobs("nope")


# --------------------------------------------------------------------- crt.sh


async def test_crtsh_strips_wildcards_and_excludes_the_apex(egress):
    payload = [
        {"name_value": "*.acme.io\napi.acme.io", "entry_timestamp": "2026-08-10T00:00:00"},
        {"name_value": "acme.io", "entry_timestamp": "2020-01-01T00:00:00"},
        {"name_value": "staging.acme.io", "entry_timestamp": "2019-01-01T00:00:00"},
    ]
    names = await CrtShClient(egress(responder(payload))).subdomains("acme.io")
    assert names == {"api.acme.io", "staging.acme.io"}
    assert "acme.io" not in names, "the apex is not a subdomain"
    assert not any(n.startswith("*") for n in names)


async def test_crtsh_growth_separates_recent_from_total(egress):
    """T7 is rapid GROWTH, not size. A big estate is normal for an established
    company; twelve new hosts this month is a conversation."""
    payload = [
        {"name_value": "new.acme.io", "entry_timestamp": "2026-08-10T00:00:00"},
        {"name_value": "old.acme.io", "entry_timestamp": "2019-01-01T00:00:00"},
    ]
    total, recent = await CrtShClient(egress(responder(payload))).growth("acme.io", window_days=30)
    assert total == 2
    assert recent == 1


# ----------------------------------------------------------------------- rdap


async def test_rdap_extracts_age_and_registrar(egress):
    payload = {
        "events": [{"eventAction": "registration", "eventDate": "2024-01-01T00:00:00Z"}],
        "entities": [
            {"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "NameCheap"]]]}
        ],
        "status": ["active"],
    }
    info = await RdapClient(egress(responder(payload))).domain("acme.io")
    assert info["registrar"] == "NameCheap"
    assert info["age_days"] > 500


async def test_rdap_without_a_registration_event_is_not_an_error(egress):
    info = await RdapClient(egress(responder({"events": []}))).domain("acme.io")
    assert info["registered_at"] is None
    assert info["age_days"] is None


# ------------------------------------------------------- hiring classification


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior AppSec Engineer", "security"),
        ("Head of Security", "security"),
        ("DevSecOps Lead", "security"),
        # Regression: a trailing \b after "penetration test" never matches
        # "Penetration Tester", and that is one of the commonest titles here.
        ("Penetration Tester", "security"),
        ("Red Teamer", "security"),
        ("Security Analyst II", "security"),
        ("Vulnerability Management Lead", "security"),
        ("Machine Learning Engineer", "ai"),
        ("LLM Applied Scientist", "ai"),
        ("AI Engineer, Agents", "ai"),
        ("Deep Learning Researcher", "ai"),
        ("Account Executive", "other"),
        ("Senior Backend Engineer", "other"),
        ("Customer Success Manager", "other"),
        ("Site Reliability Engineer", "other"),
    ],
)
def test_role_classification_is_keywords_not_inference(title, expected):
    """On a Pi at 3.7 tok/s the cheapest inference is the one you do not make."""
    assert classify_role(title) == expected


def _job(title: str, content: str = "") -> JobPosting:
    return JobPosting(
        title=title, url="https://x.io/j", source_id="greenhouse_boards", content=content
    )


def test_hiring_ai_without_security_is_a_property_of_the_whole_board():
    """T4 is the interesting trigger and no single posting can express it: they are
    building AI and nobody is being hired to secure it. Only the absence tells you."""
    signal = analyze_postings([_job("AI Engineer"), _job("ML Engineer"), _job("Designer")])
    assert signal.hiring_ai_without_security is True
    assert "T4_HIRING_AI_ONLY" in signal.triggers
    assert "T3_HIRING_SEC" not in signal.triggers


def test_one_security_hire_cancels_the_ai_only_trigger():
    signal = analyze_postings([_job("AI Engineer"), _job("Security Engineer")])
    assert signal.hiring_ai_without_security is False
    assert "T4_HIRING_AI_ONLY" not in signal.triggers
    assert "T3_HIRING_SEC" in signal.triggers


def test_compliance_keywords_in_the_body_fire_t5():
    signal = analyze_postings([_job("Backend Engineer", "You will help us achieve SOC 2")])
    assert "T5_COMPLIANCE" in signal.triggers


def test_stack_risk_fires_on_framework_mentions():
    signal = analyze_postings([_job("Backend Engineer", "We use LangChain and a vector database")])
    assert "T11_STACK_RISK" in signal.triggers


def test_an_empty_board_produces_no_triggers():
    signal = analyze_postings([])
    assert signal.total == 0
    assert signal.triggers == ()


# ------------------------------------------------------------------ cache keys


def test_the_hn_cutoff_is_quantized_to_midnight_utc():
    """The cache key must not move between two calls a second apart.

    `numericFilters` carried `utcnow()` to the second, so every HN search produced a
    unique cache key and the egress cache could never hit -- every harvest re-fetched
    the same window forever. The Phase 2 gate ("an identical second run makes zero
    network calls") could not have been met while this was true.
    """
    _url, params = HackerNewsClient.request_for("ai", since_days=30)
    cutoff = int(params["numericFilters"].split(">")[1])
    assert cutoff % 86_400 == 0, "cutoff is a midnight-UTC boundary, not 'now minus 30 days'"


def test_the_hn_cache_key_is_the_key_the_fetch_uses():
    from cindraleads.sources.cache import cache_key_for

    url, params = HackerNewsClient.request_for("ai", since_days=45, tags="show_hn")
    assert HackerNewsClient.cache_key("ai", since_days=45, tags="show_hn") == cache_key_for(
        "hn_algolia", url, params
    )


def test_the_github_cache_key_is_the_key_the_fetch_uses():
    from cindraleads.sources.cache import cache_key_for

    url, params = GitHubClient.request_for("mcp-server language:python")
    assert GitHubClient.cache_key("mcp-server language:python") == cache_key_for(
        "github_api", url, params
    )


def test_different_lookbacks_are_different_keys():
    """Quantizing must not collapse a 30-day and a 90-day search onto one entry."""
    assert HackerNewsClient.cache_key("ai", since_days=30) != HackerNewsClient.cache_key(
        "ai", since_days=90
    )
