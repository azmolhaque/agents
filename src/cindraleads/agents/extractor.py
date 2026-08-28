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
from typing import Any, get_args

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

__all__ = [
    "EXTRACT_KIND",
    "RESOLVE_KIND",
    "ExtractOutcome",
    "Extractor",
    "enqueue_stale_extractions",
    "enqueue_unextracted",
]

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

# A thermal pause clears in minutes, not hours, so it waits far less than a domain
# budget does -- but it is still a *defer* and never a failure. `MAX_THERMAL_PAUSES` is
# four hours at this interval; past that the governor is not having a spell, it is the
# steady state, and something bigger is wrong than one candidate.
THERMAL_DEFER_SECONDS = 20 * 60
MAX_THERMAL_PAUSES = 12

# How many superseded extractions one reconcile pass may re-queue. The cost of a
# backfill is decode, not disk: at ~55 s a page this is roughly 45 minutes of
# inference, which drains a several-hundred-company corpus over days while never
# being the reason a freshly harvested lead waits behind it.
DEFAULT_RESTALE_LIMIT = 50

# The one LLM failure that means "the box is busy", rather than "this page or this
# prompt is wrong". Matched on the governor's own message.
_THERMAL_MARKER = "thermal governor"


def _is_thermal(reason: str) -> bool:
    return _THERMAL_MARKER in reason.lower()


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
    # Charged separately from `deferrals`: a hot box and an exhausted domain
    # budget are different facts and must not share one allowance.
    pauses: int = 0


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
        pauses = int(payload.get("pauses") or 0)
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
            # A thermal pause is not an extraction failure, and charging it as one
            # buried a whole batch. The governor stops inference for minutes at a time;
            # this path returned `error`, which fails the stage, increments `attempts`
            # and retries within seconds -- so three pauses in under a minute
            # dead-lettered a candidate that had never once been shown to the model.
            # Observed doing exactly that to 21 freshly harvested companies.
            #
            # Same distinction the queue draws between `attempts` and `reclaims`, and
            # the Scorer between failures and pauses: the evidence differs. A schema
            # failure says the page or the prompt is wrong. A pause says the box is hot,
            # which is a designed-for state on a passively cooled Pi and resolves on its
            # own. So it gets its own counter and its own, far higher ceiling.
            if _is_thermal(str(exc)) and pauses < MAX_THERMAL_PAUSES:
                return ExtractOutcome(
                    candidate_id=candidate_id,
                    url=url,
                    source_id=self.source_id,
                    targets=targets,
                    defer_seconds=THERMAL_DEFER_SECONDS,
                    deferrals=deferrals,
                    pauses=pauses + 1,
                )
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
                            "pauses": outcome.pauses,
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
            # Carried over from the row this UPDATE is about to replace, and *that* is
            # the load-bearing part.
            #
            # The Harvester writes `template_id` into `candidates.raw_payload`; the
            # Resolver reads it back out of the same column to set
            # `companies.discovered_by`. This stage overwrites that column wholesale, so
            # anything not deliberately carried across is destroyed here.
            #
            # It read `job.payload["template_id"]` for weeks. The Harvester has never
            # put that key on the extract job -- only in the stored payload -- so the
            # value was always "", and this stage silently erased the correct one the
            # Harvester had written. `discovered_by` was NULL for all 305 companies and
            # `cindra explain` reported `(unknown)`, which reads as "these predate the
            # column" rather than "this has never worked".
            #
            # The job payload stays as a fallback and is checked first, so a re-extract
            # driven by hand can still supply it.
            "template_id": job.payload.get("template_id") or _stored_template_id(conn, outcome),
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


def _stored_template_id(conn: sqlite3.Connection, outcome: ExtractOutcome) -> str:
    """The discovering template, off the candidate row the Harvester wrote.

    Read here rather than taken from the job because this is where the value actually
    is, and because discovery provenance should survive a re-extract instead of living
    only in a transient job row.
    """
    row = conn.execute(
        "SELECT raw_payload FROM candidates WHERE candidate_id = ?", (outcome.candidate_id,)
    ).fetchone()
    if row is None:
        return ""
    try:
        stored = json.loads(row["raw_payload"])
    except (ValueError, TypeError):
        return ""
    return str(stored.get("template_id") or "") if isinstance(stored, dict) else ""


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


