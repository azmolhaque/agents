"""CindraScore: deterministic arithmetic, no model anywhere in scope.

The master prompt is blunt about this and it is worth restating: *a model must never be
allowed to invent the number*. A 4B asked to score a lead will produce a confident 78
that means nothing and cannot be audited. So this module imports no LLM, is handed no
LLM, and has nothing a model's output could reach. The Scorer stage calls a model
afterwards, separately, for three prose fields — and those fields cannot write a number
back.

Everything tunable lives in `config/scoring.yaml`. The formula here is fixed; the
weights are data, so tuning against measured precision in Phase 8 is a config diff with
the Critic's proposal attached rather than a code change.

**Decay is the point.** Fit alone is noise — every SaaS company in the world "fits". A
*dated* trigger is the lead, and a dated trigger stops being news. Exponential decay by
half-life means a funding round fades over a quarter instead of counting forever, which
is what stops the pipeline re-dispatching the same stale prospect every week.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, get_args

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.errors import ConfigError
from cindraleads.models import EmailStatus, EmployeeBand, Offer, Tier, TriggerCode, utcnow

__all__ = [
    "ARITHMETIC_VERSION",
    "ScoreInput",
    "ScoreResult",
    "ScoringConfig",
    "TriggerObservation",
    "decayed_weight",
    "recommended_offer",
    "tier_for",
]

# Bump this whenever the arithmetic in this module changes in a way that would give a
# stored lead a different score -- a new penalty, a changed condition, a reworked
# component. The config half of the fingerprint is computed automatically; this is the
# half a hash of the YAML cannot see.
#
# Forgetting to bump it means every existing lead keeps a score the current code would
# not produce, which is the failure this whole mechanism exists to prevent. Treat it
# like an entry in `RETIREMENT_RULES`: the second half of a rule change, not optional.
#
#   1: initial
#   2: `single_source` asks whether the lead rests on one source, across every
#      trigger, rather than whether its top trigger does
ARITHMETIC_VERSION = 2


@dataclass(frozen=True)
class TriggerWeight:
    weight: float
    half_life_days: float


@dataclass(frozen=True)
class ScoringConfig:
    triggers: dict[str, TriggerWeight]
    components: dict[str, float]
    penalties: dict[str, float]
    tiers: dict[str, float]
    icp_fit: dict[str, Any]
    surface: dict[str, float]
    reachability: dict[str, float]
    freshness_zero_at_days: float
    sanctioned_countries: frozenset[str]

    def fingerprint(self) -> str:
        """Identifies the calibration that produced a score.

        Hashes the *resolved values*, not the file bytes, so reformatting `scoring.yaml`
        or editing a comment does not force a corpus-wide rescore -- only a change that
        would actually move a number does. `ARITHMETIC_VERSION` covers the other half,
        the logic in this module, which no hash of the config can see.
        """
        shape = {
            "arithmetic": ARITHMETIC_VERSION,
            "triggers": {
                code: [spec.weight, spec.half_life_days]
                for code, spec in sorted(self.triggers.items())
            },
            "components": dict(sorted(self.components.items())),
            "penalties": dict(sorted(self.penalties.items())),
            "tiers": dict(sorted(self.tiers.items())),
            "icp_fit": {str(k): self.icp_fit[k] for k in sorted(self.icp_fit, key=str)},
            "surface": dict(sorted(self.surface.items())),
            "reachability": dict(sorted(self.reachability.items())),
            "freshness_zero_at_days": self.freshness_zero_at_days,
            "sanctioned": sorted(self.sanctioned_countries),
        }
        blob = json.dumps(shape, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @classmethod
    def load(cls, config: Settings | None = None) -> ScoringConfig:
        cfg = config or settings()
        data = load_yaml("scoring", base=cfg.resolve(cfg.config_dir))

        raw_triggers = data.get("triggers")
        if not isinstance(raw_triggers, dict) or not raw_triggers:
            raise ConfigError("scoring.yaml needs a non-empty 'triggers' mapping")
        triggers = {
            str(code): TriggerWeight(
                weight=float(spec.get("weight", 0)),
                half_life_days=float(spec.get("half_life_days", 0)),
            )
            for code, spec in raw_triggers.items()
        }

        components = {str(k): float(v) for k, v in (data.get("components") or {}).items()}
        total = sum(components.values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            # A set summing to 0.9 caps every score at 90 and nothing looks broken.
            raise ConfigError(f"scoring.yaml components must sum to 1.0, got {total}")

        reachability = {str(k): float(v) for k, v in (data.get("reachability") or {}).items()}
        # Fail closed on a key that no `email_status` will ever equal. `verified_email`
        # instead of `verified` silently scored every contactable lead at zero
        # reachability -- 15% of the score, gone, with nothing in the output to show it.
        missing = set(get_args(EmailStatus)) - set(reachability)
        if missing:
            raise ConfigError(
                f"scoring.yaml reachability is missing {sorted(missing)}; keys must be "
                f"EmailStatus values ({sorted(get_args(EmailStatus))})"
            )

        return cls(
            triggers=triggers,
            components=components,
            penalties={str(k): float(v) for k, v in (data.get("penalties") or {}).items()},
            tiers={str(k): float(v) for k, v in (data.get("tiers") or {}).items()},
            icp_fit=dict(data.get("icp_fit") or {}),
            surface={str(k): float(v) for k, v in (data.get("surface") or {}).items()},
            reachability=reachability,
            freshness_zero_at_days=float((data.get("freshness") or {}).get("zero_at_days", 120)),
            sanctioned_countries=frozenset(
                str(c).upper() for c in (data.get("sanctioned_countries") or [])
            ),
        )


@dataclass(frozen=True)
class TriggerObservation:
    code: str
    observed_at: datetime
    evidence_urls: tuple[str, ...] = ()
    evidence_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoreInput:
    """Everything the arithmetic needs. Deliberately not a `Lead`.

    Taking primitives rather than the full object keeps this module free of any
    dependency on the pipeline's plumbing, which is what makes it testable as pure
    arithmetic and impossible to accidentally hand a model's output.
    """

    canonical_domain: str
    triggers: tuple[TriggerObservation, ...]
    employee_band: EmployeeBand | None = None
    country: str | None = None
    industry: str | None = None
    ai_surface: tuple[str, ...] = ()
    subdomain_count: int | None = None
    hygiene_gap: bool = False
    email_status: str = "none"
    has_named_contact: bool = False
    is_anti_icp: bool = False
    is_suppressed: bool = False
    # Whether enrichment has actually run for this company. The `no_contact`
    # penalty means "we looked and found nobody", which is a fact about the
    # prospect. Before Phase 4 exists nobody has looked, and charging -25 for our
    # own missing stage put every lead below the REJECT threshold regardless of
    # how good it was -- measured 2026-08-15: a fresh T1_AI_SHIP scored 0.
    enrichment_ran: bool = False
    primary_sectors: tuple[str, ...] = ()
    local_tlds: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoreResult:
    score: int
    tier: Tier
    offer: Offer
    breakdown: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)


def decayed_weight(
    weight: float, half_life_days: float, age_days: float, *, floor: float = 0.0
) -> float:
    """`weight * 2 ** (-age / half_life)`.

    `half_life_days <= 0` means the signal does not decay: T12_LOCAL is a standing fact
    about where a company is, and being in Dhaka does not become less true. A negative
    age (a trigger dated in the future, which a bad page will produce) is clamped to
    zero rather than *amplifying* the weight.
    """
    if half_life_days <= 0:
        return weight
    return max(floor, weight * math.exp(-math.log(2) * max(0.0, age_days) / half_life_days))


def _age_days(observed_at: datetime, now: datetime) -> float:
    return (now - observed_at).total_seconds() / 86_400


def _trigger_component(inp: ScoreInput, cfg: ScoringConfig, now: datetime) -> float:
    """Sum of decayed weights, capped at 100.

    Summed rather than maxed: three live triggers genuinely is a better prospect than
    one. Capped because eight weak triggers must not out-score one T9_MARKETPLACE,
    which is somebody actively shopping for exactly what we sell.
    """
    total = 0.0
    for observation in inp.triggers:
        spec = cfg.triggers.get(observation.code)
        if spec is None:
            continue
        total += decayed_weight(
            spec.weight, spec.half_life_days, _age_days(observation.observed_at, now)
        )
    return min(100.0, total)


def _icp_component(inp: ScoreInput, cfg: ScoringConfig) -> float:
    bands = cfg.icp_fit.get("employee_band_points") or {}
    if inp.employee_band is None:
        # Most pages never state a headcount. Scoring silence as zero would punish
        # every company with a terse landing page, which is most good prospects.
        base = float(cfg.icp_fit.get("unknown_band_points", 55))
    else:
        base = float(bands.get(inp.employee_band, 55))

    codes = {t.code for t in inp.triggers}
    if inp.industry and any(
        sector.lower() in inp.industry.lower() for sector in inp.primary_sectors
    ):
        base += float(cfg.icp_fit.get("sector_bonus", 0))
    if inp.canonical_domain.endswith(tuple(inp.local_tlds)) or inp.country in {
        "BD",
        "LK",
        "NP",
        "PK",
    }:
        base += float(cfg.icp_fit.get("local_bonus", 0))
    if "T4_HIRING_AI_ONLY" in codes and "T3_HIRING_SEC" not in codes:
        # Shipping AI and hiring for it, with nobody looking at the security of it.
        base += float(cfg.icp_fit.get("no_security_hire_bonus", 0))
    return min(100.0, base)


def _surface_component(inp: ScoreInput, cfg: ScoringConfig) -> float:
    total = 0.0
    if inp.ai_surface:
        total += cfg.surface.get("ai_surface_points", 0)
    if (inp.subdomain_count or 0) >= 25:
        total += cfg.surface.get("subdomain_sprawl_points", 0)
    if inp.hygiene_gap:
        total += cfg.surface.get("hygiene_gap_points", 0)
    return min(100.0, total)


def _freshness_component(inp: ScoreInput, cfg: ScoringConfig, now: datetime) -> float:
    if not inp.triggers:
        return 0.0
    newest = min(_age_days(t.observed_at, now) for t in inp.triggers)
    span = cfg.freshness_zero_at_days
    return max(0.0, min(100.0, 100.0 * (1 - max(0.0, newest) / span))) if span > 0 else 0.0


def _penalties(inp: ScoreInput, cfg: ScoringConfig, now: datetime) -> dict[str, float]:
    applied: dict[str, float] = {}
    if inp.is_anti_icp:
        applied["anti_icp"] = cfg.penalties.get("anti_icp", 0)
    if inp.is_suppressed:
        applied["suppressed"] = cfg.penalties.get("suppressed", 0)
    if (
        inp.enrichment_ran
        and not inp.has_named_contact
        and inp.email_status in {"none", "unverified"}
    ):
        applied["no_contact"] = cfg.penalties.get("no_contact", 0)
    if inp.triggers and all(_age_days(t.observed_at, now) > 180 for t in inp.triggers):
        applied["stale_evidence"] = cfg.penalties.get("stale_evidence", 0)
    if inp.country and inp.country.upper() in cfg.sanctioned_countries:
        applied["sanctioned_country"] = cfg.penalties.get("sanctioned_country", 0)

    # Does this *lead* rest on one party's account of itself?
    #
    # It used to ask that of the top trigger alone, and on the first real corpus that
    # fired on 94% of leads -- a penalty at that incidence has stopped discriminating
    # and become a constant offset that silently moves the tier floor. 56 of the 57
    # leads corroborated by two or more independent sources were carrying it anyway,
    # because their highest-weighted trigger happened to cite a single page.
    #
    # Sources, not URLs. Three pages of a company's own site are three URLs and still
    # one party's word for it, so counting URLs would call that corroboration. This is
    # the same measure `diagnose.evidence_breadth` reports, deliberately: the number
    # that justified the change and the rule that implements it must not drift apart.
    if inp.triggers:
        sources = {source for t in inp.triggers for source in t.evidence_sources}
        if len(sources) <= 1:
            applied["single_source"] = cfg.penalties.get("single_source", 0)
    return applied


def score(inp: ScoreInput, cfg: ScoringConfig, *, now: datetime | None = None) -> ScoreResult:
    """The whole formula, in one place, with no branch that calls a model."""
    moment = now or utcnow()
    components = {
        "trigger": _trigger_component(inp, cfg, moment),
        "icp_fit": _icp_component(inp, cfg),
        "reachability": cfg.reachability.get(inp.email_status, 0.0)
        + (10.0 if inp.has_named_contact else 0.0),
        "surface": _surface_component(inp, cfg),
        "freshness": _freshness_component(inp, cfg, moment),
    }
    components["reachability"] = min(100.0, components["reachability"])

    weighted = sum(value * cfg.components.get(name, 0.0) for name, value in components.items())
    applied = _penalties(inp, cfg, moment)
    raw = weighted + sum(applied.values())
    final = round(max(0.0, min(100.0, raw)))

    return ScoreResult(
        score=final,
        tier=tier_for(final, cfg),
        offer=recommended_offer(inp),
        breakdown={name: round(value, 2) for name, value in components.items()},
        penalties=applied,
    )


def tier_for(value: int, cfg: ScoringConfig) -> Tier:
    if value >= cfg.tiers.get("A", 72):
        return "A"
    if value >= cfg.tiers.get("B", 55):
        return "B"
    if value >= cfg.tiers.get("C", 40):
        return "C"
    return "REJECT"


def recommended_offer(inp: ScoreInput) -> Offer:
    """Which of the four products to lead with (master prompt section 7).

    Order matters: a marketplace brief outranks everything because the prospect has
    already written down that they want to buy this. `snapshot_free` is the default
    on purpose — it is the founding-cohort wedge and costs us two days.
    """
    codes = {t.code for t in inp.triggers}
    if "T9_MARKETPLACE" in codes:
        return "gig"
    if ("T1_AI_SHIP" in codes or "T11_STACK_RISK" in codes) and inp.ai_surface:
        return "ai_llm_assessment"
    if ("T7_SURFACE_SPRAWL" in codes or "T5_COMPLIANCE" in codes) and not inp.ai_surface:
        return "watch"
    return "snapshot_free"


def known_codes(cfg: ScoringConfig) -> set[str]:
    return set(cfg.triggers)


def missing_trigger_weights(cfg: ScoringConfig, taxonomy: tuple[TriggerCode, ...]) -> list[str]:
    """Codes in the taxonomy with no weight configured.

    A trigger with no row here scores zero and silently stops mattering — which looks
    exactly like a trigger that never fires.
    """
    return [code for code in taxonomy if code not in cfg.triggers]
