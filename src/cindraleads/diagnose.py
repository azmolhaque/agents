"""Why the corpus scores the way it does.

One question: **is a wall of REJECTs bad calibration or a genuinely weak corpus?**
Those need opposite responses -- one is a config edit, the other is better discovery --
and the tier counts alone cannot tell them apart. 104 REJECTs looks identical either
way.

The distinguishing evidence is the counterfactual. If lifting one penalty moves forty
leads over a tier line, the penalty is miscalibrated for how this corpus actually
forms. If the component means show the trigger score sitting at 15 out of 100, no
amount of penalty tuning will help and the answer is upstream, in what discovery finds.

Everything here reads `leads.score_breakdown`, which is the exact dict the Scorer
computed -- components and applied penalties both. Nothing is re-scored. That matters
more than the convenience: a diagnostic that recomputed scores could disagree with the
scores it is diagnosing, and then you have two problems. The only arithmetic redone is
the weighted sum, from the same `scoring.yaml` the Scorer used.

**This module writes nothing and decides nothing.** It exists so a calibration change
is made against evidence, by a human, the same way `RETIREMENT_RULES` exists so a rule
change is finished rather than half-made.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from cindraleads.models import to_iso, utcnow
from cindraleads.scoring import ScoringConfig, tier_for
from cindraleads.store import Store

__all__ = [
    "LeadRow",
    "ScoreDiagnosis",
    "diagnose",
    "evidence_breadth",
]

# Components carry a 0-100 value; anything else in the breakdown dict is a penalty.
# Read from the config rather than hardcoded, so a new component appears here the day
# it is added instead of being silently misfiled as a penalty.


@dataclass(frozen=True)
class LeadRow:
    lead_id: str
    domain: str
    display_name: str
    score: int
    tier: str
    components: dict[str, float]
    penalties: dict[str, float]
    scoring_version: str = ""

    @property
    def penalty_total(self) -> float:
        return sum(self.penalties.values())


@dataclass
class ScoreDiagnosis:
    total: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    component_means: dict[str, float] = field(default_factory=dict)
    component_zero_counts: dict[str, int] = field(default_factory=dict)
    penalty_counts: dict[str, int] = field(default_factory=dict)
    penalty_cost: dict[str, float] = field(default_factory=dict)
    # tier distribution if every penalty were lifted at once
    tiers_unpenalised: dict[str, int] = field(default_factory=dict)
    # per-penalty: how many leads gain a tier if only that one is lifted
    promoted_by_lifting: dict[str, int] = field(default_factory=dict)
    near_misses: list[tuple[LeadRow, float, str]] = field(default_factory=list)
    floor: float = 0.0

    # Evidence breadth: what a lead actually rests on, across every live trigger.
    #
    # This found the `single_source` defect -- the rule inspected only the top trigger,
    # so 56 of 57 corroborated leads were penalised as one page's word for it. Now that
    # the rule counts every trigger, the same numbers are the check that the fix took:
    # `penalised_but_corroborated` should fall to zero once the corpus is rescored, and
    # a number that stays high means either the rescore has not run or the rule
    # regressed.
    corroborated: int = 0
    penalised_but_corroborated: int = 0
    promoted_if_corroboration_counted: int = 0

    # Leads whose stored score came from a different calibration than the one running.
    # Without this the report silently describes the past: `cindra reconcile` only
    # enqueues, so reading `explain` straight afterwards shows every lead exactly as
    # the old rules left it, and the fix looks like it did nothing.
    stale_calibration: int = 0

    @property
    def dispatchable(self) -> int:
        return sum(count for tier, count in self.tiers.items() if tier != "REJECT")

    @property
    def is_current(self) -> bool:
        return self.stale_calibration == 0


def _split(
    breakdown: dict[str, Any], component_names: set[str]
) -> tuple[dict[str, float], dict[str, float]]:
    components: dict[str, float] = {}
    penalties: dict[str, float] = {}
    for key, value in breakdown.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if key in component_names:
            components[key] = number
        else:
            penalties[key] = number
    return components, penalties


def _weighted(components: dict[str, float], cfg: ScoringConfig) -> float:
    return sum(value * cfg.components.get(name, 0.0) for name, value in components.items())


def _final(raw: float) -> int:
    return round(max(0.0, min(100.0, raw)))


def read_leads(store: Store, cfg: ScoringConfig) -> list[LeadRow]:
    names = set(cfg.components)
    rows: list[LeadRow] = []
    for row in store.conn.execute(
        "SELECT l.lead_id, l.canonical_domain, l.score, l.tier, l.score_breakdown, "
        "l.scoring_version, c.display_name FROM leads l "
        "JOIN companies c ON c.canonical_domain = l.canonical_domain"
    ):
        try:
            breakdown = json.loads(str(row["score_breakdown"] or "{}"))
        except ValueError:
            breakdown = {}
        components, penalties = _split(breakdown, names)
        rows.append(
            LeadRow(
                lead_id=str(row["lead_id"]),
                domain=str(row["canonical_domain"]),
                display_name=str(row["display_name"] or row["canonical_domain"]),
                score=int(row["score"]),
                tier=str(row["tier"]),
                components=components,
                penalties=penalties,
                scoring_version=str(row["scoring_version"] or ""),
            )
        )
    return rows


def evidence_breadth(store: Store) -> dict[str, int]:
    """Distinct evidence sources per company, across every live trigger.

    This is what "single source" ought to mean for a *lead*, as opposed to for one
    trigger. Counting sources rather than URLs on purpose: three pages of the same
    company's own site are three URLs and still one party's account of itself.
    """
    rows = store.conn.execute(
        "SELECT t.canonical_domain AS domain, COUNT(DISTINCT e.source_id) AS sources "
        "FROM triggers t "
        "JOIN trigger_evidence te ON te.trigger_id = t.trigger_id "
        "JOIN evidence e ON e.evidence_id = te.evidence_id "
        "WHERE t.active = 1 AND t.decays_at > ? "
        "GROUP BY t.canonical_domain",
        (to_iso(utcnow()),),
    ).fetchall()
    return {str(row["domain"]): int(row["sources"]) for row in rows}


def diagnose(
    store: Store, *, config: ScoringConfig | None = None, near_miss_limit: int = 10
) -> ScoreDiagnosis:
    cfg = config or ScoringConfig.load()
    leads = read_leads(store, cfg)
    result = ScoreDiagnosis(total=len(leads))
    result.floor = float(cfg.tiers.get("C", 40))
    if not leads:
        return result

    breadth = evidence_breadth(store)
    fingerprint = cfg.fingerprint()

    for lead in leads:
        if lead.scoring_version != fingerprint:
            result.stale_calibration += 1
        result.tiers[lead.tier] = result.tiers.get(lead.tier, 0) + 1
        for name, value in lead.components.items():
            result.component_means[name] = result.component_means.get(name, 0.0) + value
            if value <= 0:
                result.component_zero_counts[name] = result.component_zero_counts.get(name, 0) + 1
        for name, cost in lead.penalties.items():
            result.penalty_counts[name] = result.penalty_counts.get(name, 0) + 1
            result.penalty_cost[name] = result.penalty_cost.get(name, 0.0) + cost

    for name in result.component_means:
        result.component_means[name] /= len(leads)

    # The counterfactuals. `raw` is recovered rather than read: the stored score is
    # clamped at zero, so a lead whose arithmetic came to -8 and a lead that came to 0
    # are indistinguishable in the `score` column -- and the difference is exactly what
    # says whether lifting a penalty could ever help it.
    for lead in leads:
        raw = _weighted(lead.components, cfg) + lead.penalty_total

        unpenalised = tier_for(_final(raw - lead.penalty_total), cfg)
        result.tiers_unpenalised[unpenalised] = result.tiers_unpenalised.get(unpenalised, 0) + 1

        for name, cost in lead.penalties.items():
            lifted = tier_for(_final(raw - cost), cfg)
            if lifted != lead.tier:
                result.promoted_by_lifting[name] = result.promoted_by_lifting.get(name, 0) + 1

        # What the penalty would do if it asked "does this *lead* rest on one source"
        # rather than "does its top trigger". Reported, never applied -- widening the
        # rule changes which companies get called, which is not a diagnostic's call.
        if breadth.get(lead.domain, 0) >= 2:
            result.corroborated += 1
            cost = lead.penalties.get("single_source", 0.0)
            if cost:
                result.penalised_but_corroborated += 1
                if tier_for(_final(raw - cost), cfg) != lead.tier:
                    result.promoted_if_corroboration_counted += 1

    # Near misses, by true distance to the tier floor rather than by stored score. A
    # lead clamped to 0 from a raw of -8 is 48 points away, not 40, and ranking by the
    # clamped number would put a hopeless lead alongside a genuinely marginal one.
    rejected = [lead for lead in leads if lead.tier == "REJECT"]
    scored: list[tuple[LeadRow, float, str]] = []
    for lead in rejected:
        raw = _weighted(lead.components, cfg) + lead.penalty_total
        gap = result.floor - raw
        if lead.penalties:
            worst = min(lead.penalties.items(), key=lambda kv: kv[1])
            blocker = f"{worst[0]} ({worst[1]:+.0f})"
        else:
            weakest = min(
                (
                    (name, value)
                    for name, value in lead.components.items()
                    if cfg.components.get(name, 0.0) > 0
                ),
                key=lambda kv: kv[1],
                default=("", 0.0),
            )
            blocker = f"weak {weakest[0]} ({weakest[1]:.0f}/100)"
        scored.append((lead, gap, blocker))

    scored.sort(key=lambda item: item[1])
    result.near_misses = scored[:near_miss_limit]
    return result
