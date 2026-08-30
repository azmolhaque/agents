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
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
from cindraleads.scoring import (
    ScoreInput,
    ScoringConfig,
    TriggerObservation,
    band_from_open_roles,
    score,
)
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

# Decode budget for the prose call, and it has to depend on the language.
#
# `LeadProse` allows 1080 characters across three fields. In English that is roughly
# 270 tokens and 400 was ample. Bengali script is several tokens per *character* in
# this tokenizer, so a BD lead asking for a 400-character `bengali_angle` can want four
# figures of budget -- and at 400 the model ran out mid-string, producing JSON that
# ends in the middle of a value. Observed on futurestartup.com: "Invalid JSON: EOF while
# parsing a string at line 3 column 863", after which the lead dispatched with a blank
# angle.
#
# Sized by what is actually being asked for rather than raised across the board. The
# per-language split is kept for the *worst* case only: at 3.7 tok/s a runaway 1200-token
# decode is five minutes, and that ceiling should not apply to leads that cannot need it.
#
# These pair with the `LeadProse` bounds, and nothing at runtime checks that they agree.
# The grammar *does* hold -- a field bounded at 20 characters comes back as exactly 20,
# cut mid-phrase -- but it bounds characters while this bounds tokens, and those are the
# same number only in English. A 400-character `bengali_angle` is legal grammar worth
# ~1200 tokens; against a 400-token budget the decode ran out inside a string the
# grammar was perfectly happy with, and the object was lost to a JSON EOF at byte column
# 908. Shrinking the bound to 220 is the other half of this constant.
#
# The English figure went 400 -> 600 for headroom, not for a measured failure. A ceiling
# is not a cost: decode stops at the stop token, so a generous limit is free on every
# call that finishes early and is paid only by the ramble it exists to catch. Sizing it
# tightly to the bounds saved nothing and turned a long answer into a lost one.
PROSE_MAX_TOKENS = 600
PROSE_MAX_TOKENS_BENGALI = 1200


def _prose_budget(country: str | None) -> int:
    """Tokens to allow the prose call. BD gets more because Bengali costs more.

    The prompt asks for `bengali_angle` only when the country is BD, so the expensive
    budget follows the same condition rather than being applied everywhere.
    """
    return PROSE_MAX_TOKENS_BENGALI if str(country or "").upper() == "BD" else PROSE_MAX_TOKENS


def prose_version(base: Path | None = None) -> str:
    """Identity of the machinery that writes an angle: the prompts *and* the budget.

    Raising `PROSE_MAX_TOKENS_BENGALI` fixed the cause of a blank angle and healed
    nothing that already had one, because nothing could find those leads again. The
    arithmetic had not changed, so `scoring_version` matched; no trigger had moved, so
    `last_updated_at` was current; the score job had completed successfully, because a
    failed prose call is not a failed score. Three Tier B cards sat in Discord with a
    dash where the angle belongs and no query in the system disagreed with that.

    `prompt_version` alone would not have caught it either -- the fix was a constant in
    this file, not a prompt edit. So the stamp on a lead covers both, and
    `enqueue_stale_scores` re-queues an angle-less lead exactly when this differs from
    what wrote it. Same shape as `RETIREMENT_RULES`: changing the rule is half a change,
    the other half is re-running it over what the old rule produced.

    The schema bounds are in here too, and for the same reason one level up: shrinking
    `bengali_angle` from 400 to 220 changes what the model is asked to write, and on its
    own it moved neither the prompt hash nor either token constant. The two leads the
    change was made for would have stayed blank forever while the mechanism built to
    find them reported nothing to do.

    A lead that *has* an angle is never re-queued by this. Prose costs ~18 s of decode
    and an existing angle is not improved by a budget change.
    """
    digest = hashlib.sha256(prompt_version(base).encode())
    digest.update(f"|{PROSE_MAX_TOKENS}|{PROSE_MAX_TOKENS_BENGALI}".encode())
    bounds = "|".join(
        f"{name}:{_max_length(field)}" for name, field in sorted(LeadProse.model_fields.items())
    )
    digest.update(f"|{bounds}".encode())
    # What counts as a leak is part of what a good angle is. Widening the guard to the
    # offer slugs makes an angle the old build accepted one this build would discard
    # and re-ask for, which is a different answer to the same prompt.
    digest.update(f"|{_CODE_PATTERN.pattern}|{_SLUG_PATTERN.pattern}".encode())
    return digest.hexdigest()[:16]


