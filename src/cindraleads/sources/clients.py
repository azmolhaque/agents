"""Clients for the free sources.

Each one translates a domain question ("what has this company posted?") into a fetch
through the egress chokepoint plus a parse. They hold no network code of their own —
everything goes through :class:`EgressClient`, so robots, budgets, caching and circuit
breakers apply uniformly and there is still exactly one place that talks to the internet.

Two normalized shapes come out: :class:`SourceHit` for discovery (something happened,
here is a URL) and :class:`JobPosting` for the ATS boards. Normalizing at the edge means
the Extractor sees one shape rather than three vendors' JSON dialects.

The hiring analysis at the bottom is deliberately *not* an LLM call. "Is this job title a
security role?" is a keyword question, and on a Pi at 3.7 tok/s the cheapest inference is
the one you do not make.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from cindraleads.logging import get_logger
from cindraleads.models import TriggerCode, from_iso, utcnow
from cindraleads.sources.cache import cache_key_for
from cindraleads.sources.http import EgressClient

__all__ = [
    "AshbyClient",
    "CrtShClient",
    "GitHubClient",
    "GreenhouseClient",
    "HackerNewsClient",
    "HiringSignal",
    "JobPosting",
    "LeverClient",
    "RdapClient",
    "SourceHit",
    "analyze_postings",
    "classify_role",
]

log = get_logger("cindraleads.clients")


@dataclass(frozen=True)
class SourceHit:
    """One discovery result, normalized across sources."""

    url: str
    title: str
    snippet: str
    source_id: str
    published_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobPosting:
    title: str
    url: str
    source_id: str
    location: str | None = None
    department: str | None = None
    updated_at: datetime | None = None
    content: str = ""


def _safe_json(body: str, *, source_id: str, url: str) -> Any:
    """Parse JSON, or log and return None.

    A source returning an HTML error page where JSON was promised is routine. It must
    not raise into the pipeline; the stage treats it as "no results" and moves on.
    """
    try:
        return json.loads(body)
    except (ValueError, TypeError) as exc:
        log.warning("source_bad_json", source_id=source_id, url=url, error=str(exc)[:200])
        return None


# ------------------------------------------------------------------ discovery


class HackerNewsClient:
    """Algolia's HN API. Free, no auth. T1_AI_SHIP and T10_VENDOR_PRESSURE."""

    SOURCE_ID = "hn_algolia"

    def __init__(self, egress: EgressClient) -> None:
        self.egress = egress

    @classmethod
    def request_for(
        cls, query: str, *, since_days: int = 30, tags: str = "story", limit: int = 50
    ) -> tuple[str, dict[str, str]]:
        """The exact URL and params `search` will fetch.

        Separate from `search` so the cache key can be computed without making the
        request — the Scout needs to know whether an answer is already cached before
        it spends a slot in the batch planning one.

        The cutoff is **quantized to midnight UTC**, not to the current second. As a
        raw `utcnow()` timestamp it made every call a unique cache key, so the egress
        cache could never hit and every harvest re-fetched the same window. Day
        granularity costs nothing against a 30-120 day lookback.
        """
        midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = int((midnight - timedelta(days=since_days)).timestamp())
        return (
            "https://hn.algolia.com/api/v1/search_by_date",
            {
                "query": query,
                "tags": tags,
                "numericFilters": f"created_at_i>{cutoff}",
                "hitsPerPage": str(min(limit, 100)),
            },
        )

    @classmethod
    def cache_key(cls, query: str, **kwargs: Any) -> str:
        url, params = cls.request_for(query, **kwargs)
        return cache_key_for(cls.SOURCE_ID, url, params)

    async def search(
        self, query: str, *, since_days: int = 30, tags: str = "story", limit: int = 50
    ) -> list[SourceHit]:
        url, params = self.request_for(query, since_days=since_days, tags=tags, limit=limit)
        result = await self.egress.fetch(self.SOURCE_ID, url, params=params)
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, dict):
            return []

        hits: list[SourceHit] = []
        for item in data.get("hits", []):
            # A Show HN with no URL is a text post; the HN thread is then the artifact.
            url = item.get("url") or f"https://news.ycombinator.com/item?id={item.get('objectID')}"
            hits.append(
                SourceHit(
                    url=url,
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("story_text") or item.get("comment_text") or "")[:500],
                    source_id=self.SOURCE_ID,
                    published_at=_parse_iso(item.get("created_at")),
                    raw={"points": item.get("points"), "author": item.get("author")},
                )
            )
        return hits

    async def show_hn(self, *, since_days: int = 30) -> list[SourceHit]:
        """Show HN is where a small team announces the thing they just shipped —
        the highest-signal free source for T1_AI_SHIP."""
        return await self.search("", since_days=since_days, tags="show_hn")


