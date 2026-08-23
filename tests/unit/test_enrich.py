"""Enrichment: DNS hygiene, contact discovery, and the stage that ties them together.

No network and no resolver. The DNS probe is a stub returning canned records, egress
runs on `httpx.MockTransport`. What is pinned is judgement — which gaps are real, which
addresses are usable, and what a missing lookup is allowed to imply.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest

from cindraleads.agents.enricher import ENRICH_KIND, SCORE_KIND, Enricher, enqueue_unenriched
from cindraleads.contacts import (
    classify_email,
    emails_from_markup,
    extract_contacts,
    persona_for,
    security_txt_contact,
)
from cindraleads.dns_hygiene import DnsProbe, hygiene_gaps, lookup_hygiene
from cindraleads.models import DnsHygiene, Job, to_iso, utcnow
from cindraleads.queue import JobQueue
from cindraleads.sources import DocumentCache, EgressClient, SourceBreakers, SourceRegistry
from cindraleads.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"


class StubProbe(DnsProbe):
    """A resolver that answers from a dict. Nothing leaves the process."""

    def __init__(self, records: dict[tuple[str, str], list[str]]) -> None:
        super().__init__()
        self.records = records
        self.asked: list[tuple[str, str]] = []

    def query(self, name: str, record: str) -> list[str]:
        self.asked.append((name, record))
        return self.records.get((name, record), [])

    def dnssec_signed(self, domain: str) -> bool:
        return bool(self.query(domain, "DNSKEY"))


# ------------------------------------------------------------------------- DNS


async def test_a_well_configured_domain_has_no_gaps():
    probe = StubProbe(
        {
            ("acme.io", "MX"): ["10 mail.acme.io."],
            ("acme.io", "TXT"): ["v=spf1 include:_spf.google.com ~all"],
            ("_dmarc.acme.io", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@acme.io"],
            ("acme.io", "DNSKEY"): ["257 3 13 abc"],
        }
    )
    hygiene = await lookup_hygiene("acme.io", probe, security_txt=True)

    assert hygiene.mx_present is True
    assert hygiene.dmarc_policy == "reject"
    assert hygiene.dnssec is True
    assert hygiene_gaps(hygiene) == []


async def test_a_monitor_only_dmarc_is_a_gap():
    probe = StubProbe(
        {
            ("acme.io", "MX"): ["10 mail.acme.io."],
            ("acme.io", "TXT"): ["v=spf1 ~all"],
            ("_dmarc.acme.io", "TXT"): ["v=DMARC1; p=none"],
        }
    )
    gaps = hygiene_gaps(await lookup_hygiene("acme.io", probe, security_txt=False))
    assert "DMARC p=none (monitor only)" in gaps
    assert "DNSSEC not enabled" in gaps
    assert "no security.txt" in gaps


async def test_a_domain_with_no_mail_is_not_faulted_for_spf():
    """A domain that receives no mail has no reason to publish SPF. Reporting it as a
    gap would put a wrong claim on a card a human may act on."""
    probe = StubProbe({("acme.io", "DNSKEY"): ["257 3 13 abc"]})
    gaps = hygiene_gaps(await lookup_hygiene("acme.io", probe))
    assert not any("SPF" in gap for gap in gaps)
    assert not any("DMARC" in gap for gap in gaps)


def test_an_unreadable_lookup_produces_no_claim():
    """The difference between "unknown" and "absent".

    A resolver timeout must never become "this company publishes no SPF". Only
    non-None fields are allowed to contribute a gap.
    """
    unknown = DnsHygiene()  # every field None: nothing was learned
    assert hygiene_gaps(unknown) == []


async def test_a_missing_dnspython_reports_unknown_not_absent(monkeypatch):
    """The optional extra is not installed everywhere. A missing library must not look
    like a company with no SPF record."""
    monkeypatch.setattr("cindraleads.dns_hygiene.dnspython_available", lambda: False)
    hygiene = await lookup_hygiene("acme.io", security_txt=None)
    assert hygiene.mx_present is None
    assert hygiene.spf is None
    assert hygiene_gaps(hygiene) == []


async def test_the_dns_lookup_never_contacts_the_domain():
    """Every query goes to a resolver, about the domain. None goes *to* it."""
    probe = StubProbe({})
    await lookup_hygiene("acme.io", probe)
    assert probe.asked, "it did ask something"
    for name, record in probe.asked:
        assert record in {"MX", "TXT", "DNSKEY"}, f"{record} is not a public record lookup"
        assert name.endswith("acme.io")


# -------------------------------------------------------------------- contacts


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("nabila@acme.io", "verified"),
        ("hello@acme.io", "role_account"),
        ("someone@gmail.com", "risky"),
        ("throwaway@mailinator.com", "risky"),
        ("person@othercompany.com", "unverified"),
    ],
)
def test_email_classification(email, expected):
    assert classify_email(email, domain_has_mx=True, company_domain="acme.io") == expected


def test_an_unknown_mx_is_not_treated_as_a_bad_address():
    """A resolver we could not reach is our problem, not evidence about the address."""
    assert classify_email("cto@acme.io", domain_has_mx=None, company_domain="acme.io") == (
        "unverified"
    )
    assert classify_email("cto@acme.io", domain_has_mx=False, company_domain="acme.io") == "risky"


def test_no_smtp_is_reachable_from_the_contacts_module():
    """Section 12 forbids VRFY and RCPT probing. The strongest form of that guarantee
    is that this module has no way to open a connection at all.

    Checks imports rather than raw text: the docstring names VRFY and RCPT precisely
    to say they are forbidden, and grepping for the words would fail on the comment
    that documents the rule.
    """
    import ast

    import cindraleads.contacts as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("smtplib", "socket", "ssl", "asyncio", "httpx", "subprocess"):
        assert forbidden not in imported, f"contacts.py imports {forbidden}"


def test_a_personal_address_is_never_collected():
    page = "Reach the founder at hank@gmail.com or the team at hello@acme.io"
    found = extract_contacts(
        page, source_url="https://acme.io/", company_domain="acme.io", domain_has_mx=True
    )
    assert [c.email for c in found] == ["hello@acme.io"]


def test_a_named_mailbox_outranks_a_role_account():
    page = "info@acme.io · nabila@acme.io · no-reply@acme.io"
    found = extract_contacts(
        page, source_url="https://acme.io/", company_domain="acme.io", domain_has_mx=True
    )
    assert found[0].email == "nabila@acme.io"


def test_an_obfuscated_address_is_left_alone():
    """ "hello [at] acme.io" is a request not to be harvested. Honouring it costs one
    contact and keeps a promise."""
    page = "Write to hello [at] acme.io"
    assert (
        extract_contacts(
            page, source_url="https://acme.io/", company_domain="acme.io", domain_has_mx=True
        )
        == []
    )


def test_contacts_are_capped():
    page = " ".join(f"person{i}@acme.io" for i in range(20))
    found = extract_contacts(
        page, source_url="https://acme.io/", company_domain="acme.io", domain_has_mx=True
    )
    assert len(found) == 3, "data minimisation: not every address on the page"


@pytest.mark.parametrize(
    ("title", "persona"),
    [
        ("Co-Founder & CTO", "founder_cto"),
        ("VP of Engineering", "head_eng"),
        ("Head of Compliance", "compliance"),
        ("ML Lead", "ai_lead"),
        ("Office Manager", "generic"),
    ],
)
def test_persona_mapping(title, persona):
    assert persona_for(title) == persona


# ---------------------------------------------------------------- the stage


@pytest.fixture
def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "e.db", migrations_dir=MIGRATIONS)
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, ai_surface, tech_signals, "
            "first_seen_at, last_updated_at) VALUES ('acme.io','Acme','[]','[]',?,?)",
            ("2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z"),
        )

    registry = SourceRegistry.from_dict(
        {
            "sources": [
                {"id": "company_site", "legality_class": "public_web"},
                {"id": "crtsh", "legality_class": "public_record"},
                {"id": "rdap", "legality_class": "public_record"},
                {"id": "greenhouse_boards", "legality_class": "public_web"},
                {"id": "lever_postings", "legality_class": "public_web"},
                {"id": "ashby_postings", "legality_class": "public_web"},
            ],
            "defaults": {"retries": 1, "backoff_base_seconds": 0.001},
            "public_web_policy": {"min_interval_seconds": 0.0, "respect_robots": False},
        }
    )

    def build(page: str = "Contact hello@acme.io", probe: DnsProbe | None = None, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "crt.sh" in url:
                return httpx.Response(200, json=[])
            if "rdap" in url:
                return httpx.Response(404, text="{}")
            if "greenhouse" in url or "lever" in url or "ashby" in url:
                return httpx.Response(404, text="{}")
            return httpx.Response(200, text=f"<html><body>{page}</body></html>")

        egress = EgressClient(
            store=store,
            registry=registry,
            cache=DocumentCache(store, cache_dir=tmp_path / "cache"),
            breakers=SourceBreakers(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return Enricher(store=store, egress=egress, dns=probe, **kwargs)

    yield build, store
    store.close()


def job() -> Job:
    return Job(job_id="j", kind=ENRICH_KIND, payload={"canonical_domain": "acme.io"})


async def test_enrichment_finds_a_contact_and_marks_the_company(rig):
    build, store = rig
    enricher = build(probe=StubProbe({("acme.io", "MX"): ["10 mx.acme.io."]}))

    result = await enricher.run(job())

    assert result.ok
    contact = store.conn.execute("SELECT * FROM contacts").fetchone()
    assert contact["email"] == "hello@acme.io"
    assert contact["email_status"] == "role_account"
    assert contact["pii_basis"] == "public_business_contact"
    company = store.conn.execute("SELECT enriched_at FROM companies").fetchone()
    assert company["enriched_at"] is not None
    await enricher.egress.aclose()


async def test_enrichment_queues_a_rescore(rig):
    """Enrichment is the input the score was missing. A company enriched and not
    re-scored is the same lead it was before."""
    build, _store = rig
    enricher = build()
    result = await enricher.run(job())
    assert [kind for kind, _ in result.follow_on] == [SCORE_KIND]
    await enricher.egress.aclose()


async def test_a_dns_gap_becomes_an_evidenced_trigger(rig):
    build, store = rig
    enricher = build(
        probe=StubProbe(
            {("acme.io", "MX"): ["10 mx.acme.io."], ("_dmarc.acme.io", "TXT"): ["v=DMARC1; p=none"]}
        )
    )

    await enricher.run(job())

    trigger = store.conn.execute("SELECT * FROM triggers WHERE code='T8_HYGIENE_GAP'").fetchone()
    assert trigger is not None
    joined = store.conn.execute(
        "SELECT e.url, e.snippet FROM evidence e JOIN trigger_evidence te "
        "ON te.evidence_id = e.evidence_id WHERE te.trigger_id = ?",
        (trigger["trigger_id"],),
    ).fetchone()
    assert joined is not None, "a trigger with no evidence is not a trigger"
    assert "DMARC p=none" in joined["snippet"]
    await enricher.egress.aclose()


async def test_a_clean_domain_gets_no_hygiene_trigger(rig):
    build, store = rig
    enricher = build(
        probe=StubProbe(
            {
                ("acme.io", "MX"): ["10 mx.acme.io."],
                ("acme.io", "TXT"): ["v=spf1 ~all"],
                ("_dmarc.acme.io", "TXT"): ["v=DMARC1; p=reject"],
                ("acme.io", "DNSKEY"): ["257 3 13 x"],
            }
        )
    )
    await enricher.run(job())
    assert (
        store.conn.execute("SELECT * FROM triggers WHERE code='T8_HYGIENE_GAP'").fetchone() is None
    )
    await enricher.egress.aclose()


async def test_a_dead_source_does_not_fail_the_company(rig):
    """crt.sh being down costs the subdomain count and nothing else."""
    build, store = rig

    def exploding(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("crt.sh is down")

    enricher = build(probe=StubProbe({}))
    enricher.egress.client = httpx.AsyncClient(transport=httpx.MockTransport(exploding))

    result = await enricher.run(job())

    assert result.ok, "the company is still enriched from whatever answered"
    assert store.conn.execute("SELECT enriched_at FROM companies").fetchone()["enriched_at"]
    await enricher.egress.aclose()


async def test_enrichment_lifts_the_score_out_of_reject(rig):
    """The whole point of Phase 4.

    Measured before it existed: a company with three live triggers, full ICP fit and a
    shipped AI surface scored 52 (Tier C) because reachability and surface were
    structurally zero. This asserts the components are now reachable.
    """
    from cindraleads.agents.scorer import Scorer
    from cindraleads.config import settings

    build, store = rig
    cfg = settings()
    object.__setattr__(cfg, "config_dir", REPO_ROOT / "config")
    object.__setattr__(cfg, "prompt_dir", REPO_ROOT / "prompts")

    with store.tx() as conn:
        conn.execute(
            "UPDATE companies SET employee_band='11-50', industry='healthtech', country='BD', "
            "ai_surface='[\"agent_with_tools\"]'"
        )
        for i, code in enumerate(("T1_AI_SHIP", "T4_HIRING_AI_ONLY", "T2_FUNDING")):
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,?,?,?,?)",
                (f"e{i}", f"https://acme.io/p{i}", f"src{i}", "s", "2026-08-15T00:00:00Z", "h"),
            )
            conn.execute(
                "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
                "observed_at, decays_at) VALUES (?,?,?,?,?,?)",
                (f"t{i}", "acme.io", code, 0.9, "2026-08-15T00:00:00Z", "2099-01-01T00:00:00Z"),
            )
            conn.execute("INSERT INTO trigger_evidence VALUES (?,?)", (f"t{i}", f"e{i}"))

    scorer = Scorer(store=store, llm=None, config=cfg)
    before = await scorer.run(
        Job(job_id="s1", kind="score.company", payload={"canonical_domain": "acme.io"})
    )
    assert before.ok
    unenriched = store.conn.execute("SELECT score, tier FROM leads").fetchone()

    enricher = build(
        page="Reach our CTO at nabila@acme.io",
        probe=StubProbe({("acme.io", "MX"): ["10 mx.acme.io."]}),
    )
    await enricher.run(job())
    await scorer.run(
        Job(job_id="s2", kind="score.company", payload={"canonical_domain": "acme.io"})
    )
    enriched = store.conn.execute("SELECT score, tier, score_breakdown FROM leads").fetchone()

    assert enriched["score"] > unenriched["score"], (
        f"enrichment did not raise the score: {unenriched['score']} -> {enriched['score']}"
    )
    assert json.loads(enriched["score_breakdown"])["reachability"] > 0
    assert enriched["tier"] in ("A", "B"), f"still {enriched['tier']} at {enriched['score']}"
    await enricher.egress.aclose()


# --------------------------------------------------------------- reconciliation


def test_a_company_enriched_before_the_stage_existed_is_queued(rig):
    _build, store = rig
    assert enqueue_unenriched(store, JobQueue(store)) == 1


def test_reconciling_twice_queues_one_job(rig):
    _build, store = rig
    queue = JobQueue(store)
    assert (enqueue_unenriched(store, queue), enqueue_unenriched(store, queue)) == (1, 0)


def test_a_freshly_enriched_company_is_left_alone(rig):
    _build, store = rig
    with store.tx() as conn:
        conn.execute("UPDATE companies SET enriched_at = ?", ("2099-01-01T00:00:00Z",))
    assert enqueue_unenriched(store, JobQueue(store)) == 0


def test_a_stale_enrichment_is_redone(rig):
    """Subdomain counts and DMARC policies change. A month-old reading is a guess."""
    _build, store = rig
    with store.tx() as conn:
        conn.execute("UPDATE companies SET enriched_at = ?", ("2020-01-01T00:00:00Z",))
    assert enqueue_unenriched(store, JobQueue(store)) == 1


# ------------------------------------------------- what T8 is allowed to claim


def test_the_internet_default_is_not_a_trigger():
    """Measured on the first real run: T8_HYGIENE_GAP fired on 85 of 93 companies.

    Absent DNSSEC and absent security.txt were being counted as gaps, and both are the
    *default* state of the internet -- global DNSSEC adoption is a few percent. A
    trigger that fires on everybody ranks nobody, and this one became the headline on
    every card, burying the AI-launch signal that was the actual reason to call.
    """
    from cindraleads.dns_hygiene import mail_auth_weakness

    typical = DnsHygiene(
        mx_present=True,
        spf="v=spf1 include:_spf.google.com ~all",
        dmarc_policy="reject",
        dnssec=False,  # true of most of the internet
        security_txt=False,  # true of almost all of it
    )
    assert mail_auth_weakness(typical) == [], "a normal domain must not trigger"
    assert hygiene_gaps(typical), "but it is still worth showing on the card"


@pytest.mark.parametrize(
    ("hygiene", "expected"),
    [
        (DnsHygiene(mx_present=True, dmarc_policy="reject"), ["no SPF record published"]),
        (DnsHygiene(mx_present=True, spf="v=spf1 ~all"), ["no DMARC record published"]),
        (
            DnsHygiene(mx_present=True, spf="v=spf1 ~all", dmarc_policy="none"),
            ["DMARC p=none (monitor only)"],
        ),
    ],
)
def test_real_mail_auth_weakness_still_triggers(hygiene, expected):
    """The specific, unusual, actionable cases survive the narrowing."""
    from cindraleads.dns_hygiene import mail_auth_weakness

    assert mail_auth_weakness(hygiene) == expected


def test_a_domain_with_no_mail_never_triggers():
    from cindraleads.dns_hygiene import mail_auth_weakness

    assert mail_auth_weakness(DnsHygiene(mx_present=False, dnssec=False)) == []


async def test_a_typical_domain_gets_no_hygiene_trigger(rig):
    """The end-to-end version: normal mail setup, no DNSSEC, no security.txt."""
    build, store = rig
    enricher = build(
        probe=StubProbe(
            {
                ("acme.io", "MX"): ["10 mx.acme.io."],
                ("acme.io", "TXT"): ["v=spf1 ~all"],
                ("_dmarc.acme.io", "TXT"): ["v=DMARC1; p=quarantine"],
            }
        )
    )
    await enricher.run(job())
    assert (
        store.conn.execute("SELECT * FROM triggers WHERE code='T8_HYGIENE_GAP'").fetchone() is None
    )
    await enricher.egress.aclose()


# ------------------------------------------------- single-page apps lie about paths


def _spa_rig(tmp_path: Path, store, registry, shell: str):
    """A site that answers every route with the same HTML shell.

    Not hypothetical: partisanboost.com returned an identical 32,091 bytes for `/`,
    `/about`, `/contact`, `/team` and `/.well-known/security.txt` on the first real run.
    """
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "crt.sh" in url or "rdap" in url:
            return httpx.Response(404, text="{}")
        if "greenhouse" in url or "lever" in url or "ashby" in url:
            return httpx.Response(404, text="{}")
        fetched.append(request.url.path)
        return httpx.Response(200, text=shell)

    egress = EgressClient(
        store=store,
        registry=registry,
        cache=DocumentCache(store, cache_dir=tmp_path / "spa-cache"),
        breakers=SourceBreakers(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return Enricher(store=store, egress=egress, enabled_sources=frozenset({"site"})), fetched


async def test_an_spa_does_not_burn_the_domain_budget_on_mirrors(rig, tmp_path: Path):
    """Six requests a day per domain. Spending four of them re-reading one page is
    budget a real prospect's distinct pages need."""
    build, store = rig
    enricher = build()
    shell = "<html><body><div id=root>Loading…</div></body></html>"
    spa, fetched = _spa_rig(tmp_path, store, enricher.egress.registry, shell)

    await spa.prepare(job())

    content_paths = [p for p in fetched if not p.startswith("/.well-known")]
    assert len(content_paths) == 2, (
        f"stopped after one repeat, got {content_paths} -- an SPA must not cost four requests"
    )


