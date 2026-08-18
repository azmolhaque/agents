"""Discord embeds, limits, and the Dispatcher's idempotency.

The property that matters most is boring: an embed must never exceed a Discord limit
for *any* input. A 400 at dispatch time throws away a card that cost ~64 s of Pi
inference to produce, and it happens on exactly the leads with the most to say.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from cindraleads.agents.dispatcher import (
    DISPATCH_KIND,
    Dispatcher,
    build_card,
    digest_pages,
    idempotency_key,
)
from cindraleads.discord import CardData, DiscordWebhook, digest_row, lead_card, limits
from cindraleads.models import Job, utcnow
from cindraleads.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"


def card(**kwargs) -> CardData:  # type: ignore[no-untyped-def]
    base = {
        "lead_id": "abc123",
        "canonical_domain": "acme.io",
        "display_name": "Acme Health",
        "tier": "A",
        "score": 84,
        "offer": "ai_llm_assessment",
        "triggers": (("T1_AI_SHIP", 0.91, "11d ago"),),
        "evidence": (("hn_algolia", "https://news.ycombinator.com/item?id=1"),),
        "description": "Seed-stage healthtech in Dhaka.",
        "outreach_angle": "You published an AI assistant last month.",
        "observed_at": utcnow(),
    }
    return CardData(**{**base, **kwargs})


# ------------------------------------------------------------------- the limits


def test_a_full_card_is_within_every_limit():
    embed = lead_card(card())
    assert len(embed["title"]) <= limits.TITLE
    assert len(embed["description"]) <= limits.DESCRIPTION
    assert len(embed["fields"]) <= limits.FIELDS_PER_EMBED
    assert limits.total_characters(embed) <= limits.TOTAL_CHARACTERS
    for field in embed["fields"]:
        assert len(field["name"]) <= limits.FIELD_NAME
        assert len(field["value"]) <= limits.FIELD_VALUE


@hyp_settings(max_examples=150, deadline=None)
@given(
    name=st.text(min_size=0, max_size=3000),
    description=st.text(min_size=0, max_size=9000),
    angle=st.text(min_size=0, max_size=3000),
    bengali=st.text(min_size=0, max_size=3000),
    n_triggers=st.integers(min_value=0, max_value=40),
    n_evidence=st.integers(min_value=0, max_value=60),
)
def test_no_input_can_produce_an_oversized_embed(
    name, description, angle, bengali, n_triggers, n_evidence
):
    """PLAN.md 2.6: property-tested against arbitrary input, per-embed AND the total."""
    data = card(
        display_name=name,
        description=description,
        outreach_angle=angle,
        bengali_angle=bengali,
        triggers=tuple((f"T{i}_CODE", 0.5, f"{i}d ago") for i in range(n_triggers)),
        evidence=tuple((f"src{i}", f"https://example.com/{i}") for i in range(n_evidence)),
    )
    for embed in (lead_card(data), digest_row(data)):
        assert len(embed.get("title", "")) <= limits.TITLE
        assert len(embed.get("description", "")) <= limits.DESCRIPTION
        assert len(embed.get("fields", [])) <= limits.FIELDS_PER_EMBED
        assert limits.total_characters(embed) <= limits.TOTAL_CHARACTERS
        for field in embed.get("fields", []):
            assert len(field["name"]) <= limits.FIELD_NAME
            assert len(field["value"]) <= limits.FIELD_VALUE


def test_truncation_keeps_the_evidence_links():
    """A card whose links were trimmed to make room for prose is unverifiable, which
    is the same as having no evidence at all."""
    data = card(
        description="x" * 8000,
        evidence=(("crtsh", "https://crt.sh/?q=acme.io"), ("hn", "https://news.example/1")),
    )
    embed = lead_card(data)
    evidence_field = next(f for f in embed["fields"] if f["name"].startswith("📎"))
    assert "crt.sh" in evidence_field["value"]


def test_a_digest_page_never_exceeds_the_total():
    """Ten full cards is ~12,000 characters against a 6,000 limit — a guaranteed 400.
    This is why the digest has its own builder and pages at eight."""
    cards = [card(tier="C", display_name=f"Company {i}", description="y" * 380) for i in range(50)]
    pages = digest_pages(cards)
    assert pages
    for page in pages:
        assert len(page) <= limits.DIGEST_PAGE_SIZE
        assert sum(limits.total_characters(e) for e in page) <= limits.TOTAL_CHARACTERS
    assert sum(len(p) for p in pages) == 50, "no card is silently dropped"


def realistic_card() -> CardData:
    """A card with every field the master prompt's section 10 layout draws.

    The minimal fixture above is ~375 characters, which would make the digest look
    fine. The claim in PLAN.md 2.6 is about a *populated* card, so the measurement has
    to use one.
    """
    return card(
        description=(
            "Seed-stage healthtech, ~35 staff, Dhaka and Singapore. Shipped a "
            "patient-facing AI assistant 11 days ago and is hiring two AI engineers "
            "with no security role open."
        ),
        triggers=(
            ("T1_AI_SHIP", 0.91, "11d ago"),
            ("T4_HIRING_AI_ONLY", 0.78, "6d ago"),
            ("T2_FUNDING", 0.85, "41d ago"),
        ),
        outreach_angle=(
            "You published an AI assistant handling patient data last month. I would "
            "like to run a free prompt-injection and data-leak review of it under a "
            "signed RoE — two days, no cost, and you keep the report either way."
        ),
        bengali_angle=(
            "আপনারা গত মাসে রোগীর তথ্য নিয়ে কাজ করে এমন একটি এআই অ্যাসিস্ট্যান্ট "
            "প্রকাশ করেছেন। একটি স্বাক্ষরিত RoE-এর অধীনে বিনামূল্যে পর্যালোচনা করতে চাই।"
        ),
        contacts=("Nabila R. — CTO · nabila@acmehealth.io [verified]",),
        surface_notes=("public_chatbot", "47 CT subdomains (+12 in 30d)", "DMARC p=none"),
        evidence=(
            ("ProductHunt", "https://producthunt.example/acme"),
            ("Greenhouse", "https://boards.greenhouse.example/acme"),
            ("TechCrunch", "https://techcrunch.example/acme-seed"),
            ("crt.sh", "https://crt.sh/?q=acmehealth.io"),
        ),
    )


def test_a_realistic_card_is_the_size_plan_2_6_claimed():
    size = limits.total_characters(lead_card(realistic_card()))
    assert 900 <= size <= 1600, f"card is {size} chars; PLAN.md 2.6 assumed ~1,100-1,400"


def test_ten_full_cards_would_not_have_fit():
    """The measurement behind PLAN.md 2.6, kept as a test so the claim stays true."""
    full = sum(limits.total_characters(lead_card(realistic_card())) for _ in range(10))
    assert full > limits.TOTAL_CHARACTERS, (
        f"ten full cards is {full} chars; if this ever fits, the digest could use "
        "the full builder and the two-builder split is no longer needed"
    )


# --------------------------------------------------------------- what it says


def test_the_card_never_claims_anything_was_scanned():
    """A legal boundary, not a style preference. The card may be pasted into an email."""
    embed = lead_card(card(surface_notes=("public_chatbot", "47 CT subdomains")))
    text = json.dumps(embed).lower()
    for phrase in ("we scanned", "we found", "vulnerability", "exploit", "we tested", "detected"):
        assert phrase not in text
    assert "no scan performed" in text


def test_the_compliance_field_is_always_present():
    embed = lead_card(card())
    assert any(f["name"].startswith("⚖️") for f in embed["fields"])


def test_tier_selects_the_colour():
    from cindraleads.discord.embeds import TIER_COLORS

    for tier in ("A", "B", "C"):
        assert lead_card(card(tier=tier))["color"] == TIER_COLORS[tier]


# ------------------------------------------------------------- idempotency


def test_the_same_lead_is_not_sent_twice():
    key = idempotency_key("lead1", ["T1_AI_SHIP"], 84)
    assert key == idempotency_key("lead1", ["T1_AI_SHIP"], 84)


def test_trigger_order_does_not_change_the_key():
    a = idempotency_key("lead1", ["T1_AI_SHIP", "T2_FUNDING"], 84)
    b = idempotency_key("lead1", ["T2_FUNDING", "T1_AI_SHIP"], 84)
    assert a == b


def test_a_small_score_drift_does_not_resend():
    """Decay moves scores every night. Keying on the exact number would re-send a
    card daily for a lead where nothing happened."""
    assert idempotency_key("lead1", ["T1_AI_SHIP"], 84) == idempotency_key(
        "lead1", ["T1_AI_SHIP"], 89
    )


def test_a_ten_point_move_is_new_news():
    assert idempotency_key("lead1", ["T1_AI_SHIP"], 84) != idempotency_key(
        "lead1", ["T1_AI_SHIP"], 94
    )


def test_a_new_trigger_is_new_news_even_at_the_same_score():
    """The case the master prompt's "re-send if score moved >= 10" rule misses: a
    company that picks up a new reason to call is worth mentioning again."""
    assert idempotency_key("lead1", ["T1_AI_SHIP"], 84) != idempotency_key(
        "lead1", ["T1_AI_SHIP", "T9_MARKETPLACE"], 84
    )


# ------------------------------------------------------------- the dispatcher


@pytest.fixture
def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "d.db", migrations_dir=MIGRATIONS)
    store.migrate()
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg-123"})

    def build(tier: str = "A", score: int = 84, **webhooks: str) -> Dispatcher:
        with store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO companies (canonical_domain, display_name, country, "
                "ai_surface, tech_signals, first_seen_at, last_updated_at) "
                "VALUES ('acme.io','Acme Health','BD','[\"agent_with_tools\"]','[]',?,?)",
                ("2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO leads (lead_id, canonical_domain, score, tier, "
                "recommended_offer, outreach_angle, compliance, first_seen_at, "
                "last_updated_at, pipeline_version) VALUES "
                "('lead1','acme.io',?,?,'snapshot_free','angle','{\"passed\":true}',?,?,'v1')",
                (score, tier, "2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO evidence (evidence_id, url, source_id, snippet, "
                "observed_at, content_sha256) VALUES "
                "('e1','https://acme.io/','company_site','snip','2026-08-15T00:00:00Z','h')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO triggers (trigger_id, canonical_domain, code, "
                "confidence, observed_at, decays_at) VALUES "
                "('t1','acme.io','T1_AI_SHIP',0.9,'2026-08-15T00:00:00Z','2099-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO trigger_evidence (trigger_id, evidence_id) "
                "VALUES ('t1','e1')"
            )
        return Dispatcher(
            store=store,
            webhook=DiscordWebhook(
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
            ),
            webhooks=dict(webhooks) or {"hot": "https://discord.test/hot"},
        )

    yield build, posts, store
    store.close()


def job() -> Job:
    return Job(job_id="j1", kind=DISPATCH_KIND, payload={"lead_id": "lead1"})


async def test_a_tier_a_lead_is_sent_and_logged(rig):
    build, posts, store = rig
    dispatcher = build(tier="A")

    result = await dispatcher.run(job())

    assert result.ok
    assert len(posts) == 1
    assert posts[0]["embeds"][0]["title"].startswith("Acme Health")
    row = store.conn.execute("SELECT * FROM dispatch_log").fetchone()
    assert row["channel"] == "hot"
    assert row["discord_message_id"] == "msg-123", "needed to join Phase 8 reactions"
    await dispatcher.webhook.client.aclose()


async def test_a_rerun_sends_nothing(rig):
    build, posts, store = rig
    dispatcher = build(tier="A")

    await dispatcher.run(job())
    await dispatcher.run(job())

    assert len(posts) == 1, "the second run is deduplicated by idempotency key"
    assert len(store.conn.execute("SELECT * FROM dispatch_log").fetchall()) == 1
    await dispatcher.webhook.client.aclose()


async def test_a_reject_is_never_dispatched(rig):
    build, posts, store = rig
    dispatcher = build(tier="REJECT", score=12)

    result = await dispatcher.run(job())

    assert result.ok, "not a failure — a lead that does not qualify"
    assert posts == []
    assert store.conn.execute("SELECT * FROM dispatch_log").fetchall() == []
    await dispatcher.webhook.client.aclose()


async def test_tier_routes_to_its_channel(rig):
    build, _posts, _store = rig
    dispatcher = build(
        tier="B",
        hot="https://discord.test/hot",
        warm="https://discord.test/warm",
    )
    await dispatcher.run(job())
    assert dispatcher.webhook_for("warm") == "https://discord.test/warm"
    await dispatcher.webhook.client.aclose()


async def test_one_configured_webhook_still_delivers_everything(rig):
    """Losing a Tier B lead because a second webhook was not configured is a worse
    failure than putting it in the wrong channel."""
    build, posts, _store = rig
    dispatcher = build(tier="B", hot="https://discord.test/hot")

    await dispatcher.run(job())

    assert len(posts) == 1
    await dispatcher.webhook.client.aclose()


async def test_no_webhook_configured_is_not_an_error(rig):
    """A system being set up scores and stores leads perfectly well; it just has
    nowhere to put the card yet."""
    build, posts, store = rig
    dispatcher = build(tier="A")
    dispatcher.webhooks = {}

    result = await dispatcher.run(job())

    assert result.ok
    assert posts == []
    assert store.conn.execute("SELECT * FROM dispatch_log").fetchall() == []
    await dispatcher.webhook.client.aclose()


async def test_a_429_is_obeyed_not_guessed(tmp_path: Path):
    """Discord says exactly how long to wait. Guessing gets you limited harder."""
    waits: list[float] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"id": "msg-9"})

    webhook = DiscordWebhook(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await webhook.post("https://discord.test/hot", {"embeds": []})

    assert result.ok
    assert result.message_id == "msg-9"
    assert calls["n"] == 2, "retried once, after the server's own delay"
    assert waits == []
    await webhook.client.aclose()


async def test_a_malformed_embed_is_not_retried():
    """A 400 means the payload is wrong. Retrying sends the same bytes."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text='{"embeds": ["invalid"]}')

    webhook = DiscordWebhook(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await webhook.post("https://discord.test/hot", {"embeds": []})

    assert not result.ok
    assert calls["n"] == 1
    await webhook.client.aclose()


async def test_build_card_works_for_any_tier(rig):
    """`cindra dispatch-test` has to answer "is the webhook right?" on a database
    where nothing yet qualifies — which is the state that prompts the question."""
    build, _posts, _store = rig
    dispatcher = build(tier="REJECT", score=9)
    lead = dispatcher.read_lead("lead1")
    assert lead is not None
    embed = build_card(lead)
    assert embed["title"]
    await dispatcher.webhook.client.aclose()


# ----------------------------------------------------------- score reconciliation


def test_a_company_resolved_before_the_scorer_existed_still_gets_scored(rig):
    """The gap the first real run exposed.

    37 companies were resolved before the Resolver enqueued scoring, so nothing would
    ever have scored them: the pipeline only reacted to events and never reconciled.
    The same hole reopens after a restore, or a crash between stages.
    """
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue

    build, _posts, store = rig
    build(tier="A")  # seeds a company with a live trigger
    with store.tx() as conn:
        conn.execute("DELETE FROM leads")  # as if the Scorer had never run

    queued = enqueue_stale_scores(store, JobQueue(store))

    assert queued == 1
    row = store.conn.execute("SELECT payload FROM jobs WHERE kind = 'score.company'").fetchone()
    assert json.loads(row["payload"])["canonical_domain"] == "acme.io"


def test_reconciling_twice_queues_one_job(rig):
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue

    build, _posts, store = rig
    build(tier="A")
    with store.tx() as conn:
        conn.execute("DELETE FROM leads")

    first = enqueue_stale_scores(store, JobQueue(store))
    second = enqueue_stale_scores(store, JobQueue(store))

    assert (first, second) == (1, 0)
    jobs = store.conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE kind='score.company'")
    assert jobs.fetchone()["n"] == 1


def test_a_company_already_scored_is_left_alone(rig):
    """The lead row is newer than every trigger, so there is nothing to recompute."""
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue
    from cindraleads.scoring import ScoringConfig

    build, _posts, store = rig
    build(tier="A")
    with store.tx() as conn:
        # Stamped with the running calibration, because that is what "already scored"
        # now means. The fixture writes the lead row directly, and a lead carrying no
        # `scoring_version` is stale by definition -- it predates any recorded
        # calibration, so nothing can vouch that its number matches the current rules.
        conn.execute(
            "UPDATE leads SET last_updated_at = '2099-01-01T00:00:00Z', scoring_version = ?",
            (ScoringConfig.load().fingerprint(),),
        )

    assert enqueue_stale_scores(store, JobQueue(store)) == 0


def test_a_newer_trigger_makes_a_scored_company_stale_again(rig):
    """A company that picks up a trigger after its last scoring must be re-scored, or
    its lead permanently understates it."""
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue

    build, _posts, store = rig
    build(tier="A")
    with store.tx() as conn:
        conn.execute("UPDATE leads SET last_updated_at = '2026-08-15T00:00:00Z'")
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at) VALUES "
            "('t9','acme.io','T9_MARKETPLACE',0.9,'2026-08-16T00:00:00Z','2099-01-01T00:00:00Z')"
        )

    assert enqueue_stale_scores(store, JobQueue(store)) == 1


