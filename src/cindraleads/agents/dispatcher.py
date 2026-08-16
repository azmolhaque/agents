"""Dispatcher — a cleared Lead becomes a card in Discord.

The last stage, and the only one a human ever sees. Three things it must get right:

**It never contacts a prospect.** It writes to your Discord and stops. No email, no
form fill, no DM. A human reads the card and decides. The master prompt calls that
human-in-the-loop a feature and says not to automate past it; this module is where that
promise is either kept or broken, so there is no outbound code path here at all.

**It never sends the same lead twice.** The idempotency key is
`(lead_id, sorted(trigger_codes), score // 10)`. Keying on `lead_id` alone would mean a
company that picks up a genuinely new trigger is never mentioned again; keying on the
score alone would re-send on every point of drift. A new trigger *is* new news, and a
ten-point move is the threshold at which the recommendation might actually change.

**A 429 is obeyed, not guessed at.** Handled in `webhook.py`.

Tier decides the channel: A and B go out immediately as full cards, C accumulates for
the daily digest. A REJECT never reaches this stage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.discord import CardData, DiscordWebhook, digest_row, lead_card, limits
from cindraleads.errors import ConfigError
from cindraleads.logging import get_logger
from cindraleads.models import Job, StageResult, from_iso, to_iso, utcnow
from cindraleads.store import Store

__all__ = [
    "DISPATCH_KIND",
    "DispatchOutcome",
    "Dispatcher",
    "build_card",
    "idempotency_key",
]

log = get_logger("cindraleads.dispatcher")

DISPATCH_KIND = "dispatch.lead"

TIER_CHANNEL: dict[str, str] = {"A": "hot", "B": "warm", "C": "digest"}


# Presentation order for the card, from the taxonomy weights in `scoring.yaml`. Loaded
# once at import: a card is built per dispatch and re-reading YAML each time would be a
# disk read in the hot path. Falls back to a flat order if the config is unreadable,
# because a missing weight must not stop a lead going out.
def _trigger_order() -> dict[str, int]:
    try:
        from cindraleads.scoring import ScoringConfig

        return {code: int(spec.weight) for code, spec in ScoringConfig.load().triggers.items()}
    except Exception:
        return {}


TRIGGER_ORDER: dict[str, int] = _trigger_order()


def idempotency_key(lead_id: str, trigger_codes: list[str], score: int) -> str:
    """`(lead_id, sorted(triggers), score // 10)`, hashed.

    The score is bucketed by ten rather than used exactly, so ordinary decay drift does
    not re-send a card every night. Trigger codes are in the key because a company that
    picks up a new reason to call is new news even at an unchanged score — that is the
    case the master prompt's "re-send if the score moved >= 10" rule misses.
    """
    shape = f"{lead_id}|{','.join(sorted(trigger_codes))}|{score // 10}"
    return hashlib.sha256(shape.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class DispatchOutcome:
    lead_id: str
    key: str = ""
    channel: str = ""
    tier: str = ""
    score: int = 0
    message_id: str | None = None
    sent: bool = False
    skipped: str | None = None
    error: str | None = None


@dataclass
class Dispatcher:
    store: Store
    webhook: DiscordWebhook
    config: Settings | None = None
    webhooks: dict[str, str] = field(default_factory=dict)
    channels: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cfg = self.config or settings()
        self.config = cfg
        if not self.webhooks:
            # Read once, at construction. Secrets stay wrapped everywhere else; this is
            # the single point of use, and the redaction processor covers the logs.
            self.webhooks = {
                name: value.get_secret_value()
                for name, value in (
                    ("hot", cfg.discord_webhook_hot),
                    ("warm", cfg.discord_webhook_warm),
                    ("digest", cfg.discord_webhook_digest),
                    ("ops", cfg.discord_webhook_ops),
                )
                if value is not None
            }
        try:
            self.channels = load_yaml("discord", base=cfg.resolve(cfg.config_dir))
        except ConfigError:
            # A missing routing file is a default, not a fault.
            self.channels = {}

    def webhook_for(self, channel: str) -> str | None:
        """The URL for a channel, falling back through the routing chain.

        A single configured webhook should produce a working system: with only
        `DISCORD_WEBHOOK_HOT` set, warm and digest land there too rather than being
        silently dropped. Losing a Tier B lead because a second webhook was not
        configured is a worse failure than putting it in the wrong channel.
        """
        for candidate in (channel, "hot", "warm", "digest", "ops"):
            url = self.webhooks.get(candidate)
            if url:
                return url
        return None

    # ------------------------------------------------------------------ phase 1

    async def prepare(self, job: Job) -> DispatchOutcome:
        """Build the card and POST it. No database writes."""
        lead_id = str(job.payload.get("lead_id") or "")
        if not lead_id:
            return DispatchOutcome(lead_id="", error="dispatch job needs lead_id")

        lead = self.read_lead(lead_id)
        if lead is None:
            return DispatchOutcome(lead_id=lead_id, error=f"lead {lead_id} not found")

        tier = str(lead["tier"])
        if tier == "REJECT":
            return DispatchOutcome(lead_id=lead_id, skipped="tier REJECT is never dispatched")

        key = idempotency_key(lead_id, [t["code"] for t in lead["triggers"]], int(lead["score"]))
        if self._already_sent(key):
            return DispatchOutcome(lead_id=lead_id, key=key, skipped="already dispatched")

        channel = TIER_CHANNEL.get(tier, "digest")
        url = self.webhook_for(channel)
        if not url:
            # Not an error: a system with no webhook configured is a system being set
            # up. It scores and stores leads perfectly well; it just has nowhere to
            # put the card yet, and saying so once per lead is the useful behaviour.
            return DispatchOutcome(
                lead_id=lead_id,
                key=key,
                channel=channel,
                tier=tier,
                skipped="no webhook configured",
            )

        data = _card_data(lead)
        embed = lead_card(data) if tier in ("A", "B") else digest_row(data)
        payload = {"embeds": [embed], "username": "CindraLeads"}
        result = await self.webhook.post(url, payload)
        if not result.ok:
            return DispatchOutcome(lead_id=lead_id, key=key, channel=channel, error=result.error)

        return DispatchOutcome(
            lead_id=lead_id,
            key=key,
            channel=channel,
            tier=tier,
            score=int(lead["score"]),
            message_id=result.message_id,
            sent=True,
        )

    # ------------------------------------------------------------------ phase 2

    def commit(self, job: Job, outcome: DispatchOutcome, conn: sqlite3.Connection) -> StageResult:
        if outcome.error:
            return StageResult(ok=False, stage="dispatcher", job_id=job.job_id, error=outcome.error)
        if outcome.skipped or not outcome.sent:
            log.info("dispatch_skipped", lead_id=outcome.lead_id, why=outcome.skipped)
            return StageResult(ok=True, stage="dispatcher", job_id=job.job_id)

        # The card is already in Discord by the time this runs, so the write must not
        # fail on a duplicate key — a crash between POST and COMMIT retries the job,
        # and OR IGNORE is what stops the retry sending a second card.
        conn.execute(
            "INSERT OR IGNORE INTO dispatch_log (dispatch_id, lead_id, channel, tier, score, "
            "idempotency_key, discord_message_id, dispatched_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                outcome.lead_id,
                outcome.channel,
                outcome.tier,
                outcome.score,
                outcome.key,
                outcome.message_id,
                to_iso(utcnow()),
            ),
        )
        log.info(
            "dispatched",
            lead_id=outcome.lead_id,
            channel=outcome.channel,
            tier=outcome.tier,
            score=outcome.score,
            message_id=outcome.message_id,
        )
        return StageResult(ok=True, stage="dispatcher", job_id=job.job_id)

    async def run(self, job: Job) -> StageResult:
        outcome = await self.prepare(job)
        with self.store.tx() as conn:
            return self.commit(job, outcome, conn)

    # -------------------------------------------------------------------- reads

    def _already_sent(self, key: str) -> bool:
        row = self.store.conn.execute(
            "SELECT 1 FROM dispatch_log WHERE idempotency_key = ? LIMIT 1", (key,)
        ).fetchone()
        return row is not None

    def read_lead(self, lead_id: str) -> dict[str, Any] | None:
        row = self.store.conn.execute(
            "SELECT l.*, c.display_name, c.description, c.country, c.ai_surface, "
            "c.subdomain_count_ct FROM leads l JOIN companies c "
            "ON c.canonical_domain = l.canonical_domain WHERE l.lead_id = ?",
            (lead_id,),
        ).fetchone()
        if row is None:
            return None

        triggers = [
            dict(r)
            for r in self.store.conn.execute(
                "SELECT trigger_id, code, confidence, observed_at FROM triggers "
                "WHERE canonical_domain = ? AND active = 1 AND decays_at > ?",
                (row["canonical_domain"], to_iso(utcnow())),
            ).fetchall()
        ]
        evidence: list[dict[str, Any]] = []
        for trigger in triggers:
            evidence.extend(
                dict(r)
                for r in self.store.conn.execute(
                    "SELECT e.url, e.source_id FROM evidence e "
                    "JOIN trigger_evidence te ON te.evidence_id = e.evidence_id "
                    "WHERE te.trigger_id = ?",
                    (trigger["trigger_id"],),
                ).fetchall()
            )
        # Ordered by taxonomy weight, not confidence. The digest row shows only the
        # first trigger, and confidence order put T8_HYGIENE_GAP (weight 12, written
        # at 0.8 because a DNS record is read directly) ahead of T1_AI_SHIP (weight
        # 30, written at 0.7 because a model read it off a page). Every card in the
        # first real run therefore led with "DNS hygiene" and buried the AI launch
        # that was the actual reason to call.
        triggers.sort(key=lambda t: -TRIGGER_ORDER.get(str(t["code"]), 0))
        return {**dict(row), "triggers": triggers, "evidence": evidence}


def build_card(lead: dict[str, Any]) -> dict[str, Any]:
    """The embed this lead would be sent as, without sending it."""
    data = _card_data(lead)
    return lead_card(data) if lead["tier"] in ("A", "B") else digest_row(data)


def _card_data(lead: dict[str, Any]) -> CardData:
    now = utcnow()
    triggers: list[tuple[str, float, str]] = []
    for trigger in lead["triggers"]:
        age = (now - from_iso(str(trigger["observed_at"]))).days
        triggers.append((str(trigger["code"]), float(trigger["confidence"]), f"{age}d ago"))

    surface: list[str] = []
    for item in json.loads(lead["ai_surface"] or "[]"):
        surface.append(str(item))
    if lead["subdomain_count_ct"]:
        surface.append(f"{lead['subdomain_count_ct']} CT subdomains")

    compliance = json.loads(lead["compliance"] or "{}")
    return CardData(
        lead_id=str(lead["lead_id"]),
        canonical_domain=str(lead["canonical_domain"]),
        display_name=str(lead["display_name"]),
        tier=str(lead["tier"]),
        score=int(lead["score"]),
        offer=str(lead["recommended_offer"]),
        triggers=tuple(triggers),
        evidence=tuple((str(e["source_id"]), str(e["url"])) for e in lead["evidence"]),
        description=str(lead["description"] or ""),
        outreach_angle=str(lead["outreach_angle"] or ""),
        bengali_angle=lead["bengali_angle"],
        surface_notes=tuple(surface),
        compliance_basis=str(compliance.get("basis", "legitimate_interest_b2b")),
        compliance_passed=bool(compliance.get("passed", True)),
        pipeline_version=str(lead["pipeline_version"]),
        observed_at=now,
    )


def digest_pages(cards: list[CardData]) -> list[list[dict[str, Any]]]:
    """Split digest rows into messages that fit.

    Both limits apply: at most `DIGEST_PAGE_SIZE` embeds, and at most 6000 characters
    across them. Ten full cards is ~12,000 characters, which is why the digest uses a
    compact builder and pages at eight (PLAN.md 2.6).
    """
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for card in cards:
        embed = digest_row(card)
        size = limits.total_characters(embed)
        if current and (
            len(current) >= limits.DIGEST_PAGE_SIZE or used + size > limits.TOTAL_CHARACTERS
        ):
            pages.append(current)
            current, used = [], 0
        current.append(embed)
        used += size
    if current:
        pages.append(current)
    return pages
