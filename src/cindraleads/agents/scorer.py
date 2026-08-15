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
from cindraleads.errors import ConfigError, SchemaValidationError
from cindraleads.llm import StructuredLLM
from cindraleads.logging import get_logger
from cindraleads.models import Job, LeadProse, StageResult, from_iso, to_iso, utcnow
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


@dataclass(frozen=True)
class ScoreOutcome:
    canonical_domain: str
    prose: LeadProse | None = None
    error: str | None = None
    skipped: str | None = None


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
            # Prose is a nice-to-have. The lead is already fully decided without it, so
            # a model failure must not cost us the lead.
            log.warning("scorer_prose_failed", canonical_domain=domain, error=str(exc)[:200])
            return ScoreOutcome(canonical_domain=domain)
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
            _upsert_lead(conn, lead_id, outcome, facts, result, verdict, self._prompt_version)
            log.info("lead_vetoed", lead_id=lead_id, vetoes=verdict.vetoes)
            return StageResult(ok=True, stage="scorer", job_id=job.job_id)

        _upsert_lead(conn, lead_id, outcome, facts, result, verdict, self._prompt_version)
        log.info(
            "lead_scored",
            lead_id=lead_id,
            canonical_domain=outcome.canonical_domain,
            score=result.score,
            tier=result.tier,
            offer=result.offer,
            penalties=sorted(result.penalties),
        )
        if result.tier == "REJECT":
            # Scored, stored, never dispatched. Kept because tomorrow's trigger may
            # lift it over the threshold, and because a REJECT rate that suddenly
            # doubles is the first sign the ICP has drifted.
            return StageResult(ok=True, stage="scorer", job_id=job.job_id)

        return StageResult(
            ok=True,
            stage="scorer",
            job_id=job.job_id,
            follow_on=[(DISPATCH_KIND, {"lead_id": lead_id})],
        )

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

        return {
            "display_name": str(row["display_name"]),
            "description": row["description"],
            "employee_band": row["employee_band"],
            "industry": row["industry"],
            "country": row["country"],
            "ai_surface": json.loads(row["ai_surface"] or "[]"),
            "subdomain_count": row["subdomain_count_ct"],
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
) -> None:
    now = to_iso(utcnow())
    angle = outcome.prose.outreach_angle if outcome.prose else ""
    bengali = outcome.prose.bengali_angle if outcome.prose else None
    conn.execute(
        "INSERT INTO leads (lead_id, canonical_domain, score, score_breakdown, tier, "
        "recommended_offer, outreach_angle, bengali_angle, compliance, first_seen_at, "
        "last_updated_at, pipeline_version, prompt_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(lead_id) DO UPDATE SET score=excluded.score, "
        "score_breakdown=excluded.score_breakdown, tier=excluded.tier, "
        "recommended_offer=excluded.recommended_offer, "
        # An empty angle must not overwrite a good one from a run where the model
        # was available. Prose is expensive and its absence is not new information.
        "outreach_angle=CASE WHEN excluded.outreach_angle != '' "
        "THEN excluded.outreach_angle ELSE leads.outreach_angle END, "
        "bengali_angle=COALESCE(excluded.bengali_angle, leads.bengali_angle), "
        "compliance=excluded.compliance, last_updated_at=excluded.last_updated_at, "
        "pipeline_version=excluded.pipeline_version, prompt_version=excluded.prompt_version",
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
        ),
    )


def score_stamp(when: datetime) -> str:
    return to_iso(when)


def enqueue_stale_scores(store: Store, queue: Any, *, limit: int = 0) -> int:
    """Queue a score job for every company whose lead is behind its triggers.

    Scoring driven only by the resolve event is not enough, and the first real run
    proved it: 37 companies resolved before the Resolver enqueued scoring, so nothing
    would ever have scored them. A pipeline that only reacts to events cannot heal
    from a stage being added later, from a restore, or from a crash between stages.

    Reconciling instead — "which company's lead is older than its newest trigger?" —
    covers all three, and is the same query the Phase 7 nightly decay recompute needs.
    The dedupe key carries that trigger timestamp, so re-running enqueues nothing while
    a genuinely new trigger does.
    """
    rows = store.conn.execute(
        "SELECT c.canonical_domain AS domain, MAX(t.observed_at) AS newest "
        "FROM companies c "
        "JOIN triggers t ON t.canonical_domain = c.canonical_domain "
        "LEFT JOIN leads l ON l.canonical_domain = c.canonical_domain "
        "WHERE t.active = 1 AND t.decays_at > ? "
        "GROUP BY c.canonical_domain "
        "HAVING l.lead_id IS NULL OR MAX(t.observed_at) > COALESCE(l.last_updated_at, '') "
        "ORDER BY newest DESC" + (" LIMIT ?" if limit else ""),
        (to_iso(utcnow()), limit) if limit else (to_iso(utcnow()),),
    ).fetchall()

    queued = 0
    with store.tx() as conn:
        for row in rows:
            domain = str(row["domain"])
            digest = hashlib.sha256(f"{domain}|{row['newest']}".encode()).hexdigest()[:16]
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