def enqueue_unextracted(store: Store, queue: Any, *, limit: int = 0) -> int:
    """Queue an extract job for every candidate stranded without one.

    The gap this closes is one nothing else could see. Every other reconciler starts
    from `companies` -- `enqueue_unenriched` asks which company was never enriched,
    `enqueue_stale_scores` which company's lead is behind its triggers -- and a
    candidate that never extracted never became a company. It sits in `candidates` with
    status `new`, its extract job dead-lettered, invisible to every query in the system
    and to `/healthz`, which counts dead letters but cannot say what work they were.

    Found after a thermal spell dead-lettered 11 extract jobs in one minute and left 15
    candidates stranded, including most of a fresh harvest. The pause accounting stops
    that happening again; this is the other half, because fixing the cause does not
    recover what the bug already buried -- the same shape as `RETIREMENT_RULES`.

    Liveness is checked against the job table rather than assumed from status, so a
    candidate already queued or in flight is not enqueued twice. Extract jobs carry no
    dedupe key -- the Harvester creates them once per candidate -- so nothing else
    would have stopped a duplicate.
    """
    rows = store.conn.execute(
        "SELECT candidate_id, raw_payload FROM candidates c "
        "WHERE c.status = 'new' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM jobs j WHERE j.kind = ? "
        "      AND j.status IN ('pending', 'in_flight') "
        "      AND json_extract(j.payload, '$.candidate_id') = c.candidate_id) "
        "ORDER BY c.created_at" + (" LIMIT ?" if limit else ""),
        (EXTRACT_KIND, *([limit] if limit else [])),
    ).fetchall()

    queued = 0
    with store.tx() as conn:
        for row in rows:
            try:
                payload = json.loads(row["raw_payload"])
            except (ValueError, TypeError):
                continue
            url = str(payload.get("url") or "")
            if not url:
                continue
            queue.enqueue(
                EXTRACT_KIND,
                {
                    "candidate_id": str(row["candidate_id"]),
                    "url": url,
                    "targets": list(payload.get("targets") or []),
                },
                conn=conn,
            )
            queued += 1
    if queued:
        log.info("extract_backlog_requeued", candidates=queued)
    return queued


def enqueue_stale_extractions(
    store: Store,
    queue: Any,
    *,
    limit: int = DEFAULT_RESTALE_LIMIT,
    config: Settings | None = None,
) -> int:
    """Re-extract companies whose gaps a since-fixed prompt would now fill.

    Fixing the prompt does not un-write what the broken one produced -- the same shape
    as `RETIREMENT_RULES` and `enqueue_stale_scores`, and the third time this project
    has had to build the backwards-looking half of a forwards-only pipeline. 583
    companies were extracted under a prompt that never named `description` or
    `industry`, and every one of them will stay null forever: `enqueue_unextracted`
    recovers candidates that never ran, `enqueue_unenriched` and `enqueue_stale_scores`
    both start from state the Extractor already wrote. Nothing asks whether what it
    wrote is behind the prompt that wrote it.

    **Two predicates, and both are load-bearing** -- exactly the pair `prose_version`
    needed:

    * *the gap* — the company is missing `description` or `industry`. Without it this
      re-decodes 583 pages whose extractions are already fine, at ~55 s each.
    * *the version* — the extraction was stamped by a different prompt build. Without
      it a page that genuinely says nothing about what the company does (a bare login
      screen, a holding page) is asked again on every pass, forever. Once re-extracted
      under the current prompt the stamp matches and it stops asking, whatever the
      answer was.

    Bounded by default because the cost is decode, not disk: 583 candidates is roughly
    nine hours of inference on this box, and an unbounded backfill would sit in front of
    every fresh harvest in the same queue. `DEFAULT_RESTALE_LIMIT` per reconcile pass
    drains the corpus over days without ever being the reason a new lead waits.

    Re-extraction is safe to repeat: `_upsert_company` fills gaps and never erases, and
    `_write_triggers` refreshes a live trigger rather than stacking a duplicate. It does
    write fresh `evidence` rows, which the retention sweep in `cindra maintain` reclaims.
    """
    cfg = config or settings()
    current = prompt_version(cfg.resolve(cfg.prompt_dir))
    rows = store.conn.execute(
        "SELECT c.candidate_id, c.raw_payload FROM candidates c "
        "JOIN companies co ON co.canonical_domain = c.resolved_domain "
        "WHERE c.status = 'extracted' "
        "  AND (co.description IS NULL OR co.industry IS NULL) "
        "  AND COALESCE(json_extract(c.raw_payload, '$.prompt_version'), '') <> ? "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM jobs j WHERE j.kind = ? "
        "      AND j.status IN ('pending', 'in_flight') "
        "      AND json_extract(j.payload, '$.candidate_id') = c.candidate_id) "
        "ORDER BY co.last_updated_at DESC LIMIT ?",
        (current, EXTRACT_KIND, limit),
    ).fetchall()

    queued = 0
    with store.tx() as conn:
        for row in rows:
            try:
                payload = json.loads(row["raw_payload"])
            except (ValueError, TypeError):
                continue
            url = str(payload.get("url") or "")
            if not url:
                continue
            queue.enqueue(
                EXTRACT_KIND,
                {
                    "candidate_id": str(row["candidate_id"]),
                    "url": url,
                    "targets": list(payload.get("trigger_codes") or []),
                    # The Extractor's own carry-forward reads this from the stored
                    # payload, but that read happens against the row this pass is about
                    # to overwrite. Passing it on the job is the documented fallback and
                    # costs nothing; losing `discovered_by` to a backfill would undo the
                    # only table that makes a query weight checkable.
                    "template_id": str(payload.get("template_id") or ""),
                },
                conn=conn,
            )
            queued += 1
    if queued:
        log.info("extract_stale_requeued", candidates=queued, prompt_version=current)
    return queued
