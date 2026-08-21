"""Canonical domains and the duplicate ladder.

Two companies are the same company far more often than a naive key suggests: the same
firm appears as `acme.io`, `www.acme.io`, `blog.acme.io` and "Acme, Inc." across four
sources in one harvest. Deduplicating here — before enrichment and before scoring — is
what keeps one prospect from becoming four lead cards.

**The ladder** (PLAN.md 2.3, decision 3):

    rung 1  exact canonical domain            free, catches the overwhelming majority
    rung 2  fuzzy name + same country         free, catches rebrands and punctuation
    rung 3  embedding similarity              GATED OFF; bge-m3 is 1.2 GB resident
    rung 4  manual override table             the escape hatch for the ones we get wrong

Rung 3 stays off until the measured duplicate rate misses the <2% target. `sqlite-vec`
and `company_vectors` exist in the schema from day one so turning it on is a config
change, never a migration.

**No `tldextract`.** Registrable-domain extraction needs the public suffix list, and
tldextract either ships a stale snapshot or fetches one at runtime — a network call at
import time, from a passive-only system, is exactly the surprise we do not want. The
suffix set below is explicit, small, and covers the TLDs the ICP actually targets. A
domain under an unlisted multi-part suffix degrades to "last two labels", which is the
same answer tldextract gives for anything not in its list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = [
    "DuplicateMatch",
    "canonical_domain",
    "name_similarity",
    "rapidfuzz_available",
    "same_company",
]

# Multi-part public suffixes we actually meet. `.com.bd` is load-bearing: the ICP's
# secondary market is Bangladesh, and treating `dhaka-fintech.com.bd` as `com.bd`
# would collapse every Bangladeshi company onto one row.
_MULTI_PART_SUFFIXES: frozenset[str] = frozenset(
    {
        "com.bd",
        "net.bd",
        "org.bd",
        "edu.bd",
        "gov.bd",
        "ac.bd",
        "com.pk",
        "net.pk",
        "org.pk",
        "edu.pk",
        "com.lk",
        "net.lk",
        "org.lk",
        "com.np",
        "org.np",
        "co.in",
        "net.in",
        "org.in",
        "firm.in",
        "gen.in",
        "ind.in",
        "co.uk",
        "org.uk",
        "me.uk",
        "ltd.uk",
        "plc.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "co.nz",
        "net.nz",
        "org.nz",
        "com.sg",
        "com.my",
        "com.hk",
        "com.tw",
        "com.cn",
        "com.br",
        "com.mx",
        "co.jp",
        "or.jp",
        "ne.jp",
        "co.kr",
        "co.za",
        "co.id",
        "co.th",
    }
)

# Suffixes that legal-entity names carry and humans do not say out loud. Stripped
# before comparison so "Acme, Inc." and "Acme" are one company.
_LEGAL_SUFFIX = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c|ltd|ltd\.|limited|plc|gmbh|ag|bv|nv|oy|ab|as|sa|srl|"
    r"pty|pvt|private|corp|corporation|co|company|holdings|group|labs|lab|"
    r"technologies|technology|tech|software|solutions|systems|services|studio|"
    r"ventures|partners)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Hosts that belong to a platform, not to a company. A candidate whose only URL is a
# GitHub repo must not be canonicalized to `github.com` — that would merge every
# open-source project on earth into one "company".
PLATFORM_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "news.ycombinator.com",
        "ycombinator.com",
        "medium.com",
        "substack.com",
        "notion.site",
        "notion.so",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "reddit.com",
        "producthunt.com",
        "crunchbase.com",
        "upwork.com",
        "fiverr.com",
        "freelancer.com",
        # Applicant tracking systems and job boards. Every one of these hosts many
        # companies behind a slug or a subdomain, so the registrable domain names the
        # *vendor* and never the prospect -- and letting one through is worse than
        # useless: `arborealmanagement.na.teamtailor.com` canonicalizes to
        # `teamtailor.com`, so every company on that ATS would collapse onto a single
        # bogus row and poison the dedupe ladder. Reading the slug back to a domain is
        # the guess the Greenhouse work was abandoned over.
        #
        # The list below is not speculative. Each was returned by a real "Who is
        # hiring" comment on 2026-08-21: 7 of the first 10 URLs in that thread were one
        # of these, against 3 genuine company domains.
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "workable.com",
        "bdjobs.com",
        "teamtailor.com",
        "wellfound.com",
        "angel.co",
        "careerpuck.com",
        "kula.ai",
        "applicantstack.com",
        "uctalent.io",
        "smartrecruiters.com",
        "jobvite.com",
        "bamboohr.com",
        "recruitee.com",
        "breezy.hr",
        "myworkdayjobs.com",
        "taleo.net",
        "icims.com",
        "pinpointhq.com",
        "join.com",
        "rippling.com",
        "hire.withgoogle.com",
        "indeed.com",
        "glassdoor.com",
        "ziprecruiter.com",
        "otta.com",
        "builtin.com",
        # `grnh.se` is Greenhouse's own shortener and `trakstar` another ATS. Both came
        # back from the same thread on the *second* pass, which is why this list grows
        # from measurement rather than being written once from memory.
        "grnh.se",
        "trakstar.com",
        "onstrider.com",
        "hr-manager.net",
        # Form and document hosts. A hiring post linking a Google Form is asking for
        # applications, not publishing a company site -- and in the "Who wants to be
        # hired" thread a Drive link is somebody's CV, which is a person and not a
        # prospect at all.
        "forms.gle",
        "docs.google.com",
        "drive.google.com",
        "acrobat.adobe.com",
        "airtable.com",
        "typeform.com",
        "vercel.app",
        "netlify.app",
        "herokuapp.com",
        "pages.dev",
        "github.io",
        "readthedocs.io",
        "gitbook.io",
        "wordpress.com",
        "blogspot.com",
    }
)


def rapidfuzz_available() -> bool:
    try:
        import rapidfuzz  # noqa: F401
    except ImportError:
        return False
    return True


def canonical_domain(value: str) -> str | None:
    """The registrable domain for a URL or host, lowercased.

    `https://www.Acme.io/careers?x=1` and `blog.acme.io` both give `acme.io`. Returns
    None for anything that is not a usable company domain — an IP address, a bare
    label, or a platform host that belongs to somebody else.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None

    host = urlparse(raw if "//" in raw else f"//{raw}").hostname or ""
    host = host.strip(".")
    if not host or " " in host:
        return None
    # An IP address is a host, never a company identity.
    if re.fullmatch(r"[0-9.]+", host) or ":" in host:
        return None

    labels = host.split(".")
    if len(labels) < 2:
        return None

    # Platform check runs on the *full* host and every parent of it, before the
    # registrable reduction. Checking only the reduced form let
    # `news.ycombinator.com` through as `ycombinator.com`: the reduction had already
    # discarded the label that made it recognizable.
    if _is_platform(host):
        return None

    last_two = ".".join(labels[-2:])
    if len(labels) >= 3 and last_two in _MULTI_PART_SUFFIXES:
        registrable = ".".join(labels[-3:])
    else:
        registrable = last_two

    if _is_platform(registrable):
        return None
    if not re.fullmatch(r"[a-z0-9.-]+", registrable):
        return None
    return registrable