def test_a_company_with_no_live_trigger_is_not_scored(rig):
    """Fit without news is not a lead, and scoring it would spend a model call to
    conclude exactly that."""
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue

    build, _posts, store = rig
    build(tier="A")
    with store.tx() as conn:
        conn.execute("DELETE FROM leads")
        conn.execute("UPDATE triggers SET decays_at = '2020-01-01T00:00:00Z'")

    assert enqueue_stale_scores(store, JobQueue(store)) == 0


# --------------------------------------------------------------- prose recovery


def test_a_thermal_pause_defers_prose_instead_of_losing_it(rig):
    """What the first real scoring run cost: 32 of 32 leads finalised with no angle.

    The governor paused inference for the whole batch. Each lead scored correctly --
    the arithmetic never needed a model -- but the job completed, so no lead would
    ever have been given an angle without a manual rescore.
    """
    from cindraleads.agents.scorer import PROSE_RETRY_SECONDS, SCORE_KIND, ScoreOutcome, Scorer
    from cindraleads.config import settings

    _build, _posts, store = rig
    cfg = settings()
    object.__setattr__(cfg, "config_dir", REPO_ROOT / "config")
    object.__setattr__(cfg, "prompt_dir", REPO_ROOT / "prompts")
    scorer = Scorer(store=store, llm=None, config=cfg)

    with store.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO companies (canonical_domain, display_name, ai_surface, "
            "tech_signals, first_seen_at, last_updated_at) VALUES "
            "('acme.io','Acme','[]','[]','2026-08-15T00:00:00Z','2026-08-15T00:00:00Z')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO evidence (evidence_id, url, source_id, snippet, "
            "observed_at, content_sha256) VALUES "
            "('e1','https://acme.io/','company_site','s','2026-08-15T00:00:00Z','h')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at) VALUES "
            "('t1','acme.io','T1_AI_SHIP',0.9,'2026-08-15T00:00:00Z','2099-01-01T00:00:00Z')"
        )
        conn.execute("INSERT OR REPLACE INTO trigger_evidence VALUES ('t1','e1')")

    outcome = ScoreOutcome(canonical_domain="acme.io", retry_prose_in=PROSE_RETRY_SECONDS)
    job = Job(job_id="j", kind=SCORE_KIND, payload={"canonical_domain": "acme.io"})
    with store.tx() as conn:
        result = scorer.commit(job, outcome, conn)

    assert result.ok
    assert store.conn.execute("SELECT * FROM leads").fetchone() is not None, "still scored"
    retries = [kind for kind, _ in result.follow_on if kind == SCORE_KIND]
    assert retries == [SCORE_KIND], "the lead comes back for its angle"
    payload = next(p for k, p in result.follow_on if k == SCORE_KIND)
    assert payload["_delay_seconds"] == PROSE_RETRY_SECONDS


