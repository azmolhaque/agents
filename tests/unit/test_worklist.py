"""The call list. Correctness here is measured in wasted minutes rather than wrong rows:
a list that shows unreachable leads, repeats a company, or keeps a lead you already
judged is one you stop opening.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from cindraleads.models import to_iso, utcnow
from cindraleads.worklist import render_worklist, worklist

REPO_ROOT = Path(__file__).resolve().parents[2]


def _lead(
    store: Any,
    domain: str,
    *,
    score: int = 60,
    tier: str = "B",
    angle: str = "You published an AI assistant last month.",
    emails: tuple[tuple[str, str, str], ...] = (("hello@x.io", "role_account", ""),),
    trigger: str | None = "T1_AI_SHIP",
    extra_triggers: tuple[str, ...] = (),
) -> str:
    lead_id = uuid.uuid4().hex[:16]
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO companies (canonical_domain, display_name, "
            "first_seen_at, last_updated_at) VALUES (?,?,?,?)",
            (domain, domain.split(".")[0].title(), now, now),
        )
        conn.execute(
            "INSERT INTO leads (lead_id, canonical_domain, score, score_breakdown, tier, "
            "recommended_offer, outreach_angle, first_seen_at, last_updated_at, "
            "pipeline_version) VALUES (?,?,?,'{}',?,'snapshot_free',?,?,?,'test')",
            (lead_id, domain, score, tier, angle, now, now),
        )
        for email, status, full_name in emails:
            conn.execute(
                "INSERT OR IGNORE INTO contacts (contact_id, canonical_domain, full_name, "
                "email, email_status, pii_basis, first_seen_at) VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], domain, full_name or None, email, status, "x", now),
            )
        codes = ([trigger] if trigger else []) + list(extra_triggers)
        for code in codes:
            tid = uuid.uuid4().hex[:16]
            eid = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
                "observed_at, decays_at) VALUES (?,?,?,0.9,?,'2099-01-01T00:00:00Z')",
                (tid, domain, code, now),
            )
            # One URL per trigger, so a mis-chosen trigger is visible as a wrong link
            # rather than hidden behind a shared one.
            path = "proof" if code == trigger else code.lower()
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,'company_site','s',?,'h')",
                (eid, f"https://{domain}/{path}", now),
            )
            conn.execute("INSERT INTO trigger_evidence VALUES (?,?)", (tid, eid))
    return lead_id


def test_a_lead_with_no_contact_is_counted_not_listed(store: Any) -> None:
    """125 of 500 companies publish an email and contact discovery is at its ceiling, so
    most of `sendable` is not workable. Listing those leads would fill the page with
    work nobody can do; hiding the count would make the list look like the whole
    opportunity."""
    _lead(store, "reachable.io")
    _lead(store, "silent.io", emails=())

    report = worklist(store)

    assert [i.canonical_domain for i in report.items] == ["reachable.io"]
    assert report.unreachable == 1


def test_one_row_per_company_not_per_address(store: Any) -> None:
    """The first hand-written call list showed GAIA twelve times. Deduplicated in the
    contacts table it is still three rows for one email you will send."""
    _lead(
        store,
        "gaia.io",
        emails=(
            ("ceo@gaia.io", "verified", "Nabila R."),
            ("contact@gaia.io", "role_account", ""),
            ("security@gaia.io", "role_account", ""),
        ),
    )

    report = worklist(store)

    assert len(report.items) == 1
    assert report.items[0].contacts_total == 3


def test_a_named_human_is_preferred_over_a_role_account(store: Any) -> None:
    """At equal score a person answers and a shared inbox forwards."""
    _lead(
        store,
        "acme.io",
        emails=(("hello@acme.io", "verified", ""), ("cto@acme.io", "verified", "Arif H.")),
    )

    item = worklist(store).items[0]

    assert item.email == "cto@acme.io"
    assert item.named


def test_a_judged_lead_drops_off_the_list(store: Any) -> None:
    """The list is the queue. A lead you have ruled on is done, and re-reading it
    tomorrow is how a worklist becomes a report you skim."""
    from cindraleads.feedback import record_verdict

    lead_id = _lead(store, "done.io")
    _lead(store, "todo.io")

    record_verdict(store, lead_id=lead_id, verdict="bad", actor="me", source="cli")
    report = worklist(store)

    assert [i.canonical_domain for i in report.items] == ["todo.io"]
    assert worklist(store, include_judged=True).reachable == 2


def test_the_row_carries_everything_needed_to_send(store: Any) -> None:
    """The angle exists and lived in Discord scrollback; the evidence URL is what makes
    a cold email checkable rather than a blast; the feedback line is the loop's only
    human input and had the highest friction of anything in the system."""
    lead_id = _lead(store, "acme.io")

    text = render_worklist(worklist(store))

    assert "acme.io" in text
    assert "hello@x.io" in text
    assert "T1_AI_SHIP" in text
    assert "https://acme.io/proof" in text
    assert "You published an AI assistant last month." in text
    assert f"cindra feedback {lead_id} good|bad" in text


def test_an_angle_less_lead_says_so_rather_than_printing_a_blank(store: Any) -> None:
    """Prose fails for real reasons -- a thermal spell, a decode budget -- and the lead
    is still worth sending. A blank line reads as a rendering bug."""
    _lead(store, "quiet.io", angle="")

    text = render_worklist(worklist(store))

    assert "no angle written" in text


def test_tier_c_is_not_on_the_call_list_by_default(store: Any) -> None:
    """Tier C gets a batched digest. This list is for the ones worth writing to
    individually, and mixing them would bury the leads that are."""
    _lead(store, "warm.io", tier="B")
    _lead(store, "cool.io", tier="C", score=45)

    assert [i.canonical_domain for i in worklist(store).items] == ["warm.io"]
    assert len(worklist(store, tiers=("B", "C")).items) == 2


def test_it_writes_nothing(store: Any) -> None:
    """Read-only by construction. A worklist that mutated state would have to be
    trusted; this one only has to be correct."""
    _lead(store, "acme.io")
    before = store.conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]

    worklist(store)
    render_worklist(worklist(store))

    after = store.conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    assert before == after
    assert store.conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == 0


def test_a_suppressed_domain_is_vetoed_and_never_replanned(store: Any, tmp_path: Path) -> None:
    """The table has existed since migration 0001 and both readers were built -- the
    Scout consults it at plan time so a rejected company stops costing credits, and the
    ComplianceGate vetoes at dispatch. Nothing ever wrote to it.

    The case it exists for is the one no rule can catch: `under_employee_ceiling`
    deliberately does not veto on an unknown `employee_band`, so PagerDuty and JetBrains
    reach Tier B and top the call list. They are not mis-scored, they are simply not
    prospects -- a judgement, and judgements need somewhere to live.
    """
    from cindraleads.compliance import ComplianceGate, LeadFacts

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO suppression_list (entry_id, kind, value, reason, created_at) "
            "VALUES ('e1','domain','pagerduty.com','enterprise, has its own security org',?)",
            (to_iso(utcnow()),),
        )

    gate = ComplianceGate(excluded_sectors=("government",), max_employees=1000)
    gate.load_suppression(store.conn)
    verdict = gate.review(
        LeadFacts(
            canonical_domain="pagerduty.com",
            display_name="PagerDuty",
            trigger_codes=("T1_AI_SHIP",),
            evidence_urls=("https://pagerduty.com/",),
        )
    )

    assert "not_suppressed" in verdict.vetoes


def test_a_suppressed_domain_leaves_the_call_list_immediately(store: Any) -> None:
    """Suppressing a domain does not rewrite the leads already scored under the old
    answer. The ComplianceGate quarantines a vetoed lead, but `_upsert_lead` still
    stores its computed tier -- so a suppressed company keeps Tier B, and the first
    three domains ever suppressed were still sitting at number one, seven and nine.

    A stored verdict answers "was this allowed when we scored it". A call list has to
    answer "may I email them now", so it asks the table rather than the lead.
    """
    _lead(store, "pagerduty.com", score=67)
    _lead(store, "rtrvr.ai", score=66)

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO suppression_list (entry_id, kind, value, reason, created_at) "
            "VALUES ('e1','domain','pagerduty.com','enterprise',?)",
            (to_iso(utcnow()),),
        )

    assert [i.canonical_domain for i in worklist(store).items] == ["rtrvr.ai"]


def test_a_quarantined_lead_is_not_on_the_call_list(store: Any) -> None:
    """The other vetoes -- government, competitor, over the employee ceiling -- have the
    same shape: the lead keeps its tier and only dispatch is stopped, so nothing else
    would keep it off a list of people to email."""
    lead_id = _lead(store, "defence.gov", score=70)
    _lead(store, "rtrvr.ai", score=66)

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO quarantine (quarantine_id, subject_kind, subject_id, reason_code, "
            "detail, created_at) VALUES ('q1','lead',?,'not_government_or_cni','',?)",
            (lead_id, to_iso(utcnow())),
        )

    assert [i.canonical_domain for i in worklist(store).items] == ["rtrvr.ai"]


def _evidence_urls(store: Any, domain: str, urls: list[str]) -> None:
    """Replace the domain's evidence, in an order the query will actually return.

    The join has no ORDER BY and SQLite walks it by `evidence_id`, not by insertion --
    verified directly, because the first version of this test inserted the platform URL
    first and still got the good one. So which URL a card cites is decided by a random
    hex id: the defect is a coin flip per company rather than a consistent wrong answer,
    which is exactly why it reached production on Findcheap and not on the fixture.

    Ids are assigned in order here so the *bad* URL is the one the old code would pick.
    A test whose outcome depends on unspecified row order proves nothing either way.
    """
    now = to_iso(utcnow())
    with store.tx() as conn:
        tid = conn.execute(
            "SELECT trigger_id FROM triggers WHERE canonical_domain = ?", (domain,)
        ).fetchone()["trigger_id"]
        conn.execute("DELETE FROM trigger_evidence WHERE trigger_id = ?", (tid,))
        for n, url in enumerate(urls):
            eid = f"{n:016d}"
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,'company_site','s',?,'h')",
                (eid, url, now),
            )
            conn.execute("INSERT INTO trigger_evidence VALUES (?,?)", (tid, eid))


def test_the_cited_evidence_is_the_company_own_page_not_a_store_listing(store: Any) -> None:
    """A URL that is not theirs proves nothing about them.

    The first real call list cited `chromewebstore.google.com/detail/findcheap/...` as
    proof of what findcheap.ai had announced. A trigger can carry several evidence rows
    and `_top_trigger` took whichever the join returned first; `PLATFORM_HOSTS` is
    applied when a *company* is canonicalized and nowhere near an evidence URL, so the
    store listing reached the one line the reader is invited to click.

    Same family as the TechCrunch defect: a live page *about* the company standing in
    for the company's own word.
    """
    _lead(store, "findcheap.ai", emails=(("jake@findcheap.ai", "verified", ""),))
    _evidence_urls(
        store,
        "findcheap.ai",
        [
            "https://chromewebstore.google.com/detail/findcheap/fpghkhnkfjlen",
            "https://findcheap.ai/",
        ],
    )

    assert worklist(store, limit=5).items[0].evidence_url == "https://findcheap.ai/"


def test_a_platform_link_is_still_shown_when_it_is_all_we_have(store: Any) -> None:
    """Reported, not silently dropped.

    Blanking the URL would make a weakly-evidenced trigger look like a well-evidenced
    one with a rendering bug. The operator has to see that the only proof we hold is
    somebody else's page *before* deciding to send -- the same choice as printing the
    unreachable count beside `jobs_lost` rather than hiding the exemption.
    """
    only = "https://chromewebstore.google.com/detail/onlystore/xyz"
    _lead(store, "onlystore.ai", emails=(("hi@onlystore.ai", "verified", ""),))
    _evidence_urls(store, "onlystore.ai", [only])

    assert worklist(store, limit=5).items[0].evidence_url == only


def test_borrowed_evidence_is_marked_on_the_card(store: Any) -> None:
    """Preferring their own page is only half the job.

    When a platform link is all we hold the card still cites it -- deliberately -- and
    an unmarked store listing reads exactly like the company's own announcement. The
    operator is about to tell a stranger "you published this"; they have to see whose
    page it actually is first. Same reasoning as printing the unreachable count beside
    `jobs_lost`: an exemption nobody can see is not an exemption, it is a silent
    downgrade.
    """
    only = "https://chromewebstore.google.com/detail/onlystore/xyz"
    _lead(store, "onlystore.ai", emails=(("hi@onlystore.ai", "verified", ""),))
    _evidence_urls(store, "onlystore.ai", [only])

    rendered = render_worklist(worklist(store, limit=5))

    assert only in rendered
    assert "not their page" in rendered


def test_their_own_page_carries_no_warning(store: Any) -> None:
    """The bound. A marker on every card is a marker nobody reads."""
    _lead(store, "traccia.ai", emails=(("founders@traccia.ai", "verified", ""),))
    _evidence_urls(store, "traccia.ai", ["https://traccia.ai/"])

    rendered = render_worklist(worklist(store, limit=5))

    assert "https://traccia.ai/" in rendered
    assert "not their page" not in rendered


# ------------------------------------------------- which desk we are writing to


def test_security_beats_legal_even_though_the_alphabet_disagrees(store: Any) -> None:
    """The tiebreak among role accounts used to be `ORDER BY email`.

    `abuse@` < `hello@` < `legal@` < `security@`, so the one role account this
    function's own docstring calls *good* -- RFC 9116 makes it the mailbox the company
    nominated for exactly this conversation -- sorted last. ThunderPhone's best contact
    came out `legal@`. The comment named the principle and the ORDER BY encoded the
    alphabet.
    """
    _lead(
        store,
        "acme.io",
        emails=(
            ("legal@acme.io", "role_account", ""),
            ("security@acme.io", "role_account", ""),
        ),
    )

    report = worklist(store)

    assert report.items[0].email == "security@acme.io"
    assert not report.items[0].contact_is_wrong_desk


def test_a_complaints_desk_is_shown_and_marked_rather_than_dropped(store: Any) -> None:
    """`legal@` may be the only address a company publishes, and a lead you cannot see
    is worse than one you can see is awkward. What the operator must not do is paste a
    cold pitch into a complaints desk without noticing -- an unsolicited commercial mail
    there is the fastest route to a hostile reply. Same call as the borrowed evidence
    URL: marked, not blanked."""
    _lead(store, "acme.io", emails=(("legal@acme.io", "role_account", ""),))

    report = worklist(store)

    assert report.items[0].email == "legal@acme.io", "the lead is still reachable"
    assert report.items[0].contact_is_wrong_desk
    assert "complaints/wrong desk" in render_worklist(report)


def test_an_ordinary_front_door_carries_no_warning(store: Any) -> None:
    """A warning on every row is one nobody reads -- the reason the platform-evidence
    marker is scoped the way it is."""
    _lead(store, "acme.io", emails=(("hello@acme.io", "role_account", ""),))

    report = worklist(store)

    assert not report.items[0].contact_is_wrong_desk
    assert "complaints/wrong desk" not in render_worklist(report)


def test_a_named_human_still_outranks_every_desk(store: Any) -> None:
    """The bound. `security@` is the best *role* account, not better than a person --
    `has_named_contact` is worth +10 of the reachability component for a reason."""
    _lead(
        store,
        "acme.io",
        emails=(
            ("security@acme.io", "role_account", ""),
            ("sarah@acme.io", "verified", "Sarah Chen"),
        ),
    )

    report = worklist(store)

    assert report.items[0].email == "sarah@acme.io"


def test_a_placeholder_name_is_not_shown_to_a_human(store: Any) -> None:
    """`null · bopbook.com` reached a near-miss list as the literal four characters --
    not NULL, so it survives every `IS NOT NULL AND <> ''` filter in the system. A card
    built from that row greets a stranger as "null"."""
    _lead(store, "bopbook.com")
    with store.tx() as conn:
        conn.execute(
            "UPDATE companies SET display_name = 'null' WHERE canonical_domain = ?",
            ("bopbook.com",),
        )

    report = worklist(store)

    assert report.items[0].display_name == "bopbook.com"


def test_the_why_line_cites_the_trigger_the_angle_actually_argues(store: Any) -> None:
    """Matcha's `why:` said `T3_HIRING_SEC` over an angle describing a mail-auth gap.

    `_top_trigger` returned the *heaviest* trigger and the renderer printed its
    evidence URL directly beneath the model's prose, so **the link the reader is
    invited to click did not support the sentence above it**. Weight answers "how much
    is this lead worth"; this line answers "what is this card about", and one ordering
    was serving both questions.
    """
    _lead(
        store,
        "matcha.io",
        trigger="T3_HIRING_SEC",  # weight 20, the heavier of the two
        extra_triggers=("T8_HYGIENE_GAP",),  # weight 12
        angle=(
            "You publish a mail-authentication policy with gaps in it -- your DMARC "
            "record is still p=none. I'd like to run your first external "
            "attack-surface Snapshot free as a founding-cohort client."
        ),
    )

    item = worklist(store).items[0]

    assert item.trigger == "T8_HYGIENE_GAP"
    assert item.evidence_url == "https://matcha.io/t8_hygiene_gap"


def test_weight_still_decides_when_the_angle_argues_nothing(store: Any) -> None:
    """The fallback is today's behaviour, and it has to stay reachable: an angle-less
    lead, or one whose wording matches no `means` phrase, still needs a `why:` line.

    Two distinct content words and a strict winner, or the heaviest trigger wins --
    a guess that reads well is worse than the ordering it replaced, because nothing
    downstream would ever question it.
    """
    _lead(
        store,
        "quiet.io",
        trigger="T3_HIRING_SEC",
        extra_triggers=("T8_HYGIENE_GAP",),
        angle="",
    )

    item = worklist(store).items[0]

    assert item.trigger == "T3_HIRING_SEC"
    assert item.evidence_url == "https://quiet.io/proof"


def test_the_ask_is_not_evidence_of_what_the_card_is_about(store: Any) -> None:
    """Every offer phrase names "security", "report" or "assessment", so matching
    against the whole angle would hand T3 or T10 every card in the corpus -- a marker
    present in ~100% of cases is a constant, not a discriminator. The subject is what
    comes before the ask."""
    _lead(
        store,
        "offered.io",
        trigger="T1_AI_SHIP",
        extra_triggers=("T10_VENDOR_PRESSURE",),
        angle=(
            "You announced an AI assistant last month. I'd like to run a security "
            "assessment for you and send a report -- a customer asked us for one "
            "last week, so the pentest report format is settled."
        ),
    )

    item = worklist(store).items[0]

    assert item.trigger == "T1_AI_SHIP"


def test_the_why_line_says_what_the_code_means(store: Any) -> None:
    """`why: T3_HIRING_SEC` is a slug shown to the person deciding whether to send --
    exactly where every trigger code stood before `means` existed. The Discord card
    was taught this and the call list was not."""
    _lead(store, "acme.io", trigger="T1_AI_SHIP")

    rendered = render_worklist(worklist(store))

    assert "announced an AI feature or assistant" in rendered
