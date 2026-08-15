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
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from cindraleads.errors import CindraError
from cindraleads.logging import get_logger
from cindraleads.models import Job, QueryPlan, StageResult, to_iso, utcnow
from cindraleads.queue import JobQueue
from cindraleads.sources.clients import GitHubClient, HackerNewsClient, SourceHit
from cindraleads.sources.http import EgressClient, FetchDenied
from cindraleads.store import Store

__all__ = ["EXTRACT_KIND", "HARVEST_KIND", "HarvestOutcome", "Harvester"]

log = get_logger("cindraleads.harvester")

HARVEST_KIND = "harvest.query"
EXTRACT_KIND = "extract.candidate"


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

    def __post_init__(self) -> None:
        if self.hn is None:
            self.hn = HackerNewsClient(self.egress)
        if self.github is None:
            self.github = GitHubClient(self.egress)

    def supports(self, engine: str) -> bool:
        return engine in {"hn_algolia", "github_api"}

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
                return await self.hn.search(
                    plan.query,
                    since_days=since_days,
                    tags=plan.params.get("tags") or "story",
                )
            assert self.github is not None
            return await self.github.search_repos(plan.query)
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
        for hit in outcome.hits:
            if self._seen(conn, hit.url):
                continue
            candidate_id = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO candidates (candidate_id, content_sha256, raw_payload, "
                "status, created_at) VALUES (?,?,?,?,?)",
                (
                    candidate_id,
                    "",  # filled by the Extractor once the page is fetched
                    _payload_json(hit, outcome.plan),
                    "new",
                    to_iso(utcnow()),
                ),
            )
            payloads.append(
                {
                    "candidate_id": candidate_id,
                    "url": hit.url,
                    "title": hit.title,
                    "source_id": hit.source_id,
                    "targets": list(outcome.plan.targets),
                    "origin_job_id": job.job_id,
                }
            )

        log.info(
            "harvest_complete",
            job_id=job.job_id,
            stage="harvester",
            engine=outcome.plan.engine,
            hits=len(outcome.hits),
            new_candidates=len(payloads),
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


def _payload_json(hit: SourceHit, plan: QueryPlan) -> str:
    return json.dumps(
        {
            "url": hit.url,
            "title": hit.title,
            "snippet": hit.snippet,
            "source_id": hit.source_id,
            "published_at": to_iso(hit.published_at) if hit.published_at else None,
            "targets": list(plan.targets),
            "raw": hit.raw,
        },
        separators=(",", ":"),
    )