class GitHubClient:
    """GitHub REST. 5,000 req/h authenticated, which is effectively unlimited here."""

    SOURCE_ID = "github_api"

    def __init__(self, egress: EgressClient, token: str | None = None) -> None:
        self.egress = egress
        self.token = token

    @classmethod
    def request_for(
        cls, query: str, *, sort: str = "updated", limit: int = 30
    ) -> tuple[str, dict[str, str]]:
        return (
            "https://api.github.com/search/repositories",
            {"q": query, "sort": sort, "order": "desc", "per_page": str(min(limit, 100))},
        )

    @classmethod
    def cache_key(cls, query: str, **kwargs: Any) -> str:
        url, params = cls.request_for(query, **kwargs)
        return cache_key_for(cls.SOURCE_ID, url, params)

    async def search_repos(
        self, query: str, *, sort: str = "updated", limit: int = 30
    ) -> list[SourceHit]:
        url, params = self.request_for(query, sort=sort, limit=limit)
        result = await self.egress.fetch(self.SOURCE_ID, url, params=params)
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, dict):
            return []

        hits: list[SourceHit] = []
        for repo in data.get("items", []):
            owner = repo.get("owner") or {}
            hits.append(
                SourceHit(
                    url=str(repo.get("html_url") or ""),
                    title=str(repo.get("full_name") or ""),
                    snippet=str(repo.get("description") or "")[:500],
                    source_id=self.SOURCE_ID,
                    published_at=_parse_iso(repo.get("pushed_at")),
                    raw={
                        "stars": repo.get("stargazers_count"),
                        "language": repo.get("language"),
                        "homepage": repo.get("homepage"),
                        "owner_type": owner.get("type"),
                        "owner": owner.get("login"),
                    },
                )
            )
        return hits

    async def stack_risk_repos(self, *, since_days: int = 180) -> list[SourceHit]:
        """T11_STACK_RISK: public code importing agent/LLM frameworks.

        Restricted to organizations. A personal side project is not a B2B prospect,
        and the anti-ICP rule already excludes unaffiliated individuals.
        """
        cutoff = (utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%d")
        return await self.search_repos(
            f"langchain OR mcp-server OR llamaindex OR autogen pushed:>{cutoff}"
        )


# ----------------------------------------------------------------- enrichment


class GreenhouseClient:
    """A company's Greenhouse board. Public JSON, no auth.

    This is the source that makes decision 7 work: T3/T4/T5/T11 were assigned to paid
    search in the master prompt, and they are free once the company is known.
    """

    SOURCE_ID = "greenhouse_boards"

    def __init__(self, egress: EgressClient) -> None:
        self.egress = egress

    async def jobs(self, board_token: str) -> list[JobPosting]:
        result = await self.egress.fetch(
            self.SOURCE_ID,
            f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs",
            params={"content": "true"},
        )
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, dict):
            return []
        return [
            JobPosting(
                title=str(job.get("title") or ""),
                url=str(job.get("absolute_url") or ""),
                source_id=self.SOURCE_ID,
                location=(job.get("location") or {}).get("name"),
                department=_first_name(job.get("departments")),
                updated_at=_parse_iso(job.get("updated_at")),
                content=str(job.get("content") or "")[:2000],
            )
            for job in data.get("jobs", [])
        ]


