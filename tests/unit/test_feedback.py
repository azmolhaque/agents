"""Phase 8: reactions in, precision out.

Everything here runs without a Discord token, a guild, or a socket -- which is the
point of keeping `feedback/store.py` free of Discord types. `bot.py` is tested
separately and only for the translation layer.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from cindraleads.feedback import (
    REACTION_VERDICTS,
    VERDICTS,
    lead_for_message,
    precision_report,
    record_reaction,
    record_verdict,
    remove_reaction,
    unjudged_leads,
)
from cindraleads.models import to_iso, utcnow
from cindraleads.store import Store

PIPELINE = "test"


def _dispatch(
    store: Store,
    *,
    lead_id: str,
    message_id: str | None,
    tier: str = "B",
    score: int = 60,
    age_days: float = 0.0,
    domain: str = "example.com",
) -> None:
    """A dispatched lead, its company and its Discord message id.

    Written as raw SQL rather than through the Dispatcher because these tests are about
    the join, not about how the row got there -- and a card sent before
    `discord_message_id` existed is one of the cases under test, which the Dispatcher
    can no longer produce.
    """
    stamp = to_iso(utcnow() - timedelta(days=age_days))
    with store.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO companies (canonical_domain, display_name, "
            "first_seen_at, last_updated_at) VALUES (?,?,?,?)",
            (domain, domain.split(".")[0].title(), stamp, stamp),
        )
        conn.execute(
            "INSERT OR IGNORE INTO leads (lead_id, canonical_domain, score, tier, "
            "recommended_offer, first_seen_at, last_updated_at, pipeline_version) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (lead_id, domain, score, tier, "snapshot", stamp, stamp, PIPELINE),
        )
        conn.execute(
            "INSERT INTO dispatch_log (dispatch_id, lead_id, channel, tier, score, "
            "idempotency_key, discord_message_id, dispatched_at) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, lead_id, "warm", tier, score, uuid.uuid4().hex, message_id, stamp),
        )


def _verdicts(store: Store, lead_id: str) -> list[tuple[Any, ...]]:
    return [
        (row["verdict"], row["actor"], row["source"])
        for row in store.conn.execute(
            "SELECT verdict, actor, source FROM feedback WHERE lead_id = ? ORDER BY created_at",
            (lead_id,),
        ).fetchall()
    ]


# ------------------------------------------------------------------------ the join


def test_a_reaction_finds_its_lead_through_the_message_id(store: Store) -> None:
    _dispatch(store, lead_id="lead-1", message_id="msg-1")

    result = record_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")

    assert result.recorded
    assert result.lead_id == "lead-1"
    assert _verdicts(store, "lead-1") == [("good", "zahin", "discord")]


def test_a_card_sent_before_message_ids_were_captured_cannot_be_reacted_to(store: Store) -> None:
    """The failure mode this must not have is *guessing* which lead was meant.

    `discord_message_id` is nullable and older rows have it NULL. Matching on NULL, or
    falling back to "the most recent dispatch", would attach a verdict to a lead nobody
    judged and quietly poison the precision figure.
    """
    _dispatch(store, lead_id="lead-old", message_id=None)

    assert lead_for_message(store, "") is None
    result = record_reaction(store, message_id="msg-unknown", emoji="✅", actor="zahin")

    assert not result.recorded
    assert "no dispatched lead" in result.reason
    assert _verdicts(store, "lead-old") == []


def test_reacting_to_someone_elses_message_is_ignored_not_an_error(store: Store) -> None:
    """A gateway client sees every reaction in the channel, most of them not ours."""
    result = record_reaction(store, message_id="just-chat", emoji="👍", actor="zahin")
    assert not result.recorded


def test_an_unrecognised_emoji_is_ignored_without_touching_the_database(store: Store) -> None:
    _dispatch(store, lead_id="lead-1", message_id="msg-1")

    result = record_reaction(store, message_id="msg-1", emoji="🍕", actor="zahin")

    assert not result.recorded
    assert "not a verdict" in result.reason
    assert _verdicts(store, "lead-1") == []


# -------------------------------------------------------------- changing your mind


def test_changing_your_mind_replaces_the_verdict_rather_than_adding_one(store: Store) -> None:
    _dispatch(store, lead_id="lead-1", message_id="msg-1")

    record_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="msg-1", emoji="❌", actor="zahin")

    assert _verdicts(store, "lead-1") == [("bad", "zahin", "discord")]


def test_two_people_disagreeing_is_two_verdicts_not_a_correction(store: Store) -> None:
    """Supersede is scoped to the actor. Overwriting across people would make the last
    reaction win, which is a different system than the pessimistic one below."""
    _dispatch(store, lead_id="lead-1", message_id="msg-1")

    record_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="msg-1", emoji="❌", actor="someone-else")

    assert sorted(_verdicts(store, "lead-1")) == [
        ("bad", "someone-else", "discord"),
        ("good", "zahin", "discord"),
    ]


def test_an_outcome_does_not_overwrite_a_judgement(store: Store) -> None:
    """`contacted` answers "what happened next", `good` answers "was it worth sending".

    Superseding one with the other would lose the judgement the precision report is
    computed from, on the single action -- emailing them -- that most suggests it was
    a good lead.
    """
    _dispatch(store, lead_id="lead-1", message_id="msg-1")

    record_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="msg-1", emoji="📧", actor="zahin")

    assert sorted(_verdicts(store, "lead-1")) == [
        ("contacted", "zahin", "discord"),
        ("good", "zahin", "discord"),
    ]


def test_un_reacting_retracts_the_verdict(store: Store) -> None:
    _dispatch(store, lead_id="lead-1", message_id="msg-1")
    record_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")

    removed = remove_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")

    assert removed.recorded
    assert _verdicts(store, "lead-1") == []


def test_un_reacting_a_reaction_you_never_left_removes_nothing(store: Store) -> None:
    _dispatch(store, lead_id="lead-1", message_id="msg-1")
    record_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")

    remove_reaction(store, message_id="msg-1", emoji="✅", actor="someone-else")
    remove_reaction(store, message_id="msg-1", emoji="❌", actor="zahin")

    assert _verdicts(store, "lead-1") == [("good", "zahin", "discord")]


def test_un_reacting_does_not_retract_a_verdict_typed_at_the_cli(store: Store) -> None:
    """Discord's UI shows only its own reactions, so removing one is not an instruction
    about a verdict recorded by hand -- which the person may not even be able to see."""
    _dispatch(store, lead_id="lead-1", message_id="msg-1")
    record_verdict(store, lead_id="lead-1", verdict="good", source="cli", actor="zahin")

    remove_reaction(store, message_id="msg-1", emoji="✅", actor="zahin")

    assert _verdicts(store, "lead-1") == [("good", "zahin", "cli")]


def test_the_cli_supersedes_its_own_earlier_verdict(store: Store) -> None:
    """The defect that made `record_verdict` shared: `cindra feedback` inserted
    unconditionally, so correcting yourself left both rows and the pessimistic
    resolution below made the correction unreachable rather than authoritative."""
    _dispatch(store, lead_id="lead-1", message_id="msg-1")

    record_verdict(store, lead_id="lead-1", verdict="good", source="cli", actor="zahin")
    record_verdict(store, lead_id="lead-1", verdict="bad", source="cli", actor="zahin")

    assert _verdicts(store, "lead-1") == [("bad", "zahin", "cli")]


def test_an_unattributed_verdict_still_supersedes_itself(store: Store) -> None:
    """`actor` is nullable and `NULL = NULL` is false in SQL, so an unattributed row
    could never match its own supersede clause and would accumulate one row per
    correction. A CLI run with no `$USER` is exactly that case."""
    _dispatch(store, lead_id="lead-1", message_id="msg-1")

    record_verdict(store, lead_id="lead-1", verdict="good", source="cli", actor=None)
    record_verdict(store, lead_id="lead-1", verdict="bad", source="cli", actor=None)

    assert len(_verdicts(store, "lead-1")) == 1


def test_an_unknown_verdict_is_refused(store: Store) -> None:
    result = record_verdict(store, lead_id="lead-1", verdict="maybe", source="cli")
    assert not result.recorded
    assert _verdicts(store, "lead-1") == []


def test_every_reaction_maps_to_a_verdict_the_table_accepts(store: Store) -> None:
    """The emoji map and the verdict vocabulary are edited independently; a reaction
    mapped to a verdict `record_verdict` refuses would drop feedback silently."""
    assert set(REACTION_VERDICTS.values()) <= set(VERDICTS)


# --------------------------------------------------------------------- precision


def test_precision_counts_only_judgements(store: Store) -> None:
    """A prospect who said no was still a *correct* lead to surface. Folding outcomes
    into precision would measure conversion and print it under the wrong name."""
    _dispatch(store, lead_id="good-1", message_id="m1", domain="a.com")
    _dispatch(store, lead_id="bad-1", message_id="m2", domain="b.com")
    _dispatch(store, lead_id="outcome-only", message_id="m3", domain="c.com")

    record_reaction(store, message_id="m1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="m2", emoji="❌", actor="zahin")
    record_reaction(store, message_id="m3", emoji="🚫", actor="zahin")

    report = precision_report(store, days=7)

    assert report.judged == 2
    assert (report.good, report.bad) == (1, 1)
    assert report.precision == pytest.approx(0.5)


def test_disagreement_resolves_pessimistically(store: Store) -> None:
    """Over-reporting precision tunes the whole system toward the wrong target; chasing
    one bad lead costs an hour. The asymmetry decides the tie."""
    _dispatch(store, lead_id="lead-1", message_id="m1")

    record_reaction(store, message_id="m1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="m1", emoji="❌", actor="someone-else")

    report = precision_report(store, days=7)

    assert report.judged == 1
    assert (report.good, report.bad) == (0, 1)


def test_one_lead_judged_twice_by_one_person_is_one_judged_lead(store: Store) -> None:
    _dispatch(store, lead_id="lead-1", message_id="m1")
    record_reaction(store, message_id="m1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="m1", emoji="✅", actor="zahin")

    report = precision_report(store, days=7)

    assert report.judged == 1
    assert report.good == 1


def test_a_lead_dispatched_twice_is_one_judged_lead(store: Store) -> None:
    """Re-dispatch on a score move writes a second `dispatch_log` row. Counting rows
    rather than leads would let a lead that moved tiers weigh twice."""
    _dispatch(store, lead_id="lead-1", message_id="m1", score=60, tier="B", age_days=1)
    _dispatch(store, lead_id="lead-1", message_id="m2", score=72, tier="A", age_days=0)

    record_reaction(store, message_id="m2", emoji="✅", actor="zahin")

    report = precision_report(store, days=7)

    assert report.dispatched == 1
    assert report.judged == 1
    # The symptom the first version of this query had: coverage above 1.0.
    assert report.coverage <= 1.0
    assert report.by_tier == {"A": (1, 0)}


def test_precision_is_none_rather_than_zero_when_nothing_is_judged(store: Store) -> None:
    """Zero would read as "everything we sent was bad" on a week nobody reacted."""
    _dispatch(store, lead_id="lead-1", message_id="m1")

    report = precision_report(store, days=7)

    assert report.judged == 0
    assert report.precision is None
    assert report.dispatched == 1
    assert report.coverage == 0.0


def test_the_window_excludes_older_dispatches(store: Store) -> None:
    _dispatch(store, lead_id="recent", message_id="m1", domain="a.com", age_days=2)
    _dispatch(store, lead_id="ancient", message_id="m2", domain="b.com", age_days=40)
    record_reaction(store, message_id="m1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="m2", emoji="❌", actor="zahin")

    report = precision_report(store, days=7)

    assert report.dispatched == 1
    assert (report.judged, report.good, report.bad) == (1, 1, 0)


def test_by_tier_splits_the_same_counts(store: Store) -> None:
    _dispatch(store, lead_id="a-1", message_id="m1", tier="A", domain="a.com")
    _dispatch(store, lead_id="b-1", message_id="m2", tier="B", domain="b.com")
    _dispatch(store, lead_id="b-2", message_id="m3", tier="B", domain="c.com")
    record_reaction(store, message_id="m1", emoji="✅", actor="zahin")
    record_reaction(store, message_id="m2", emoji="✅", actor="zahin")
    record_reaction(store, message_id="m3", emoji="❌", actor="zahin")

    report = precision_report(store, days=7)

    assert report.by_tier == {"A": (1, 0), "B": (1, 1)}
    assert sum(sum(counts) for counts in report.by_tier.values()) == report.judged


def test_coverage_reports_how_much_of_the_week_was_judged(store: Store) -> None:
    for n in range(4):
        _dispatch(store, lead_id=f"lead-{n}", message_id=f"m{n}", domain=f"d{n}.com")
    record_reaction(store, message_id="m0", emoji="✅", actor="zahin")

    report = precision_report(store, days=7)

    assert report.coverage == pytest.approx(0.25)


# ------------------------------------------------------------------- unjudged work


def test_unjudged_lists_what_the_report_is_waiting_on_highest_score_first(
    store: Store,
) -> None:
    _dispatch(store, lead_id="judged", message_id="m1", score=80, domain="a.com")
    _dispatch(store, lead_id="low", message_id="m2", score=45, domain="b.com")
    _dispatch(store, lead_id="high", message_id="m3", score=70, domain="c.com")
    record_reaction(store, message_id="m1", emoji="✅", actor="zahin")

    pending = unjudged_leads(store, days=7, limit=10)

    assert [row["lead_id"] for row in pending] == ["high", "low"]


def test_an_outcome_only_reaction_leaves_a_lead_unjudged(store: Store) -> None:
    """Marking a lead `contacted` says nothing about whether it was worth surfacing, so
    it must stay on the list of leads still needing a verdict."""
    _dispatch(store, lead_id="lead-1", message_id="m1")
    record_reaction(store, message_id="m1", emoji="📧", actor="zahin")

    assert [row["lead_id"] for row in unjudged_leads(store, days=7)] == ["lead-1"]