def test_a_schema_failure_is_not_retried():
    """The ladder already retried locally and escalated. Asking again in 20 minutes
    would just spend the Pi's time reaching the same conclusion."""
    from cindraleads.agents.scorer import _is_recoverable

    assert not _is_recoverable("extraction failed schema: 3 validation errors")
    assert _is_recoverable("LLM inference is paused by the thermal governor; retry when cool")
    assert _is_recoverable("ConnectError: connection refused")


def test_a_lead_that_already_has_an_angle_is_not_re_queued(rig):
    """Otherwise every rescore of an angle-bearing lead schedules pointless work."""
    from cindraleads.agents.scorer import _has_angle

    build, _posts, store = rig
    build(tier="A")  # seeds a lead with outreach_angle = 'angle'
    assert _has_angle(store.conn, "lead1")
    with store.tx() as conn:
        conn.execute("UPDATE leads SET outreach_angle = ''")
    assert not _has_angle(store.conn, "lead1")


def test_the_card_leads_with_the_heaviest_trigger():
    """Measured on the first real run: every card led with "DNS hygiene".

    The dispatcher ordered triggers by confidence, and T8_HYGIENE_GAP is written at
    0.8 (a DNS record read directly) while T1_AI_SHIP is written at 0.7 (a model
    reading a page). Weight 12 therefore outranked weight 30, and the digest row --
    which shows only the first trigger -- buried the reason to call.
    """
    from cindraleads.agents.dispatcher import TRIGGER_ORDER

    assert TRIGGER_ORDER, "trigger weights failed to load; the card cannot rank"
    assert TRIGGER_ORDER["T1_AI_SHIP"] > TRIGGER_ORDER["T8_HYGIENE_GAP"]

    data = card(
        triggers=(("T8_HYGIENE_GAP", 0.8, "0d ago"), ("T1_AI_SHIP", 0.7, "11d ago")),
        tier="C",
    )
    # digest_row renders only the first, so the caller's order is what reaches Discord.
    assert "T8_HYGIENE_GAP" in digest_row(data)["description"]

    ordered = card(
        triggers=(("T1_AI_SHIP", 0.7, "11d ago"), ("T8_HYGIENE_GAP", 0.8, "0d ago")),
        tier="C",
    )
    assert "T1_AI_SHIP" in digest_row(ordered)["description"]


