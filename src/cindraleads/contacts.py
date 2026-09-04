"""Finding a business contact, and saying honestly how sure we are.

**No SMTP, ever.** Section 12 forbids VRFY and RCPT probing, and this module has no
socket in it at all. Verification is a ladder of things knowable without contacting the
prospect's mail server:

    syntax  →  domain has MX  →  not a disposable provider  →  role vs personal

Each rung is cheap and public. "Verified" here therefore means *deliverable-looking*,
not *confirmed to exist*, and `EmailStatus` says so by never using the word verified for
anything stronger than "the domain accepts mail and the address is well-formed".
Overstating this would put a wrong claim on a lead card.

**Business addresses only.** An address at a free-mail provider belongs to a person, not
a company, and the compliance gate vetoes a lead carrying one. This module refuses to
collect them in the first place, so the gate is a backstop rather than the only check.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cindraleads.compliance import PERSONAL_EMAIL_DOMAINS
from cindraleads.logging import get_logger

__all__ = [
    "DISPOSABLE_DOMAINS",
    "ROLE_LOCAL_PARTS",
    "DiscoveredContact",
    "classify_email",
    "emails_from_markup",
    "extract_contacts",
    "persona_for",
    "security_txt_contact",
]

log = get_logger("cindraleads.contacts")

# Deliberately permissive on the local part and strict on the domain: the goal is to
# find addresses in prose, not to validate against RFC 5322.
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24})\b")

# Obfuscations that appear on pages trying to slow scrapers down. Honouring the
# obfuscation is the polite reading: if a company wrote "hello [at] acme.io" they did
# not want it harvested, so we do not un-obfuscate it.
_OBFUSCATED = re.compile(r"\b[\w.+-]+\s*(?:\[at\]|\(at\)|\s+at\s+)\s*[\w.-]+\.\w{2,}\b", re.I)

# The address in `href="mailto:..."`. **This is where most contacts actually are**, and
# the reason the enricher found them for only 23 of 201 companies: `extract_text` keeps
# visible prose and throws attributes away, so a page whose contact is a button reading
# "Get in touch" published an address we then discarded before ever looking.
#
# `?subject=...` and `&cc=...` are stripped, and so is any surrounding whitespace or
# URL-encoding of the delimiter. Nothing here un-obfuscates anything: a `mailto:` link
# is a company publishing a clickable address, which is the opposite of hiding one.
_MAILTO = re.compile(r"""mailto:\s*([A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24})""")

# RFC 9116 makes `Contact:` mandatory in a security.txt, and it is frequently an email.
# We already fetch the file to decide whether one exists and then throw the body away --
# so this costs no request at all. It is also the *most* relevant address we can find:
# it is the mailbox the company nominated for exactly this conversation.
_SECURITY_TXT_CONTACT = re.compile(r"^\s*contact:\s*(?:mailto:)?\s*(\S+@\S+?)\s*$", re.I | re.M)

ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "info",
        "hello",
        "contact",
        "support",
        "sales",
        "admin",
        "team",
        "help",
        "enquiries",
        "inquiries",
        "office",
        "mail",
        "hi",
        "ask",
        "press",
        "media",
        "security",
        "abuse",
        "privacy",
        "legal",
        "careers",
        "jobs",
        "hr",
        "billing",
        "accounts",
        "no-reply",
        "noreply",
        "donotreply",
        "postmaster",
        "webmaster",
    }
)

# A subset that is *useful* despite being a role account: a human reads these.
REACHABLE_ROLES: frozenset[str] = frozenset(
    {"hello", "contact", "info", "team", "security", "sales", "enquiries", "inquiries", "hi"}
)

DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "10minutemail.com",
        "tempmail.com",
        "throwawaymail.com",
        "yopmail.com",
        "trashmail.com",
        "getnada.com",
        "sharklasers.com",
        "maildrop.cc",
        "dispostable.com",
        "fakeinbox.com",
        "temp-mail.org",
        "mohmal.com",
        "spamgourmet.com",
        "mailnesia.com",
    }
)

# Role titles that map to a persona worth addressing differently.
_PERSONA_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(founder|co-?founder|ceo|cto|chief technology)\b", "founder_cto"),
    (
        r"\b(vp eng|head of eng|engineering manager|director of engineering|vp of engineering)\b",
        "head_eng",
    ),
    (r"\b(compliance|grc|risk|audit|iso ?27001|soc ?2)\b", "compliance"),
    (r"\b(ai|ml|machine learning|data science) (lead|head|director|manager)\b", "ai_lead"),
    (r"\b(head of (ai|ml)|chief (ai|data) officer)\b", "ai_lead"),
)


@dataclass(frozen=True)
class DiscoveredContact:
    email: str
    status: str
    source_url: str
    full_name: str | None = None
    role_title: str | None = None
    persona: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether this address should reach a lead card at all."""
        return self.status in {"verified", "role_account", "catch_all"}


