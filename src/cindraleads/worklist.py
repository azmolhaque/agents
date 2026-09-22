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

from cindraleads.dedupe import display_name_or_domain
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
    # True when the only proof we hold for the top trigger is somebody else's page.
    evidence_is_platform: bool = False
    # True when the best address we have is a desk that cannot act on a cold email --
    # support, careers, or worse, a complaints mailbox like `legal@` or `abuse@`.
    contact_is_wrong_desk: bool = False
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
        trigger, evidence, borrowed = _top_trigger(store, str(row["canonical_domain"]))
        items.append(
            WorkItem(
                lead_id=str(row["lead_id"]),
                canonical_domain=str(row["canonical_domain"]),
                display_name=display_name_or_domain(
                    row["display_name"], str(row["canonical_domain"])
                ),
                score=int(row["score"]),
                tier=str(row["tier"]),
                offer=str(row["recommended_offer"] or ""),
                email=str(contact["email"]),
                email_status=str(contact["email_status"] or ""),
                contact_is_wrong_desk=mailbox_rank(str(contact["email"])) >= WRONG_DESK_RANK,
                role_title=str(contact["role_title"] or ""),
                full_name=str(contact["full_name"] or ""),
                angle=str(row["outreach_angle"] or ""),
                trigger=trigger,
                evidence_url=evidence,
                evidence_is_platform=borrowed,
                contacts_total=int(row["contacts_total"] or 1),
            )
        )

    items.sort(key=lambda i: (-i.score, not i.named))
    return Worklist(items=items[:limit], reachable=len(items), unreachable=unreachable)


# What a mailbox is *for*, which is not what the alphabet says about it.
#
# The tiebreak among role accounts used to be `ORDER BY email`, so `abuse@` beat
# `hello@` beat `legal@` beat `security@` -- and this function's own docstring singles
# out `security@` as the one role account that is *good*. The comment named the
# principle and the ORDER BY encoded the alphabet, which is how ThunderPhone's best
# contact came out `legal@`.
#
# The bottom of this list is not merely a worse hit rate. `legal@` and `abuse@` are
# complaint desks: an unsolicited commercial mail there is the fastest route to a
# hostile reply, and at a company with a real abuse desk it may be filed as precisely
# the thing that desk exists to log.
#
# Ranked, never filtered. A company that publishes only `legal@` is still reachable and
# the lead is still worth working -- the operator just has to see which desk they are
# writing to before they send, the same call as showing a borrowed evidence URL and
# marking it rather than blanking it.
_MAILBOX_RANK: dict[str, int] = {
    # RFC 9116 makes this the mailbox the company nominated for exactly this
    # conversation. Nothing we could write to is better.
    "security": 0,
    "psirt": 0,
    "security-reports": 0,
    "secure": 0,
    # An ordinary front door.
    "hello": 1,
    "hi": 1,
    "contact": 1,
    "info": 1,
    "team": 1,
    "founders": 1,
    "sales": 1,
    "partnerships": 1,
    "bd": 1,
    # 2 is everything unlisted: a person's name, or an alias we cannot judge. Unknown
    # beats a desk we know is wrong, and loses to a desk we know is right.
    # The wrong desk. Not harmful, just read by someone who cannot act on it.
    "support": 3,
    "help": 3,
    "helpdesk": 3,
    "careers": 3,
    "jobs": 3,
    "recruiting": 3,
    "hr": 3,
    "press": 3,
    "media": 3,
    "investors": 3,
    "ir": 3,
    "billing": 3,
    "accounts": 3,
    "accounting": 3,
    "invoices": 3,
    # Complaint and compliance desks.
    "legal": 4,
    "abuse": 4,
    "dmca": 4,
    "copyright": 4,
    "takedown": 4,
    "privacy": 4,
    "gdpr": 4,
    "dpo": 4,
    # Never a human.
    "postmaster": 5,
    "noreply": 5,
    "no-reply": 5,
    "donotreply": 5,
    "do-not-reply": 5,
    "bounce": 5,
    "mailer-daemon": 5,
}
_UNRANKED_MAILBOX = 2
#: At or above this, the operator should see which desk it is before sending.
WRONG_DESK_RANK = 3


def mailbox_rank(email: str) -> int:
    """How suitable this mailbox is for a first cold email. Lower is better."""
    local = email.split("@", 1)[0].strip().lower()
    return _MAILBOX_RANK.get(local, _UNRANKED_MAILBOX)