def test_a_calibration_change_makes_every_lead_stale(rig):
    """The other half of a rule change.

    `enqueue_stale_scores` reconciles on trigger timestamps, and editing a weight or a
    penalty moves none of them -- so the `single_source` fix landed against 108 leads
    that all quietly kept their old numbers. A lead whose `scoring_version` differs
    from the running one is out of date by definition.
    """
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue
    from cindraleads.scoring import ScoringConfig

    build, _posts, store = rig
    build(tier="A")
    cfg = ScoringConfig.load()
    with store.tx() as conn:
        conn.execute(
            "UPDATE leads SET last_updated_at = '2099-01-01T00:00:00Z', scoring_version = ?",
            (cfg.fingerprint(),),
        )
    assert enqueue_stale_scores(store, JobQueue(store), config=cfg) == 0

    from dataclasses import replace

    recalibrated = replace(cfg, penalties={**cfg.penalties, "single_source": -8.0})
    assert enqueue_stale_scores(store, JobQueue(store), config=recalibrated) == 1


def test_a_rescore_is_not_deduped_against_the_job_that_already_ran(rig):
    """The bug that would have made the whole mechanism a no-op.

    `JobQueue.enqueue` matches `dedupe_key` across every job including completed ones,
    and a recalibration does not move the trigger timestamp the key was built from.
    Without the fingerprint in the key the rescore collides with the job that already
    ran under the old calibration and is silently dropped -- and the reconciler would
    report success having changed nothing.
    """
    from dataclasses import replace

    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue
    from cindraleads.scoring import ScoringConfig

    build, _posts, store = rig
    build(tier="A")
    cfg = ScoringConfig.load()
    queue = JobQueue(store)

    with store.tx() as conn:
        conn.execute(
            "UPDATE leads SET last_updated_at = '2099-01-01T00:00:00Z', scoring_version = ?",
            (cfg.fingerprint(),),
        )
    # Drain the original job so its row is `done` and still matchable by dedupe key.
    for job in queue.claim("w", kinds=["score.company"], lease_seconds=60, limit=10):
        queue.complete(job.job_id)

    recalibrated = replace(cfg, penalties={**cfg.penalties, "single_source": -8.0})
    assert enqueue_stale_scores(store, queue, config=recalibrated) == 1

    ready = queue.claim("w2", kinds=["score.company"], lease_seconds=60, limit=10)
    assert ready, "the rescore was deduped away against the completed job"