class LeverClient:
    SOURCE_ID = "lever_postings"

    def __init__(self, egress: EgressClient) -> None:
        self.egress = egress

    async def postings(self, company: str) -> list[JobPosting]:
        result = await self.egress.fetch(
            self.SOURCE_ID,
            f"https://api.lever.co/v0/postings/{company}",
            params={"mode": "json"},
        )
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, list):
            return []
        return [
            JobPosting(
                title=str(job.get("text") or ""),
                url=str(job.get("hostedUrl") or ""),
                source_id=self.SOURCE_ID,
                location=(job.get("categories") or {}).get("location"),
                department=(job.get("categories") or {}).get("team"),
                updated_at=_parse_epoch_ms(job.get("createdAt")),
                content=str(job.get("descriptionPlain") or "")[:2000],
            )
            for job in data
        ]


class AshbyClient:
    SOURCE_ID = "ashby_postings"

    def __init__(self, egress: EgressClient) -> None:
        self.egress = egress

    async def jobs(self, org: str) -> list[JobPosting]:
        result = await self.egress.fetch(
            self.SOURCE_ID, f"https://api.ashbyhq.com/posting-api/job-board/{org}"
        )
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, dict):
            return []
        return [
            JobPosting(
                title=str(job.get("title") or ""),
                url=str(job.get("jobUrl") or ""),
                source_id=self.SOURCE_ID,
                location=job.get("location"),
                department=job.get("department"),
                updated_at=_parse_iso(job.get("publishedAt")),
                content=str(job.get("descriptionPlain") or "")[:2000],
            )
            for job in data.get("jobs", [])
        ]


class CrtShClient:
    """Certificate Transparency. A public log, queried as a log — T7_SURFACE_SPRAWL."""

    SOURCE_ID = "crtsh"

    def __init__(self, egress: EgressClient) -> None:
        self.egress = egress

    async def subdomains(self, domain: str) -> set[str]:
        result = await self.egress.fetch(
            self.SOURCE_ID, "https://crt.sh/", params={"q": f"%.{domain}", "output": "json"}
        )
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, list):
            return set()

        names: set[str] = set()
        for entry in data:
            # name_value is newline-separated and full of wildcards.
            for raw in str(entry.get("name_value") or "").splitlines():
                name = raw.strip().lower().lstrip("*.")
                if name.endswith(domain) and name != domain:
                    names.add(name)
        return names

    async def growth(self, domain: str, *, window_days: int = 30) -> tuple[int, int]:
        """Returns ``(total, added_in_window)``.

        The trigger is *rapid growth*, not size. A large estate is normal for an
        established company; twelve new hosts this month is a change worth a
        conversation.
        """
        result = await self.egress.fetch(
            self.SOURCE_ID, "https://crt.sh/", params={"q": f"%.{domain}", "output": "json"}
        )
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, list):
            return 0, 0

        cutoff = utcnow() - timedelta(days=window_days)
        seen: set[str] = set()
        recent: set[str] = set()
        for entry in data:
            issued = _parse_iso(entry.get("entry_timestamp") or entry.get("not_before"))
            for raw in str(entry.get("name_value") or "").splitlines():
                name = raw.strip().lower().lstrip("*.")
                if not name.endswith(domain) or name == domain:
                    continue
                seen.add(name)
                if issued is not None and issued >= cutoff:
                    recent.add(name)
        return len(seen), len(recent)


class RdapClient:
    """RDAP registry lookup. Domain age is a strong seed/early-stage signal."""

    SOURCE_ID = "rdap"

    def __init__(self, egress: EgressClient) -> None:
        self.egress = egress

    async def domain(self, domain: str) -> dict[str, Any]:
        result = await self.egress.fetch(self.SOURCE_ID, f"https://rdap.org/domain/{domain}")
        data = _safe_json(result.body, source_id=self.SOURCE_ID, url=result.url)
        if not isinstance(data, dict):
            return {}

        registered: datetime | None = None
        for event in data.get("events", []):
            if event.get("eventAction") == "registration":
                registered = _parse_iso(event.get("eventDate"))
        return {
            "registered_at": registered,
            "age_days": (utcnow() - registered).days if registered else None,
            "registrar": _registrar(data),
            "status": data.get("status", []),
        }


