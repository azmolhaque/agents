"""The call list: which leads a human should actually work, and what to say.

Everything upstream of this answers "is this a lead?". This answers "what do I do with
it before lunch?", and those are different enough to deserve separate output.

Three facts shaped it, all measured:

* **Only ~25% of the corpus is reachable.** 125 of 500 companies publish an email, and
  contact discovery is at its ceiling -- fetching more of their pages was tried and
  returned four addresses. So the sellable universe is much smaller than `sendable`
  suggests, and a list that shows unreachable leads wastes the reader's attention on
  work they cannot do.
* **The angle already exists and was hard to find.** The Scorer writes it, the
  Dispatcher puts it on a card, and then it lives in Discord scrollback. Reprinting it
  beside the address turns "look up the lead" into "paste and send".
* **Judging is the loop's only human input and had the highest friction.** Six days at
  `judged: 0` was not disagreement, it was a lead id you had to copy out of another
  report. The `cindra feedback` line is printed ready to run.

Read-only by construction: it writes nothing and takes no `--apply`. A worklist that
mutated state would need to be trusted; this one only has to be correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cindraleads.store import Store

__all__ = ["WorkItem", "render_worklist", "worklist"]

# The score below which a lead is not worth a personal email. Tier C exists and gets a
# batched digest; this list is for the ones you write to individually.
DEFAULT_TIERS = ("A", "B")


@dataclass(frozen=True)
class WorkItem:
    lead_id: str
    canonical_domain: str
    display_name: str
    score: int
    tier: str
    offer: str
    email: str
    email_status: str
    role_title: str = ""
    full_name: str = ""
    angle: str = ""
    trigger: str = ""
    evidence_url: str = ""
    contacts_total: int = 1

    @property
    def named(self) -> bool:
        """A human's address outranks a role account for a first email."""
        return bool(self.full_name)


@dataclass
class Worklist:
    items: list[WorkItem] = field(default_factory=list)
    reachable: int = 0
    unreachable: int = 0
    judged: int = 0

    @property
    def total(self) -> int:
        return self.reachable + self.unreachable


def worklist(
    store: Store,
    *,
    tiers: tuple[str, ...] = DEFAULT_TIERS,
    limit: int = 25,
    include_judged: bool = False,
) -> Worklist:
    """Reachable leads worth a personal email, best first.

    One row per company, not per address. A company with `security@`, `hello@` and a
    named CTO is one conversation, and listing it three times is how the first hand-run
    call list became unreadable -- GAIA appeared twelve times before contacts were
    deduplicated, and even deduplicated it is three rows for one email you will send.

    Ordered by score, then by whether the contact is a named human: at equal score, a
    person answers and a shared inbox forwards.
    """
    placeholders = ",".join("?" for _ in tiers)
    judged_clause = "" if include_judged else "  AND f.lead_id IS NULL\n"
    rows = store.conn.execute(
        "SELECT l.lead_id, l.canonical_domain, l.score, l.tier, l.recommended_offer, "
        "       l.outreach_angle, c.display_name, "
        "       (SELECT COUNT(*) FROM contacts x "
        "          WHERE x.canonical_domain = l.canonical_domain AND x.email IS NOT NULL) "
        "         AS contacts_total "
        "FROM leads l "
        "JOIN companies c ON c.canonical_domain = l.canonical_domain "
        "LEFT JOIN (SELECT DISTINCT lead_id FROM feedback) f ON f.lead_id = l.lead_id "
        # Asked live, not read off the lead. Suppressing a domain does not rewrite the
        # leads already scored under the old answer: the ComplianceGate quarantines a
        # vetoed lead but `_upsert_lead` still stores its computed tier, so a suppressed
        # company keeps Tier B and would sit at the top of this list forever. The first
        # three domains ever suppressed were still number one, seven and nine.
        #
        # A stored verdict answers "was this allowed when we scored it". A call list has
        # to answer "may I email them now", and only the table can say.
        "LEFT JOIN suppression_list s "
        "  ON s.kind = 'domain' AND s.value = l.canonical_domain "
        # Quarantine covers the other vetoes -- government, competitor, over the
        # employee ceiling. Same reasoning: the lead keeps its tier, so nothing else
        # would keep it off the list.
        "LEFT JOIN (SELECT DISTINCT subject_id FROM quarantine WHERE subject_kind = 'lead') q "
        "  ON q.subject_id = l.lead_id "
        f"WHERE l.tier IN ({placeholders}) AND l.archived = 0\n{judged_clause}"
        "  AND s.value IS NULL AND q.subject_id IS NULL "
        "ORDER BY l.score DESC",
        tuple(tiers),
    ).fetchall()

    items: list[WorkItem] = []
    unreachable = 0
    for row in rows:
        contact = _best_contact(store, str(row["canonical_domain"]))
        if contact is None:
            # Counted, not listed. The number is the point -- it is the difference
            # between "we have 188 sendable leads" and "you can email 125 companies",
            # and hiding it would make the list look like the whole opportunity.
            unreachable += 1
            continue
        trigger, evidence = _top_trigger(store, str(row["canonical_domain"]))
        items.append(
            WorkItem(
                lead_id=str(row["lead_id"]),
                canonical_domain=str(row["canonical_domain"]),
                display_name=str(row["display_name"] or row["canonical_domain"]),
                score=int(row["score"]),
                tier=str(row["tier"]),
                offer=str(row["recommended_offer"] or ""),
                email=str(contact["email"]),
                email_status=str(contact["email_status"] or ""),
                role_title=str(contact["role_title"] or ""),
                full_name=str(contact["full_name"] or ""),
                angle=str(row["outreach_angle"] or ""),
                trigger=trigger,
                evidence_url=evidence,
                contacts_total=int(row["contacts_total"] or 1),
            )
        )

    items.sort(key=lambda i: (-i.score, not i.named))
    return Worklist(items=items[:limit], reachable=len(items), unreachable=unreachable)