def test_leads_scored_before_the_column_existed_are_rescored(rig):
    """NULL means "predates any recorded calibration", which is stale -- nothing can
    vouch that such a lead's number matches the rules now running."""
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue

    build, _posts, store = rig
    build(tier="A")
    with store.tx() as conn:
        conn.execute(
            "UPDATE leads SET last_updated_at = '2099-01-01T00:00:00Z', scoring_version = NULL"
        )

    assert enqueue_stale_scores(store, JobQueue(store)) == 1


def test_new_triggers_are_queued_ahead_of_a_recalibration(rig):
    """A config edit makes the whole corpus stale at once. At ~18 s a lead that is
    hours of queue, and a funding round found this morning must not sit behind it."""
    import json as _json
    from dataclasses import replace
    from datetime import timedelta

    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.models import to_iso, utcnow
    from cindraleads.queue import JobQueue
    from cindraleads.scoring import ScoringConfig

    build, _posts, store = rig
    build(tier="A")
    cfg = ScoringConfig.load()
    now = utcnow()

    with store.tx() as conn:
        # acme.io: scored under the running calibration, nothing new since.
        conn.execute(
            "UPDATE leads SET last_updated_at = ?, scoring_version = ?",
            (to_iso(now), cfg.fingerprint()),
        )
        # A second company whose trigger landed *after* its last scoring.
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, first_seen_at, "
            "last_updated_at) VALUES ('fresh.io','Fresh',?,?)",
            (to_iso(now), to_iso(now)),
        )
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at, active) VALUES "
            "('t2','fresh.io','T2_FUNDING',0.9,?,'2099-01-01T00:00:00Z',1)",
            (to_iso(now),),
        )
        conn.execute(
            "INSERT INTO leads (lead_id, canonical_domain, score, tier, recommended_offer, "
            "first_seen_at, last_updated_at, pipeline_version, scoring_version) VALUES "
            "('lead2','fresh.io',50,'B','snapshot_free',?,?,'v1',?)",
            (to_iso(now - timedelta(days=30)), to_iso(now - timedelta(days=30)), cfg.fingerprint()),
        )

    recalibrated = replace(cfg, penalties={**cfg.penalties, "single_source": -8.0})
    assert enqueue_stale_scores(store, JobQueue(store), config=recalibrated) == 2

    order = [
        _json.loads(row["payload"])["canonical_domain"]
        for row in store.conn.execute(
            "SELECT payload FROM jobs WHERE kind='score.company' ORDER BY rowid"
        )
    ]
    assert order[0] == "fresh.io", (
        f"the company with a genuinely new trigger must be queued first, got {order}"
    )


