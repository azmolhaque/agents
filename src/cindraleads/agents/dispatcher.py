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
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, get_args

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.dedupe import display_name_or_domain
from cindraleads.discord import (
    CardData,
    DiscordWebhook,
    TriggerLine,
    digest_row,
    digest_summary,
    lead_card,
    limits,
)
from cindraleads.errors import ConfigError
from cindraleads.logging import get_logger
from cindraleads.metrics import snapshot
from cindraleads.models import Job, Offer, StageResult, from_iso, to_iso, utcnow
from cindraleads.scoring import ScoringConfig
from cindraleads.store import Store

__all__ = [
    "DISPATCH_KIND",
    "IMMEDIATE_TIERS",
    "DigestReport",
    "DispatchOutcome",
    "Dispatcher",
    "build_card",
    "idempotency_key",
    "send_digest",
]

log = get_logger("cindraleads.dispatcher")

DISPATCH_KIND = "dispatch.lead"

TIER_CHANNEL: dict[str, str] = {"A": "hot", "B": "warm", "C": "digest"}

# Tiers worth interrupting someone for. A and B go out the moment they score, because
# a funding round or a shipped LLM feature is worth a call this week. C is a backlog to
# read over coffee, and `send_digest` batches it once a day -- posting a Tier C card the
# instant it scores turns the digest channel into a stream nobody reads, which costs the
# A and B cards their signal value too.
IMMEDIATE_TIERS = ("A", "B")


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
        blocked = self._blocked(lead)
        if blocked:
            return DispatchOutcome(lead_id=lead_id, tier=tier, skipped=blocked)
        if tier not in IMMEDIATE_TIERS:
            # Not dropped -- deferred. `send_digest` picks it up on the daily timer by
            # querying for tiers nothing has logged yet, so no state is needed here.
            return DispatchOutcome(lead_id=lead_id, tier=tier, skipped=f"tier {tier} digests daily")

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

    def _blocked(self, lead: dict[str, Any]) -> str:
        """Why this lead may not be dispatched, or "" if it may.

        **`arxiv.org` rendered `⚖️ Compliance: VETO` on a Tier A card at score 74 and
        nothing refused to send it.** `compliance_passed` was read, printed, and never
        acted on: the gate quarantines a vetoed lead while `_upsert_lead` still stores
        the tier the arithmetic computed, so the row keeps Tier A and every dispatch
        predicate here is about tier. The card said the right thing in a field nothing
        read.

        Three questions, and they are genuinely different, which is why all three are
        asked. The stored verdict answers "was this allowed when we scored it"; the
        quarantine and suppression tables answer "may I write to them *now*" and change
        without moving a single lead row -- `suppressed_domains` is deliberately outside
        `calibration_version` for exactly that reason, so a suppressed company keeps its
        tier forever and no rescore will ever come.

        `worklist` already joins both tables live and says why in its own comment. The
        Dispatcher is the other reader of the same question and it joined neither --
        which is how a suppression added after a lead was scored stopped the operator's
        call list and not the Discord card.
        """
        compliance = json.loads(lead["compliance"] or "{}")
        if not bool(compliance.get("passed", True)):
            return "compliance veto"
        domain = str(lead["canonical_domain"])
        row = self.store.conn.execute(
            "SELECT "
            "  (SELECT 1 FROM suppression_list "
            "     WHERE kind = 'domain' AND value = ?) AS suppressed, "
            "  (SELECT 1 FROM quarantine "
            "     WHERE subject_kind = 'lead' AND subject_id = ?) AS quarantined",
            (domain, str(lead["lead_id"])),
        ).fetchone()
        if row and row["suppressed"]:
            return "domain suppressed"
        if row and row["quarantined"]:
            return "lead quarantined"
        return ""

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


def _any_offer_is_free(config: Any = None, offer: str = "") -> bool:
    """Whether *this lead's* offer costs the prospect nothing.

    Keyed on the lead's own `recommended_offer`, not on "is anything free anywhere".
    The broader question stopped discriminating the moment the founding-cohort Snapshot
    was recorded correctly: with one free offer in the config, a blanket allowance lets
    an angle promise a free $2k-8k assessment again -- which is the original defect,
    reintroduced by its own fix.

    Read from config rather than assumed in either direction. A guard hardcoded to
    "nothing is free" would be one more claim about money written into code, which is
    the shape of the defect it exists to catch -- and it was wrong within a day.
    """
    try:
        cfg = config or ScoringConfig.load()
        if offer:
            return cfg.offer_is_free(offer)
        return any(cfg.offer_is_free(slug) for slug in get_args(Offer))
    except (ConfigError, OSError):  # a card must still render if the config is broken
        return False


