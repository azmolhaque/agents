"""Harvester — executes a QueryPlan and turns the results into extract jobs.

Owns no network code of its own: every request goes through :class:`EgressClient`, so
this stage inherits robots, budgets, caching and circuit breakers without restating any
of them. Its actual job is dispatch and bookkeeping.

**The transaction boundary is the important part.** Follow-on jobs are enqueued in the
same transaction that completes the harvest job. Killed before the COMMIT, the harvest
job's lease expires and it is retried with nothing half-written; killed after, the
extract jobs are durable and the harvest is never repeated. Enqueueing outside that
transaction would give you the two classic failures — orphaned extract jobs for a
harvest that "never happened", or a completed harvest whose results vanished.

**One dead source must not fail the batch.** A plan that raises is logged and skipped;
the rest of the batch proceeds. That is the same rule the circuit breaker enforces at a
lower level, applied here to the loop.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from cindraleads.dedupe import canonical_domain
from cindraleads.errors import CindraError
from cindraleads.logging import get_logger
from cindraleads.models import Job, QueryPlan, StageResult, to_iso, utcnow
from cindraleads.queue import JobQueue
from cindraleads.sources.clients import (
    GitHubClient,
    HackerNewsClient,
    SerpApiClient,
    SourceHit,
)
from cindraleads.sources.http import EgressClient, FetchDenied
from cindraleads.store import Store

__all__ = [
    "EXTRACT_KIND",
    "HARVEST_KIND",
    "HarvestOutcome",
    "Harvester",
    "extraction_target",
]

log = get_logger("cindraleads.harvester")

HARVEST_KIND = "harvest.query"
EXTRACT_KIND = "extract.candidate"

# Per-run harvest yield, written to `metrics` so `cindra explain` can report a template
# that produces nothing. The company table cannot: a template yielding zero companies
# has no `discovered_by` row and simply does not appear.
HARVEST_YIELD_METRIC = "harvest_yield"

# Bounds on comment expansion. A monthly "Who is hiring" thread runs past 500 comments
# and each accepted one becomes an extract job at ~64 s of decode -- unbounded, a single
# harvest would queue more than the Pi drains in a day and starve every other template
# behind it. Three threads covers the current month plus the two before it, which is the
# whole useful window: an entry from four months ago has been filled or withdrawn.
MAX_COMMENT_THREADS = 3
MAX_COMMENTS_PER_THREAD = 40

# An HN item URL, which is the only shape whose comments can be fetched.
_HN_ITEM = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")


def _hn_item_id(url: str) -> str | None:
    match = _HN_ITEM.search(url or "")
    return match.group(1) if match else None


@dataclass(frozen=True)
class HarvestOutcome:
    """What `prepare` learned, before anything is written."""

    plan: QueryPlan | None
    hits: list[SourceHit]
    duration_ms: int = 0
    error: str | None = None


@dataclass
class Harvester:
    store: Store
    egress: EgressClient
    queue: JobQueue
    # Concretely typed rather than a dict[str, Any]: an untyped client registry makes
    # every call site return Any, which silently disables checking on the results.
    hn: HackerNewsClient | None = None
    github: GitHubClient | None = None
    serpapi: SerpApiClient | None = None
    serpapi_key: str | None = None

    def __post_init__(self) -> None:
        if self.hn is None:
            self.hn = HackerNewsClient(self.egress)
        if self.github is None:
            self.github = GitHubClient(self.egress)
        if self.serpapi is None:
            self.serpapi = SerpApiClient(self.egress, api_key=self.serpapi_key)

    def supports(self, engine: str) -> bool:
        return engine in {"hn_algolia", "github_api"} or SerpApiClient.supports(engine)

    def cache_key_for_plan(self, plan: QueryPlan) -> str | None:
        """The cache key this plan's fetch will actually use, without fetching.

        Delegated to the client rather than recomputed here. The Scout used to build
        its own key from ``(engine, plan.query, plan.params)`` while the client fetched
        under ``(source_id, url, api_params)``; the two never matched, so
        ``skip_if_cached`` silently never fired.
        """
        if plan.engine == "hn_algolia":
            return HackerNewsClient.cache_key(
                plan.query,
                since_days=int(plan.params.get("since_days", 30)),
                tags=plan.params.get("tags") or "story",
            )
        if plan.engine == "github_api":
            return GitHubClient.cache_key(plan.query)
        if SerpApiClient.supports(plan.engine):
            return SerpApiClient.cache_key(plan.engine, plan.query)
        return None

    # ------------------------------------------------------------- execution

    async def execute(self, plan: QueryPlan) -> list[SourceHit]:
        """Run one plan. Returns normalized hits, or [] if the source declined."""
        if not self.supports(plan.engine):
            # A plan for a source with no client yet is a gap, not a crash. It gets
            # logged and skipped so the rest of the batch still runs.
            log.warning("harvester_no_client", engine=plan.engine, query=plan.query)
            return []

        since_days = int(plan.params.get("since_days", 30))
        try:
            if plan.engine == "hn_algolia":
                assert self.hn is not None
                stories = await self.hn.search(
                    plan.query,
                    since_days=since_days,
                    tags=plan.params.get("tags") or "story",
                )
                if plan.params.get("comments") == "true":
                    return await self._expand_comments(stories, plan)
                return stories
            if SerpApiClient.supports(plan.engine):
                assert self.serpapi is not None
                return await self.serpapi.search(plan.engine, plan.query)
            assert self.github is not None
            # Default ON. A personal repo is a person, and the anti-ICP rule excludes
            # unaffiliated individuals -- so the company-shaped default is the org
            # filter, and a template has to opt *out* deliberately. The first corpus
            # ran without it and filled with side projects.
            return await self.github.search_repos(
                plan.query,
                organizations_only=plan.params.get("organizations_only", "true") != "false",
            )
        except FetchDenied as exc:
            # Policy said no. Nothing broke, so this is info, not an error.
            log.info("harvester_denied", engine=plan.engine, reason=exc.reason)
            return []
        except (httpx.HTTPError, OSError, CindraError) as exc:
            log.warning(
                "harvester_source_failed",
                engine=plan.engine,
                query=plan.query,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

    async def _expand_comments(self, stories: list[SourceHit], plan: QueryPlan) -> list[SourceHit]:
        """Replace each story with the companies named in its comments.

        For a thread like "Ask HN: Who is hiring", the story is an index and the
        comments are the content -- so the stories are consumed here and never returned.
        Returning both would put the thread's own news.ycombinator.com URL back in the
        hit list, where it would be dropped as a platform URL and counted against this
        template's yield: the exact number that made the template look broken.

        Bounded by `MAX_COMMENT_THREADS` and `MAX_COMMENTS_PER_THREAD` because a monthly
        hiring thread runs to 500+ comments and every accepted one becomes an extract
        job at ~64 s of decode. Left unbounded, one harvest would queue more work than
        the Pi can drain in a day and starve every other template behind it.
        """
        assert self.hn is not None
        out: list[SourceHit] = []
        for story in stories[:MAX_COMMENT_THREADS]:
            story_id = _hn_item_id(story.url)
            if story_id is None:
                # A story whose URL is somewhere else entirely is not a discussion
                # thread; it is an ordinary hit and keeps its own meaning.
                out.append(story)
                continue
            comments = await self.hn.thread_comments(story_id, limit=MAX_COMMENTS_PER_THREAD)
            log.info(
                "harvester_thread_expanded",
                template_id=plan.template_id,
                story_id=story_id,
                comments=len(comments),
            )
            out.extend(comments)
        return out

    # ----------------------------------------------------------------- stage
    #
    # Split in two on purpose.
    #
    # `prepare` does the network work and writes nothing. `commit` writes, inside the
    # caller's transaction, alongside the queue completion. Two properties fall out
    # that a single combined method cannot have at once:
    #
    #   * No network I/O inside a write transaction. A fetch can take 30 s and
    #     BEGIN IMMEDIATE holds the write lock, so a combined method would block
    #     every other worker for the duration of an HTTP call.
    #   * Side effect and completion still commit together. An earlier version
    #     persisted candidates in their own transaction before the job was marked
    #     done; a crash in that window left the candidates written but the extract
    #     jobs unqueued, and on retry the URL dedupe suppressed them -- losing the
    #     work permanently while looking like success.

    async def prepare(self, job: Job) -> HarvestOutcome:
        """Phase 1: fetch. No database writes."""
        started = utcnow()
        try:
            plan = QueryPlan.model_validate(job.payload)
        except ValueError as exc:
            return HarvestOutcome(plan=None, hits=[], error=f"bad QueryPlan: {exc}")
        hits = await self.execute(plan)
        return HarvestOutcome(
            plan=plan,
            hits=hits,
            duration_ms=int((utcnow() - started).total_seconds() * 1000),
        )

    def commit(self, job: Job, outcome: HarvestOutcome, conn: sqlite3.Connection) -> StageResult:
        """Phase 2: write, inside the caller's transaction."""
        if outcome.plan is None:
            return StageResult(ok=False, stage="harvester", job_id=job.job_id, error=outcome.error)

        payloads: list[dict[str, Any]] = []
        dropped_platform = 0
        for hit in outcome.hits:
            target = extraction_target(hit)
            if target is None:
                # A platform URL with no company site behind it. The Resolver would
                # drop it anyway — a GitHub repo has no canonical domain — so
                # extracting it first would spend ~60 s of Pi inference to learn
                # something already knowable here for free. Measured on the first real
                # run: 13 of 51 resolutions were exactly this.
                dropped_platform += 1
                continue
            if self._seen(conn, target):
                continue
            candidate_id = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO candidates (candidate_id, content_sha256, raw_payload, "
                "status, created_at) VALUES (?,?,?,?,?)",
                (
                    candidate_id,
                    "",  # filled by the Extractor once the page is fetched
                    _payload_json(hit, outcome.plan, target),
                    "new",
                    to_iso(utcnow()),
                ),
            )
            payloads.append(
                {
                    "candidate_id": candidate_id,
                    "url": target,
                    "title": hit.title,
                    "source_id": hit.source_id,
                    "targets": list(outcome.plan.targets),
                    "origin_job_id": job.job_id,
                }
            )

        # Persisted, not just logged. A template that returns nothing but platform URLs
        # produces no company, so it has no row in `companies.discovered_by` and is
        # invisible to `cindra explain`'s yield table -- it reads as a template that was
        # never tried rather than one that fails every time. Two of them were doing
        # exactly that at weights 98 and 94, spending SerpAPI credits hourly for zero
        # candidates, and nothing in the system could say so.
        conn.execute(
            "INSERT INTO metrics (name, value, labels, recorded_at) VALUES (?,?,?,?)",
            (
                HARVEST_YIELD_METRIC,
                float(len(payloads)),
                json.dumps(
                    {
                        "template_id": outcome.plan.template_id,
                        "engine": outcome.plan.engine,
                        "hits": len(outcome.hits),
                        "candidates": len(payloads),
                        "dropped_platform": dropped_platform,
                    },
                    separators=(",", ":"),
                ),
                to_iso(utcnow()),
            ),
        )
        log.info(
            "harvest_complete",
            job_id=job.job_id,
            stage="harvester",
            # The template, not only the engine. Several templates share an engine, so
            # `engine=hn_algolia` could not tell you *which* query found nothing.
            template_id=outcome.plan.template_id,
            engine=outcome.plan.engine,
            hits=len(outcome.hits),
            new_candidates=len(payloads),
            dropped_platform=dropped_platform,
            duration_ms=outcome.duration_ms,
        )
        return StageResult(
            ok=True,
            stage="harvester",
            job_id=job.job_id,
            follow_on=[(EXTRACT_KIND, payload) for payload in payloads],
            duration_ms=outcome.duration_ms,
        )

    async def run(self, job: Job) -> StageResult:
        """Convenience for callers outside the worker loop (tests, `cindra harvest`).

        The worker itself calls prepare/commit separately so the completion lands in
        the same transaction as the writes.
        """
        outcome = await self.prepare(job)
        with self.store.tx() as conn:
            return self.commit(job, outcome, conn)

    @staticmethod
    def _seen(conn: sqlite3.Connection, url: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM candidates WHERE json_extract(raw_payload, '$.url') = ? LIMIT 1",
            (url,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ scheduling

    def enqueue_plans(self, plans: list[QueryPlan]) -> tuple[list[str], int]:
        """Turn a Scout batch into durable jobs. Returns (job ids, how many are new).

        **The dedupe key carries a time bucket, and it has to.** `JobQueue.enqueue`
        matches a dedupe key across *every* job including completed ones, so a key of
        just `(engine, query, params)` meant a query could never run a second time:
        the first harvest ran, and every harvest after it deduped onto those finished
        jobs and did nothing. Left alone, the Phase 7 hourly timer would have
        harvested once at boot and then idled forever, looking healthy the whole time.

        Bucketing by the plan's cache TTL gives the intended rule instead — one run
        per query per cache window. Inside the window, re-planning is a no-op because
        the answer would be served from cache anyway; once it lapses, the query is
        genuinely new work and gets a new key.
        """
        ids: list[str] = []
        new = 0
        with self.store.tx() as conn:
            for plan in plans:
                key = self.dedupe_key_for(plan)
                existed = conn.execute(
                    "SELECT 1 FROM jobs WHERE dedupe_key = ? LIMIT 1", (key,)
                ).fetchone()
                ids.append(
                    self.queue.enqueue(
                        HARVEST_KIND,
                        plan.model_dump(mode="json"),
                        dedupe_key=key,
                        conn=conn,
                    )
                )
                if existed is None:
                    new += 1
        return ids, new

    @staticmethod
    def dedupe_key_for(plan: QueryPlan, *, now: datetime | None = None) -> str:
        shape = (plan.query, tuple(sorted(plan.params.items())))
        digest = hashlib.sha256(repr(shape).encode()).hexdigest()[:16]
        ttl = max(1, plan.cache_ttl_hours)
        bucket = int((now or utcnow()).timestamp()) // (ttl * 3600)
        return f"harvest:{plan.engine}:{digest}:{bucket}"


def extraction_target(hit: SourceHit) -> str | None:
    """The URL worth spending an extraction on, or None.

    A discovery hit often points at a platform rather than at a company: a GitHub
    repo, an HN thread, a LinkedIn page. Two cases:

    * The source handed us the company's own site alongside it — GitHub's API returns
      the repo's `homepage` field — so extract that instead. This is the difference
      between reading a README and reading the company's landing page.
    * It did not, and there is no company site to read. The Resolver refuses platform
      hosts, so extracting one is ~60 s of Pi inference spent to reach a conclusion
      available here for nothing.

    A side effect that matters as much: github.com stops consuming the 6-per-domain
    politeness budget, which is meant for a *prospect's* infrastructure. On the first
    real run it was being spent on the platform instead, deferring genuine prospects.
    """
    if canonical_domain(hit.url) is not None:
        return hit.url
    homepage = str(hit.raw.get("homepage") or "").strip()
    if homepage and canonical_domain(homepage) is not None:
        return homepage if "//" in homepage else f"https://{homepage}"
    return None


def _payload_json(hit: SourceHit, plan: QueryPlan, target: str) -> str:
    return json.dumps(
        {
            "url": target,
            "discovered_at": hit.url,
            "title": hit.title,
            "snippet": hit.snippet,
            "source_id": hit.source_id,
            "published_at": to_iso(hit.published_at) if hit.published_at else None,
            "targets": list(plan.targets),
            "template_id": plan.template_id,
            "raw": hit.raw,
        },
        separators=(",", ":"),
    )
