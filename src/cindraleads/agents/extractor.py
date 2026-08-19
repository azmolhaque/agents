"""Extractor — one fetched page becomes one validated Candidate.

The only stage besides the Scorer's prose fields that runs a model, and the expensive
one: measured at p50 64 s per page on the Pi (`docs/BENCHMARKS.md`). Everything here is
shaped by that number.

**What the model is and is not asked for.** It emits `CompanyExtraction` — flat claims
and verbatim snippets. It is never asked for a URL or a content hash, because those are
facts the Harvester already established, and a 4B asked to repeat a URL will eventually
invent one. Provenance is attached by this stage from the `FetchResult` it holds. The
model cannot fabricate evidence because it is never given the chance.

**Two hard filters after the model returns**, both from the "no evidence, no lead" rule:

  * A snippet that does not literally appear in the page text is dropped. That is the
    cheap, mechanical check that turns "the model quoted something" into "the page said
    something".
  * A candidate left with no snippet keeps no trigger claims. A trigger with no evidence
    is precisely the fluent hallucination the project exists to avoid.

**The stage owns no tools.** It gets a fetch function and an LLM, and nothing else. A
successful prompt injection can therefore only produce a wrong extraction; there is no
tool for it to call. See `injection.py` for why that is the primary defence and the
regex tripwire only a signal.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import get_args

import httpx

from cindraleads import injection
from cindraleads.config import Settings, load_prompt, prompt_version, settings
from cindraleads.dedupe import canonical_domain
from cindraleads.errors import CindraError, SchemaValidationError
from cindraleads.llm import StructuredLLM
from cindraleads.logging import get_logger
from cindraleads.models import (
    CompanyExtraction,
    Job,
    StageResult,
    TriggerCode,
    to_iso,
    utcnow,
)
from cindraleads.sources.http import EgressClient, FetchDenied
from cindraleads.store import Store
from cindraleads.textextract import extract_text, extract_title

__all__ = ["EXTRACT_KIND", "RESOLVE_KIND", "ExtractOutcome", "Extractor"]

log = get_logger("cindraleads.extractor")

EXTRACT_KIND = "extract.candidate"
RESOLVE_KIND = "resolve.company"

# The measured budget. 1500 chars of page text cost 64 s/page against 150 s for 4000
# (docs/BENCHMARKS.md, 2026-08-15). Phase 3 re-tunes this against *field accuracy*
# rather than schema validity: a short budget drops footers, and footers are where
# headcount and location live.
PROMPT_CHAR_BUDGET = 1500

# How long to hold a candidate that hit the per-domain budget, and how many times.
# The budget is a rolling 24 h window, so 6 h retries land inside the same day
# without hammering; 4 attempts covers a full window before giving up for real.
DEFER_SECONDS = 6 * 3600
MAX_DEFERRALS = 4


@dataclass(frozen=True)
class ExtractOutcome:
    """What `prepare` learned. No database writes have happened yet."""

    candidate_id: str
    url: str
    source_id: str
    extraction: CompanyExtraction | None = None
    content_sha256: str = ""
    page_title: str = ""
    verified_snippets: tuple[str, ...] = ()
    verdict: injection.InjectionVerdict | None = None
    targets: tuple[str, ...] = ()
    duration_ms: int = 0
    skipped: str | None = None
    error: str | None = None
    # Set when the candidate is early rather than finished: the job is re-queued
    # with a delay instead of being marked done.
    defer_seconds: int = 0
    deferrals: int = 0


@dataclass
class Extractor:
    store: Store
    egress: EgressClient
    llm: StructuredLLM
    config: Settings | None = None
    source_id: str = "company_site"

    def __post_init__(self) -> None:
        cfg = self.config or settings()
        self.config = cfg
        base = cfg.resolve(cfg.prompt_dir)
        # Loaded once, at construction. Re-reading the file per page would be a disk
        # read inside the hot path, and worse, would let an edit take effect mid-batch
        # so half a run used a different prompt than the other half.
        self._template = load_prompt("extract_company", base=base)
        self._prompt_version = prompt_version(base)

    # ------------------------------------------------------------------ phase 1

    async def prepare(self, job: Job) -> ExtractOutcome:
        """Fetch the page and run the model. No database writes."""
        started = utcnow()
        payload = job.payload
        candidate_id = str(payload.get("candidate_id") or "")
        url = str(payload.get("url") or "")
        targets = tuple(str(t) for t in payload.get("targets") or ())
        deferrals = int(payload.get("deferrals") or 0)
        if not candidate_id or not url:
            return ExtractOutcome(
                candidate_id=candidate_id,
                url=url,
                source_id=self.source_id,
                error="extract job needs candidate_id and url",
            )

        # Refuse before fetching, not after extracting. The Resolver drops a URL with
        # no canonical domain, so fetching one costs a request, a slot in the domain's
        # politeness budget, and ~60 s of inference to reach a conclusion available
        # here for free. The Harvester now filters these at discovery; this is the
        # second line, and it is what drains a backlog queued before that existed.
        if canonical_domain(url) is None:
            return ExtractOutcome(
                candidate_id=candidate_id,
                url=url,
                source_id=self.source_id,
                skipped="not a company URL (platform host or unusable domain)",
            )

        try:
            result = await self.egress.fetch(self.source_id, url)
        except FetchDenied as exc:
            # Policy said no. *Which* policy decides whether this candidate is finished
            # or merely early, and conflating the two loses real work: the per-domain
            # budget is 6 per rolling 24 h, so the 7th URL on one domain is fetchable
            # tomorrow. Marking it "skipped" would discard it permanently and silently.
            if _is_temporary(exc.reason) and deferrals < MAX_DEFERRALS:
                return ExtractOutcome(
                    candidate_id=candidate_id,
                    url=url,
                    source_id=self.source_id,
                    targets=targets,
                    defer_seconds=DEFER_SECONDS,
                    deferrals=deferrals + 1,
                )
            return ExtractOutcome(
                candidate_id=candidate_id,
                url=url,
                source_id=self.source_id,
                skipped=f"fetch denied: {exc.reason}",
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500:
                # The page is gone or forbidden. It will say the same thing on every
                # retry, so failing the job would spend three attempts and a
                # dead-letter row establishing that. Same rule the egress retry policy
                # already applies: 4xx is an answer, not a fault.
                return ExtractOutcome(
                    candidate_id=candidate_id,
                    url=url,
                    source_id=self.source_id,
                    skipped=f"page returned {status}",
                )
            return ExtractOutcome(
                candidate_id=candidate_id,
                url=url,
                source_id=self.source_id,
                error=f"HTTP {status}",
            )
        except (httpx.HTTPError, OSError, CindraError) as exc:
            # Transient by assumption — a timeout or a connection reset. Failing the
            # job puts it back for a later attempt, which is what we want.
            return ExtractOutcome(
                candidate_id=candidate_id,
                url=url,
                source_id=self.source_id,
                error=f"{type(exc).__name__}: {exc}",
            )

        page_text = extract_text(result.body, max_chars=PROMPT_CHAR_BUDGET)
        title = extract_title(result.body)
        if not page_text.strip():
            return ExtractOutcome(
                candidate_id=candidate_id,
                url=url,
                source_id=self.source_id,
                content_sha256=result.content_sha256,
                skipped="page had no extractable text",
            )

        # Scan the *whole* body, not just the truncated prompt slice: an injection
        # placed past the 1500-char budget still tells us the page is hostile, and it
        # would reach the model on any future re-tune of the budget.
        verdict = injection.scan_for_injection(extract_text(result.body, max_chars=20_000))

        prompt = self._template.format(url=url, content=injection.wrap_untrusted(page_text))
        try:
            structured = await self.llm.generate(prompt, CompanyExtraction)
        except SchemaValidationError as exc:
            # The ladder already retried locally and, if permitted, escalated. This is
            # the dead-letter end of it.
            return ExtractOutcome(
                candidate_id=candidate_id,
                url=url,
                source_id=self.source_id,
                content_sha256=result.content_sha256,
                verdict=verdict,
                error=f"extraction failed schema: {exc}",
            )

        verified = _verify_snippets(structured.value.evidence_snippets, result.body, page_text)
        log.info(
            "extract_complete",
            candidate_id=candidate_id,
            url=url,
            model=structured.model,
            escalated=structured.escalated,
            latency_ms=structured.latency_ms,
            snippets_claimed=len(structured.value.evidence_snippets),
            snippets_verified=len(verified),
            suspicious=bool(verdict.suspicious),
        )
        return ExtractOutcome(
            candidate_id=candidate_id,
            url=url,
            source_id=self.source_id,
            extraction=structured.value,
            content_sha256=result.content_sha256,
            page_title=title,
            verified_snippets=verified,
            verdict=verdict,
            targets=targets,
            duration_ms=int((utcnow() - started).total_seconds() * 1000),
        )

    # ------------------------------------------------------------------ phase 2

    def commit(self, job: Job, outcome: ExtractOutcome, conn: sqlite3.Connection) -> StageResult:
        """Persist the candidate, its evidence, and the follow-on resolve job."""
        if outcome.error:
            return StageResult(ok=False, stage="extractor", job_id=job.job_id, error=outcome.error)

        if outcome.verdict is not None and outcome.verdict.suspicious:
            injection.quarantine(
                conn,
                subject_kind="candidate",
                subject_id=outcome.candidate_id,
                verdict=outcome.verdict,
            )
            _set_status(conn, outcome.candidate_id, "quarantined")
            return StageResult(
                ok=True, stage="extractor", job_id=job.job_id, duration_ms=outcome.duration_ms
            )

        if outcome.defer_seconds:
            log.info(
                "extract_deferred",
                candidate_id=outcome.candidate_id,
                url=outcome.url,
                attempt=outcome.deferrals,
                seconds=outcome.defer_seconds,
            )
            return StageResult(
                ok=True,
                stage="extractor",
                job_id=job.job_id,
                follow_on=[
                    (
                        EXTRACT_KIND,
                        {
                            "candidate_id": outcome.candidate_id,
                            "url": outcome.url,
                            "targets": list(outcome.targets),
                            "deferrals": outcome.deferrals,
                            "_delay_seconds": outcome.defer_seconds,
                        },
                    )
                ],
                duration_ms=outcome.duration_ms,
            )

        if outcome.skipped or outcome.extraction is None:
            _set_status(conn, outcome.candidate_id, "skipped")
            log.info("extract_skipped", candidate_id=outcome.candidate_id, why=outcome.skipped)
            return StageResult(
                ok=True, stage="extractor", job_id=job.job_id, duration_ms=outcome.duration_ms
            )

        extraction = outcome.extraction
        evidence_ids: list[str] = []
        for snippet in outcome.verified_snippets:
            evidence_id = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,?,?,?,?)",
                (
                    evidence_id,
                    outcome.url,
                    outcome.source_id,
                    snippet[:500],
                    to_iso(utcnow()),
                    outcome.content_sha256,
                ),
            )
            evidence_ids.append(evidence_id)

        # No verified snippet means no evidence, and a trigger without evidence is not
        # a trigger. The claims are kept for debugging but cannot become a lead.
        # The Harvester's `targets` are what the query was *looking for*, so they are
        # a fallback only, and validated against the taxonomy: a payload is data, and
        # an unknown code must not reach a trigger row.
        known = set(get_args(TriggerCode))
        claimed: list[str] = [str(c) for c in extraction.trigger_codes] or [
            t for t in outcome.targets if t in known
        ]
        trigger_codes = claimed if evidence_ids else []
        if claimed and not evidence_ids:
            log.info(
                "extract_triggers_dropped",
                candidate_id=outcome.candidate_id,
                why="no snippet verified against the page",
                dropped=claimed,
            )

        payload = {
            "extraction": extraction.model_dump(mode="json"),
            "url": outcome.url,
            "source_id": outcome.source_id,
            "page_title": outcome.page_title,
            "content_sha256": outcome.content_sha256,
            "evidence_ids": evidence_ids,
            "trigger_codes": trigger_codes,
            "prompt_version": self._prompt_version,
            # Forwarded, not dropped. The Harvester puts `template_id` on the extract
            # job and the Resolver reads it out of *this* payload to write
            # `companies.discovered_by` -- and this stage sat silently between them, so
            # the column was NULL for every company ever recorded and `cindra explain`'s
            # per-template yield table could never populate. The whole point of that
            # table is that a weight in `icp.yaml` is a guess until it disagrees; with
            # no attribution it could not disagree with anything.
            #
            # It goes in the stored candidate payload rather than the follow-on job
            # because that is where the Resolver reads from, and because discovery
            # provenance should survive a re-resolve rather than living only in a
            # transient job row.
            "template_id": job.payload.get("template_id") or "",
        }
        conn.execute(
            "UPDATE candidates SET content_sha256 = ?, raw_payload = ?, status = ? "
            "WHERE candidate_id = ?",
            (
                outcome.content_sha256,
                json.dumps(payload, separators=(",", ":")),
                "extracted",
                outcome.candidate_id,
            ),
        )
        return StageResult(
            ok=True,
            stage="extractor",
            job_id=job.job_id,
            follow_on=[(RESOLVE_KIND, {"candidate_id": outcome.candidate_id})],
            duration_ms=outcome.duration_ms,
        )

    async def run(self, job: Job) -> StageResult:
        outcome = await self.prepare(job)
        with self.store.tx() as conn:
            return self.commit(job, outcome, conn)


def _set_status(conn: sqlite3.Connection, candidate_id: str, status: str) -> None:
    conn.execute("UPDATE candidates SET status = ? WHERE candidate_id = ?", (status, candidate_id))


def _normalize_ws(text: str) -> str:
    return " ".join(text.split()).lower()


def _verify_snippets(claimed: list[str], body: str, page_text: str) -> tuple[str, ...]:
    """Keep only snippets that literally appear in the page.

    Whitespace-insensitive, because the model reflows what it read and an exact
    substring test would reject almost everything. Checked against the *full* body as
    well as the truncated prompt slice: a quote from past the budget is still a real
    quote from the page, and rejecting it would punish the model for our truncation.
    """
    haystacks = (_normalize_ws(page_text), _normalize_ws(body))
    verified: list[str] = []
    for snippet in claimed:
        needle = _normalize_ws(snippet)
        if len(needle) < 12:
            # Too short to be evidence of anything; "AI" appears on every page.
            continue
        if any(needle in hay for hay in haystacks):
            verified.append(snippet.strip())
    return tuple(verified)


def _is_temporary(reason: str) -> bool:
    """Whether a `FetchDenied` will plausibly answer differently later.

    A per-domain budget refills; robots.txt does not change its mind on our schedule.
    """
    return "budget" in reason.lower()
