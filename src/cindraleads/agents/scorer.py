"""Scorer — a resolved company becomes a scored, compliance-cleared Lead.

Runs in strict order, and the order is the safety property:

    arithmetic  →  compliance  →  prose

The number is computed by `scoring.py` with no model in scope. The gate then vetoes or
clears. Only *after* both is a model asked to write the rationale and the outreach
angle, and it is handed the score as a fact rather than asked to produce one. A model
that returns nothing, times out, or is paused by the thermal governor costs the lead its
prose and nothing else — the tier, the offer and the verdict are already decided.

That ordering is what makes "a model must never be allowed to invent the number"
structurally true rather than a convention. There is no branch in which the LLM's output
reaches `score`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cindraleads import PIPELINE_VERSION
from cindraleads.compliance import ComplianceGate, LeadFacts
from cindraleads.config import Settings, load_prompt, load_yaml, prompt_version, settings
from cindraleads.dns_hygiene import hygiene_gaps
from cindraleads.errors import ConfigError, SchemaValidationError
from cindraleads.llm import StructuredLLM
from cindraleads.logging import get_logger
from cindraleads.models import (
    DnsHygiene,
    Job,
    LeadProse,
    StageResult,
    from_iso,
    to_iso,
    utcnow,
)
from cindraleads.scoring import ScoreInput, ScoringConfig, TriggerObservation, score
from cindraleads.store import Store

__all__ = [
    "DISPATCH_KIND",
    "SCORE_KIND",
    "ScoreOutcome",
    "Scorer",
    "enqueue_stale_scores",
]

log = get_logger("cindraleads.scorer")

SCORE_KIND = "score.company"
DISPATCH_KIND = "dispatch.lead"


def lead_id_for(canonical_domain: str) -> str:
    """`sha256(canonical_domain)[:16]`, per the master prompt's section 11.

    Stable across runs by construction, which is what lets the Dispatcher recognise a
    lead it has already sent without keeping a separate identity table.
    """
    return hashlib.sha256(canonical_domain.encode()).hexdigest()[:16]


# How long to wait before retrying prose that the thermal governor blocked. The Pi
# plateaus at 80-82 C under load and sheds that in minutes once inference stops, so
# 20 minutes is comfortably past the recovery without hammering the governor.
PROSE_RETRY_SECONDS = 20 * 60


@dataclass(frozen=True)
class ScoreOutcome:
    canonical_domain: str
    prose: LeadProse | None = None
    error: str | None = None
    skipped: str | None = None
    # Set when the model was unavailable for a *recoverable* reason. The lead is still
    # scored and stored -- the arithmetic never needed a model -- but the job asks to
    # come back for the prose rather than finalising the lead without an angle.
    retry_prose_in: int = 0


@dataclass
class Scorer:
    store: Store
    llm: StructuredLLM | None = None
    config: Settings | None = None
    gate: ComplianceGate | None = None

    def __post_init__(self) -> None:
        cfg = self.config or settings()
        self.config = cfg
        self.scoring = ScoringConfig.load(cfg)
        # Stamped on every lead so a later calibration change can find the leads it
        # invalidated. Read once here rather than per lead: it is a hash of config
        # already in memory, but the Scorer writes it on a path that runs per company.
        self._scoring_version = self.scoring.fingerprint()
        self.gate = self.gate or ComplianceGate.from_config(cfg)
        base = cfg.resolve(cfg.prompt_dir)
        self._prompt_version = prompt_version(base)
        try:
            self._angle_prompt: str | None = load_prompt("outreach_angle", base=base)
        except ConfigError:
            self._angle_prompt = None
        icp = load_yaml("icp", base=cfg.resolve(cfg.config_dir))
        self._sectors = tuple(str(s) for s in (icp.get("profile") or {}).get("primary") or ())
        self._local_tlds = tuple(
            str(t) for t in (icp.get("geography") or {}).get("local_tlds") or ()
        )

    # ------------------------------------------------------------------ phase 1

    async def prepare(self, job: Job) -> ScoreOutcome:
        """Write the prose. The number is not computed here and cannot be.

        `prepare` is the phase that may do slow I/O, which on this box means the model.
        Deliberately the *only* thing it does: everything that decides whether a lead
        exists happens in `commit`, from database state, with no model involved.
        """
        domain = str(job.payload.get("canonical_domain") or "")
        if not domain:
            return ScoreOutcome(canonical_domain="", error="score job needs canonical_domain")

        facts = self._read(domain)
        if facts is None:
            return ScoreOutcome(canonical_domain=domain, error=f"company {domain} not found")
        if not facts["triggers"]:
            # Fit without news. Not a lead, and not worth a model call to describe.
            return ScoreOutcome(canonical_domain=domain, skipped="no live trigger")

        if self.llm is None or self._angle_prompt is None:
            return ScoreOutcome(canonical_domain=domain)

        result = score(self._score_input(facts), self.scoring)
        prompt = self._angle_prompt.format(
            display_name=facts["display_name"],
            canonical_domain=domain,
            description=facts["description"] or "",
            triggers=", ".join(
                f"{t.code} (observed {t.observed_at:%Y-%m-%d})"
                for t in self._score_input(facts).triggers
            ),
            offer=result.offer,
            country=facts["country"] or "",
        )
        try:
            structured = await self.llm.generate(prompt, LeadProse, max_tokens=400)
        except SchemaValidationError as exc:
            # Prose is a nice-to-have: the lead is already fully decided without it, so
            # a model failure must never cost us the lead. But *why* it failed decides
            # whether to come back. A thermal pause or a dead Ollama is temporary, and
            # finalising the lead without an angle would mean the whole batch scored
            # during one hot spell is permanently angle-less -- which is what happened
            # on the first real scoring run: 32 of 32.
            reason = str(exc)
            recoverable = _is_recoverable(reason)
            log.warning(
                "scorer_prose_failed",
                canonical_domain=domain,
                error=reason[:200],
                will_retry=recoverable,
            )
            return ScoreOutcome(
                canonical_domain=domain,
                retry_prose_in=PROSE_RETRY_SECONDS if recoverable else 0,
            )
        return ScoreOutcome(canonical_domain=domain, prose=structured.value)

    # ------------------------------------------------------------------ phase 2

    def commit(self, job: Job, outcome: ScoreOutcome, conn: sqlite3.Connection) -> StageResult:
        if outcome.error:
            return StageResult(ok=False, stage="scorer", job_id=job.job_id, error=outcome.error)
        if outcome.skipped:
            log.info(
                "score_skipped", canonical_domain=outcome.canonical_domain, why=outcome.skipped
            )
            return StageResult(ok=True, stage="scorer", job_id=job.job_id)

        facts = self._read(outcome.canonical_domain, conn=conn)
        if facts is None:
            return StageResult(
                ok=False,
                stage="scorer",
                job_id=job.job_id,
                error=f"company {outcome.canonical_domain} disappeared",
            )

        result = score(self._score_input(facts), self.scoring)
        assert self.gate is not None
        self.gate.load_suppression(conn)
        verdict = self.gate.review(
            LeadFacts(
                canonical_domain=outcome.canonical_domain,
                display_name=facts["display_name"],
                employee_band=facts["employee_band"],
                industry=facts["industry"],
                country=facts["country"],
                trigger_codes=tuple(t["code"] for t in facts["triggers"]),
                evidence_urls=tuple(facts["evidence_urls"]),
            )
        )

        lead_id = lead_id_for(outcome.canonical_domain)
        if not verdict.passed:
            ComplianceGate.quarantine(conn, subject_id=lead_id, verdict=verdict)
            _upsert_lead(
                conn,
                lead_id,
                outcome,
                facts,
                result,
                verdict,
                self._prompt_version,
                self._scoring_version,
            )
            log.info("lead_vetoed", lead_id=lead_id, vetoes=verdict.vetoes)
            return StageResult(ok=True, stage="scorer", job_id=job.job_id)

        _upsert_lead(
            conn,
            lead_id,
            outcome,
            facts,
            result,
            verdict,
            self._prompt_version,
            self._scoring_version,
        )
        log.info(
            "lead_scored",
            lead_id=lead_id,
            canonical_domain=outcome.canonical_domain,
            score=result.score,
            tier=result.tier,
            offer=result.offer,
            penalties=sorted(result.penalties),
        )
        follow_on: list[tuple[str, dict[str, Any]]] = []
        if outcome.retry_prose_in and not _has_angle(conn, lead_id):
            follow_on.append(
                (
                    SCORE_KIND,
                    {
                        "canonical_domain": outcome.canonical_domain,
                        "_delay_seconds": outcome.retry_prose_in,
                    },
                )
            )

        if result.tier == "REJECT":
            # Scored, stored, never dispatched. Kept because tomorrow's trigger may
            # lift it over the threshold, and because a REJECT rate that suddenly
            # doubles is the first sign the ICP has drifted.
            return StageResult(ok=True, stage="scorer", job_id=job.job_id, follow_on=follow_on)

        follow_on.append((DISPATCH_KIND, {"lead_id": lead_id}))
        return StageResult(ok=True, stage="scorer", job_id=job.job_id, follow_on=follow_on)

    async def run(self, job: Job) -> StageResult:
        outcome = await self.prepare(job)
        with self.store.tx() as conn:
            return self.commit(job, outcome, conn)

    # -------------------------------------------------------------------- reads

    def _read(
        self, domain: str, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        active = conn or self.store.conn
        row = active.execute(
            "SELECT * FROM companies WHERE canonical_domain = ?", (domain,)
        ).fetchone()
        if row is None:
            return None

        triggers = [
            dict(r)
            for r in active.execute(
                "SELECT trigger_id, code, observed_at, confidence FROM triggers "
                "WHERE canonical_domain = ? AND active = 1 AND decays_at > ?",
                (domain, to_iso(utcnow())),
            ).fetchall()
        ]
        evidence: list[dict[str, Any]] = []
        for trigger in triggers:
            evidence.extend(
                dict(r)
                for r in active.execute(
                    "SELECT e.url, e.source_id, e.snippet FROM evidence e "
                    "JOIN trigger_evidence te ON te.evidence_id = e.evidence_id "
                    "WHERE te.trigger_id = ?",
                    (trigger["trigger_id"],),
                ).fetchall()
            )
            trigger["evidence"] = [
                dict(r)
                for r in active.execute(
                    "SELECT e.url, e.source_id FROM evidence e "
                    "JOIN trigger_evidence te ON te.evidence_id = e.evidence_id "
                    "WHERE te.trigger_id = ?",
                    (trigger["trigger_id"],),
                ).fetchall()
            ]

        contacts = [
            dict(r)
            for r in active.execute(
                "SELECT email, email_status, full_name FROM contacts WHERE canonical_domain = ? "
                "ORDER BY CASE email_status WHEN 'verified' THEN 0 WHEN 'role_account' THEN 1 "
                "WHEN 'catch_all' THEN 2 ELSE 3 END",
                (domain,),
            ).fetchall()
        ]

        return {
            "canonical_domain": domain,
            "contacts": contacts,
            "enriched_at": row["enriched_at"],
            "display_name": str(row["display_name"]),
            "description": row["description"],
            "employee_band": row["employee_band"],
            "industry": row["industry"],
            "country": row["country"],
            "ai_surface": json.loads(row["ai_surface"] or "[]"),
            "subdomain_count": row["subdomain_count_ct"],
            "hygiene_gaps": _hygiene_gaps(row["dns_hygiene"]),
            "triggers": triggers,
            "evidence": evidence,
            "evidence_urls": [e["url"] for e in evidence],
        }

    def _score_input(self, facts: dict[str, Any]) -> ScoreInput:
        return ScoreInput(
            canonical_domain=facts.get("canonical_domain", ""),
            triggers=tuple(
                TriggerObservation(
                    code=str(t["code"]),
                    observed_at=from_iso(str(t["observed_at"])),
                    evidence_urls=tuple(e["url"] for e in t.get("evidence", [])),
                    evidence_sources=tuple(e["source_id"] for e in t.get("evidence", [])),
                )
                for t in facts["triggers"]
            ),
            employee_band=facts["employee_band"],
            country=facts["country"],
            industry=facts["industry"],
            ai_surface=tuple(facts["ai_surface"]),
            subdomain_count=facts["subdomain_count"],
            # The best contact we have decides reachability. Sorted by status in the
            # query, so this is the strongest one rather than an arbitrary one.
            email_status=str(facts["contacts"][0]["email_status"]) if facts["contacts"] else "none",
            has_named_contact=any(c.get("full_name") for c in facts["contacts"]),
            hygiene_gap=bool(facts["hygiene_gaps"]),
            # Gates the `no_contact` penalty. Charging a lead for a missing contact
            # before anything has looked for one punishes our own pipeline, not the
            # prospect -- measured 2026-08-15, it put every lead below REJECT.
            enrichment_ran=facts["enriched_at"] is not None,
            primary_sectors=self._sectors,
            local_tlds=self._local_tlds,
        )


def _upsert_lead(
    conn: sqlite3.Connection,
    lead_id: str,
    outcome: ScoreOutcome,
    facts: dict[str, Any],
    result: Any,
    verdict: Any,
    prompt_ver: str,
    scoring_ver: str,
) -> None:
    now = to_iso(utcnow())
    angle = outcome.prose.outreach_angle if outcome.prose else ""
    bengali = outcome.prose.bengali_angle if outcome.prose else None
    conn.execute(
        "INSERT INTO leads (lead_id, canonical_domain, score, score_breakdown, tier, "
        "recommended_offer, outreach_angle, bengali_angle, compliance, first_seen_at, "
        "last_updated_at, pipeline_version, prompt_version, scoring_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(lead_id) DO UPDATE SET score=excluded.score, "
        "score_breakdown=excluded.score_breakdown, tier=excluded.tier, "
        "recommended_offer=excluded.recommended_offer, "
        # An empty angle must not overwrite a good one from a run where the model
        # was available. Prose is expensive and its absence is not new information.
        "outreach_angle=CASE WHEN excluded.outreach_angle != '' "
        "THEN excluded.outreach_angle ELSE leads.outreach_angle END, "
        "bengali_angle=COALESCE(excluded.bengali_angle, leads.bengali_angle), "
        "compliance=excluded.compliance, last_updated_at=excluded.last_updated_at, "
        "pipeline_version=excluded.pipeline_version, prompt_version=excluded.prompt_version, "
        "scoring_version=excluded.scoring_version",
        (
            lead_id,
            outcome.canonical_domain,
            result.score,
            json.dumps({**result.breakdown, **result.penalties}),
            result.tier,
            result.offer,
            angle,
            bengali,
            verdict.model_dump_json(),
            now,
            now,
            PIPELINE_VERSION,
            prompt_ver,
            scoring_ver,
        ),
    )


def score_stamp(when: datetime) -> str:
    return to_iso(when)


def enqueue_stale_scores(
    store: Store,
    queue: Any,
    *,
    limit: int = 0,
    config: ScoringConfig | None = None,
    force: bool = False,
) -> int:
    """Queue a score job for every company whose lead is behind its triggers.

    Scoring driven only by the resolve event is not enough, and the first real run
    proved it: 37 companies resolved before the Resolver enqueued scoring, so nothing
    would ever have scored them. A pipeline that only reacts to events cannot heal
    from a stage being added later, from a restore, or from a crash between stages.

    Reconciling instead — "which company's lead is older than its newest trigger?" —
    covers all three, and is the same query the Phase 7 nightly decay recompute needs.
    The dedupe key carries that trigger timestamp, so re-running enqueues nothing while
    a genuinely new trigger does.

    **A stale calibration counts as stale too.** Timestamps cannot see a config edit:
    narrowing `single_source` changed what every score should be and moved neither a
    trigger's `observed_at` nor a lead's `last_updated_at`, so 108 leads would have
    kept numbers the current code does not produce. A lead whose `scoring_version`
    differs from the running one is out of date by definition, and NULL — a lead
    written before the column existed — is the same thing.

    `force` bypasses the dedupe key, and exists because of a specific way this wedges:
    **a job that ran but achieved nothing is indistinguishable from one that worked.**
    A worker executing a stale build drained a batch of rescores without stamping any
    calibration, and those `done` rows now hold the keys for the very work they failed
    to do — so the reconciler skips those companies forever. No query over this data
    can detect it; it needs a human saying "recompute anyway".
    """
    fingerprint = (config or ScoringConfig.load()).fingerprint()
    now = to_iso(utcnow())
    rows = store.conn.execute(
        "SELECT c.canonical_domain AS domain, "
        "MAX(t.observed_at) AS newest, "
        "MIN(COALESCE(l.scoring_version, '') = ?) AS calibrated "
        "FROM companies c "
        "JOIN triggers t ON t.canonical_domain = c.canonical_domain "
        "LEFT JOIN leads l ON l.canonical_domain = c.canonical_domain "
        "WHERE t.active = 1 AND t.decays_at > ? "
        "GROUP BY c.canonical_domain "
        "HAVING l.lead_id IS NULL "
        "   OR MAX(t.observed_at) > COALESCE(l.last_updated_at, '') "
        "   OR calibrated = 0 "
        # Genuinely new triggers first, recalibrations behind them. A config edit makes
        # the whole corpus stale at once, and at ~18 s a lead that is hours of queue --
        # long enough that a funding round found this morning would sit behind it.
        "ORDER BY calibrated DESC, newest DESC" + (" LIMIT ?" if limit else ""),
        (fingerprint, now, *([limit] if limit else [])),
    ).fetchall()

    queued = 0
    with store.tx() as conn:
        for row in rows:
            domain = str(row["domain"])
            # The fingerprint belongs in the key. `enqueue` matches `dedupe_key`
            # across every job including completed ones, and a recalibration does not
            # move `newest` -- so without it the rescore collides with the job that
            # already ran under the old calibration and is silently dropped. The whole
            # mechanism would then look like it worked and change nothing.
            # `force` adds a nonce so the key cannot match the completed job blocking
            # this one -- see the docstring for why that situation is undetectable.
            shape = f"{domain}|{row['newest']}|{fingerprint}"
            if force:
                shape += f"|force:{now}"
            digest = hashlib.sha256(shape.encode()).hexdigest()[:16]
            existing = conn.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ? LIMIT 1", (f"score:{digest}",)
            ).fetchone()
            queue.enqueue(
                SCORE_KIND,
                {"canonical_domain": domain},
                dedupe_key=f"score:{digest}",
                conn=conn,
            )
            if existing is None:
                queued += 1
    return queued


# Failures that will plausibly answer differently later. A schema violation from the
# model will not -- it has already been retried at temperature 0 and escalated.
_RECOVERABLE = ("thermal governor", "no escalation backend", "connect", "timeout", "refused")


def _is_recoverable(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in _RECOVERABLE)


def _has_angle(conn: sqlite3.Connection, lead_id: str) -> bool:
    row = conn.execute("SELECT outreach_angle FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    return bool(row and str(row["outreach_angle"] or "").strip())


def _hygiene_gaps(raw: Any) -> list[str]:
    """Published DNS gaps, from the JSON the Enricher stored."""
    if not raw:
        return []
    try:
        return hygiene_gaps(DnsHygiene.model_validate_json(str(raw)))
    except ValueError:
        return []
