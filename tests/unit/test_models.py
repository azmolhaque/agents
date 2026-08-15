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


# ------------------------------------------------- extraction output bounds


def test_extraction_output_is_bounded_by_the_schema_itself():
    """Measured on a Pi 5: decode is 68-99% of extraction latency at ~2.8 tok/s.
    The cheapest way to bound output is the grammar, because maxLength/maxItems
    reach Ollama through the JSON Schema and the model then *cannot* run long.
    Asking politely in the prompt is not enforcement."""
    from cindraleads.models import CompanyExtraction

    props = CompanyExtraction.model_json_schema()["properties"]

    def bound(name: str, key: str) -> int | None:
        node = props[name]
        if key in node:
            return int(node[key])
        for branch in node.get("anyOf", []):
            if key in branch:
                return int(branch[key])
        return None

    assert bound("description", "maxLength") == 160
    assert bound("display_name", "maxLength") == 80
    assert bound("tech_signals", "maxItems") == 6
    assert bound("ai_surface", "maxItems") == 4
    assert bound("trigger_codes", "maxItems") == 4
    assert bound("evidence_snippets", "maxItems") == 2


def test_extraction_rejects_an_over_long_description():
    from cindraleads.models import CompanyExtraction

    with pytest.raises(ValidationError):
        CompanyExtraction(display_name="Acme", description="x" * 161)


def test_extraction_rejects_too_many_snippets():
    from cindraleads.models import CompanyExtraction

    with pytest.raises(ValidationError):
        CompanyExtraction(display_name="Acme", evidence_snippets=["a", "b", "c"])


def test_worst_case_output_fits_the_token_cap_and_the_timeout():
    """Three numbers have to agree, or a maximally-detailed page fails:

    * what the schema permits (maxLength / maxItems)
    * the num_predict cap  -- too low truncates mid-JSON into invalid output
    * the request timeout  -- too low cuts the connection before it finishes

    Measured Pi 5 decode is ~2.8 tok/s, and prefill costs up to ~30 s on a long
    page. Pinning the relationship here stops one of the three drifting alone.
    """
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path

    from cindraleads.models import CompanyExtraction

    props = CompanyExtraction.model_json_schema()["properties"]

    def maxlen(node):
        if "maxLength" in node:
            return int(node["maxLength"])
        for branch in node.get("anyOf", []):
            if "maxLength" in branch:
                return int(branch["maxLength"])
        return 24

    worst_chars = 0
    for name, node in props.items():
        items = node.get("maxItems") or next(
            (b.get("maxItems") for b in node.get("anyOf", []) if b.get("maxItems")), None
        )
        if items:
            worst_chars += int(items) * maxlen(node.get("items") or {})
        else:
            worst_chars += maxlen(node)
        worst_chars += len(name) + 6

    worst_tokens = worst_chars / 3.2

    spec = importlib.util.spec_from_file_location(
        "bm_defaults", _Path(__file__).resolve().parents[2] / "scripts" / "benchmark_models.py"
    )
    bm = importlib.util.module_from_spec(spec)
    _sys.modules["bm_defaults"] = bm
    spec.loader.exec_module(bm)
    defaults = {a.dest: a.default for a in bm.build_parser()._actions}

    assert worst_tokens <= defaults["max_tokens"], (
        f"schema allows ~{worst_tokens:.0f} tokens but num_predict is "
        f"{defaults['max_tokens']}: a full answer would be truncated into invalid JSON"
    )

    pi_decode_tps, worst_prefill_s = 2.8, 30
    worst_seconds = worst_tokens / pi_decode_tps + worst_prefill_s
    assert worst_seconds < defaults["timeout"], (
        f"worst case is ~{worst_seconds:.0f}s but the timeout is {defaults['timeout']}s"
    )
