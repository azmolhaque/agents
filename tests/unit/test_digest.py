"""The daily Tier C batch, and the tier split it depends on.

Two behaviours are load-bearing here and neither is obvious from the code:

* The per-lead stage **refuses** anything below Tier B. If that refusal ever silently
  became a drop rather than a deferral, Tier C leads would vanish with no error.
* `send_digest` **reconciles** against `dispatch_log` rather than draining a queue, so
  a missed morning costs a day's delay and not a day's leads.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from cindraleads.agents.dispatcher import (
    IMMEDIATE_TIERS,
    Dispatcher,
    idempotency_key,
    send_digest,
)
from cindraleads.models import Job, to_iso, utcnow


class _Webhook:
    """Records every POST, and can be told to fail from a given call onward."""

    def __init__(self, fail_from: int | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail_from = fail_from

    async def post(self, url: str, payload: dict[str, Any]) -> Any:
        self.posts.append(payload)
        ok = self.fail_from is None or len(self.posts) < self.fail_from
        return type(
            "Result",
            (),
            {
                "ok": ok,
                "message_id": f"m{len(self.posts)}" if ok else None,
                "error": None if ok else "429 rate limited",
            },
        )()


def _lead(store: Any, domain: str, *, tier: str, score: int) -> str:
    lead_id = uuid.uuid4().hex[:16]
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, first_seen_at, "
            "last_updated_at) VALUES (?,?,?,?)",
            (domain, domain.split(".")[0].title(), now, now),
        )
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at, active) VALUES (?,?,?,?,?,?,1)",
            (f"t-{lead_id}", domain, "T1_AI_SHIP", 0.7, now, "2099-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO leads (lead_id, canonical_domain, score, tier, recommended_offer, "
            "first_seen_at, last_updated_at, pipeline_version) "
            "VALUES (?,?,?,?,'snapshot_free',?,?,'test')",
            (lead_id, domain, score, tier, now, now),
        )
    return lead_id


def _dispatcher(store: Any, webhook: _Webhook) -> Dispatcher:
    return Dispatcher(store=store, webhook=webhook, webhooks={"digest": "https://x.test/hook"})


# ------------------------------------------------------------------- the tier split


async def test_tier_c_is_deferred_by_the_per_lead_stage(store: Any) -> None:
    """Deferred, not dropped. The outcome has to say which, because a silent drop and
    a deferral look the same from the queue: one completed job either way."""
    lead_id = _lead(store, "acme.io", tier="C", score=45)
    webhook = _Webhook()
    dispatcher = _dispatcher(store, webhook)

    outcome = await dispatcher.prepare(
        Job(job_id="j1", kind="dispatch.lead", payload={"lead_id": lead_id})
    )

    assert outcome.sent is False
    assert outcome.skipped is not None and "digest" in outcome.skipped
    assert webhook.posts == [], "a Tier C lead must not post immediately"


@pytest.mark.parametrize("tier", IMMEDIATE_TIERS)
async def test_a_and_b_still_go_out_immediately(store: Any, tier: str) -> None:
    """Whatever the digest does, a funding round must not wait until morning."""
    lead_id = _lead(store, f"{tier.lower()}corp.io", tier=tier, score=80)
    webhook = _Webhook()

    outcome = await _dispatcher(store, webhook).prepare(
        Job(job_id="j1", kind="dispatch.lead", payload={"lead_id": lead_id})
    )

    assert outcome.sent is True
    assert len(webhook.posts) == 1


async def test_reject_is_never_dispatched_by_either_path(store: Any) -> None:
    reject = _lead(store, "nope.io", tier="REJECT", score=10)
    webhook = _Webhook()
    dispatcher = _dispatcher(store, webhook)

    outcome = await dispatcher.prepare(
        Job(job_id="j1", kind="dispatch.lead", payload={"lead_id": reject})
    )
    assert outcome.skipped is not None and "REJECT" in outcome.skipped

    report = await send_digest(dispatcher)
    assert report.pending == 0
    assert webhook.posts == []


# ------------------------------------------------------------------- the batch path


async def test_the_digest_batches_into_one_message(store: Any) -> None:
    """The point of the whole exercise: five Tier C leads are one message, not five."""
    for n in range(5):
        _lead(store, f"c{n}.io", tier="C", score=45 - n)
    webhook = _Webhook()

    report = await send_digest(_dispatcher(store, webhook))

    assert report.sent == 5
    assert report.pages == 1
    assert len(webhook.posts) == 1
    assert len(webhook.posts[0]["embeds"]) == 5


async def test_a_second_run_sends_nothing(store: Any) -> None:
    _lead(store, "c1.io", tier="C", score=45)
    webhook = _Webhook()
    dispatcher = _dispatcher(store, webhook)

    first = await send_digest(dispatcher)
    second = await send_digest(dispatcher)

    assert (first.sent, second.sent) == (1, 0)
    assert second.skipped == "nothing new below Tier B"
    assert len(webhook.posts) == 1


async def test_a_missed_day_is_delay_not_loss(store: Any) -> None:
    """It reconciles rather than draining a queue, so leads that piled up while the
    timer was not firing all go out on the next run."""
    for n in range(3):
        _lead(store, f"day1-{n}.io", tier="C", score=45)
    webhook = _Webhook()
    dispatcher = _dispatcher(store, webhook)

    # No run today. Three more arrive.
    for n in range(3):
        _lead(store, f"day2-{n}.io", tier="C", score=44)

    report = await send_digest(dispatcher)
    assert report.sent == 6


async def test_no_webhook_is_a_skip_not_a_loss(store: Any) -> None:
    """A system being set up scores and stores leads perfectly well. Nothing is
    logged as dispatched, so they all go out once a webhook exists."""
    _lead(store, "c1.io", tier="C", score=45)
    dispatcher = Dispatcher(store=store, webhook=_Webhook(), webhooks={})

    report = await send_digest(dispatcher)

    assert report.skipped == "no webhook configured"
    assert report.sent == 0
    assert store.conn.execute("SELECT COUNT(*) FROM dispatch_log").fetchone()[0] == 0


async def test_a_failed_page_stops_and_keeps_the_rest_pending(store: Any) -> None:
    """A 429 mid-digest must not be answered by hammering the next page.

    Pages already sent are logged and never repeat; the rest stay pending and go out
    on the next run.
    """
    for n in range(20):
        _lead(store, f"c{n}.io", tier="C", score=50 - n)
    webhook = _Webhook(fail_from=2)
    dispatcher = _dispatcher(store, webhook)

    report = await send_digest(dispatcher)

    assert report.error and "429" in report.error
    assert report.pages == 1, "it stopped rather than pressing on through the failure"
    assert len(webhook.posts) == 2, "one success, one failure, then stop"

    logged = store.conn.execute("SELECT COUNT(*) FROM dispatch_log").fetchone()[0]
    assert logged == report.sent < 20

    # The remainder is still pending, so the next run picks it up.
    follow_up = await send_digest(_dispatcher(store, _Webhook()))
    assert follow_up.sent == 20 - report.sent


async def test_the_limit_caps_one_run_without_dropping_the_rest(store: Any) -> None:
    for n in range(10):
        _lead(store, f"c{n}.io", tier="C", score=50 - n)
    dispatcher = _dispatcher(store, _Webhook())

    first = await send_digest(dispatcher, limit=4)
    assert (first.pending, first.sent) == (10, 4)

    second = await send_digest(dispatcher, limit=4)
    assert (second.pending, second.sent) == (6, 4)


async def test_highest_scoring_leads_go_first(store: Any) -> None:
    """Paging means the tail of a long backlog may not fit. What gets cut has to be
    the weakest lead, not whichever row the database happened to return first."""
    for score in (30, 50, 40):
        _lead(store, f"s{score}.io", tier="C", score=score)
    webhook = _Webhook()

    await send_digest(_dispatcher(store, webhook), limit=2)

    sent = {
        row[0] for row in store.conn.execute("SELECT score FROM dispatch_log ORDER BY score DESC")
    }
    assert sent == {50, 40}


async def test_dry_run_posts_nothing(store: Any) -> None:
    for n in range(3):
        _lead(store, f"c{n}.io", tier="C", score=45)
    webhook = _Webhook()

    report = await send_digest(_dispatcher(store, webhook), dry_run=True)

    assert report.pending == 3
    assert report.pages == 1
    assert webhook.posts == []
    assert store.conn.execute("SELECT COUNT(*) FROM dispatch_log").fetchone()[0] == 0


async def test_a_rescored_lead_digests_again(store: Any) -> None:
    """The idempotency key carries the score bucket and the trigger set, so a lead
    that genuinely changed is new news -- the same rule the per-lead path uses."""
    lead_id = _lead(store, "c1.io", tier="C", score=41)
    dispatcher = _dispatcher(store, _Webhook())
    assert (await send_digest(dispatcher)).sent == 1

    with store.tx() as conn:
        conn.execute("UPDATE leads SET score = 59 WHERE lead_id = ?", (lead_id,))

    assert (await send_digest(dispatcher)).sent == 1


def test_the_key_ignores_drift_within_a_bucket() -> None:
    assert idempotency_key("l1", ["T1_AI_SHIP"], 41) == idempotency_key("l1", ["T1_AI_SHIP"], 49)
    assert idempotency_key("l1", ["T1_AI_SHIP"], 41) != idempotency_key("l1", ["T1_AI_SHIP"], 51)