async def test_an_spa_is_not_recorded_as_publishing_a_security_txt(rig, tmp_path: Path):
    """The correctness half. A 200 carrying an HTML shell was recorded as
    `security_txt = True`, putting a false published-policy claim about the company
    into `hygiene_gaps` -- where it can reach a lead card."""
    build, store = rig
    enricher = build()
    shell = "<!doctype html><html><body>app</body></html>"
    spa, _ = _spa_rig(tmp_path, store, enricher.egress.registry, shell)

    outcome = await spa.prepare(job())

    assert outcome.hygiene is None or outcome.hygiene.security_txt is not True


async def test_a_real_security_txt_is_still_recognised(rig, tmp_path: Path):
    build, store = rig
    enricher = build()
    body = "Contact: mailto:security@acme.io\nExpires: 2027-01-01T00:00:00.000Z\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/.well-known"):
            return httpx.Response(200, text=body)
        return httpx.Response(200, text=f"<html><body>page {request.url.path}</body></html>")

    egress = EgressClient(
        store=store,
        registry=enricher.egress.registry,
        cache=DocumentCache(store, cache_dir=tmp_path / "sec-cache"),
        breakers=SourceBreakers(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    site = Enricher(store=store, egress=egress, enabled_sources=frozenset({"site"}))

    found = await site._site("acme.io")
    assert found.security_txt is True
    # The `Contact:` line comes back with it. We were paying for this fetch and reading
    # one boolean out of the body -- RFC 9116 makes the field mandatory, so on any real
    # security.txt it is a free address the company nominated for this exact subject.
    assert "security@acme.io" in found.emails


def test_the_security_txt_validator_tells_a_policy_from_a_page():
    from cindraleads.agents.enricher import _is_security_txt

    assert _is_security_txt("Contact: mailto:s@acme.io\nExpires: 2027-01-01T00:00:00Z\n")
    assert _is_security_txt("# comment\nContact: https://acme.io/report\n")

    assert not _is_security_txt("<!doctype html><html><body>Contact: us!</body></html>")
    assert not _is_security_txt("<html><head></head><body>contact: sales</body></html>")
    # No Contact field at all: RFC 9116 makes it mandatory, so absence is decisive.
    assert not _is_security_txt("Expires: 2027-01-01T00:00:00Z\n")
    assert not _is_security_txt("")


# ------------------------------------------- where the contacts actually were


def test_an_address_behind_a_button_is_found():
    """The 89% miss, in one line of HTML.

    `extract_text` keeps what a visitor sees and drops attributes. A contact page whose
    address is a "Get in touch" button publishes the address in the `href` and shows
    none of it as prose -- so every such company came back with no contact at all, and
    `reachability` scored zero on 173 of 195 leads.
    """
    html = '<a class="btn" href="mailto:hello@acme.io">Get in touch</a>'

    assert emails_from_markup(html) == ["hello@acme.io"]


def test_mailto_query_parameters_are_not_part_of_the_address():
    html = '<a href="mailto:sales@acme.io?subject=Hi%20there&cc=x@acme.io">Email</a>'

    assert emails_from_markup(html) == ["sales@acme.io"]


def test_an_obfuscated_address_is_still_left_alone():
    """The politeness rule does not bend for this change. Writing `hello [at] acme.io`
    is a request not to be harvested; a `mailto:` link is the opposite -- the company
    made it clickable. Only the second is collected."""
    html = "<p>Write to hello [at] acme.io or hello (at) acme.io</p>"

    assert emails_from_markup(html) == []
    assert extract_contacts(html, source_url="u", company_domain="acme.io") == []


def test_the_same_address_in_markup_and_prose_is_one_contact():
    found = extract_contacts(
        "Reach us at hello@acme.io",
        source_url="https://acme.io/",
        company_domain="acme.io",
        domain_has_mx=True,
        extra_emails=["hello@acme.io"],
    )

    assert [c.email for c in found] == ["hello@acme.io"]


def test_an_address_from_markup_faces_the_same_compliance_rules():
    """One code path decides which addresses may reach a card, because "may we contact
    this" is a compliance question and must not have two answers. A free-mail address in
    a `mailto:` is still a person, and a foreign domain is still not this company."""
    found = extract_contacts(
        "",
        source_url="https://acme.io/",
        company_domain="acme.io",
        domain_has_mx=True,
        extra_emails=["founder@gmail.com", "hi@someoneelse.io", "hello@acme.io"],
    )

    assert [c.email for c in found] == ["hello@acme.io"]


def test_the_security_txt_contact_is_read():
    body = (
        "# our policy\n"
        "Contact: mailto:security@acme.io\n"
        "Expires: 2027-01-01T00:00:00Z\n"
        "Contact: https://acme.io/report\n"
    )

    assert security_txt_contact(body) == "security@acme.io"


def test_a_security_txt_without_an_email_contact_yields_nothing():
    """`Contact:` may be a URL, and RFC 9116 allows that. A form is not an address."""
    assert security_txt_contact("Contact: https://acme.io/report\n") is None
    assert security_txt_contact("") is None


def test_forced_reconciliation_re_enriches_a_company_already_enriched(store, tmp_path):
    """Improving the Enricher is half a change; the other half is re-running it.

    `enriched_at` records that we looked, not what we were able to see. Teaching the
    stage to read `mailto:` attributes did nothing for the 201 companies already marked
    enriched -- they would have kept `reachability = 0` until the 30-day sweep reached
    them. Same shape as `RETIREMENT_RULES`.
    """
    queue = JobQueue(store)
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, enriched_at, "
            "first_seen_at, last_updated_at) VALUES ('acme.io','Acme',?,?,?)",
            (now, now, now),
        )

    assert enqueue_unenriched(store, queue) == 0, "freshly enriched, nothing to do"
    assert enqueue_unenriched(store, queue, force=True) == 1


def test_forcing_twice_in_a_day_is_not_swallowed_by_the_dedupe_bucket(store):
    """The key is day-bucketed, so under `--force` today's completed job is precisely
    what would block the re-enrichment. A nonce makes it unmatchable, as scoring does."""
    queue = JobQueue(store)
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, enriched_at, "
            "first_seen_at, last_updated_at) VALUES ('acme.io','Acme',?,?,?)",
            (now, now, now),
        )

    assert enqueue_unenriched(store, queue, force=True) == 1
    assert enqueue_unenriched(store, queue, force=True) == 1


