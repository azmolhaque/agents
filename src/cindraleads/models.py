"""The typed object graph. Nothing crosses a stage boundary except one of these.

Mirrors ``db/migrations/0001_init.sql``. Where the two disagree, the migration is
authoritative for storage and this module is authoritative for semantics.

Timestamps are timezone-aware UTC everywhere in memory and ISO-8601 'Z' strings on
disk; :func:`to_iso` / :func:`from_iso` are the only sanctioned conversion, because
the queue's lease logic depends on those strings sorting lexicographically.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator

__all__ = [
    "Candidate",
    "Company",
    "ComplianceVerdict",
    "Contact",
    "DnsHygiene",
    "EmailStatus",
    "EmployeeBand",
    "Evidence",
    "FundingInfo",
    "Job",
    "JobStatus",
    "Lead",
    "LegalityClass",
    "Offer",
    "Persona",
    "QueryPlan",
    "RawDocument",
    "StageResult",
    "Tier",
    "Trigger",
    "TriggerCode",
    "from_iso",
    "lead_id_for",
    "to_iso",
    "utcnow",
]


# ------------------------------------------------------------------ time helpers


def utcnow() -> datetime:
    """Timezone-aware now, in UTC. Never use ``datetime.now()`` bare."""
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Serialize to a lexicographically sortable ISO-8601 UTC string."""
    if value.tzinfo is None:
        raise ValueError("refusing to serialize a naive datetime; attach UTC first")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_iso(value: str) -> datetime:
    """Parse what :func:`to_iso` produced (and plain ISO-8601, for hand-edited rows)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def lead_id_for(canonical_domain: str) -> str:
    """Stable forever: ``sha256(canonical_domain)[:16]``."""
    return hashlib.sha256(canonical_domain.strip().lower().encode()).hexdigest()[:16]


# ------------------------------------------------------------------ vocabularies

# T0_INBOUND is not in the master prompt's taxonomy. PLAN.md 2.8: inbound mail from
# contact@cindrasec.com is the highest-intent source there is, and it needs a trigger
# code so it inherits dedupe, compliance and scoring like everything else.
TriggerCode = Literal[
    "T0_INBOUND",
    "T1_AI_SHIP",
    "T2_FUNDING",
    "T3_HIRING_SEC",
    "T4_HIRING_AI_ONLY",
    "T5_COMPLIANCE",
    "T6_INCIDENT",
    "T7_SURFACE_SPRAWL",
    "T8_HYGIENE_GAP",
    "T9_MARKETPLACE",
    "T10_VENDOR_PRESSURE",
    "T11_STACK_RISK",
    "T12_LOCAL",
]

Persona = Literal["founder_cto", "head_eng", "compliance", "ai_lead", "generic"]
EmailStatus = Literal["verified", "role_account", "catch_all", "risky", "unverified", "none"]
EmployeeBand = Literal["1-10", "11-50", "51-200", "201-1000", "1000+"]
Tier = Literal["A", "B", "C", "REJECT"]
Offer = Literal["snapshot_free", "watch", "ai_llm_assessment", "gig"]
LegalityClass = Literal["public_record", "public_web", "licensed_api", "first_party"]
JobStatus = Literal["pending", "in_flight", "done", "failed", "dead"]


class _Model(BaseModel):
    """Strict base: unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ------------------------------------------------------------------ provenance


class Evidence(_Model):
    """Proof of what we saw and when. A claim without one of these does not exist."""

    evidence_id: str = ""
    url: HttpUrl
    source_id: str
    snippet: str = Field(max_length=500)
    observed_at: datetime
    content_sha256: str
    last_checked_at: datetime | None = None
    reachable: bool | None = None

    def model_post_init(self, _context: Any, /) -> None:
        if not self.evidence_id:
            digest = hashlib.sha256(f"{self.url}|{self.content_sha256}".encode()).hexdigest()
            object.__setattr__(self, "evidence_id", digest[:16])


class RawDocument(_Model):
    """A fetched artifact. ``body`` is held in memory only.

    PLAN.md 2.7: the body is persisted zstd-compressed under ``var/cache/``, and only
    this row's metadata goes to SQLite, so the DB stays small enough to back up nightly.
    """

    content_sha256: str
    url: HttpUrl
    source_id: str
    legality_class: LegalityClass
    content_type: str | None = None
    byte_size: int = 0
    fetched_at: datetime
    expires_at: datetime | None = None
    injection_flag: bool = False
    body: str | None = None


# ------------------------------------------------------------------ entities