# A price the prospect can read: `$2,000-8,000`, `$150`, `BDT 5,000-12,000`, `৳7,000`.
# Loose on formatting on purpose -- the question is only whether a number with a
# currency on it survived into the angle, not whether the model reproduced the
# punctuation. An exact-string match would withhold a correct card for writing
# "$2,000-$8,000".
_PRICE = re.compile(r"(?:\$|BDT\s*|\u09f3\s*)\s*\d[\d,]*", re.IGNORECASE)


def _free_claim_is_backed(text: str, offer: str, country: str | None, config: Any = None) -> bool:
    """Whether a "free" in this angle is the one the config put there.

    **The guard was withholding the text the config was rewritten to produce**, and it
    silenced 275 of 280 paid-offer leads -- 98%, which is the shape of a constant rather
    than a discriminator, the same tell as `single_source` at 96%.

    Two commits did this to each other and each was right alone. `offers` deliberately
    names the free Snapshot inside a *paid* phrase, so the ask stays small without
    giving the engagement away -- `ai_llm_assessment` reads "a free first attack-surface
    Snapshot, and an AI/LLM security assessment after it ... ($2,000-8,000, 2-5 days)".
    And the dispatch guard was deliberately narrowed to key on the lead's own offer.
    Together: the config puts "free" in the text and the guard withholds the text for
    containing it.

    Allowing every "free" on a paid offer would give the original defect back -- an
    angle reading "I'd like to run an AI/LLM assessment for you, free" is exactly what
    this exists to stop. **The price is the discriminator.** A paid phrase always names
    one, the honest angle carries it, and the dangerous angle is the one that claims
    free and drops it. Sampled against the real corpus, both leads reproduce the price
    verbatim.

    Fails closed on a config that promises nothing free: a "free" there is invented, and
    an invented one is the case with no excuse.
    """
    try:
        cfg = config or ScoringConfig.load()
        if cfg.offer_is_free(offer):
            return True
        phrase = cfg.offer_phrase(offer, country)
    except (ConfigError, OSError, KeyError):
        # A card must still render if the config is broken, but nothing here may be
        # read as permission: an unverifiable claim about money is withheld.
        return False
    if not _FREE_CLAIM.search(phrase):
        return False
    return bool(_PRICE.search(text))


def _trigger_means() -> dict[str, str]:
    """`code -> means` from `scoring.yaml`, or empty when the config cannot be read.

    Empty rather than raising, the same call `_any_offer_is_free` makes: a card must
    still render if the config is broken, and a trigger line degrades to the bare code
    it printed before. `means` is validated non-empty at load, so a missing phrase here
    means the file did not open, not that a code was forgotten.
    """
    try:
        return {code: rule.means for code, rule in ScoringConfig.load().triggers.items()}
    except (ConfigError, OSError):
        return {}


