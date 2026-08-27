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
        if trigger:
            tid = uuid.uuid4().hex[:16]
            eid = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
                "observed_at, decays_at) VALUES (?,?,?,0.9,?,'2099-01-01T00:00:00Z')",
                (tid, domain, trigger, now),
            )
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,'company_site','s',?,'h')",
                (eid, f"https://{domain}/proof", now),
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