def _best_contact(store: Store, domain: str) -> dict[str, Any] | None:
    """The one address to write to.

    A named human first, then a verified address, **then the desk the mailbox belongs
    to**, and only then the alphabet. `security@` is a role account but a good one --
    RFC 9116 makes it the mailbox the company nominated for exactly this conversation.

    Ranked in Python rather than in the ORDER BY because the judgement is about the
    local part, and a `CASE` over forty of them in SQL is a lookup table written in the
    wrong language. A domain has a handful of contacts; there is nothing to optimise.
    """
    rows = [
        dict(r)
        for r in store.conn.execute(
            "SELECT email, email_status, role_title, full_name FROM contacts "
            "WHERE canonical_domain = ? AND email IS NOT NULL",
            (domain,),
        ).fetchall()
    ]
    if not rows:
        return None

    status_rank = {"verified": 0, "role_account": 1}

    def rank(contact: dict[str, Any]) -> tuple[int, int, int, str]:
        email = str(contact["email"])
        return (
            0 if contact["full_name"] else 1,
            status_rank.get(str(contact["email_status"] or ""), 2),
            mailbox_rank(email),
            email,
        )

    return min(rows, key=rank)


def _top_trigger(store: Store, domain: str) -> tuple[str, str, bool]:
    """The heaviest live trigger and one URL that proves it.

    The evidence URL is the whole reason a cold email lands: "your DMARC record is
    p=none" is checkable in ten seconds, and quoting it is what separates this from a
    blast. A trigger whose evidence nobody can open is one the project refuses to send.

    **And a URL that is not theirs proves nothing about them.** A trigger can cite
    several evidence rows and this took whichever the join returned first, so the
    Findcheap card on the first real call list cited
    `chromewebstore.google.com/detail/findcheap/...` as proof of what findcheap.ai had
    announced. `PLATFORM_HOSTS` is applied when a *company* is canonicalized and nowhere
    near an evidence URL, so the store listing sailed through to the one line the reader
    is invited to click. Same family as the TechCrunch defect: a live page about the
    company, standing in for the company's own word.

    So the preference is their own domain first, then any non-platform URL, and a
    platform link only when it is the single thing we hold -- reported rather than
    silently dropped, because the operator needs to see that this trigger's proof is
    weak before they decide to send it.
    """
    from cindraleads.agents.dispatcher import TRIGGER_ORDER
    from cindraleads.dedupe import canonical_domain, is_platform_url

    rows = store.conn.execute(
        "SELECT t.code, e.url FROM triggers t "
        "LEFT JOIN trigger_evidence te ON te.trigger_id = t.trigger_id "
        "LEFT JOIN evidence e ON e.evidence_id = te.evidence_id "
        "WHERE t.canonical_domain = ? AND t.active = 1",
        (domain,),
    ).fetchall()
    if not rows:
        return ("", "", False)

    code = str(max(rows, key=lambda r: TRIGGER_ORDER.get(str(r["code"]), 0))["code"])
    urls = [str(r["url"]) for r in rows if str(r["code"]) == code and r["url"]]

    def rank(url: str) -> int:
        if canonical_domain(url) == domain:
            return 0  # their own page: what we want to quote
        return 2 if is_platform_url(url) else 1

    if not urls:
        return (code, "", False)
    best = min(urls, key=rank)
    return (code, best, rank(best) == 2)


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
        # Marked, not hidden, and for the same reason the borrowed evidence URL is:
        # `legal@` may be the only address a company publishes, and a lead you cannot
        # see is worse than one you can see is awkward. What the operator must not do
        # is paste a sales pitch into a complaints desk without noticing.
        desk = (
            "  [!] complaints/wrong desk -- read before sending"
            if item.contact_is_wrong_desk
            else ""
        )
        out.append(f"      {item.email}  [{who}]{extra}{desk}")
        if item.trigger:
            # Marked, not hidden. Preferring the company's own page is only half the
            # job: when a platform link is all we hold the card still cites it, and an
            # unmarked store listing reads exactly like their own announcement. The
            # operator is about to tell a stranger "you published this" -- they need to
            # see whose page it actually is before they do.
            borrowed = (
                "  [!] not their page -- verify before sending" if item.evidence_is_platform else ""
            )
            out.append(f"      why: {item.trigger}  {item.evidence_url}{borrowed}")
        if item.angle:
            out.append(f"      {item.angle}")
        else:
            # Says so rather than printing a blank line. An angle-less lead is still
            # worth sending; you just have to write the first sentence yourself.
            out.append("      (no angle written -- see the card's triggers)")
        out.append(f"      cindra feedback {item.lead_id} good|bad")
        out.append("")
    return "\n".join(out)
