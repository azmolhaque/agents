"""Public DNS record lookups. Never a probe.

Every query here is one a public resolver answers for anyone: MX, TXT, DNSSEC. No
connection is ever made to the prospect's own infrastructure, no port is touched, and
nothing is authenticated. The distinction is the whole compliance story, so it is worth
being precise about what this is *not*:

* It is not a scan. Asking Cloudflare's resolver what a domain's MX record says is
  reading a public directory, not interacting with the company.
* It is not a finding. `DMARC p=none` here means "the published policy is none", which
  is a prioritisation hint for us. It never appears in outreach as something we
  discovered about their security, and `embeds.py` enforces that wording.
* It is not an email probe. There is no SMTP connection anywhere in this module. Email
  verification stops at "does the domain accept mail at all", which is an MX lookup.
  VRFY and RCPT are on the forbidden list in `passive.py`.

`dnspython` is an optional extra. Without it every field comes back None, which the
scorer reads as "unknown" rather than "absent" — a missing library must never look like
a company with no SPF record.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from cindraleads.logging import get_logger
from cindraleads.models import DnsHygiene, utcnow

__all__ = ["DnsProbe", "dnspython_available", "hygiene_gaps", "lookup_hygiene"]

log = get_logger("cindraleads.dns")

_DMARC_POLICY = re.compile(r"\bp\s*=\s*(none|quarantine|reject)\b", re.IGNORECASE)
# Selectors that cover the large mail providers. DKIM has no discovery mechanism, so
# absence here means "not found at a common selector", never "no DKIM".
_DKIM_SELECTORS = ("google", "selector1", "selector2", "k1", "default", "mail", "dkim")

DEFAULT_TIMEOUT = 5.0


def dnspython_available() -> bool:
    try:
        import dns.resolver  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class DnsProbe:
    """Wraps `dnspython` so the resolver can be swapped in tests.

    Named a probe reluctantly — every call is a read of a public record. Nothing here
    contacts the domain being asked about.
    """

    timeout: float = DEFAULT_TIMEOUT
    nameservers: tuple[str, ...] = ()

    def _resolver(self) -> Any:
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.timeout = self.timeout
        resolver.lifetime = self.timeout
        if self.nameservers:
            resolver.nameservers = list(self.nameservers)
        return resolver

    def query(self, name: str, record: str) -> list[str]:
        """Records as strings, or [] for anything that is not an answer.

        NXDOMAIN, an empty answer and a timeout are all "we did not learn anything".
        Only the caller can tell those apart from "the record does not exist", and it
        does so by distinguishing None from False in the fields below.
        """
        if not dnspython_available():
            return []
        try:
            answers = self._resolver().resolve(name, record)
        except Exception as exc:  # dnspython raises a wide family of lookup errors
            log.debug("dns_no_answer", name=name, record=record, error=type(exc).__name__)
            return []
        return [str(rdata).strip('"').replace('" "', "") for rdata in answers]

    def dnssec_signed(self, domain: str) -> bool:
        return bool(self.query(domain, "DNSKEY"))


async def lookup_hygiene(
    domain: str, probe: DnsProbe | None = None, *, security_txt: bool | None = None
) -> DnsHygiene:
    """Read a domain's published mail and DNS policy.

    Runs in a worker thread: `dnspython` is synchronous, and six lookups at a 5 s
    timeout would otherwise block the event loop for up to 30 s while every other stage
    waits. On a single-worker Pi that is the difference between a slow enrichment and a
    stalled pipeline.
    """
    if not dnspython_available():
        # Every field stays None. "Unknown" and "absent" must not be the same value:
        # a missing optional dependency would otherwise look like a company with no
        # SPF record, and score them for a gap they do not have.
        log.info("dns_unavailable", domain=domain, why="dnspython not installed")
        return DnsHygiene(security_txt=security_txt, checked_at=utcnow())

    resolver = probe or DnsProbe()
    return await asyncio.to_thread(_lookup_sync, domain, resolver, security_txt)


def _lookup_sync(domain: str, probe: DnsProbe, security_txt: bool | None) -> DnsHygiene:
    mx = probe.query(domain, "MX")
    txt = probe.query(domain, "TXT")
    spf = next((record for record in txt if record.lower().startswith("v=spf1")), None)

    dmarc_records = probe.query(f"_dmarc.{domain}", "TXT")
    policy = None
    for record in dmarc_records:
        match = _DMARC_POLICY.search(record)
        if match:
            policy = match.group(1).lower()
            break

    dkim = any(
        probe.query(f"{selector}._domainkey.{domain}", "TXT") for selector in _DKIM_SELECTORS
    )

    return DnsHygiene(
        mx_present=bool(mx),
        spf=spf,
        dmarc_policy=policy,  # type: ignore[arg-type]
        dkim_present=dkim or None,  # False here means "not at a common selector"
        dnssec=probe.dnssec_signed(domain),
        security_txt=security_txt,
        checked_at=utcnow(),
    )


def hygiene_gaps(hygiene: DnsHygiene) -> list[str]:
    """Human-readable gaps, or [] .

    Only *published* facts count. A field we could not read contributes nothing —
    reporting "no SPF" because the lookup timed out would put a false claim on a lead
    card, and the card is a document a human may act on.
    """
    gaps: list[str] = []
    if hygiene.spf is None and hygiene.mx_present:
        # Only meaningful for a domain that actually receives mail.
        gaps.append("no SPF record published")
    if hygiene.dmarc_policy is None and hygiene.mx_present:
        gaps.append("no DMARC record published")
    elif hygiene.dmarc_policy == "none":
        gaps.append("DMARC p=none (monitor only)")
    if hygiene.dnssec is False:
        gaps.append("DNSSEC not enabled")
    if hygiene.security_txt is False:
        gaps.append("no security.txt")
    return gaps
