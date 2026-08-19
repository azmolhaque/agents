"""Reactions in, feedback rows out. No Discord types anywhere in this module.

The gateway client hands this an emoji, a message id and who reacted; everything that
decides what happens next is here, so the rules can be tested without a token or a
guild. `bot.py` is then thin enough to be obviously correct by inspection, which
matters because it is the one part that cannot be tested end to end offline.

**The join is the whole mechanism.** A reaction carries a message id and nothing else
-- Discord has never heard of a lead. `dispatch_log.discord_message_id` is the only
bridge back, which is why the Dispatcher POSTs with `?wait=true` and stores the id at
send time. A card sent before that column was populated can never be reacted to
usefully, and this module says so rather than guessing at a lead.

**Changing your mind is normal and must be cheap.** Marking a lead `good`, thinking
again and marking it `bad` should leave one verdict, not two -- a precision figure
computed over both would count one lead twice and disagree with itself. The latest
reaction from a given person wins.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from cindraleads.logging import get_logger
from cindraleads.models import to_iso, utcnow
from cindraleads.store import Store

__all__ = [
    "REACTION_VERDICTS",
    "VERDICTS",
    "FeedbackResult",
    "PrecisionReport",
    "lead_for_message",
    "precision_report",
    "record_reaction",
    "record_verdict",
    "remove_reaction",
    "unjudged_leads",
]

log = get_logger("cindraleads.feedback")

# Which reactions mean something, and what. Deliberately small: every emoji that is not
# here is ignored silently, because a channel where people react with anything at all
# would otherwise fill the table with noise that the precision figure then averages.
#
# `contacted` and `not_interested` are outcomes rather than judgements of the lead, and
# the precision calculation excludes them for that reason -- a lead you contacted and
# who said no was still a *correct* lead to surface.
REACTION_VERDICTS: dict[str, str] = {
    "✅": "good",
    "👍": "good",
    "❌": "bad",
    "👎": "bad",
    "📧": "contacted",
    "🚫": "not_interested",
}

# Verdicts that answer "was this lead worth surfacing?". The others answer "what
# happened next?", which is a different question and must not move precision.
_JUDGEMENTS = ("good", "bad")

# Everything the `feedback` table accepts, from any surface. The CLI validates against
# this rather than its own copy, so a verdict the bot can write is one `cindra
# feedback` can write and `precision_report` has heard of.
VERDICTS: tuple[str, ...] = ("good", "bad", "contacted", "not_interested")


@dataclass(frozen=True)
class FeedbackResult:
    recorded: bool
    lead_id: str = ""
    verdict: str = ""
    reason: str = ""


def lead_for_message(store: Store, message_id: str) -> str | None:
    """The lead a Discord message is about, or None.

    None has two causes worth telling apart in the log: a message that is not one of
    ours at all (someone reacting to chat), and one of our cards sent before
    `discord_message_id` was being captured. Neither is an error; both mean the
    reaction cannot be attributed and must be dropped rather than guessed at.
    """
    if not message_id:
        return None
    row = store.conn.execute(
        "SELECT lead_id FROM dispatch_log WHERE discord_message_id = ? "
        "ORDER BY dispatched_at DESC LIMIT 1",
        (str(message_id),),
    ).fetchone()
    return str(row["lead_id"]) if row else None


def record_reaction(
    store: Store,
    *,
    message_id: str,
    emoji: str,
    actor: str,
    note: str = "",
) -> FeedbackResult:
    """Translate one reaction into a feedback row, or explain why not.

    Returns rather than raises for every ordinary outcome. A gateway client processes a
    stream of events it does not control, and an unrecognised emoji is not an error --
    it is Tuesday.
    """
    verdict = REACTION_VERDICTS.get(emoji)
    if verdict is None:
        return FeedbackResult(False, reason=f"emoji {emoji!r} is not a verdict")

    lead_id = lead_for_message(store, message_id)
    if lead_id is None:
        log.debug("feedback_unmatched_message", message_id=message_id, emoji=emoji)
        return FeedbackResult(False, reason="no dispatched lead for that message")

    return record_verdict(
        store, lead_id=lead_id, verdict=verdict, source="discord", actor=actor, note=note
    )


def record_verdict(
    store: Store,
    *,
    lead_id: str,
    verdict: str,
    source: str,
    actor: str | None = None,
    note: str = "",
) -> FeedbackResult:
    """Write one verdict, superseding the same person's previous answer to the same question.

    Shared by the gateway bot and `cindra feedback` so the two surfaces cannot drift.
    They did drift once: the CLI inserted unconditionally, so marking a lead `good` and
    then `bad` by hand left both rows and the pessimistic resolution in
    `precision_report` silently made the earlier `good` unreachable rather than replaced.

    Supersede scope is the *question*, not everything the actor ever said. `contacted`
    does not overwrite `good` -- one is a judgement of the lead and the other an
    outcome, and they are meant to coexist on the same row set.
    """
    if verdict not in VERDICTS:
        return FeedbackResult(False, lead_id=lead_id, reason=f"unknown verdict {verdict!r}")

    # NULL never equals NULL in SQL, so an unattributed row could never be superseded by
    # its own author and would accumulate one row per correction.
    who = actor or "unknown"
    superseded = _JUDGEMENTS if verdict in _JUDGEMENTS else (verdict,)
    placeholders = ",".join("?" * len(superseded))

    with store.tx() as conn:
        conn.execute(
            f"DELETE FROM feedback WHERE lead_id = ? AND actor = ? AND verdict IN ({placeholders})",
            (lead_id, who, *superseded),
        )
        conn.execute(
            "INSERT INTO feedback (feedback_id, lead_id, verdict, source, actor, note, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, lead_id, verdict, source, who, note, to_iso(utcnow())),
        )

    log.info("feedback_recorded", lead_id=lead_id, verdict=verdict, actor=who, source=source)
    return FeedbackResult(True, lead_id=lead_id, verdict=verdict)


def remove_reaction(store: Store, *, message_id: str, emoji: str, actor: str) -> FeedbackResult:
    """Un-reacting retracts the verdict.

    Without this a mis-click is permanent: Discord's own affordance for undo is to
    remove the reaction, and a feedback table that ignores that diverges from what the
    channel visibly says.
    """
    verdict = REACTION_VERDICTS.get(emoji)
    if verdict is None:
        return FeedbackResult(False, reason=f"emoji {emoji!r} is not a verdict")

    lead_id = lead_for_message(store, message_id)
    if lead_id is None:
        return FeedbackResult(False, reason="no dispatched lead for that message")

    with store.tx() as conn:
        # Scoped to `source = 'discord'`: un-reacting retracts what the reaction wrote,
        # never a verdict the same person typed at the CLI. Discord's UI shows only its
        # own reactions, so removing one cannot be an instruction about anything else.
        removed = conn.execute(
            "DELETE FROM feedback WHERE lead_id = ? AND actor = ? AND verdict = ? "
            "AND source = 'discord'",
            (lead_id, actor or "unknown", verdict),
        ).rowcount

    if removed:
        log.info("feedback_retracted", lead_id=lead_id, verdict=verdict, actor=actor)
    return FeedbackResult(bool(removed), lead_id=lead_id, verdict=verdict)


# ------------------------------------------------------------------------ precision


@dataclass(frozen=True)
class PrecisionReport:
    """Precision over judged leads only, plus what is missing.

    `judged` is reported alongside `precision` and not buried: 1.0 over two leads is
    not evidence of anything, and a report that showed only the ratio would read as
    though it were.
    """

    since: datetime
    dispatched: int = 0
    judged: int = 0
    good: int = 0
    bad: int = 0
    by_tier: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def precision(self) -> float | None:
        return (self.good / self.judged) if self.judged else None

    @property
    def coverage(self) -> float:
        return (self.judged / self.dispatched) if self.dispatched else 0.0


def precision_report(store: Store, *, days: float = 7.0) -> PrecisionReport:
    """How many dispatched leads were worth sending, over the last `days`.

    Only `good` and `bad` count. `contacted` and `not_interested` describe what
    happened after the lead was surfaced, and a prospect saying no does not make
    surfacing them a mistake -- folding those in would measure conversion and call it
    precision.

    Counted per *lead*, not per feedback row. One person reacting twice, or two people
    agreeing, is one judged lead either way.
    """
    since = utcnow() - timedelta(days=days)
    stamp = to_iso(since)

    dispatched = int(
        store.conn.execute(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM dispatch_log WHERE dispatched_at >= ?",
            (stamp,),
        ).fetchone()["n"]
    )

    rows = store.conn.execute(
        # `latest` collapses a lead to one row before the feedback join. A lead that was
        # re-dispatched after a score move has two `dispatch_log` rows, and if the tier
        # moved with it, grouping by (lead, tier) counted it twice -- `judged` could then
        # exceed `dispatched` and coverage exceed 1.0. It reports under the tier it was
        # last sent at, which is the card the person actually reacted to.
        #
        # The bare `tier` beside `MAX(dispatched_at)` is SQLite's documented
        # min/max bare-column rule: it comes from the row that supplied the maximum.
        "WITH latest AS ("
        "  SELECT lead_id, tier, MAX(dispatched_at) AS last_at FROM dispatch_log "
        "  WHERE dispatched_at >= ? GROUP BY lead_id) "
        "SELECT d.lead_id AS lead_id, d.tier AS tier, "
        # A lead is `bad` if anyone called it bad. Disagreement resolves pessimistically
        # on purpose: the cost of chasing a bad lead is an hour, and the cost of
        # over-reporting precision is tuning the whole system toward the wrong target.
        "MAX(CASE WHEN f.verdict = 'bad' THEN 1 ELSE 0 END) AS any_bad, "
        "MAX(CASE WHEN f.verdict = 'good' THEN 1 ELSE 0 END) AS any_good "
        "FROM latest d JOIN feedback f ON f.lead_id = d.lead_id "
        "WHERE f.verdict IN ('good','bad') "
        "GROUP BY d.lead_id",
        (stamp,),
    ).fetchall()

    good = bad = 0
    by_tier: dict[str, list[int]] = {}
    for row in rows:
        verdict_good = not int(row["any_bad"]) and bool(int(row["any_good"]))
        good += int(verdict_good)
        bad += int(not verdict_good)
        bucket = by_tier.setdefault(str(row["tier"]), [0, 0])
        bucket[0 if verdict_good else 1] += 1

    return PrecisionReport(
        since=since,
        dispatched=dispatched,
        judged=len(rows),
        good=good,
        bad=bad,
        by_tier={tier: (counts[0], counts[1]) for tier, counts in sorted(by_tier.items())},
    )


def unjudged_leads(store: Store, *, days: float = 7.0, limit: int = 20) -> list[dict[str, Any]]:
    """Dispatched leads nobody has reacted to. The work the report is waiting on."""
    stamp = to_iso(utcnow() - timedelta(days=days))
    return [
        dict(row)
        for row in store.conn.execute(
            "SELECT d.lead_id, d.tier, d.score, c.display_name, c.canonical_domain "
            "FROM dispatch_log d "
            "JOIN leads l ON l.lead_id = d.lead_id "
            "JOIN companies c ON c.canonical_domain = l.canonical_domain "
            "WHERE d.dispatched_at >= ? AND NOT EXISTS ("
            "  SELECT 1 FROM feedback f WHERE f.lead_id = d.lead_id "
            "  AND f.verdict IN ('good','bad')) "
            "GROUP BY d.lead_id ORDER BY d.score DESC LIMIT ?",
            (stamp, limit),
        ).fetchall()
    ]