def _card_data(lead: dict[str, Any]) -> CardData:
    # Local, matching `worklist._top_trigger`: the scorer imports `TRIGGER_ORDER` from
    # this module, so a module-level import back into it is a cycle.
    from cindraleads.agents.scorer import DERIVED_TRIGGERS

    now = utcnow()
    offer_slug = str(lead["recommended_offer"] or "")
    country = str(lead["country"] or "") or None
    means = _trigger_means()
    triggers: list[TriggerLine] = []
    for trigger in lead["triggers"]:
        code = str(trigger["code"])
        age = (now - from_iso(str(trigger["observed_at"]))).days
        # A derived trigger's `observed_at` is when *we* looked, and the card was
        # printing it as when they acted -- `T8_HYGIENE_GAP · 0d ago` on a domain whose
        # DMARC has read `p=none` for years. The prose learned this and the card did
        # not; the provenance phrase ("public record") is the honest answer instead.
        when = "" if code in DERIVED_TRIGGERS else f"{age}d ago"
        triggers.append(
            TriggerLine(
                code=code,
                confidence=float(trigger["confidence"]),
                when=when,
                means=means.get(code, ""),
            )
        )

    surface: list[str] = []
    for item in json.loads(lead["ai_surface"] or "[]"):
        surface.append(str(item))
    if lead["subdomain_count_ct"]:
        surface.append(f"{lead['subdomain_count_ct']} CT subdomains")

    compliance = json.loads(lead["compliance"] or "{}")
    return CardData(
        lead_id=str(lead["lead_id"]),
        canonical_domain=str(lead["canonical_domain"]),
        display_name=display_name_or_domain(lead["display_name"], str(lead["canonical_domain"])),
        tier=str(lead["tier"]),
        score=int(lead["score"]),
        offer=str(lead["recommended_offer"]),
        triggers=tuple(triggers),
        evidence=tuple((str(e["source_id"]), str(e["url"])) for e in lead["evidence"]),
        description=str(lead["description"] or ""),
        # Withheld here, not just at generation. The Scorer refuses to *store* an angle
        # naming our internal taxonomy, but leads scored before that guard existed
        # already have one, and nothing will re-queue them -- their calibration is
        # current, so the reconciler sees nothing stale. This is the last point before
        # Discord and the only one that catches prose written under an older rule.
        outreach_angle=_publishable(
            str(lead["outreach_angle"] or ""),
            lead["lead_id"],
            allow_free=_free_claim_is_backed(
                str(lead["outreach_angle"] or ""), offer_slug, country
            ),
        ),
        bengali_angle=_bengali(
            lead["bengali_angle"],
            lead["lead_id"],
            allow_free=_free_claim_is_backed(str(lead["bengali_angle"] or ""), offer_slug, country),
        ),
        surface_notes=tuple(surface),
        compliance_basis=str(compliance.get("basis", "legitimate_interest_b2b")),
        compliance_passed=bool(compliance.get("passed", True)),
        pipeline_version=str(lead["pipeline_version"]),
        observed_at=now,
    )


@dataclass
class DigestReport:
    """What one digest run did. `pending` is what it found before any limit applied."""

    pending: int = 0
    sent: int = 0
    pages: int = 0
    skipped: str = ""
    error: str = ""


async def send_digest(
    dispatcher: Dispatcher, *, limit: int = 40, dry_run: bool = False
) -> DigestReport:
    """Post the day's Tier C backlog as paginated digest rows.

    The per-lead stage refuses anything below Tier B, so this is the only path a Tier C
    lead has to Discord. It reads the backlog rather than a queue: "leads at a digest
    tier whose current idempotency key is not in `dispatch_log`" is a reconciliation,
    which means a missed timer, a crash, or a webhook that was not configured yesterday
    all heal on the next run instead of losing a day of leads.

    Each page is POSTed and then logged, in that order, for the same reason the
    per-lead path does it: the write must not be able to claim a card was sent when the
    POST failed. A crash between the two re-sends one page, and `INSERT OR IGNORE` on
    the idempotency key keeps that from double-logging.
    """
    report = DigestReport()
    rows = dispatcher.store.conn.execute(
        "SELECT lead_id FROM leads WHERE tier NOT IN ('REJECT','A','B') "
        "ORDER BY score DESC, last_updated_at DESC"
    ).fetchall()

    cards: list[tuple[str, str, int, CardData]] = []
    for row in rows:
        lead = dispatcher.read_lead(str(row["lead_id"]))
        if lead is None:
            continue
        # The same question the per-lead stage asks, because this is the other route to
        # Discord and a veto that only one of them honours is not a veto. Tier C is the
        # *larger* population, so a gate that covered only A and B would leave most of
        # the corpus unguarded -- the `digest_pages` shape again, seen from the gate
        # side rather than the renderer's.
        blocked = dispatcher._blocked(lead)
        if blocked:
            log.info("digest_skipped", lead_id=str(lead["lead_id"]), why=blocked)
            continue
        key = idempotency_key(
            str(lead["lead_id"]), [t["code"] for t in lead["triggers"]], int(lead["score"])
        )
        if dispatcher._already_sent(key):
            continue
        cards.append((str(lead["lead_id"]), key, int(lead["score"]), _card_data(lead)))

    report.pending = len(cards)
    if not cards:
        report.skipped = "nothing new below Tier B"
        return report

    cards = cards[:limit]
    url = dispatcher.webhook_for("digest")
    if not url:
        report.skipped = "no webhook configured"
        return report
    if dry_run:
        report.pages = len(digest_pages([card for _, _, _, card in cards]))
        return report

    # Read once, before the first POST, so every page of one digest describes the same
    # moment. Recomputing per page would have the summary disagree with itself when a
    # worker commits between two posts -- and these rows are logged *after* each page
    # is sent, so a late read would count some of this digest and not the rest.
    # The line therefore describes the corpus the digest was assembled from, this
    # run's own rows excluded.
    summary = digest_summary(snapshot(dispatcher.store))

    index = 0
    pages = digest_pages([card for _, _, _, card in cards])
    for number, page in enumerate(pages, 1):
        batch = cards[index : index + len(page)]
        index += len(page)
        payload: dict[str, Any] = {"embeds": page, "username": "CindraLeads digest"}
        # Under the last page, and as `content` rather than an embed of its own, so it
        # costs no extra message and cannot be mistaken for a lead. A thin digest is the
        # moment someone asks why, and "queue 0 ready · cloud $0.50/24h" answers it
        # where the question is asked.
        if number == len(pages):
            payload["content"] = summary
        result = await dispatcher.webhook.post(url, payload)
        if not result.ok:
            # Stop, do not continue to the next page. The pages already sent are
            # logged and will not repeat; the rest stay pending and go out tomorrow,
            # or on the next run. Pressing on through a 429 would make it worse.
            report.error = result.error or "digest post failed"
            log.warning("digest_page_failed", page=report.pages + 1, error=report.error)
            break

        with dispatcher.store.tx() as conn:
            for lead_id, key, score, _ in batch:
                conn.execute(
                    "INSERT OR IGNORE INTO dispatch_log (dispatch_id, lead_id, channel, tier, "
                    "score, idempotency_key, discord_message_id, dispatched_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        lead_id,
                        "digest",
                        "C",
                        score,
                        key,
                        result.message_id,
                        to_iso(utcnow()),
                    ),
                )
        report.pages += 1
        report.sent += len(batch)

    log.info("digest_sent", sent=report.sent, pages=report.pages, pending=report.pending)
    return report