def classify_email(email: str, *, domain_has_mx: bool | None, company_domain: str) -> str:
    """One of the section 11 statuses, without contacting anything.

    `domain_has_mx` is None when the DNS lookup could not run. That must not become
    "risky": an unavailable resolver is our problem, not evidence about the address.
    """
    local, _, host = email.lower().partition("@")
    if not local or not host or "." not in host:
        return "unverified"
    if host in DISPOSABLE_DOMAINS:
        return "risky"
    if host in PERSONAL_EMAIL_DOMAINS:
        # A free-mail address is an individual, and section 12 says B2B only. Marked
        # rather than silently dropped so the veto has something to point at.
        return "risky"
    if not _same_company(host, company_domain):
        # An address on someone else's domain — an agency, a marketplace, a personal
        # site. Not evidence of how to reach *this* company.
        return "unverified"
    if domain_has_mx is False:
        return "risky"
    if domain_has_mx is None:
        return "unverified"
    if local in ROLE_LOCAL_PARTS:
        return "role_account"
    return "verified"


def _same_company(host: str, company_domain: str) -> bool:
    return host == company_domain or host.endswith(f".{company_domain}")


def persona_for(role_title: str | None) -> str | None:
    if not role_title:
        return None
    lowered = role_title.lower()
    for pattern, persona in _PERSONA_PATTERNS:
        if re.search(pattern, lowered):
            return persona
    return "generic"


def emails_from_markup(html: str) -> list[str]:
    """Addresses published as `mailto:` links, in document order.

    Reads the raw body, not the extracted prose. That is the whole point: a contact
    page whose address is behind a "Get in touch" button puts the address in an
    attribute, and `extract_text` keeps only what a reader sees. Every such company
    looked contactless.

    No un-obfuscation happens here and none should: a `mailto:` link is an address the
    company made clickable. `hello [at] acme.io` stays untouched, as it always has.
    """
    if not html:
        return []
    seen: set[str] = set()
    found: list[str] = []
    for match in _MAILTO.finditer(html):
        email = match.group(1).lower().rstrip(".,;")
        if email not in seen:
            seen.add(email)
            found.append(email)
    return found


def security_txt_contact(body: str) -> str | None:
    """The address a company nominated for security correspondence, or None.

    RFC 9116 requires `Contact:`, and it is often `mailto:security@…`. We already fetch
    this file to decide whether one exists, so reading the field costs nothing — and it
    is the single most relevant mailbox we can find, because it is the one the company
    chose to publish for precisely this conversation.

    Returns the first match only. A security.txt listing three contacts is offering
    alternatives, not three people to write to, and section 12's data minimisation says
    take one.
    """
    if not body:
        return None
    match = _SECURITY_TXT_CONTACT.search(body)
    return match.group(1).lower() if match else None


def extract_contacts(
    text: str,
    *,
    source_url: str,
    company_domain: str,
    domain_has_mx: bool | None = None,
    limit: int = 3,
    extra_emails: Sequence[str] = (),
) -> list[DiscoveredContact]:
    """Pull business addresses out of a page we already fetched.

    Reads text the Extractor's fetch already produced — no new request. Obfuscated
    addresses are deliberately left alone: writing "hello [at] acme.io" is a request
    not to be harvested, and honouring it costs us one contact and keeps the promise.

    `extra_emails` are addresses found somewhere other than prose -- `mailto:` hrefs and
    the `Contact:` line of a security.txt. They are classified and ranked by exactly the
    same rules rather than by a second code path, because "which addresses may reach a
    lead card" is a compliance question and must have one answer.

    Capped at `limit`, preferring the most useful. Section 12's data-minimisation rule
    is not satisfied by storing every address on a company's contact page.
    """
    if not text and not extra_emails:
        return []
    if _OBFUSCATED.search(text):
        log.debug("contacts_obfuscated_present", url=source_url)

    seen: set[str] = set()
    found: list[DiscoveredContact] = []
    candidates = [e.lower() for e in extra_emails]
    candidates += [f"{m.group(1)}@{m.group(2)}".lower() for m in _EMAIL.finditer(text)]

    for email in candidates:
        if email in seen:
            continue
        seen.add(email)
        status = classify_email(email, domain_has_mx=domain_has_mx, company_domain=company_domain)
        contact = DiscoveredContact(email=email, status=status, source_url=source_url)
        if contact.is_usable:
            found.append(contact)

    # A named mailbox outranks a role account, and a role account a person can read
    # outranks `no-reply`. `is_usable` already dropped the rest.
    def rank(contact: DiscoveredContact) -> tuple[int, str]:
        local = contact.email.split("@", 1)[0]
        if local not in ROLE_LOCAL_PARTS:
            return (0, contact.email)
        return (1 if local in REACHABLE_ROLES else 2, contact.email)

    found.sort(key=rank)
    return found[:limit]