def test_an_unforced_reconcile_still_skips_a_fresh_company(store):
    """`--force` is the override, not the behaviour. Re-enrichment spends real fetches
    against the 6-per-domain budget, so the nightly pass must stay conservative."""
    queue = JobQueue(store)
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, enriched_at, "
            "first_seen_at, last_updated_at) VALUES ('acme.io','Acme',?,?,?)",
            (now, now, now),
        )

    assert enqueue_unenriched(store, queue) == 0


def test_re_enriching_does_not_duplicate_a_contact(store):
    """`INSERT OR IGNORE` read as dedupe protection and provided none.

    The primary key is a fresh `uuid4()`, so it can never collide, and until migration
    0007 nothing constrained `(canonical_domain, email)`. Every re-enrichment therefore
    appended another copy of the same address -- and `cindra reconcile --force` exists
    to re-enrich deliberately.

    Invisible to every report: reachability reads the best contact by status, and a
    duplicate is not better than its original, so no score moved. It surfaced the first
    time a human pulled a call list to actually email people, by which point GAIA
    appeared 12 times and xalgorix 9.
    """
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, ai_surface, "
            "tech_signals, first_seen_at, last_updated_at) "
            "VALUES ('acme.io','Acme','[]','[]',?,?)",
            ("2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z"),
        )
        for _ in range(3):
            conn.execute(
                "INSERT OR IGNORE INTO contacts (contact_id, canonical_domain, email, "
                "email_status, pii_basis, first_seen_at) VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], "acme.io", "hello@acme.io", "verified", "x", "t"),
            )

    rows = store.conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE canonical_domain = 'acme.io'"
    ).fetchone()
    assert rows["n"] == 1, "three enrichments of the same address are one contact"


def test_two_people_without_an_address_are_still_two_people(store):
    """The bound on the constraint. A NULL email means a name-only contact, and merging
    those would lose genuinely different people -- a worse outcome than the duplication
    being fixed. SQLite treats NULLs as distinct in a UNIQUE index, which is the
    behaviour relied on here rather than an accident."""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, ai_surface, "
            "tech_signals, first_seen_at, last_updated_at) "
            "VALUES ('acme.io','Acme','[]','[]',?,?)",
            ("2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z"),
        )
        for name in ("Nabila R.", "Arif H."):
            conn.execute(
                "INSERT OR IGNORE INTO contacts (contact_id, canonical_domain, full_name, "
                "email_status, pii_basis, first_seen_at) VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], "acme.io", name, "none", "x", "t"),
            )

    rows = store.conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE canonical_domain = 'acme.io'"
    ).fetchone()
    assert rows["n"] == 2