def _max_length(field: Any) -> int | None:
    """The `max_length` off a Pydantic field, wherever this version keeps it."""
    for entry in getattr(field, "metadata", ()) or ():
        length = getattr(entry, "max_length", None)
        if length is not None:
            return int(length)
    return None


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

    # How many times prose has failed for this lead, carried into the follow-on payload
    # so the chain can count itself. Split by cause: a fault and a thermal pause are
    # charged to different ceilings. See `MAX_PROSE_ATTEMPTS` / `MAX_PROSE_PAUSES`.
    prose_attempt: int = 0
    prose_pauses: int = 0


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
        # The *prose* version, not the bare prompt hash. What this column means on a
        # lead is "which build wrote this angle" -- and the budget that decides whether
        # an angle can finish is part of that build. `prompt_version` keeps its plain
        # meaning on a candidate, where the Extractor stamps it and the golden fixtures
        # depend on it.
        self._prompt_version = prose_version(base)
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

        # A retry only exists to fetch a missing angle, so if the angle arrived while it
        # was waiting there is nothing left for it to do. `commit` already refuses to
        # *schedule* a retry for a lead that has prose, but a job queued twenty minutes
        # ago cannot know what happened since -- and this one re-decoded an angle
        # `bisecto.com` had already been given, for 94 s on a box that does 3.7 tok/s.
        # The most expensive possible no-op, and invisible: the job completes, the lead
        # is fine, and the only trace is the duration.
        #
        # Scoped to retries. A plain score job re-writes prose on purpose.
        if _is_prose_retry(job) and _has_angle(self.store.conn, lead_id_for(domain)):
            log.info("scorer_prose_retry_moot", canonical_domain=domain)
            return ScoreOutcome(canonical_domain=domain)

        result = score(self._score_input(facts), self.scoring)
        prompt = self._angle_prompt.format(
            display_name=facts["display_name"],
            canonical_domain=domain,
            description=facts["description"] or "",
            # The human phrase, never the code. Handing the model `T1_AI_SHIP` gave it
            # nothing to say, so it said "T1_AI_SHIP" -- in an angle written to be
            # pasted into a prospect's inbox. `means` is validated non-empty at config
            # load, so the fallback is unreachable and exists only so a future code
            # added without one degrades to a vague angle rather than a crash.
            triggers=self._trigger_phrases(self._score_input(facts).triggers),
            offer=result.offer,
            country=facts["country"] or "",
        )
        try:
            structured = await self.llm.generate(
                prompt, LeadProse, max_tokens=_prose_budget(facts["country"])
            )
        except SchemaValidationError as exc:
            # Prose is a nice-to-have: the lead is already fully decided without it, so
            # a model failure must never cost us the lead. But *why* it failed decides
            # whether to come back. A thermal pause or a dead Ollama is temporary, and
            # finalising the lead without an angle would mean the whole batch scored
            # during one hot spell is permanently angle-less -- which is what happened
            # on the first real scoring run: 32 of 32.
            reason = str(exc)
            paused = _THERMAL in reason.lower()
            # Exactly one of the two counters moves, so every retry is charged to the
            # evidence it actually provides -- the same accounting the queue does with
            # `attempts` and `reclaims`.
            attempt = int(job.payload.get(PROSE_ATTEMPT_KEY) or 0) + (0 if paused else 1)
            pauses = int(job.payload.get(PROSE_PAUSE_KEY) or 0) + (1 if paused else 0)
            within = pauses < MAX_PROSE_PAUSES if paused else attempt < MAX_PROSE_ATTEMPTS
            retry = _is_recoverable(reason) and within
            log.warning(
                "scorer_prose_failed",
                canonical_domain=domain,
                error=reason[:200],
                attempt=attempt,
                pauses=pauses,
                will_retry=retry,
                gave_up=_is_recoverable(reason) and not retry,
            )
            return ScoreOutcome(
                canonical_domain=domain,
                retry_prose_in=PROSE_RETRY_SECONDS if retry else 0,
                prose_attempt=attempt,
                prose_pauses=pauses,
            )
        prose = structured.value
        leaked = _leaked_codes(prose)
        if leaked:
            # Discard the prose, keep the lead. An angle naming our internal taxonomy
            # is worse than no angle: the card exists to be pasted into an email, and
            # "You published T1_AI_SHIP" is unusable there. Not retried -- the same
            # prompt would produce the same thing -- so the lead ships angle-less and
            # the log names the codes that leaked.
            log.warning("scorer_prose_leaked_codes", canonical_domain=domain, codes=leaked)
            return ScoreOutcome(canonical_domain=domain)
        return ScoreOutcome(canonical_domain=domain, prose=prose)

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
                        PROSE_ATTEMPT_KEY: outcome.prose_attempt,
                        PROSE_PAUSE_KEY: outcome.prose_pauses,
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
            # Stated first, inferred second, and only ever downward. This one key
            # feeds both the `employee_band_points` gradient and the
            # `under_employee_ceiling` veto, which is why filling it here fixes both
            # at once -- and why both were dead while it was null for 615 of 616.
            "employee_band": row["employee_band"]
            or band_from_open_roles(row["open_roles"], self.scoring),
            "industry": row["industry"],
            "country": row["country"],
            "ai_surface": json.loads(row["ai_surface"] or "[]"),
            "subdomain_count": row["subdomain_count_ct"],
            "hygiene_gaps": _hygiene_gaps(row["dns_hygiene"]),
            "triggers": triggers,
            "evidence": evidence,
            "evidence_urls": [e["url"] for e in evidence],
        }

    def _trigger_phrases(self, triggers: Any) -> str:
        """The triggers as the prospect would recognise them, strongest first.

        Ordered by weight rather than by whatever the query returned, because the model
        leads with what it is given first and the opening clause is the reason the email
        gets read. Every angle on the first real call list opened with T1_AI_SHIP --
        "you announced an AI feature" -- including a lead whose actual news was a
        customer asking them for a pentest report.

        A derived trigger carries no date. See `DERIVED_TRIGGERS`.
        """
        from cindraleads.agents.dispatcher import TRIGGER_ORDER

        ordered = sorted(triggers, key=lambda t: -TRIGGER_ORDER.get(str(t.code), 0))
        parts: list[str] = []
        for trig in ordered:
            rule = self.scoring.triggers.get(trig.code)
            if rule is None or not rule.means:
                # Unreachable while every code has a `means`, and deliberately vague if
                # one is ever added without: a wrong specific is worse than a right vague.
                parts.append("published something relevant recently")
                continue
            if trig.code in DERIVED_TRIGGERS:
                parts.append(rule.means)
            else:
                parts.append(f"{rule.means} ({_age_phrase(trig.observed_at)})")
        return "; ".join(parts)

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
    prose_ver = prose_version()
    now = to_iso(utcnow())
    rows = store.conn.execute(
        "SELECT c.canonical_domain AS domain, "
        "MAX(t.observed_at) AS newest, "
        "MIN(COALESCE(l.scoring_version, '') = ?) AS calibrated, "
        # Angle-less *and* written by an older prose build. Both halves are load
        # bearing: without the first this re-decodes angles that are already fine,
        # and without the second a lead the model can never write an angle for --
        # one whose prose leaks trigger codes -- is re-queued on every reconcile
        # forever. The scorer stamps this column even when the prose call fails, so
        # a lead that stays blank under the new build stops asking after one attempt.
        "MIN(COALESCE(l.outreach_angle, '') != '' "
        "    OR COALESCE(l.prompt_version, '') = ?) AS prosed "
        "FROM companies c "
        "JOIN triggers t ON t.canonical_domain = c.canonical_domain "
        "LEFT JOIN leads l ON l.canonical_domain = c.canonical_domain "
        "WHERE t.active = 1 AND t.decays_at > ? "
        "GROUP BY c.canonical_domain "
        "HAVING l.lead_id IS NULL "
        "   OR MAX(t.observed_at) > COALESCE(l.last_updated_at, '') "
        "   OR calibrated = 0 "
        "   OR prosed = 0 "
        # Genuinely new triggers first, recalibrations behind them, angle repairs last.
        # A config edit makes the whole corpus stale at once, and at ~18 s a lead that
        # is hours of queue -- long enough that a funding round found this morning
        # would sit behind it. An angle repair is the least urgent of the three: the
        # lead already dispatched, and what is being fixed is the copy on the card.
        #
        # Ranked by a row's *most* urgent reason, not by each flag in turn. A row can
        # be selected for several at once, and ordering on the flags alone let an
        # incidental one decide: a company with a funding round found this morning
        # also had no angle yet, so `prosed` sorted it behind a pure recalibration.
        "ORDER BY (l.lead_id IS NULL "
        "          OR MAX(t.observed_at) > COALESCE(l.last_updated_at, '')) DESC, "
        "         calibrated DESC, prosed DESC, newest DESC" + (" LIMIT ?" if limit else ""),
        (fingerprint, prose_ver, now, *([limit] if limit else [])),
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
            # The prose version is in the key for the same reason the fingerprint is:
            # an angle repair moves neither `newest` nor the calibration, so without it
            # the job collides with the completed one that produced the blank angle and
            # is silently dropped -- the mechanism reporting success having done nothing.
            shape = f"{domain}|{row['newest']}|{fingerprint}|{prose_ver}"
            if force:
                # A uuid, not `now`. `to_iso` has millisecond resolution, so two forced
                # runs in the same millisecond produced the same key and the second was
                # swallowed by the first -- the precise failure `--force` exists to
                # escape.
                shape += f"|force:{uuid.uuid4().hex}"
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
# Markers of a failure that waiting can fix. Every one names a condition external to
# the request: the box was too hot, the socket did not open, the model did not answer
# in time. Send the same prompt in twenty minutes and it may well work.
#
# `no escalation backend` used to be in this list and had to come out. It is not a
# description of the failure -- it is appended to *every* exhausted-ladder message on a
# box with no cloud tier configured, which is this box, always. So it matched
# unconditionally and made `_is_recoverable` return True for failures that waiting
# cannot fix: the Bengali decode that ran out of budget mid-string was retried every
# twenty minutes on a deterministic JSON truncation. A marker present in 100% of cases
# is a constant, not a discriminator -- the same shape as `single_source` at 96%
# incidence.
_RECOVERABLE = ("thermal governor", "connect", "timeout", "refused")

# The one recoverable cause that is a designed-for state rather than a fault, and so is
# counted separately. See `MAX_PROSE_PAUSES`.
_THERMAL = "thermal governor"

# How many times prose may be retried before the lead ships without an angle.
#
# The retry needs its own ceiling because nothing else can supply one. A prose failure
# is not a stage failure -- the lead is scored and stored, the job returns `ok=True`
# and completes -- so `attempts` never increments and the queue's `max_attempts` never
# applies. Without this the loop is unbounded and *invisible*: no failed job, no dead
# letter, `/healthz` ok, and a decode call every twenty minutes forever on the box
# whose binding constraint is decode.
MAX_PROSE_ATTEMPTS = 3

# A thermal pause gets its own, far higher ceiling, for the reason the queue separates
# `attempts` from `reclaims`: the evidence differs. Three failed calls say the prompt or
# the budget is wrong; three pauses say the box was hot for an hour, which is a
# designed-for state on a passively-cooled Pi under sustained decode. Charging them to
# the same counter would spend the whole allowance on a single hot spell and leave the
# lead permanently angle-less -- and *silently*, because by then `prose_version` matches
# and `enqueue_stale_scores` is right to report nothing to do.
#
# 12 is four hours at `PROSE_RETRY_SECONDS`. Past that the governor is not having a
# spell, it is the steady state, and something bigger is wrong than one lead's angle.
MAX_PROSE_PAUSES = 12

# Carried in the follow-on payload. Not columns: this is per-attempt state belonging to
# the retry chain, and a lead re-scored for any other reason should start over.
PROSE_ATTEMPT_KEY = "_prose_attempt"
PROSE_PAUSE_KEY = "_prose_pauses"


def _is_recoverable(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in _RECOVERABLE)


def _is_prose_retry(job: Job) -> bool:
    """Whether this job is a follow-on asking for an angle a previous attempt missed.

    Read off the counters rather than a flag of its own: they exist only on the retry
    chain, and a lead re-scored for any other reason arrives without them.
    """
    return bool(job.payload.get(PROSE_ATTEMPT_KEY) or job.payload.get(PROSE_PAUSE_KEY))


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


# Triggers derived from a lookup we ran, rather than from something the company did.
#
# Their `observed_at` is *our* observation time, and the prose was printing it as the
# prospect's: "you published a mail-authentication policy with gaps today" reached the
# call list on companies whose DMARC record has read `p=none` for years. Not a false
# claim about scanning -- rule 4 still holds -- but it mistakes when we looked for when
# they acted, on the one line of prose that goes into somebody's inbox.
#
# Scoring is unaffected. `freshness` measures how current our knowledge is, and for a
# lookup the answer really is "today".
DERIVED_TRIGGERS = frozenset({"T7_SURFACE_SPRAWL", "T8_HYGIENE_GAP"})


def _age_phrase(observed: datetime) -> str:
    """ "11 days ago", not a date. The angle reads as a person describing something they
    saw, and a person does not write "observed 2026-08-17"."""
    days = max(0, (utcnow() - observed).days)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


# Any internal trigger code, in any prose field. `\b` on both sides so a company
# genuinely called "T5" in its own name is not caught.
_CODE_PATTERN = re.compile(r"\bT\d{1,2}_[A-Z][A-Z_]+\b")

# The offer slugs, for the same reason and from the same mistake. `Offer` is a
# `Literal` of four identifiers handed to the prose prompt with nothing that knows
# what they mean -- exactly the position `T1_AI_SHIP` was in before `means` existed.
#
# The model usually humanises them: eight of ten cards on one worklist read "an
# AI-LLM assessment". The ninth read "I'd like to run an ai_llm_assessment for you",
# which is why usually is not a guarantee and why the guard exists rather than a
# better prompt. Underscores only -- a card saying "watch" or "snapshot" in running
# English is fine, and matching those words would withhold half the corpus.
_SLUG_PATTERN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _leaked_codes(prose: LeadProse) -> list[str]:
    """Internal taxonomy codes that reached prospect-facing text.

    Checked on every prose field rather than just the angle: `rationale` is ours to
    read, but the Bengali angle is as prospect-facing as the English one, and a card
    is a single object that gets pasted whole.
    """
    text = " ".join(str(value) for value in (prose.outreach_angle, prose.bengali_angle) if value)
    return sorted(set(_CODE_PATTERN.findall(text)) | set(_SLUG_PATTERN.findall(text)))