def _is_platform(host: str) -> bool:
    """True if `host` is a platform host or lives under one."""
    labels = host.split(".")
    return any(".".join(labels[i:]) in PLATFORM_HOSTS for i in range(len(labels)))


def normalize_name(name: str) -> str:
    """A company name reduced to its comparable core."""
    lowered = name.strip().lower()
    without_legal = _LEGAL_SUFFIX.sub(" ", lowered)
    return _NON_ALNUM.sub("", without_legal)


def name_similarity(left: str, right: str) -> float:
    """0..100 similarity between two company names.

    Uses rapidfuzz when installed (it is a `[dedupe]` extra, 58 MB) and falls back to
    `difflib` otherwise. The fallback is slower and slightly less forgiving, but the
    ladder must not silently stop working on a machine that skipped the extra —
    degrading to "fewer merges" is acceptable, crashing is not.
    """
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    try:
        from rapidfuzz import fuzz
    except ImportError:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a, b).ratio() * 100
    return float(fuzz.token_sort_ratio(a, b))


@dataclass(frozen=True)
class DuplicateMatch:
    rung: int
    canonical_domain: str
    score: float
    reason: str


def same_company(
    *,
    domain: str | None,
    name: str,
    country: str | None,
    known: list[tuple[str, str, str | None]],
    threshold: float = 92.0,
) -> DuplicateMatch | None:
    """Find the existing company this one duplicates, or None.

    `known` is `(canonical_domain, display_name, country)` rows.

    Country is a *guard*, not a signal: two companies with near-identical names in
    different countries are usually two companies. It only participates when both
    sides state one — a missing country must not block a merge, or the ladder stops
    working on exactly the sparse pages it exists to handle.
    """
    if domain:
        for existing_domain, _name, _country in known:
            if existing_domain == domain:
                return DuplicateMatch(1, existing_domain, 100.0, "exact canonical domain")

    best: DuplicateMatch | None = None
    for existing_domain, existing_name, existing_country in known:
        if country and existing_country and country != existing_country:
            continue
        score = name_similarity(name, existing_name)
        if score >= threshold and (best is None or score > best.score):
            best = DuplicateMatch(
                2, existing_domain, score, f"name ~{score:.0f} vs {existing_name}"
            )
    return best