# ------------------------------------------------------- deterministic triggers

# Note the \w* suffixes. A trailing \b after "penetration test" does NOT match
# "Penetration Tester" -- there is no word boundary between "test" and "er" -- and
# that is one of the commonest titles in the category.
_SECURITY_ROLE = re.compile(
    r"\b(appsec|application security|security engineer\w*|infosec|secops|devsecops|"
    r"product security|offensive security|penetration\s+test\w*|pentest\w*|red team\w*|"
    r"security architect\w*|ciso|head of security|vulnerability\s+\w+|grc|"
    r"compliance manager|security analyst\w*|threat\s+\w+)\b",
    re.IGNORECASE,
)
_AI_ROLE = re.compile(
    r"\b(machine learning|ml engineer\w*|ai engineer\w*|llm\w*|genai|generative ai|"
    r"applied scientist\w*|research engineer\w*|nlp|prompt engineer\w*|ai/ml|mlops|"
    r"deep learning|data scientist\w*)\b",
    re.IGNORECASE,
)


def classify_role(title: str) -> str:
    """``security`` | ``ai`` | ``other``.

    Keyword matching, not inference. On a Pi at 3.7 tok/s the cheapest LLM call is the
    one you never make, and job titles are formulaic enough that a regex is both faster
    and more predictable than a 4B.
    """
    if _SECURITY_ROLE.search(title):
        return "security"
    if _AI_ROLE.search(title):
        return "ai"
    return "other"


@dataclass(frozen=True)
class HiringSignal:
    total: int
    security_roles: int
    ai_roles: int
    triggers: tuple[TriggerCode, ...]

    @property
    def hiring_ai_without_security(self) -> bool:
        return self.ai_roles > 0 and self.security_roles == 0


def analyze_postings(postings: list[JobPosting]) -> HiringSignal:
    """Turn a company's board into trigger codes.

    T4_HIRING_AI_ONLY is the interesting one and it is a property of the *set*, not of
    any single posting: they are building AI and nobody is being hired to secure it.
    No individual job ad can tell you that — only the absence across all of them can.
    """
    security = sum(1 for p in postings if classify_role(p.title) == "security")
    ai = sum(1 for p in postings if classify_role(p.title) == "ai")

    triggers: list[TriggerCode] = []
    if security:
        triggers.append("T3_HIRING_SEC")
    if ai and not security:
        triggers.append("T4_HIRING_AI_ONLY")
    if any(
        re.search(r"\b(soc\s?2|iso\s?27001|pci|gdpr|hipaa|fedramp)\b", p.content, re.IGNORECASE)
        for p in postings
    ):
        triggers.append("T5_COMPLIANCE")
    if any(
        re.search(
            r"\b(langchain|llamaindex|mcp|rag|vector database|agent framework)\b",
            f"{p.title} {p.content}",
            re.IGNORECASE,
        )
        for p in postings
    ):
        triggers.append("T11_STACK_RISK")

    return HiringSignal(
        total=len(postings), security_roles=security, ai_roles=ai, triggers=tuple(triggers)
    )


# --------------------------------------------------------------------- helpers


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return from_iso(value)
    except (ValueError, TypeError):
        return None


def _parse_epoch_ms(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=utcnow().tzinfo)
    except (OverflowError, OSError, ValueError):
        return None


def _first_name(items: Any) -> str | None:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        name = items[0].get("name")
        return str(name) if name else None
    return None


def _registrar(data: dict[str, Any]) -> str | None:
    for entity in data.get("entities", []):
        if "registrar" in (entity.get("roles") or []):
            for item in entity.get("vcardArray", [None, []])[1]:
                if isinstance(item, list) and item and item[0] == "fn":
                    return str(item[3])
    return None
