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
from dataclasses import dataclass

from cindraleads.compliance import PERSONAL_EMAIL_DOMAINS
from cindraleads.logging import get_logger

__all__ = [
    "DISPOSABLE_DOMAINS",
    "ROLE_LOCAL_PARTS",
    "DiscoveredContact",
    "classify_email",
    "extract_contacts",
    "persona_for",
]

log = get_logger("cindraleads.contacts")

# Deliberately permissive on the local part and strict on the domain: the goal is to
# find addresses in prose, not to validate against RFC 5322.
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24})\b")

# Obfuscations that appear on pages trying to slow scrapers down. Honouring the
# obfuscation is the polite reading: if a company wrote "hello [at] acme.io" they did
# not want it harvested, so we do not un-obfuscate it.
_OBFUSCATED = re.compile(r"\b[\w.+-]+\s*(?:\[at\]|\(at\)|\s+at\s+)\s*[\w.-]+\.\w{2,}\b", re.I)

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


def extract_contacts(
    text: str,
    *,
    source_url: str,
    company_domain: str,
    domain_has_mx: bool | None = None,
    limit: int = 3,
) -> list[DiscoveredContact]:
    """Pull business addresses out of a page we already fetched.

    Reads text the Extractor's fetch already produced — no new request. Obfuscated
    addresses are deliberately left alone: writing "hello [at] acme.io" is a request
    not to be harvested, and honouring it costs us one contact and keeps the promise.

    Capped at `limit`, preferring the most useful. Section 12's data-minimisation rule
    is not satisfied by storing every address on a company's contact page.
    """
    if not text:
        return []
    if _OBFUSCATED.search(text):
        log.debug("contacts_obfuscated_present", url=source_url)

    seen: set[str] = set()
    found: list[DiscoveredContact] = []
    for match in _EMAIL.finditer(text):
        email = f"{match.group(1)}@{match.group(2)}".lower()
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
