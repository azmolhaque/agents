"""Enricher — what else is publicly true about a company we already know.

The stage the score was waiting for. `reachability` is 15% of CindraScore and `surface`
another 10%, and both are structurally zero without this: measured on 2026-08-15, a
company with three live triggers and full ICP fit topped out at 52 (Tier C) purely
because nobody had looked for a contact.

**Every source here is a public record or a self-published page.** CT logs, DNS, RDAP,
the company's own `/about` and `/security.txt`, and their ATS board's public JSON. No
port is touched, nothing is authenticated, no SMTP connection is made. That is not a
constraint this stage works around — it is the reason the whole product is sellable.

**One failing source must not fail the company.** The fan-out is
`asyncio.gather(return_exceptions=True)`: crt.sh being down costs the subdomain count
and nothing else. A company enriched from four of six sources is a better lead than a
company not enriched at all, and marking it enriched anyway is correct — we did look.

**It writes triggers, so it writes evidence.** T7 and T8 are derived from lookups rather
than quoted from a page, so the evidence URL is the *query* — a crt.sh URL a human can
open and see the same answer. A trigger whose evidence nobody can check is exactly the
thing the project refuses to produce.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from cindraleads.config import Settings, settings
from cindraleads.contacts import (
    DiscoveredContact,
    emails_from_markup,
    extract_contacts,
    persona_for,
    security_txt_contact,
)
from cindraleads.dns_hygiene import DnsProbe, hygiene_gaps, lookup_hygiene, mail_auth_weakness
from cindraleads.errors import CindraError
from cindraleads.logging import get_logger
from cindraleads.models import DnsHygiene, Job, StageResult, to_iso, utcnow
from cindraleads.sources.clients import (
    AshbyClient,
    CrtShClient,
    GreenhouseClient,
    JobPosting,
    LeverClient,
    RdapClient,
    analyze_postings,
)
from cindraleads.sources.http import EgressClient, FetchDenied
from cindraleads.store import Store
from cindraleads.textextract import extract_text

__all__ = ["ENRICH_KIND", "SCORE_KIND", "EnrichOutcome", "Enricher", "enqueue_unenriched"]

log = get_logger("cindraleads.enricher")

ENRICH_KIND = "enrich.company"
SCORE_KIND = "score.company"

# Rapid growth is the trigger, not size. An established company with 200 subdomains is
# normal; twelve new hosts this month is a change worth a conversation.
SPRAWL_TOTAL = 25
SPRAWL_GROWTH = 8
SPRAWL_WINDOW_DAYS = 30

# Pages that may carry a human contact, ordered by expected yield per fetch. The order
# is load-bearing: the loop stops as soon as it has an address, because the only thing
# that reads the collected text is `extract_contacts`.
#
# `/privacy`, `/imprint` and `/impressum` are new and are the highest-yield additions
# available: a privacy notice must name a controller contact under GDPR Art. 13, and an
# Impressum is legally mandatory in DE/AT/CH and always carries an email. They are
# obligations rather than marketing pages, so they are populated even on sites that
# publish nothing else useful -- exactly the companies `reachability` scores zero.
#
# `/about` and `/team` stay, below them, because they are where a *named* human appears
# and a named contact outscores a role account.
CONTACT_PATHS = (
    "/",
    "/contact",
    "/privacy",
    "/imprint",
    "/impressum",
    "/about",
    "/team",
    "/legal",
)
SECURITY_TXT_PATH = "/.well-known/security.txt"

TRIGGER_DECAY_DAYS = {"T7_SURFACE_SPRAWL": 60, "T8_HYGIENE_GAP": 60}


@dataclass(frozen=True)
class SecurityTxt:
    """Whether one is published, and the mailbox it nominates.

    `present` is three-valued for the reason in `_security_txt`: absent and unknown are
    different answers. `contact` is only ever set when `present` is True.
    """

    present: bool | None = None
    contact: str | None = None


@dataclass(frozen=True)
class SiteFindings:
    """What the company's own pages yielded.

    Prose and addresses are separate fields because they come from different halves of
    the same document -- `extract_text` keeps what a reader sees and drops attributes,
    which is where `mailto:` lives. Returning only the first is why contact discovery
    ran at 11%.
    """

    text: str = ""
    emails: tuple[str, ...] = ()
    security_txt: bool | None = None


@dataclass(frozen=True)
class EnrichOutcome:
    canonical_domain: str
    subdomain_total: int | None = None
    subdomain_growth: int | None = None
    hygiene: DnsHygiene | None = None
    contacts: tuple[DiscoveredContact, ...] = ()
    hiring_triggers: tuple[str, ...] = ()
    hiring_evidence: str | None = None
    age_days: int | None = None
    sources_ok: tuple[str, ...] = ()
    sources_failed: tuple[str, ...] = ()
    duration_ms: int = 0
    error: str | None = None


@dataclass
class Enricher:
    store: Store
    egress: EgressClient
    config: Settings | None = None
    dns: DnsProbe | None = None
    # Set false only in tests that must not touch the network at all.
    enabled_sources: frozenset[str] = field(
        default_factory=lambda: frozenset({"crtsh", "dns", "site", "ats", "rdap"})
    )

    def __post_init__(self) -> None:
        self.config = self.config or settings()
        self.crtsh = CrtShClient(self.egress)
        self.rdap = RdapClient(self.egress)
        self.greenhouse = GreenhouseClient(self.egress)
        self.lever = LeverClient(self.egress)
        self.ashby = AshbyClient(self.egress)

    # ------------------------------------------------------------------ phase 1

    async def prepare(self, job: Job) -> EnrichOutcome:
        """Fan out. No database writes, and no single source can fail the company."""
        started = utcnow()
        domain = str(job.payload.get("canonical_domain") or "")
        if not domain:
            return EnrichOutcome(canonical_domain="", error="enrich job needs canonical_domain")

        board = str(job.payload.get("board_token") or domain.split(".")[0])

        # Gathered together so four independent lookups take as long as the slowest,
        # not their sum. `return_exceptions` keeps one dead source from failing the
        # company -- unpacked one at a time because a heterogeneous gather erases the
        # types and a mis-assigned tuple here would be a silent data corruption.
        gathered = await asyncio.gather(
            self._subdomains(domain),
            self._site(domain),
            self._boards(board),
            self._age(domain),
            return_exceptions=True,
        )
        names = ("crtsh", "site", "ats", "rdap")
        failed: list[str] = []
        for name, result in zip(names, gathered, strict=True):
            if isinstance(result, BaseException):
                failed.append(name)
                log.warning(
                    "enrich_source_failed", domain=domain, source=name, error=str(result)[:200]
                )
            elif result is None:
                failed.append(name)

        sprawl = gathered[0] if isinstance(gathered[0], tuple) else None
        total, growth = sprawl if sprawl else (None, None)
        site = gathered[1] if isinstance(gathered[1], SiteFindings) else SiteFindings()
        security_txt = site.security_txt
        hiring_codes, hiring_url = gathered[2] if isinstance(gathered[2], tuple) else ((), None)
        age = gathered[3] if isinstance(gathered[3], int) else None

        hygiene = (
            await lookup_hygiene(domain, self.dns, security_txt=security_txt)
            if "dns" in self.enabled_sources
            else None
        )
        contacts = tuple(
            extract_contacts(
                site.text,
                source_url=f"https://{domain}/",
                company_domain=domain,
                domain_has_mx=hygiene.mx_present if hygiene else None,
                extra_emails=site.emails,
            )
        )

        log.info(
            "enrich_complete",
            canonical_domain=domain,
            subdomains=total,
            growth=growth,
            contacts=len(contacts),
            hiring_triggers=list(hiring_codes),
            dns_gaps=len(hygiene_gaps(hygiene)) if hygiene else None,
            failed=failed,
        )
        return EnrichOutcome(
            canonical_domain=domain,
            subdomain_total=total,
            subdomain_growth=growth,
            hygiene=hygiene,
            contacts=contacts,
            hiring_triggers=tuple(hiring_codes),
            hiring_evidence=hiring_url,
            age_days=age,
            sources_ok=tuple(n for n in names if n not in failed),
            sources_failed=tuple(failed),
            duration_ms=int((utcnow() - started).total_seconds() * 1000),
        )

    # ---------------------------------------------------------------- the sources

    async def _subdomains(self, domain: str) -> tuple[int, int] | None:
        if "crtsh" not in self.enabled_sources:
            return None
        try:
            return await self.crtsh.growth(domain, window_days=SPRAWL_WINDOW_DAYS)
        except (FetchDenied, httpx.HTTPError, OSError, CindraError):
            return None

    async def _site(self, domain: str) -> SiteFindings:
        """The company's own pages, within the per-domain politeness budget.

        Stops at the first `FetchDenied`: the budget is 6 per rolling 24 h and the
        Extractor has usually spent one already. Burning the rest here would starve
        tomorrow's re-check of the evidence URL.

        Returns the prose *and* the `mailto:` addresses, because they are not the same
        thing and only reading the first is why 89% of the corpus looked unreachable.
        `extract_text` keeps what a visitor sees; an address behind a "Get in touch"
        button lives in an attribute and never appears in the text at all.
        """
        if "site" not in self.enabled_sources:
            return SiteFindings()

        # security.txt first, and that ordering is a fix rather than a preference. It
        # was fetched *after* the content loop, so a domain whose budget ran out during
        # the loop never got one -- and `security_txt` feeds `hygiene_gaps`, so an
        # exhausted budget silently cost a trigger as well as a contact. It is a fixed
        # one-fetch cost that answers a published fact either way.
        found = await self._security_txt(domain)

        collected: list[str] = []
        emails: list[str] = [found.contact] if found.contact else []
        seen: set[str] = set()

        for path in CONTACT_PATHS:
            # Everything below this loop reads `site.text`, and the only thing that
            # reads it is `extract_contacts`. So once an address is in hand the
            # remaining fetches cannot change the outcome -- they can only spend a
            # per-domain budget of 6 per rolling 24 h that tomorrow's evidence re-check
            # also needs. The old loop always ran all four paths.
            #
            # `/` is exempt: it is fetched even when security.txt already gave an
            # address, because a role account is not a named human and `reachability`
            # scores those differently.
            if emails and path != "/":
                log.debug("site_contact_found_early", canonical_domain=domain, at=path)
                break
            try:
                result = await self.egress.fetch("company_site", f"https://{domain}{path}")
            except FetchDenied:
                break  # out of budget for today; keep what we have
            except (httpx.HTTPError, OSError, CindraError):
                continue

            # A single-page app serves byte-identical HTML for /about, /contact and
            # /team. Reading it three more times teaches nothing and costs three of
            # the six requests a day this domain gets -- budget that a real
            # prospect's distinct pages need. One repeat is enough to know.
            digest = hashlib.sha256(result.body.encode("utf-8", "ignore")).hexdigest()
            if digest in seen:
                log.debug("site_paths_mirrored", canonical_domain=domain, at=path)
                break
            seen.add(digest)
            collected.append(extract_text(result.body, max_chars=8000))
            # From the raw body, before the text extractor discards attributes. Even
            # the mirrored-SPA case gets one pass, which is often where the address is:
            # an app shell with a footer `mailto:` renders no prose worth reading and
            # still publishes a contact.
            before = len(emails)
            emails.extend(emails_from_markup(result.body))
            if len(emails) > before:
                # Which page actually produced the address. Path priority above is an
                # argument until this says otherwise -- the same reason
                # `companies.discovered_by` exists for query templates.
                log.info("site_contact_path", canonical_domain=domain, path=path)

        return SiteFindings(
            text="\n".join(collected),
            emails=(*emails, *([found.contact] if found.contact else [])),
            security_txt=found.present,
        )

    async def _security_txt(self, domain: str) -> SecurityTxt:
        """Three-valued, and decided by content rather than by status code.

        **200 is not evidence that a security.txt exists.** An SPA answers 200 with
        its HTML shell for every route, so trusting the status recorded "publishes a
        security.txt" about companies publishing nothing of the kind -- a false fact
        that reaches `hygiene_gaps` and can be printed on a card.

        True only for something that parses as one, False only for a definite 404,
        and None for everything else: an app shell, a 403, an exhausted budget. Same
        rule as `evidence.reachable` -- absent and unknown are different answers.

        The `Contact:` line comes back with it. We were already paying for this fetch
        and throwing the body away after reading one boolean out of it -- and RFC 9116
        makes that field mandatory, so on any real security.txt it is a free address,
        nominated by the company for security correspondence specifically.
        """
        try:
            result = await self.egress.fetch("company_site", f"https://{domain}{SECURITY_TXT_PATH}")
        except FetchDenied:
            return SecurityTxt()
        except (httpx.HTTPError, OSError, CindraError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # A 404 is a real, publishable fact. A 403 is "you may not see it".
            return SecurityTxt(present=False if status == 404 else None)
        if not _is_security_txt(result.body):
            return SecurityTxt()
        # Only from a file that actually parses as a security.txt. An app shell that
        # happens to contain an address is not a published security contact.
        return SecurityTxt(present=True, contact=security_txt_contact(result.body))

    async def _boards(self, board_token: str) -> tuple[tuple[str, ...], str | None]:
        """Whichever ATS the company uses. This is what makes decision 7 work.

        T3/T4/T5/T11 were assigned to paid search in the master prompt and are free
        once the company is known, because every one of these boards publishes JSON.
        """
        if "ats" not in self.enabled_sources:
            return (), None

        # Each vendor names the call differently, so the fetchers are bound here
        # rather than assumed uniform -- Lever's is `postings`, not `jobs`.
        boards: tuple[tuple[Callable[[str], Awaitable[list[JobPosting]]], str], ...] = (
            (self.greenhouse.jobs, f"https://boards.greenhouse.io/{board_token}"),
            (self.lever.postings, f"https://jobs.lever.co/{board_token}"),
            (self.ashby.jobs, f"https://jobs.ashbyhq.com/{board_token}"),
        )
        for fetch, url in boards:
            try:
                postings: list[JobPosting] = await fetch(board_token)
            except (FetchDenied, httpx.HTTPError, OSError, CindraError):
                continue
            if postings:
                return tuple(analyze_postings(postings).triggers), url
        return (), None

    async def _age(self, domain: str) -> int | None:
        if "rdap" not in self.enabled_sources:
            return None
        try:
            return (await self.rdap.domain(domain)).get("age_days")
        except (FetchDenied, httpx.HTTPError, OSError, CindraError):
            return None

    # ------------------------------------------------------------------ phase 2

    def commit(self, job: Job, outcome: EnrichOutcome, conn: sqlite3.Connection) -> StageResult:
        if outcome.error:
            return StageResult(ok=False, stage="enricher", job_id=job.job_id, error=outcome.error)

        domain = outcome.canonical_domain
        if (
            conn.execute("SELECT 1 FROM companies WHERE canonical_domain = ?", (domain,)).fetchone()
            is None
        ):
            return StageResult(
                ok=False, stage="enricher", job_id=job.job_id, error=f"company {domain} not found"
            )

        now = to_iso(utcnow())
        conn.execute(
            "UPDATE companies SET subdomain_count_ct = COALESCE(?, subdomain_count_ct), "
            "dns_hygiene = COALESCE(?, dns_hygiene), enriched_at = ?, last_updated_at = ? "
            "WHERE canonical_domain = ?",
            (
                outcome.subdomain_total,
                outcome.hygiene.model_dump_json() if outcome.hygiene else None,
                now,
                now,
                domain,
            ),
        )

        for contact in outcome.contacts:
            evidence_id = _evidence(conn, contact.source_url, "company_site", contact.email)
            # `OR IGNORE` is load-bearing only since migration 0007. Before it there was
            # no constraint for it to fall foul of -- the primary key is a fresh uuid,
            # which cannot collide -- so it read as dedupe protection and provided none,
            # and every re-enrichment appended another copy of the same address.
            conn.execute(
                "INSERT OR IGNORE INTO contacts (contact_id, canonical_domain, full_name, "
                "role_title, persona, email, email_status, evidence_id, pii_basis, "
                "first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:16],
                    domain,
                    contact.full_name,
                    contact.role_title,
                    contact.persona or persona_for(contact.role_title),
                    contact.email,
                    contact.status,
                    evidence_id,
                    "public_business_contact",
                    now,
                ),
            )

        written = self._write_triggers(conn, domain, outcome)
        log.info(
            "enrich_stored",
            canonical_domain=domain,
            contacts=len(outcome.contacts),
            new_triggers=written,
            sources_failed=list(outcome.sources_failed),
        )
        # Always re-score: enrichment is the input the score was missing, so a company
        # enriched and not re-scored is the same lead it was before.
        return StageResult(
            ok=True,
            stage="enricher",
            job_id=job.job_id,
            follow_on=[(SCORE_KIND, {"canonical_domain": domain})],
            duration_ms=outcome.duration_ms,
        )

    async def run(self, job: Job) -> StageResult:
        outcome = await self.prepare(job)
        with self.store.tx() as conn:
            return self.commit(job, outcome, conn)

    def _write_triggers(
        self, conn: sqlite3.Connection, domain: str, outcome: EnrichOutcome
    ) -> list[str]:
        """Derived triggers, each joined to an evidence URL a human can open."""
        written: list[str] = []

        total, growth = outcome.subdomain_total, outcome.subdomain_growth
        if (
            total is not None
            and growth is not None
            and (total >= SPRAWL_TOTAL and growth >= SPRAWL_GROWTH)
        ):
            url = f"https://crt.sh/?q=%25.{domain}"
            evidence = _evidence(
                conn,
                url,
                "crtsh",
                f"{total} certificate names, {growth} new in {SPRAWL_WINDOW_DAYS}d",
            )
            written.append(_trigger(conn, domain, "T7_SURFACE_SPRAWL", evidence, url))

        if outcome.hygiene is not None:
            # The narrow set. `hygiene_gaps` is what the card shows; this is what is
            # strong enough to claim as a reason to call.
            gaps = mail_auth_weakness(outcome.hygiene)
            if gaps:
                # The evidence is the published record itself. Phrased as what they
                # publish, never as something we found wrong with them -- this string
                # can reach a lead card.
                url = f"https://dns.google/query?name={domain}&type=TXT"
                evidence = _evidence(conn, url, "dns_public", "; ".join(gaps)[:500])
                written.append(_trigger(conn, domain, "T8_HYGIENE_GAP", evidence, url))

        if outcome.hiring_triggers and outcome.hiring_evidence:
            evidence = _evidence(
                conn,
                outcome.hiring_evidence,
                "ats_board",
                f"public job board lists {', '.join(outcome.hiring_triggers)}",
            )
            for code in outcome.hiring_triggers:
                written.append(_trigger(conn, domain, code, evidence, outcome.hiring_evidence))
        return [t for t in written if t]


def _evidence(conn: sqlite3.Connection, url: str, source_id: str, snippet: str) -> str:
    evidence_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
        "content_sha256) VALUES (?,?,?,?,?,?)",
        (evidence_id, url, source_id, snippet[:500], to_iso(utcnow()), ""),
    )
    return evidence_id


def _trigger(conn: sqlite3.Connection, domain: str, code: str, evidence_id: str, url: str) -> str:
    """Insert or refresh, mirroring the Resolver so an hourly run does not stack rows."""
    now = utcnow()
    decay = TRIGGER_DECAY_DAYS.get(code, 60)
    fresh = conn.execute(
        "SELECT trigger_id FROM triggers WHERE canonical_domain = ? AND code = ? "
        "AND active = 1 AND decays_at > ? LIMIT 1",
        (domain, code, to_iso(now)),
    ).fetchone()
    if fresh is not None:
        trigger_id = str(fresh["trigger_id"])
        conn.execute(
            "UPDATE triggers SET observed_at = ?, decays_at = ? WHERE trigger_id = ?",
            (to_iso(now), to_iso(now + timedelta(days=decay)), trigger_id),
        )
    else:
        trigger_id = uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, observed_at, "
            "decays_at, rationale, active) VALUES (?,?,?,?,?,?,?,1)",
            (
                trigger_id,
                domain,
                code,
                0.8,  # a public record read directly, not a model's reading of a page
                to_iso(now),
                to_iso(now + timedelta(days=decay)),
                f"public record at {url}"[:280],
            ),
        )
    conn.execute(
        "INSERT OR IGNORE INTO trigger_evidence (trigger_id, evidence_id) VALUES (?,?)",
        (trigger_id, evidence_id),
    )
    return trigger_id


def enqueue_unenriched(
    store: Store, queue: Any, *, stale_after_days: int = 30, force: bool = False
) -> int:
    """Queue enrichment for companies never enriched, or enriched long ago.

    Reconciling rather than reacting, for the same reason scoring does: the Resolver
    only enqueues for companies it resolves *now*, which strands everything that
    existed before this stage did. Subdomain counts and DMARC policies also change,
    so a month-old enrichment is worth redoing.

    **`force` exists because improving the Enricher is half a change.** `enriched_at`
    records that we looked, not what we were capable of seeing at the time: teaching the
    stage to read `mailto:` attributes did nothing for the 201 companies already marked
    enriched, which would have kept `reachability = 0` until the 30-day sweep reached
    them. Same shape as `RETIREMENT_RULES` -- editing a rule leaves the rows the old one
    wrote, and something has to re-run it over them.
    """
    now = utcnow()
    params: list[Any] = []
    where = "1=1"
    if not force:
        where = "enriched_at IS NULL OR enriched_at < ?"
        params.append(to_iso(now - timedelta(days=stale_after_days)))
    rows = store.conn.execute(
        f"SELECT canonical_domain FROM companies WHERE {where} ORDER BY last_updated_at DESC",
        params,
    ).fetchall()

    queued = 0
    with store.tx() as conn:
        for row in rows:
            domain = str(row["canonical_domain"])
            # Day-bucketed so a re-run today is a no-op but tomorrow's is not. Under
            # `force` the bucket is the problem rather than the protection -- today's
            # completed job is exactly what would swallow the re-enrichment -- so a
            # timestamp nonce makes the key unmatchable, as `enqueue_stale_scores` does.
            key = f"enrich:{domain}:{now:%Y-%m-%d}"
            if force:
                # A uuid, not a timestamp. `to_iso` has millisecond resolution and two
                # forced runs inside the same millisecond would collide -- the second
                # silently swallowed by the first, which is the exact failure `--force`
                # exists to escape.
                key += f"|force:{uuid.uuid4().hex}"
            existing = conn.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ? LIMIT 1", (key,)
            ).fetchone()
            queue.enqueue(ENRICH_KIND, {"canonical_domain": domain}, dedupe_key=key, conn=conn)
            if existing is None:
                queued += 1
    return queued


def _is_security_txt(body: str) -> bool:
    """Does this actually parse as RFC 9116, or is it an app shell?

    `Contact:` is the one field RFC 9116 makes mandatory, so its absence is decisive.
    The HTML check is not redundant: a single-page app's shell could contain the word
    "contact:" in ordinary copy, and that must not be read as a published policy.
    """
    head = body.lstrip()[:2000].lower()
    if head.startswith("<!doctype") or head.startswith("<html") or "<body" in head:
        return False
    # RFC 9116 field names are case-insensitive, and real files write `Contact:`.
    return any(line.strip().lower().startswith("contact:") for line in body.splitlines()[:50])