def _best_contact(store: Store, domain: str) -> dict[str, Any] | None:
    """The one address to write to.

    A named human first, then a verified address, then anything. `security@` is a role
    account but a good one -- RFC 9116 makes it the mailbox the company nominated for
    exactly this conversation -- so status decides before the local part does.
    """
    row = store.conn.execute(
        "SELECT email, email_status, role_title, full_name FROM contacts "
        "WHERE canonical_domain = ? AND email IS NOT NULL "
        "ORDER BY (full_name IS NULL), "
        "         CASE email_status WHEN 'verified' THEN 0 WHEN 'role_account' THEN 1 "
        "                           ELSE 2 END, "
        "         email "
        "LIMIT 1",
        (domain,),
    ).fetchone()
    return dict(row) if row else None


def _top_trigger(store: Store, domain: str) -> tuple[str, str]:
    """The heaviest live trigger and one URL that proves it.

    The evidence URL is the whole reason a cold email lands: "your DMARC record is
    p=none" is checkable in ten seconds, and quoting it is what separates this from a
    blast. A trigger whose evidence nobody can open is one the project refuses to send.
    """
    from cindraleads.agents.dispatcher import TRIGGER_ORDER

    rows = store.conn.execute(
        "SELECT t.code, e.url FROM triggers t "
        "LEFT JOIN trigger_evidence te ON te.trigger_id = t.trigger_id "
        "LEFT JOIN evidence e ON e.evidence_id = te.evidence_id "
        "WHERE t.canonical_domain = ? AND t.active = 1",
        (domain,),
    ).fetchall()
    if not rows:
        return ("", "")
    best = max(rows, key=lambda r: TRIGGER_ORDER.get(str(r["code"]), 0))
    return (str(best["code"]), str(best["url"] or ""))


def render_worklist(report: Worklist) -> str:
    """Plain text, wide enough to read and narrow enough to paste."""
    if not report.items:
        return (
            "Nothing to work.\n"
            f"  {report.unreachable} lead(s) at this tier have no contact, "
            "and every reachable one has already been judged.\n"
        )

    out: list[str] = [
        f"{report.reachable} reachable lead(s) worth an email"
        f" · {report.unreachable} at this tier have no contact",
        "",
    ]
    for n, item in enumerate(report.items, 1):
        who = f"{item.full_name} · {item.role_title}".strip(" ·") or item.email_status
        extra = f"  (+{item.contacts_total - 1} more)" if item.contacts_total > 1 else ""
        out.append(
            f"{n:>3}. {item.score:>3} {item.tier}  {item.display_name} · {item.canonical_domain}"
        )
        out.append(f"      {item.email}  [{who}]{extra}")
        if item.trigger:
            out.append(f"      why: {item.trigger}  {item.evidence_url}")
        if item.angle:
            out.append(f"      {item.angle}")
        else:
            # Says so rather than printing a blank line. An angle-less lead is still
            # worth sending; you just have to write the first sentence yourself.
            out.append("      (no angle written -- see the card's triggers)")
        out.append(f"      cindra feedback {item.lead_id} good|bad")
        out.append("")
    return "\n".join(out)
