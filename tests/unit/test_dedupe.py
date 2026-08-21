"""Canonical domains and the duplicate ladder."""

from __future__ import annotations

import pytest

from cindraleads.dedupe import canonical_domain, name_similarity, normalize_name, same_company

# ------------------------------------------------------------- canonical domain


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.acme.io/careers?x=1", "acme.io"),
        ("http://ACME.io", "acme.io"),
        ("acme.io", "acme.io"),
        ("blog.acme.io", "acme.io"),
        ("deep.sub.domain.acme.io", "acme.io"),
        ("https://acme.io.", "acme.io"),
        ("  https://acme.io/  ", "acme.io"),
    ],
)
def test_variants_of_one_company_collapse_to_one_domain(value, expected):
    assert canonical_domain(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The ICP's secondary market. Treating `.com.bd` as the registrable domain
        # would merge every Bangladeshi company into a single row.
        ("https://shop.dhakafin.com.bd/about", "dhakafin.com.bd"),
        ("colombo-tech.com.lk", "colombo-tech.com.lk"),
        ("www.karachi-soft.com.pk", "karachi-soft.com.pk"),
        ("api.mumbai-saas.co.in", "mumbai-saas.co.in"),
        ("www.example.co.uk", "example.co.uk"),
    ],
)
def test_multi_part_suffixes_keep_three_labels(value, expected):
    assert canonical_domain(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/someorg/somerepo",
        "https://news.ycombinator.com/item?id=123",
        "https://someproject.github.io/docs",
        "https://myapp.vercel.app",
        "https://www.linkedin.com/company/acme",
        "https://boards.greenhouse.io/acme",
    ],
)
def test_a_platform_host_is_not_a_company(value):
    """The most expensive dedupe bug available.

    Canonicalizing a GitHub repo to `github.com` would merge every open-source
    project on earth into one company row, and the merge is not reversible once the
    triggers and evidence have been attached to it.
    """
    assert canonical_domain(value) is None


@pytest.mark.parametrize(
    "value", ["", "   ", "localhost", "192.168.1.1", "https://10.0.0.1/x", "not a url at all"]
)
def test_junk_yields_no_domain(value):
    assert canonical_domain(value) is None


def test_canonicalization_is_idempotent():
    """PLAN.md Phase 3 property: canonicalizing a canonical domain changes nothing."""
    for value in ("https://www.acme.io/x", "sub.dhakafin.com.bd", "ACME.IO"):
        once = canonical_domain(value)
        assert once is not None
        assert canonical_domain(once) == once


# --------------------------------------------------------------- name matching


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme, Inc.", "Acme"),
        ("Acme Technologies Ltd", "Acme"),
        ("acme labs", "Acme Labs, LLC"),
        ("Acme  Software   Pvt Ltd", "acme"),
    ],
)
def test_legal_suffixes_do_not_make_two_companies(left, right):
    assert name_similarity(left, right) == 100.0


def test_genuinely_different_names_score_low():
    assert name_similarity("Acme Health", "Zenith Logistics") < 60


def test_normalization_strips_punctuation_and_case():
    assert normalize_name("  ACME, Inc.  ") == "acme"


# ----------------------------------------------------------------- the ladder


def test_rung_1_is_the_exact_domain():
    match = same_company(
        domain="acme.io",
        name="Something Else Entirely",
        country="US",
        known=[("acme.io", "Acme", "US")],
    )
    assert match is not None
    assert match.rung == 1


def test_rung_2_catches_a_renamed_domain():
    match = same_company(
        domain="acme-health.io",
        name="Acme Health Inc",
        country="US",
        known=[("acmehealth.com", "Acme Health", "US")],
    )
    assert match is not None
    assert match.rung == 2
    assert match.canonical_domain == "acmehealth.com"


def test_the_same_name_in_two_countries_is_two_companies():
    """Country is a guard against a false merge, not a similarity signal."""
    assert (
        same_company(
            domain="acme.co.uk",
            name="Acme",
            country="GB",
            known=[("acme.io", "Acme", "US")],
        )
        is None
    )


def test_a_missing_country_does_not_block_a_merge():
    """Most pages never state a location. If an absent country blocked rung 2, the
    ladder would stop working on exactly the sparse pages it exists to handle."""
    match = same_company(
        domain="acme-io.com", name="Acme", country=None, known=[("acme.io", "Acme Inc", "US")]
    )
    assert match is not None
    assert match.rung == 2


def test_an_unknown_company_matches_nothing():
    assert same_company(domain="brandnew.io", name="Brand New", country=None, known=[]) is None


def test_the_ladder_is_symmetric():
    """PLAN.md Phase 3 property: A duplicates B iff B duplicates A."""
    a = ("acme.io", "Acme Health", "US")
    b = ("acmehealth.io", "Acme Health Inc", "US")
    forward = same_company(domain=b[0], name=b[1], country=b[2], known=[a])
    backward = same_company(domain=a[0], name=a[1], country=a[2], known=[b])
    assert (forward is None) == (backward is None)


def test_an_ats_host_is_never_a_company():
    """Measured against a real "Who is hiring" thread on 2026-08-21: 7 of the first 10
    URLs were an ATS, a job board or a Google Form, against 3 genuine company domains.

    Letting one through is worse than dropping it. Every one of these hosts many
    companies behind a slug or subdomain, so `arborealmanagement.na.teamtailor.com`
    canonicalizes to `teamtailor.com` -- and every company on that ATS would merge onto
    one bogus row, which is dedupe rung 1 doing exactly what it should to data that
    should never have reached it.
    """
    from cindraleads.dedupe import canonical_domain

    for url in (
        "https://arborealmanagement.na.teamtailor.com/jobs/684013-principal-engineer",
        "https://wellfound.com/l/2CyQz1",
        "https://app.careerpuck.com/job-board/choco/job/079ab75d",
        "https://careers.kula.ai/alaffia/34483c",
        "https://realtors.applicantstack.com/x/detail/a2yfvt49zurx",
        "https://uctalent.io/referral/Huynh_Nhu_Bao_Nhan221138/nedZedvOV6rcVKOQ",
        "https://forms.gle/R5b8FaGEM7CSJzsC7",
    ):
        assert canonical_domain(url) is None, url


def test_a_company_careers_page_on_its_own_domain_still_resolves():
    """The bound on the test above. The whole point of reading the thread is the
    companies that publish on their own domain, and over-filtering would drop them."""
    from cindraleads.dedupe import canonical_domain

    assert canonical_domain("https://spade.com/careers/") == "spade.com"
    assert canonical_domain("https://creativelens.ai") == "creativelens.ai"
    # A Greenhouse *job id* on the company's own careers host is the company's page.
    assert canonical_domain("https://careers.dat.com/jobs/?gh_jid=6139594004") == "dat.com"