class DnsHygiene(_Model):
    """Public DNS record lookups only.

    Never a finding, never "we scanned you", never an active probe. An internal
    prioritisation hint and nothing else.
    """

    mx_present: bool | None = None
    spf: str | None = None
    dmarc_policy: Literal["none", "quarantine", "reject"] | None = None
    dkim_present: bool | None = None
    dnssec: bool | None = None
    security_txt: bool | None = None
    checked_at: datetime | None = None


class FundingInfo(_Model):
    stage: str | None = None
    amount_usd: float | None = None
    currency: str | None = None
    announced_at: datetime | None = None
    investors: list[str] = Field(default_factory=list)


class Trigger(_Model):
    """A dated reason to buy. Fit alone is noise; this is the actual product."""

    code: TriggerCode
    confidence: float = Field(ge=0, le=1)
    observed_at: datetime
    decays_at: datetime
    evidence: list[Evidence] = Field(min_length=1)
    rationale: str = Field(default="", max_length=280)


class Contact(_Model):
    full_name: str | None = None
    role_title: str | None = None
    persona: Persona | None = None
    email: EmailStr | None = None
    email_status: EmailStatus = "none"
    linkedin_url: HttpUrl | None = None
    source: Evidence
    # B2B only. Business contacts, never personal addresses, never unaffiliated people.
    pii_basis: Literal["public_business_contact"] = "public_business_contact"


class Company(_Model):
    canonical_domain: str
    legal_name: str | None = None
    display_name: str
    country: str | None = Field(default=None, min_length=2, max_length=2)
    hq_city: str | None = None
    employee_band: EmployeeBand | None = None
    industry: str | None = None
    description: str | None = None
    tech_signals: list[str] = Field(default_factory=list)
    ai_surface: list[str] = Field(default_factory=list)
    subdomain_count_ct: int | None = None
    dns_hygiene: DnsHygiene | None = None
    funding: FundingInfo | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("canonical_domain", mode="before")
    @classmethod
    def _normalize(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("canonical_domain is required and must be a non-empty string")
        return v.strip().lower().rstrip(".")

    @field_validator("country", mode="before")
    @classmethod
    def _upper_country(cls, v: str | None) -> str | None:
        return v.strip().upper() if isinstance(v, str) and v.strip() else None


class ComplianceVerdict(_Model):
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    basis: str = "legitimate_interest_b2b"
    vetoes: list[str] = Field(default_factory=list)
    reviewed_at: datetime = Field(default_factory=utcnow)


class Lead(_Model):
    lead_id: str
    company: Company
    contacts: list[Contact] = Field(default_factory=list, max_length=3)
    triggers: list[Trigger] = Field(min_length=1)  # NO TRIGGER, NO LEAD.
    score: int = Field(ge=0, le=100)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    tier: Tier
    recommended_offer: Offer
    outreach_angle: str = Field(default="", max_length=400)
    bengali_angle: str | None = None
    risk_notes: list[str] = Field(default_factory=list)
    compliance: ComplianceVerdict
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_updated_at: datetime = Field(default_factory=utcnow)
    pipeline_version: str
    prompt_version: str = ""


# ------------------------------------------------------------------ pipeline transport


class QueryPlan(_Model):
    """Scout output: one search to run, and why."""

    query: str
    engine: str = "google"
    params: dict[str, str] = Field(default_factory=dict)
    targets: list[TriggerCode] = Field(default_factory=list)
    rationale: str = ""
    cache_ttl_hours: int = 24


class Candidate(_Model):
    """Extractor output: unresolved claims, still awaiting canonicalization."""

    candidate_id: str
    content_sha256: str
    display_name: str
    homepage: HttpUrl | None = None
    country: str | None = None
    description: str | None = None
    tech_signals: list[str] = Field(default_factory=list)
    ai_surface: list[str] = Field(default_factory=list)
    trigger_claims: list[Trigger] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    resolved_domain: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Job(_Model):
    """A row of the durable queue."""

    job_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = "pending"
    priority: int = 100
    attempts: int = 0
    max_attempts: int = 3
    dedupe_key: str | None = None
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    available_at: datetime = Field(default_factory=utcnow)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class StageResult(_Model):
    """What a stage hands back. ``follow_on`` jobs are enqueued in the same
    transaction that completes the current job — that is what makes the pipeline
    exactly-once across a power cut."""

    ok: bool
    stage: str
    job_id: str
    follow_on: list[tuple[str, dict[str, Any]]] = Field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    cost_units: float = 0.0