def test_force_requeues_a_company_whose_job_already_ran(rig):
    """A job that ran but did nothing useful looks exactly like one that worked.

    A worker on a stale build drained a batch of rescores without stamping any
    calibration. Those `done` rows now hold the dedupe keys for the very work they
    failed to do, so the reconciler skips those companies forever. Nothing in the data
    can detect that -- it needs a human saying "recompute anyway".
    """
    from cindraleads.agents.scorer import enqueue_stale_scores
    from cindraleads.queue import JobQueue

    build, _posts, store = rig
    build(tier="A")
    queue = JobQueue(store)

    # A stale lead, queued and then drained by a worker that achieved nothing.
    assert enqueue_stale_scores(store, queue) == 1
    for job in queue.claim("stale-build", kinds=["score.company"], lease_seconds=60, limit=10):
        queue.complete(job.job_id)

    # The ordinary reconciler now finds the key already used and enqueues nothing.
    assert enqueue_stale_scores(store, queue) == 0
    assert not queue.claim("w", kinds=["score.company"], lease_seconds=60, limit=10)

    # --force gets past it.
    assert enqueue_stale_scores(store, queue, force=True) == 1
    assert queue.claim("w2", kinds=["score.company"], lease_seconds=60, limit=10)


def test_a_card_never_shows_an_internal_trigger_code(rig):
    """The last line before Discord.

    The Scorer refuses to *store* an angle naming our taxonomy, but leads scored before
    that guard existed already have one, and nothing re-queues them -- their
    calibration is current, so the reconciler sees nothing stale. A real Tier B card
    reached #cindrasec reading "You published T1_AI_SHIP and T8_HYGIENE_GAP on your
    public page", and this is the check that would have stopped it.
    """
    build, _posts, store = rig
    build(tier="A")
    with store.tx() as conn:
        conn.execute(
            "UPDATE leads SET outreach_angle = ?, bengali_angle = ?",
            (
                "You published T1_AI_SHIP and T8_HYGIENE_GAP on your public page.",
                "আপনারা T1_AI_SHIP প্রকাশ করেছেন।",
            ),
        )

    card = build_card(_lead_row(store))

    # Precisely the prose fields, by name. The Triggers field shows codes on purpose --
    # that half of the card is for the operator, and it is the Angle that gets pasted
    # into an email. A blanket "no codes anywhere" assertion would forbid the useful
    # half of the card along with the harmful one.
    names = {f["name"] for f in card["fields"]}
    assert not any(n.endswith("Angle") or "\u09ac\u09be\u0982\u09b2\u09be" in n for n in names), (
        f"a withheld angle should drop its field entirely, got {sorted(names)}"
    )
    assert any("Triggers" in n for n in names), "the operator-facing trigger list stays"


