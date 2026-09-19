"""One test per section 12 rule. CI fails if a rule has none.

The last test in this file is the enforcement: it walks `compliance.RULES` and asserts
every entry is named by a test in this module. A rule added without a test is a rule
nobody has checked the behaviour of, in the module that protects the business.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from cindraleads.compliance import RULES, ComplianceGate, LeadFacts
from cindraleads.passive import (
    FORBIDDEN_BINARIES,
    PassiveOnlyViolation,
    assert_command_allowed,
    is_forbidden_command,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"


def facts(**kwargs) -> LeadFacts:  # type: ignore[no-untyped-def]
    base = {
        "canonical_domain": "acme.io",
        "display_name": "Acme Health",
        "trigger_codes": ("T1_AI_SHIP",),
        "evidence_urls": ("https://acme.io/",),
    }
    return LeadFacts(**{**base, **kwargs})


@pytest.fixture
def gate() -> ComplianceGate:
    return ComplianceGate(excluded_sectors=("government", "security vendor"), max_employees=1000)


def test_a_clean_lead_passes(gate: ComplianceGate):
    verdict = gate.review(facts())
    assert verdict.passed
    assert verdict.vetoes == []
    assert verdict.basis == "legitimate_interest_b2b"


# ------------------------------------------------------- one test per rule


def test_rule_has_evidence(gate: ComplianceGate):
    """No evidence, no lead. The rule the whole project rests on."""
    verdict = gate.review(facts(evidence_urls=()))
    assert not verdict.passed
    assert "has_evidence" in verdict.vetoes


def test_rule_has_trigger(gate: ComplianceGate):
    """Fit alone is noise; a dated trigger is the product."""
    verdict = gate.review(facts(trigger_codes=()))
    assert "has_trigger" in verdict.vetoes


def test_rule_not_suppressed(gate: ComplianceGate):
    assert "not_suppressed" in gate.review(facts(suppressed=True)).vetoes


def test_rule_is_business_not_individual(gate: ComplianceGate):
    """B2B only. A person with no business affiliation is not a prospect."""
    assert "is_business_not_individual" in gate.review(facts(has_business_affiliation=False)).vetoes


def test_rule_no_personal_email(gate: ComplianceGate):
    """Business contacts only, never personal addresses."""
    assert "no_personal_email" in gate.review(facts(contact_emails=("someone@gmail.com",))).vetoes
    assert gate.review(facts(contact_emails=("cto@acme.io",))).passed


def test_rule_under_employee_ceiling(gate: ComplianceGate):
    """An enterprise with a named CISO and an in-house red team is a competitor's
    customer, not ours."""
    assert "under_employee_ceiling" in gate.review(facts(employee_band="1000+")).vetoes
    assert gate.review(facts(employee_band="51-200")).passed


def test_an_unknown_headcount_does_not_veto(gate: ComplianceGate):
    """Silence is not evidence of size. Vetoing on a missing field would reject every
    company with a terse landing page."""
    assert gate.review(facts(employee_band=None)).passed


def test_rule_not_government_or_cni(gate: ComplianceGate):
    for industry in ("government services", "defence contractor", "nuclear power grid"):
        assert "not_government_or_cni" in gate.review(facts(industry=industry)).vetoes


def test_a_government_domain_is_vetoed_whatever_the_page_calls_itself(gate: ComplianceGate):
    """The word match reads `industry` and `display_name` -- text a 4B model wrote from
    a web page. For a hard exclude that is the wrong evidence: whether we may contact a
    public body must not depend on the extractor choosing to type "government" into a
    field it often leaves empty.

    `nyc.gov` arrived from the HN hiring thread as "New York City Public Interest Tech",
    which contains none of the words the rule looks for. The TLD is reserved by registry
    policy, so it is a fact about the organisation rather than a guess about it.
    """
    for domain in ("nyc.gov", "defense.gov", "army.mil", "hmrc.gov.uk", "dhaka.gov.bd"):
        verdict = gate.review(facts(canonical_domain=domain, display_name="Innovation Lab"))
        assert "not_government_or_cni" in verdict.vetoes, domain


def test_a_company_domain_that_merely_contains_gov_is_not_vetoed(gate: ComplianceGate):
    """The bound. Suffix matching, not substring -- `govinda.io` and `datagov.com` are
    ordinary companies, and a rule that vetoed them would silently shrink the corpus."""
    for domain in ("govinda.io", "datagov.com", "governor.app", "milton.io"):
        assert gate.review(facts(canonical_domain=domain)).passed, domain


def test_rule_not_a_competitor(gate: ComplianceGate):
    for name in ("Redteam Security Ltd", "Acme Penetration Testing", "Managed Security Co"):
        assert "not_a_competitor" in gate.review(facts(display_name=name)).vetoes


def test_rule_not_an_excluded_sector(gate: ComplianceGate):
    assert "not_an_excluded_sector" in gate.review(facts(industry="government")).vetoes


def test_rule_has_canonical_domain(gate: ComplianceGate):
    assert "has_canonical_domain" in gate.review(facts(canonical_domain="")).vetoes


# ------------------------------------------------------------- gate behaviour


def test_every_rule_is_evaluated_not_short_circuited(gate: ComplianceGate):
    """A lead failing three rules records three.

    Short-circuiting on the first veto would make the reasons useless for tuning the
    ICP — you would only ever learn about the first thing wrong with a prospect.
    """
    verdict = gate.review(
        facts(evidence_urls=(), trigger_codes=(), display_name="Gov Pentest Agency")
    )
    assert len(verdict.vetoes) >= 3
    assert set(verdict.checks) == set(RULES), "every rule reports, pass or fail"


def test_a_veto_is_recorded_never_silently_dropped(tmp_path: Path, gate: ComplianceGate):
    from cindraleads.store import Store

    store = Store(tmp_path / "c.db", migrations_dir=MIGRATIONS)
    store.migrate()
    verdict = gate.review(facts(evidence_urls=()))
    with store.tx() as conn:
        ComplianceGate.quarantine(conn, subject_id="lead1", verdict=verdict)

    row = store.conn.execute("SELECT * FROM quarantine").fetchone()
    assert row["subject_id"] == "lead1"
    assert "has_evidence" in row["reason_code"]
    store.close()


def test_the_suppression_list_is_consulted(tmp_path: Path, gate: ComplianceGate):
    from cindraleads.store import Store

    store = Store(tmp_path / "s.db", migrations_dir=MIGRATIONS)
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO suppression_list (entry_id, kind, value, reason, created_at) "
            "VALUES ('1','domain','acme.io','asked us not to','2026-08-15T00:00:00Z')"
        )
        gate.load_suppression(conn)

    assert "not_suppressed" in gate.review(facts()).vetoes
    assert gate.review(facts(canonical_domain="other.io")).passed
    store.close()


def test_the_shipped_icp_config_loads():
    from cindraleads.config import settings

    cfg = settings()
    object.__setattr__(cfg, "config_dir", REPO_ROOT / "config")
    shipped = ComplianceGate.from_config(cfg)
    assert shipped.excluded_sectors
    assert shipped.max_employees == 1000


# ---------------------------------------------------------------- passive only


@pytest.mark.parametrize(
    "command",
    [
        "nmap -sV acme.io",
        "/usr/bin/nmap acme.io",
        "./masscan -p1-65535 acme.io",
        "nuclei -u https://acme.io",
        "nikto -h acme.io",
        "sqlmap -u https://acme.io/?id=1",
        "gobuster dir -u https://acme.io -w words.txt",
        "ffuf -u https://acme.io/FUZZ -w words.txt",
        "hydra -l admin -P rockyou.txt acme.io ssh",
        "amass enum -d acme.io",
        "sh -c 'nmap acme.io'",
        "xargs nmap",
    ],
)
def test_every_forbidden_action_raises(command: str):
    """Section 12: each forbidden category must be impossible to express."""
    with pytest.raises(PassiveOnlyViolation):
        assert_command_allowed(command)


@pytest.mark.parametrize(
    "command",
    [
        "curl https://acme.io/",
        "dig +short MX acme.io",
        "git status",
        "python -m pytest",
    ],
)
def test_a_public_record_lookup_is_allowed(command: str):
    """DNS, CT logs, RDAP and a plain HTTPS GET are the whole allowed set."""
    assert_command_allowed(command)


def test_an_smtp_probe_is_forbidden():
    """Email verification must never issue VRFY or RCPT against a prospect."""
    assert is_forbidden_command("swaks --quit-after RCPT TO acme.io") is not None


def test_the_denylist_is_non_empty_and_consulted():
    """PLAN.md Phase 4: a denylist nobody checks is documentation, not enforcement."""
    assert len(FORBIDDEN_BINARIES) >= 30
    assert is_forbidden_command("nmap acme.io") is not None
    assert is_forbidden_command("") is None


def test_no_stage_shells_out_at_all():
    """The strongest form of the guarantee: there is nothing to guard yet.

    If a `subprocess` call ever appears, this fails and whoever added it has to route
    it through `assert_command_allowed` deliberately.

    Checks imports rather than raw text, for the same reason the no-SMTP test does:
    the modules that document this rule have to be able to name `subprocess` in a
    comment without tripping the test that enforces it.
    """
    import ast

    src = REPO_ROOT / "src" / "cindraleads"
    allowed = {"passive.py", "thermal.py"}
    offenders: list[Path] = []
    for path in src.rglob("*.py"):
        if path.name in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            if "subprocess" in names:
                offenders.append(path.relative_to(REPO_ROOT))
                break

    assert not offenders, f"these modules shell out without the passive-only guard: {offenders}"


# ------------------------------------------------------------ the meta-rule


def test_every_rule_has_a_test():
    """CI fails on a missing compliance test. This is that check.

    A rule added to `RULES` without a `test_rule_<name>` here is a rule whose
    behaviour nobody has pinned, in the one module where being wrong is a legal
    problem rather than a bug.
    """
    module = inspect.getmodule(test_every_rule_has_a_test)
    assert module is not None
    tested = {name[len("test_rule_") :] for name in dir(module) if name.startswith("test_rule_")}
    missing = set(RULES) - tested
    assert not missing, f"compliance rules with no test: {sorted(missing)}"


def test_a_university_is_vetoed_whatever_the_page_calls_itself(gate: ComplianceGate):
    """`github_orgs` finds universities by design, and the first 29 companies proved it.

    The location search asks who is *there*, and what is there includes the computer
    science department. `iutoic-dhaka.edu` arrived as "Department of Computer Science
    and Engineering" -- no word in the rule matches that, which is why the suffix is
    checked first, exactly as it is for `nyc.gov`.
    """
    for domain in ("iutoic-dhaka.edu", "mbstu.ac.bd", "mit.edu", "ox.ac.uk", "iitb.ac.in"):
        verdict = gate.review(facts(canonical_domain=domain, display_name="Research Group"))
        assert "not_academic" in verdict.vetoes, domain


def test_rule_not_academic(gate: ComplianceGate):
    """A school on a `.com` is still a school. The suffix is the strong evidence, not
    the only one."""
    for name in ("Mawlana Bhashani Science and Technology University", "Dhaka City College"):
        verdict = gate.review(facts(canonical_domain="example.com", display_name=name))
        assert "not_academic" in verdict.vetoes, name


def test_a_company_whose_domain_merely_contains_edu_is_not_vetoed(gate: ComplianceGate):
    """The bound, and the reason this is a suffix match.

    Edtech is a real prospect and several are in the corpus -- a rule that vetoed every
    domain containing "edu" would delete a sector, silently, which is the one thing a
    hard exclude must never do.
    """
    for domain in ("eduflow.com", "edutech.io", "stacklearner.com", "educative.io"):
        verdict = gate.review(facts(canonical_domain=domain, display_name="Course Platform"))
        assert "not_academic" not in verdict.vetoes, domain


def test_the_nonprofits_the_academic_rule_deliberately_misses(gate: ComplianceGate):
    """Documents a known gap rather than pretending it is closed.

    `arced.foundation` and a student rover team at `bracu-mongoltori.com` came from the
    same batch and are equally out of ICP, and no safe general rule reaches them: bare
    "foundation" would veto a "Foundation Health", which is primary ICP -- the trap that
    killed bare "media" in the publication list and the name-does-not-match-domain rule
    that `Rover · rtrvr.ai` killed.

    If this test ever starts failing because a rule was added, check that the rule
    cannot also veto a healthtech company before deleting it.
    """
    known_gaps = (
        ("arced.foundation", "ARCED Foundation"),
        ("bracu-mongoltori.com", "Mongol-Tori"),
    )
    for domain, name in known_gaps:
        assert gate.review(facts(canonical_domain=domain, display_name=name)).passed, domain


def test_adding_a_veto_rule_invalidates_the_stored_corpus():
    """A veto is arithmetic, so a new one must re-score what the old rules produced.

    `fingerprint` hashed `excluded_sectors` and `max_employees` under a docstring
    promising the identity of the veto *rules*. Adding `not_academic` moves neither, so
    the stamp matched, `enqueue_stale_scores` reported nothing to do, and every
    university already in the corpus would have kept its score -- the same defect this
    function exists to fix, one level up.
    """
    from cindraleads.compliance import RULES, ComplianceGate

    base = ComplianceGate(excluded_sectors=("government",), max_employees=1000)
    before = base.fingerprint()

    original = dict(RULES)
    try:
        RULES["a_new_veto"] = lambda _facts: True
        assert base.fingerprint() != before, (
            "a rule added to RULES did not move the stamp; the corpus would never "
            "be re-scored under it"
        )
    finally:
        RULES.clear()
        RULES.update(original)
    assert base.fingerprint() == before, "and removing it must restore the old identity"


def test_widening_a_veto_list_invalidates_the_stored_corpus(monkeypatch):
    """The other half: a rule whose *literals* changed vetoes different companies.

    Adding `ac.pk` to the academic suffixes is as much a veto change as adding a sector,
    and it happens in a tuple rather than in `icp.yaml` -- so hashing the config alone
    would miss it exactly the way hashing `scoring.yaml` alone missed `exclude_sectors`.
    """
    from cindraleads import compliance

    gate = compliance.ComplianceGate(excluded_sectors=("government",), max_employees=1000)
    before = gate.fingerprint()
    monkeypatch.setattr(compliance, "_ACADEMIC_SUFFIXES", (*compliance._ACADEMIC_SUFFIXES, "ac.pk"))
    assert gate.fingerprint() != before
