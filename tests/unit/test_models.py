"""The typed boundary. Nothing crosses a stage except one of these."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from cindraleads.models import (
    Company,
    ComplianceVerdict,
    Contact,
    Evidence,
    Lead,
    Trigger,
    from_iso,
    lead_id_for,
    to_iso,
    utcnow,
)


def _evidence(url: str = "https://example.io/post") -> Evidence:
    return Evidence(
        url=url,
        source_id="producthunt",
        snippet="Shipped an AI assistant",
        observed_at=utcnow(),
        content_sha256="a" * 64,
    )


def _trigger(code: str = "T1_AI_SHIP") -> Trigger:
    now = utcnow()
    return Trigger(
        code=code,
        confidence=0.91,
        observed_at=now,
        decays_at=now + timedelta(days=180),
        evidence=[_evidence()],
    )


# ------------------------------------------------------------------ time helpers


def test_iso_roundtrip_is_lossless_to_the_millisecond():
    original = datetime(2026, 8, 14, 9, 14, 2, 123000, tzinfo=UTC)
    assert from_iso(to_iso(original)) == original


def test_iso_strings_sort_lexicographically():
    """The queue's lease comparison is a string comparison in SQL, so this ordering
    property is load-bearing, not cosmetic."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    stamps = [to_iso(base + timedelta(seconds=s)) for s in (0, 5, 60, 3600, 86400)]
    assert stamps == sorted(stamps)


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError, match="naive datetime"):
        to_iso(datetime(2026, 1, 1))


def test_lead_id_is_stable_and_normalized():
    assert lead_id_for("acme.io") == lead_id_for("  ACME.IO  ")
    assert len(lead_id_for("acme.io")) == 16


# ---------------------------------------------------------------------- evidence


def test_evidence_id_is_derived_from_url_and_content_hash():
    a, b = _evidence(), _evidence()
    assert a.evidence_id == b.evidence_id
    assert a.evidence_id != _evidence("https://other.io/x").evidence_id


def test_snippet_is_capped_at_500_chars():
    with pytest.raises(ValidationError):
        Evidence(
            url="https://x.io",
            source_id="s",
            snippet="x" * 501,
            observed_at=utcnow(),
            content_sha256="a" * 64,
        )


# ----------------------------------------------------------------------- company


def test_canonical_domain_is_lowercased_and_trailing_dot_stripped():
    assert Company(canonical_domain=" ACME.io. ", display_name="Acme").canonical_domain == "acme.io"


def test_canonical_domain_is_required():
    with pytest.raises(ValidationError):
        Company(canonical_domain="   ", display_name="Acme")


def test_country_is_uppercased_alpha2():
    assert Company(canonical_domain="a.io", display_name="A", country="bd").country == "BD"
    with pytest.raises(ValidationError):
        Company(canonical_domain="a.io", display_name="A", country="BGD")


def test_unknown_fields_are_rejected():
    """extra='forbid' is what stops a renamed field from silently becoming None
    halfway down the pipeline."""
    with pytest.raises(ValidationError):
        Company(canonical_domain="a.io", display_name="A", emplyee_band="11-50")


# ----------------------------------------------------------------------- trigger


def test_a_trigger_cannot_exist_without_evidence():
    """NO EVIDENCE, NO TRIGGER — enforced by the type, not by a code review."""
    now = utcnow()
    with pytest.raises(ValidationError):
        Trigger(code="T1_AI_SHIP", confidence=0.9, observed_at=now, decays_at=now, evidence=[])


def test_confidence_is_bounded():
    now = utcnow()
    with pytest.raises(ValidationError):
        Trigger(
            code="T1_AI_SHIP",
            confidence=1.4,
            observed_at=now,
            decays_at=now,
            evidence=[_evidence()],
        )


def test_inbound_trigger_code_exists():
    """PLAN.md 2.8: inbound mail needs a taxonomy slot or it cannot become a Lead."""
    assert _trigger("T0_INBOUND").code == "T0_INBOUND"


def test_unknown_trigger_code_is_rejected():
    now = utcnow()
    with pytest.raises(ValidationError):
        Trigger(
            code="T99_MADE_UP",
            confidence=0.5,
            observed_at=now,
            decays_at=now,
            evidence=[_evidence()],
        )


# -------------------------------------------------------------------------- lead


def _lead(**overrides: object) -> Lead:
    company = Company(canonical_domain="acme.io", display_name="Acme")
    defaults: dict[str, object] = {
        "lead_id": lead_id_for("acme.io"),
        "company": company,
        "triggers": [_trigger()],
        "score": 84,
        "tier": "A",
        "recommended_offer": "ai_llm_assessment",
        "compliance": ComplianceVerdict(passed=True),
        "pipeline_version": "0.1.0",
    }
    defaults.update(overrides)
    return Lead(**defaults)  # type: ignore[arg-type]


def test_a_lead_requires_at_least_one_trigger():
    """NO TRIGGER, NO LEAD."""
    with pytest.raises(ValidationError):
        _lead(triggers=[])


def test_score_is_bounded_to_0_100():
    with pytest.raises(ValidationError):
        _lead(score=101)


def test_contacts_are_capped_at_three():
    contact = Contact(email="a@acme.io", source=_evidence())
    with pytest.raises(ValidationError):
        _lead(contacts=[contact] * 4)


def test_outreach_angle_is_capped_at_400_chars():
    with pytest.raises(ValidationError):
        _lead(outreach_angle="x" * 401)


def test_contact_pii_basis_cannot_be_anything_else():
    """B2B only. The literal type is the enforcement."""
    with pytest.raises(ValidationError):
        Contact(email="a@acme.io", source=_evidence(), pii_basis="personal")


def test_lead_serializes_and_reparses_cleanly():
    original = _lead()
    assert Lead.model_validate_json(original.model_dump_json()) == original