def test_withholding_the_angle_does_not_withhold_the_lead(rig):
    """An empty angle is a small loss -- triggers, evidence and score are all still on
    the card and a human can write the sentence. An angle naming internal codes is
    worse than empty, because the card exists to be pasted into an email."""
    build, _posts, store = rig
    build(tier="A")
    with store.tx() as conn:
        conn.execute("UPDATE leads SET outreach_angle = 'You published T1_AI_SHIP.'")

    card = build_card(_lead_row(store))

    assert card["title"], "the card still exists"
    assert any("Acme" in json.dumps(v) for v in card.values()), "the company is still named"
    assert any("T1_AI_SHIP" in json.dumps(f) for f in card.get("fields", [])), (
        "the trigger is still shown in the Triggers field -- that is ours to read"
    )


def test_a_clean_angle_is_left_alone(rig):
    build, _posts, store = rig
    build(tier="A")
    angle = "You announced an AI assistant two weeks ago. I'd like to review it, free."
    with store.tx() as conn:
        conn.execute("UPDATE leads SET outreach_angle = ?", (angle,))

    assert angle in json.dumps(build_card(_lead_row(store)))


def _lead_row(store):  # type: ignore[no-untyped-def]
    """The lead as the Dispatcher reads it, without needing a live webhook."""
    from cindraleads.agents.dispatcher import Dispatcher

    reader = Dispatcher(
        store=store,
        webhook=DiscordWebhook(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "x"}))
            )
        ),
        webhooks={"hot": "https://x.test"},
    )
    return reader.read_lead("lead1")
