"""Resolver — collapse candidates onto one canonical company.

The stage that decides "is this the same company we already know?". Getting it wrong in
one direction produces four lead cards for one prospect; wrong in the other, two real
companies merge and one of them silently disappears. Neither is recoverable downstream,
which is why the ladder is explicit and every rung is logged.

**Merge, never overwrite.** A company row accumulates. A second sighting that knows the
country when the first did not should fill the country in; it must never blank a field
the first sighting established, because a sparse page is not evidence of absence. So the
merge is field-wise "keep what we have unless the new value is non-empty".

**No LLM.** Entity resolution here is a domain comparison and a string distance. The
master prompt lists Resolver among the agents; on this hardware, asking a 4B "are these
the same company?" costs ~30 s to answer a question `rapidfuzz` settles in microseconds
and more consistently. This stage is given no model handle at all — structurally, not by
convention (PLAN.md 2.9).

**Purely local, no network.** `prepare` exists only to satisfy the stage protocol; all
the work is in `commit`, inside the caller's transaction.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, get_args

from cindraleads.dedupe import canonical_domain, same_company
from cindraleads.logging import get_logger
from cindraleads.models import Job, StageResult, TriggerCode, to_iso, utcnow
from cindraleads.store import Store

__all__ = ["ENRICH_KIND", "RESOLVE_KIND", "TRIGGER_DECAY_DAYS", "Resolver"]

log = get_logger("cindraleads.resolver")

RESOLVE_KIND = "resolve.company"
ENRICH_KIND = "enrich.company"

# How long a trigger stays "news" (master prompt §2). A funding round is interesting for
# a quarter; a shipped AI feature for two months; an inbound enquiry for a month.
TRIGGER_DECAY_DAYS: dict[str, int] = {
    "T0_INBOUND": 30,
    "T1_AI_SHIP": 60,
    "T2_FUNDING": 90,
    "T3_HIRING_SEC": 45,
    "T4_HIRING_AI_ONLY": 45,
    "T5_COMPLIANCE": 90,
    "T6_INCIDENT": 45,
    "T7_SURFACE_SPRAWL": 60,
    "T8_HYGIENE_GAP": 60,
    "T9_MARKETPLACE": 14,
    "T10_VENDOR_PRESSURE": 60,
    "T11_STACK_RISK": 120,
    "T12_LOCAL": 180,
}
DEFAULT_DECAY_DAYS = 60


@dataclass
class Resolver:
    store: Store

    async def prepare(self, job: Job) -> None:
        """Nothing to fetch. Resolution reads only what is already in the database."""
        return None

    def commit(self, job: Job, outcome: None, conn: sqlite3.Connection) -> StageResult:
        candidate_id = str(job.payload.get("candidate_id") or "")
        if not candidate_id:
            return StageResult(
                ok=False,
                stage="resolver",
                job_id=job.job_id,
                error="resolve job needs candidate_id",
            )

        row = conn.execute(
            "SELECT raw_payload, status FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return StageResult(
                ok=False,
                stage="resolver",
                job_id=job.job_id,
                error=f"candidate {candidate_id} not found",
            )

        try:
            payload = json.loads(row["raw_payload"])
            extraction = payload["extraction"]
        except (ValueError, KeyError, TypeError) as exc:
            return StageResult(
                ok=False,
                stage="resolver",
                job_id=job.job_id,
                error=f"candidate {candidate_id} has no usable extraction: {exc}",
            )

        display_name = str(extraction.get("display_name") or "").strip()
        if not display_name:
            return self._drop(conn, candidate_id, job, "extraction has no display_name")

        # The model's `canonical_domain` is a claim; the URL we actually fetched is a
        # fact. Prefer the claim only when it canonicalizes cleanly, since a page can
        # legitimately state a different corporate domain than the one it is served on.
        domain = canonical_domain(
            str(extraction.get("canonical_domain") or "")
        ) or canonical_domain(str(payload.get("url") or ""))
        if not domain:
            # A candidate whose only URL is a platform host (a GitHub repo, an HN
            # thread) has no company identity yet. Enrichment may find one later; for
            # now it is not a company and must not become one called "github.com".
            return self._drop(conn, candidate_id, job, "no canonical domain could be derived")

        country = (str(extraction.get("country") or "").strip().upper() or None) or None
        known = [
            (str(r["canonical_domain"]), str(r["display_name"]), r["country"])
            for r in conn.execute(
                "SELECT canonical_domain, display_name, country FROM companies"
            ).fetchall()
        ]
        match = same_company(domain=domain, name=display_name, country=country, known=known)
        resolved = match.canonical_domain if match else domain

        created = self._upsert_company(
            conn,
            resolved,
            extraction,
            display_name,
            country,
            template_id=str(payload.get("template_id") or ""),
        )
        trigger_ids = self._write_triggers(
            conn,
            resolved,
            codes=[
                c for c in payload.get("trigger_codes") or [] if c in set(get_args(TriggerCode))
            ],
            evidence_ids=[str(e) for e in payload.get("evidence_ids") or []],
            url=str(payload.get("url") or ""),
        )

        conn.execute(
            "UPDATE candidates SET resolved_domain = ?, status = ? WHERE candidate_id = ?",
            (resolved, "resolved", candidate_id),
        )
        log.info(
            "resolve_complete",
            candidate_id=candidate_id,
            canonical_domain=resolved,
            company_created=created,
            merged_by_rung=match.rung if match else None,
            merge_reason=match.reason if match else None,
            triggers=len(trigger_ids),
        )
        # Only a company with a live trigger is worth pursuing. Fit without news is
        # not a lead, and enriching it would spend real fetches to conclude that.
        #
        # Enrichment comes before scoring, not after: reachability and surface are
        # 25% of CindraScore, so a company scored first would be scored against
        # two components that are structurally zero. The Enricher enqueues the
        # score itself when it is done.
        follow_on = [(ENRICH_KIND, {"canonical_domain": resolved})] if trigger_ids else []
        return StageResult(ok=True, stage="resolver", job_id=job.job_id, follow_on=follow_on)

    async def run(self, job: Job) -> StageResult:
        await self.prepare(job)
        with self.store.tx() as conn:
            return self.commit(job, None, conn)

    # ------------------------------------------------------------------ writes

    def _drop(self, conn: sqlite3.Connection, candidate_id: str, job: Job, why: str) -> StageResult:
        """Not a failure — a candidate that cannot become a company.

        Marked and left alone rather than retried: the next attempt would reach the
        same conclusion from the same data, so failing the job would just burn three
        attempts on its way to the dead-letter table.
        """
        conn.execute(
            "UPDATE candidates SET status = ? WHERE candidate_id = ?",
            ("unresolvable", candidate_id),
        )
        log.info("resolve_dropped", candidate_id=candidate_id, why=why)
        return StageResult(ok=True, stage="resolver", job_id=job.job_id)

    def _upsert_company(
        self,
        conn: sqlite3.Connection,
        domain: str,
        extraction: dict[str, Any],
        display_name: str,
        country: str | None,
        template_id: str = "",
    ) -> bool:
        """Insert or merge. Returns True if the company is new."""
        now = to_iso(utcnow())
        existing = conn.execute(
            "SELECT canonical_domain, display_name, country, hq_city, employee_band, industry, "
            "description, tech_signals, ai_surface FROM companies WHERE canonical_domain = ?",
            (domain,),
        ).fetchone()

        tech = [str(t) for t in extraction.get("tech_signals") or []]
        surface = [str(s) for s in extraction.get("ai_surface") or []]

        if existing is None:
            conn.execute(
                # `discovered_by` is written here and nowhere else. Credit for
                # finding a company belongs to the template that found it first; the
                # merge branch below deliberately leaves it alone, so a company seen
                # again by a second template does not reassign its own discovery.
                "INSERT INTO companies (canonical_domain, display_name, country, employee_band, "
                "industry, description, tech_signals, ai_surface, discovered_by, "
                "first_seen_at, last_updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    domain,
                    display_name,
                    country,
                    extraction.get("employee_band"),
                    extraction.get("industry"),
                    extraction.get("description"),
                    json.dumps(sorted(set(tech))),
                    json.dumps(sorted(set(surface))),
                    template_id or None,
                    now,
                    now,
                ),
            )
            return True

        # Merge. `or existing[...]` in this direction is the whole rule: a new sighting
        # fills gaps and never erases. A page that omits the country is silent about it,
        # not asserting the company has none.
        conn.execute(
            "UPDATE companies SET display_name = ?, country = ?, employee_band = ?, industry = ?, "
            "description = ?, tech_signals = ?, ai_surface = ?, last_updated_at = ? "
            "WHERE canonical_domain = ?",
            (
                existing["display_name"] or display_name,
                country or existing["country"],
                extraction.get("employee_band") or existing["employee_band"],
                extraction.get("industry") or existing["industry"],
                extraction.get("description") or existing["description"],
                json.dumps(sorted(set(tech) | set(_json_list(existing["tech_signals"])))),
                json.dumps(sorted(set(surface) | set(_json_list(existing["ai_surface"])))),
                now,
                domain,
            ),
        )
        return False

    def _write_triggers(
        self,
        conn: sqlite3.Connection,
        domain: str,
        *,
        codes: list[str],
        evidence_ids: list[str],
        url: str,
    ) -> list[str]:
        """One trigger row per code, each joined to at least one evidence row.

        Enforced here rather than trusted: a code with no evidence is dropped on the
        floor. `trigger_evidence` has no NOT NULL that could catch this — a trigger with
        zero joined rows is schema-valid and semantically worthless.
        """
        if not evidence_ids:
            if codes:
                log.info("triggers_dropped_no_evidence", canonical_domain=domain, codes=codes)
            return []

        now = utcnow()
        written: list[str] = []
        for code in dict.fromkeys(codes):  # de-dup, order preserved
            decay_days = TRIGGER_DECAY_DAYS.get(code, DEFAULT_DECAY_DAYS)
            # Re-observing a live trigger refreshes it instead of stacking a duplicate;
            # otherwise an hourly harvest would grow one row per hour per company.
            fresh = conn.execute(
                "SELECT trigger_id FROM triggers WHERE canonical_domain = ? AND code = ? "
                "AND active = 1 AND decays_at > ? LIMIT 1",
                (domain, code, to_iso(now)),
            ).fetchone()
            if fresh is not None:
                trigger_id = str(fresh["trigger_id"])
                conn.execute(
                    "UPDATE triggers SET observed_at = ?, decays_at = ? WHERE trigger_id = ?",
                    (to_iso(now), to_iso(now + timedelta(days=decay_days)), trigger_id),
                )
            else:
                trigger_id = uuid.uuid4().hex[:16]
                conn.execute(
                    "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
                    "observed_at, decays_at, rationale, active) VALUES (?,?,?,?,?,?,?,1)",
                    (
                        trigger_id,
                        domain,
                        code,
                        0.7,
                        to_iso(now),
                        to_iso(now + timedelta(days=decay_days)),
                        f"observed at {url}"[:280],
                    ),
                )
            for evidence_id in evidence_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO trigger_evidence (trigger_id, evidence_id) VALUES (?,?)",
                    (trigger_id, evidence_id),
                )
            written.append(trigger_id)
        return written


def _json_list(raw: Any) -> list[str]:
    try:
        loaded = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [str(x) for x in loaded] if isinstance(loaded, list) else []