# Any internal trigger code. Kept in step with the Scorer's copy deliberately rather
# than shared: this one guards what leaves the building, and it must keep working even
# if the generation-side rule is loosened.
_INTERNAL_CODE = re.compile(r"\bT\d{1,2}_[A-Z][A-Z_]+\b|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# Every way an angle can tell a prospect they pay nothing.
#
# Withheld here for the same reason the codes are, and the case is stronger: an angle
# naming our taxonomy is embarrassing, an angle promising a $250-$600 product at no
# charge is a commitment in writing on a card made to be pasted into an email.
#
# 325 sendable leads carry an angle written while `snapshot_free: {free: true}` was in
# the config, and **nothing will ever re-queue them** -- their calibration is current,
# no trigger has moved, and `enqueue_stale_scores` only re-proses a lead whose angle is
# *missing*. A lead with a wrong angle is invisible to every reconciler in the system.
# So the repair has to happen at the last point before Discord, exactly as it did for
# the trigger codes.
#
# "free" is matched as a whole word, so "freedom" and "freelance" pass. That is the
# entire vocabulary a model reaches for when it has been told something costs nothing.
#
# **It was English-only, and the Bengali angle made the same promise unguarded.** Five
# of five cards previewed on 2026-09-21 logged `card_prose_withheld_free_claim` against
# the English and then shipped `আমি একটি মুক্ত পরীক্ষা প্রস্তাব করছি` -- an
# `ai_llm_assessment`, a $2k-8k engagement, offered at no charge in a language the
# guard could not read. The guard withheld the compliant text and passed the other.
#
# The Bengali terms deliberately carry **no `\b`**, because `\b` is meaningless here:
# Bengali vowel signs are category Mc/Mn and so are not word characters, which makes
# `\bবিনামূল্যে\b` fail to match the word itself (it ends in `ে`) while `\bমুক্ত\b`
# happily matches inside `মুক্তিযুদ্ধ`. Word boundaries give the false negative *and*
# the false positive. A substring gives only the false positive, and the asymmetry
# says take it: a wrong match costs a card its angle, a miss puts a price commitment
# in a prospect's inbox.
_FREE_CLAIM = re.compile(
    r"\bfree\b|\bno charge\b|\bat no cost\b|\bfree of charge\b|\bcomplimentary\b"
    r"|\bgratis\b|\bon us\b|\bzero cost\b"
    r"|বিনামূল্যে|মুক্ত|ফ্রি|নিখরচায়|বিনা খরচে|বিনা মূল্যে",
    re.IGNORECASE,
)


#: Why an angle must not reach a prospect. Phrased for a human, because the call list
#: prints them.
LEAKS_INTERNAL_CODE = "names our internal taxonomy"
UNBACKED_FREE_CLAIM = "promises free without naming the price"


def angle_withheld_reason(text: str, *, allow_free: bool) -> str:
    """Why this prose must not be sent, or `""` when it may be.

    **One predicate, two readers, and the second one was missing.** `_publishable`
    guarded the Discord card and `send_digest` was taught the same lesson about
    `_blocked` -- but `cindra worklist` prints `outreach_angle` raw, and the call list
    is the route that actually reaches a prospect: a card is something you read, a
    worklist row is something you copy. Measured on the Pi on 2026-09-22, two of the
    top eight rows served an angle offering a $2k-8k engagement with "free" attached
    and no price -- byte-identical to the text the Dispatcher had just refused.

    Same shape as `digest_pages` and `_blocked`, from the third side: the guard was
    applied where it was written, then extended to the other Discord route, and the
    one path with a human and a clipboard on the end of it was never asked.
    """
    if not text:
        return ""
    if _INTERNAL_CODE.search(text):
        return LEAKS_INTERNAL_CODE
    if not allow_free and _FREE_CLAIM.search(text):
        return UNBACKED_FREE_CLAIM
    return ""


def _publishable(text: str | None, lead_id: Any = "", *, allow_free: bool = False) -> Any:
    """Prose, or nothing, if it names something only we should see or promises a price
    we do not offer.

    An empty angle on a card is a small loss -- the triggers, evidence and score are
    all still there and a human can write the sentence themselves. An angle reading
    "You published T1_AI_SHIP on your public page" is worse than empty: the card is
    made to be pasted into an email, and that one cannot be. An angle offering a paid
    engagement for nothing is worse again, because a human may well send it.

    `allow_free` is driven by the running config rather than assumed, so if Cindrasec
    ever does publish a free tier the guard stops applying the day `company.yaml`
    records a zero price -- and not a day before.
    """
    if not text:
        return text
    # `allow_free` is "the config says a free claim belongs in this angle" rather than
    # "this lead's offer is free" -- a *paid* phrase deliberately names the free first
    # Snapshot, and keying on the offer alone withheld 98% of paid leads.
    reason = angle_withheld_reason(str(text), allow_free=allow_free)
    if reason == LEAKS_INTERNAL_CODE:
        log.warning(
            "card_prose_withheld",
            lead_id=str(lead_id),
            codes=sorted(set(_INTERNAL_CODE.findall(str(text)))),
        )
        return ""
    if reason:
        log.warning(
            "card_prose_withheld_free_claim",
            lead_id=str(lead_id),
            matched=sorted({m.lower() for m in _FREE_CLAIM.findall(str(text))}),
        )
        return ""
    return text


def _bengali(text: str | None, lead_id: Any = "", *, allow_free: bool = False) -> Any:
    """The Bengali angle, which by default does not reach a card at all.

    Everything `_publishable` withholds, plus the whole field unless
    `DISPATCH_BENGALI_ANGLE` is set. `Settings` carries the reasoning; the short of it
    is that a 4B writing marketing Bengali reads as foreign to a native speaker, which
    is the opposite of what a Bengali card is for.

    **The guards could not have covered it anyway, and that is a separate finding.**
    `_FREE_CLAIM` was English-only, so the same free promise two commits were spent
    getting exactly right in English sailed through in Bengali -- and on the five cards
    previewed it was the *only* angle that survived, because the English one was
    correctly withheld by the guard the Bengali is invisible to. The guard withheld
    the compliant text and shipped the non-compliant text.

    The order matters: the flag is checked first, so turning it on is a deliberate act
    and the content guards still apply after it.
    """
    if not text:
        return text
    if not settings().dispatch_bengali_angle:
        log.info("card_bengali_withheld", lead_id=str(lead_id), why="not reviewed by a human")
        return None
    return _publishable(text, lead_id, allow_free=allow_free)


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
