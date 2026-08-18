"""CindraScore arithmetic.

Pure functions, no database, no model. If any test here needs one, the separation the
module exists to enforce has been broken.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cindraleads.config import settings
from cindraleads.models import TriggerCode, utcnow
from cindraleads.scoring import (
    ScoreInput,
    ScoringConfig,
    TriggerObservation,
    decayed_weight,
    recommended_offer,
    score,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cfg() -> ScoringConfig:
    s = settings()
    object.__setattr__(s, "config_dir", REPO_ROOT / "config")
    return ScoringConfig.load(s)


def observation(code: str, *, days_ago: float = 0, urls: int = 2) -> TriggerObservation:
    return TriggerObservation(
        code=code,
        observed_at=utcnow() - timedelta(days=days_ago),
        evidence_urls=tuple(f"https://x.io/{i}" for i in range(urls)),
        evidence_sources=tuple(f"src{i}" for i in range(urls)),
    )


def make(**kwargs) -> ScoreInput:  # type: ignore[no-untyped-def]
    base = {"canonical_domain": "acme.io", "triggers": (observation("T1_AI_SHIP"),)}
    return ScoreInput(**{**base, **kwargs})


# ------------------------------------------------------------------ config sanity


def test_every_taxonomy_trigger_has_a_weight(cfg: ScoringConfig):
    """A code with no row scores zero, which looks exactly like a trigger that never
    fires. Adding a trigger to the taxonomy without a weight is a silent no-op."""
    import typing

    missing = [c for c in typing.get_args(TriggerCode) if c not in cfg.triggers]
    assert not missing, f"scoring.yaml has no weight for {missing}"


def test_component_weights_sum_to_one(cfg: ScoringConfig):
    assert abs(sum(cfg.components.values()) - 1.0) < 1e-9


def test_a_config_that_does_not_sum_to_one_is_fatal(tmp_path: Path):
    """A set summing to 0.9 caps every score at 90 and nothing looks broken."""
    from cindraleads.errors import ConfigError

    (tmp_path / "scoring.yaml").write_text(
        "triggers:\n  T1_AI_SHIP: {weight: 30, half_life_days: 180}\n"
        "components:\n  trigger: 0.5\n  icp_fit: 0.2\n"
    )
    s = settings()
    object.__setattr__(s, "config_dir", tmp_path)
    with pytest.raises(ConfigError, match=r"must sum to 1\.0"):
        ScoringConfig.load(s)


# ------------------------------------------------------------------------ decay


def test_a_trigger_is_worth_half_at_its_half_life():
    assert decayed_weight(30, 180, 180) == pytest.approx(15.0, abs=0.01)
    assert decayed_weight(30, 180, 360) == pytest.approx(7.5, abs=0.01)


def test_a_trigger_with_no_half_life_does_not_decay():
    """T12_LOCAL is a standing fact. Being in Dhaka does not become less true."""
    assert decayed_weight(10, 0, 5000) == 10.0


def test_a_future_dated_trigger_is_not_amplified():
    """A bad page will produce a date in the future. Without the clamp the exponent
    goes positive and a garbage date becomes the highest-scoring signal there is."""
    assert decayed_weight(30, 180, -5000) == 30.0


def test_decay_is_monotonic_in_age(cfg: ScoringConfig):
    ages = [0, 10, 30, 90, 180, 365]
    values = [decayed_weight(30, 180, a) for a in ages]
    assert values == sorted(values, reverse=True)


# ---------------------------------------------------------------- the properties


def test_score_is_monotonic_in_trigger_weight(cfg: ScoringConfig):
    """PLAN.md Phase 5 property, stated correctly.

    "Monotonic in trigger weight" is about the *weight*, holding the trigger set
    fixed — raising a trigger's configured weight must never lower the score. It is
    NOT "a heavier code always outscores a lighter one": different codes carry
    different side effects, and T4_HIRING_AI_ONLY legitimately outscores heavier
    triggers because hiring AI with no security role is itself an ICP signal.
    """
    from dataclasses import replace

    from cindraleads.scoring import TriggerWeight

    previous = -1.0
    for weight in (1, 5, 10, 20, 30, 50, 90):
        tuned = replace(cfg, triggers={**cfg.triggers, "T1_AI_SHIP": TriggerWeight(weight, 180)})
        current = score(make(triggers=(observation("T1_AI_SHIP"),)), tuned).score
        assert current >= previous, f"raising the weight to {weight} lowered the score"
        previous = current


def test_the_trigger_component_is_monotonic_across_codes(cfg: ScoringConfig):
    """The component itself, unlike the total, IS ordered by weight — the other
    components are what break the ordering, and they do so for good reasons."""
    ordered = sorted(cfg.triggers, key=lambda c: cfg.triggers[c].weight)
    values = [
        score(make(triggers=(observation(code),)), cfg).breakdown["trigger"] for code in ordered
    ]
    assert values == sorted(values)


def test_more_triggers_never_lower_the_score(cfg: ScoringConfig):
    one = score(make(triggers=(observation("T1_AI_SHIP"),)), cfg).score
    two = score(make(triggers=(observation("T1_AI_SHIP"), observation("T2_FUNDING"))), cfg).score
    assert two >= one


def test_an_older_trigger_scores_lower(cfg: ScoringConfig):
    fresh = score(make(triggers=(observation("T2_FUNDING", days_ago=1),)), cfg).score
    stale = score(make(triggers=(observation("T2_FUNDING", days_ago=150),)), cfg).score
    assert stale < fresh


def test_the_score_is_always_in_range(cfg: ScoringConfig):
    extremes = [
        make(triggers=()),
        make(triggers=tuple(observation(c) for c in cfg.triggers)),
        make(is_anti_icp=True),
        make(is_suppressed=True, is_anti_icp=True),
    ]
    for candidate in extremes:
        assert 0 <= score(candidate, cfg).score <= 100


# ------------------------------------------------------------------- the vetoes


def test_anti_icp_is_a_hard_reject(cfg: ScoringConfig):
    """-100 expressed as arithmetic, so one code path produces the score and nothing
    has to remember to check a separate flag."""
    strong = make(triggers=tuple(observation(c) for c in ("T9_MARKETPLACE", "T1_AI_SHIP")))
    baseline = score(strong, cfg).score
    assert baseline > 0

    vetoed = make(
        triggers=tuple(observation(c) for c in ("T9_MARKETPLACE", "T1_AI_SHIP")),
        is_anti_icp=True,
    )
    assert score(vetoed, cfg).score == 0
    assert score(vetoed, cfg).tier == "REJECT"


def test_suppression_is_a_hard_reject(cfg: ScoringConfig):
    assert score(make(is_suppressed=True), cfg).tier == "REJECT"


def test_a_single_sourced_lead_is_penalised(cfg: ScoringConfig):
    """One party's word for it is weaker than two independent sightings."""
    multi = make(triggers=(observation("T1_AI_SHIP", urls=3),))
    single = make(triggers=(observation("T1_AI_SHIP", urls=1),))
    assert "single_source" in score(single, cfg).penalties
    assert "single_source" not in score(multi, cfg).penalties
    assert score(single, cfg).score < score(multi, cfg).score


def test_corroboration_across_triggers_counts(cfg: ScoringConfig):
    """The rule asks about the lead, not about its highest-weighted trigger.

    A company seen shipping AI on its own site, with a DNS gap in the public record
    and a funding round in the news, rests on three independent parties. Judging only
    the top trigger penalised exactly that lead -- 56 of 57 corroborated leads on the
    first real corpus -- because T1 happened to cite a single page.
    """
    thin_top = TriggerObservation(
        code="T1_AI_SHIP",
        observed_at=utcnow(),
        evidence_urls=("https://acme.io/blog",),
        evidence_sources=("company_site",),
    )
    elsewhere = TriggerObservation(
        code="T2_FUNDING",
        observed_at=utcnow(),
        evidence_urls=("https://news.test/round",),
        evidence_sources=("serpapi_news",),
    )

    assert "single_source" in score(make(triggers=(thin_top,)), cfg).penalties
    assert "single_source" not in score(make(triggers=(thin_top, elsewhere)), cfg).penalties


def test_many_pages_of_one_site_are_not_corroboration(cfg: ScoringConfig):
    """The reason it counts sources rather than URLs. Three pages of a company's own
    site are three URLs and still one party's account of itself -- and keying on URLs
    would let any site with an /about page exempt itself."""
    own_site_only = (
        TriggerObservation(
            code="T1_AI_SHIP",
            observed_at=utcnow(),
            evidence_urls=("https://acme.io/", "https://acme.io/about"),
            evidence_sources=("company_site", "company_site"),
        ),
        TriggerObservation(
            code="T4_HIRING_AI_ONLY",
            observed_at=utcnow(),
            evidence_urls=("https://acme.io/careers",),
            evidence_sources=("company_site",),
        ),
    )

    assert "single_source" in score(make(triggers=own_site_only), cfg).penalties


def test_evidence_older_than_180_days_is_penalised(cfg: ScoringConfig):
    stale = make(triggers=(observation("T1_AI_SHIP", days_ago=200),))
    assert "stale_evidence" in score(stale, cfg).penalties


# ----------------------------------------------------------------------- tiers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (100, "A"),
        (72, "A"),
        (71, "B"),
        (55, "B"),
        (54, "C"),
        (40, "C"),
        (39, "REJECT"),
        (0, "REJECT"),
    ],
)
def test_tier_boundaries(value, expected, cfg: ScoringConfig):
    from cindraleads.scoring import tier_for

    assert tier_for(value, cfg) == expected


# ----------------------------------------------------------------------- offers


def test_a_marketplace_brief_outranks_everything():
    """Somebody has written down that they want to buy this."""
    inp = make(
        triggers=(observation("T9_MARKETPLACE"), observation("T1_AI_SHIP")),
        ai_surface=("public_chatbot",),
    )
    assert recommended_offer(inp) == "gig"


def test_a_shipped_ai_surface_gets_the_ai_assessment():
    inp = make(triggers=(observation("T1_AI_SHIP"),), ai_surface=("agent_with_tools",))
    assert recommended_offer(inp) == "ai_llm_assessment"


def test_ai_talk_without_a_surface_is_not_an_ai_assessment():
    """ "AI-powered" in marketing copy is not a shipped AI surface."""
    inp = make(triggers=(observation("T1_AI_SHIP"),), ai_surface=())
    assert recommended_offer(inp) == "snapshot_free"


def test_compliance_pressure_without_ai_gets_watch():
    inp = make(triggers=(observation("T5_COMPLIANCE"),), ai_surface=())
    assert recommended_offer(inp) == "watch"


def test_the_default_is_the_free_snapshot():
    """The founding-cohort wedge. Costs us two days; use it liberally."""
    assert recommended_offer(make(triggers=(observation("T12_LOCAL"),))) == "snapshot_free"


# -------------------------------------------------------------------- ICP fit


def test_an_unknown_headcount_is_not_punished(cfg: ScoringConfig):
    """Most pages never state one. Scoring silence as zero would reject every company
    with a terse landing page, which is most good prospects."""
    unknown = score(make(employee_band=None), cfg)
    tiny = score(make(employee_band="1-10"), cfg)
    assert unknown.breakdown["icp_fit"] > 0
    assert abs(unknown.breakdown["icp_fit"] - tiny.breakdown["icp_fit"]) < 40


def test_hiring_ai_without_security_scores_higher(cfg: ScoringConfig):
    both = make(triggers=(observation("T4_HIRING_AI_ONLY"), observation("T3_HIRING_SEC")))
    ai_only = make(triggers=(observation("T4_HIRING_AI_ONLY"),))
    assert ai_only.triggers
    assert score(ai_only, cfg).breakdown["icp_fit"] > score(both, cfg).breakdown["icp_fit"]


def test_no_llm_is_reachable_from_this_module():
    """The structural half of "a model must never invent the number"."""
    import cindraleads.scoring as scoring_module

    source = Path(scoring_module.__file__).read_text()
    for forbidden in ("StructuredLLM", "OllamaBackend", "llm", "anthropic"):
        assert f"import {forbidden}" not in source
    assert "llm" not in {f.name for f in ScoreInput.__dataclass_fields__.values()}


def test_every_email_status_has_a_reachability_score(cfg: ScoringConfig):
    """The bug this exists to prevent.

    `scoring.yaml` said `verified_email` where `EmailStatus` says `verified`. The
    lookup missed, defaulted to 0, and a fully contactable lead lost its entire 15%
    reachability component with nothing in the output to show it -- the same shape as
    the SerpAPI budget key that silently uncapped spending.
    """
    import typing

    from cindraleads.models import EmailStatus

    missing = set(typing.get_args(EmailStatus)) - set(cfg.reachability)
    assert not missing, f"scoring.yaml reachability has no entry for {sorted(missing)}"


def test_a_reachability_map_with_an_unknown_key_will_not_load(tmp_path: Path):
    from cindraleads.errors import ConfigError

    (tmp_path / "scoring.yaml").write_text(
        "triggers:\n  T1_AI_SHIP: {weight: 30, half_life_days: 180}\n"
        "components: {trigger: 1.0}\n"
        "reachability: {verified_email: 100}\n"
    )
    s = settings()
    object.__setattr__(s, "config_dir", tmp_path)
    with pytest.raises(ConfigError, match="reachability is missing"):
        ScoringConfig.load(s)


def test_a_contactable_lead_outscores_an_unreachable_one(cfg: ScoringConfig):
    """Reachability is 15% of the score, and answers "who do I talk to?" -- half the
    question the whole product exists to answer."""
    unknown = score(make(email_status="none"), cfg)
    reachable = score(make(email_status="verified", has_named_contact=True), cfg)
    assert reachable.score > unknown.score
    assert reachable.breakdown["reachability"] == 100.0


# ------------------------------------------------- calibration fingerprinting


def test_the_fingerprint_ignores_cosmetic_config_changes(cfg: ScoringConfig):
    """It hashes resolved values, not file bytes. Reformatting `scoring.yaml` or
    editing a comment must not force a corpus-wide rescore at ~18 s a lead."""
    from dataclasses import replace

    assert cfg.fingerprint() == replace(cfg).fingerprint()


def test_the_fingerprint_moves_when_a_number_moves(cfg: ScoringConfig):
    from dataclasses import replace

    original = cfg.fingerprint()
    assert (
        replace(cfg, penalties={**cfg.penalties, "single_source": -8.0}).fingerprint() != original
    )
    assert replace(cfg, tiers={**cfg.tiers, "C": 35.0}).fingerprint() != original
    assert replace(cfg, components={**cfg.components, "trigger": 0.5}).fingerprint() != original


def test_the_fingerprint_covers_the_arithmetic_not_just_the_config(cfg: ScoringConfig):
    """A hash of `scoring.yaml` cannot see a change to the penalty logic in this
    module, which is why `ARITHMETIC_VERSION` exists and has to be bumped by hand."""
    import cindraleads.scoring as scoring_module

    before = cfg.fingerprint()
    original = scoring_module.ARITHMETIC_VERSION
    try:
        scoring_module.ARITHMETIC_VERSION = original + 1
        assert cfg.fingerprint() != before
    finally:
        scoring_module.ARITHMETIC_VERSION = original


# ------------------------------------------------- what the prospect actually reads


def test_every_trigger_has_a_human_phrase(cfg: ScoringConfig):
    """The first Tier B card ever dispatched read "You published T1_AI_SHIP and
    T8_HYGIENE_GAP on your public page".

    The prompt was handed the raw code, so the 4B had nothing to say and said the code
    -- in an angle written to be pasted into a prospect's inbox. A trigger with no
    `means` is a card waiting to do that again.
    """
    for code, spec in cfg.triggers.items():
        assert spec.means, f"{code} has no human phrase"
        assert not spec.means.isupper(), f"{code}'s phrase looks like a code: {spec.means}"


def test_a_trigger_without_a_phrase_is_fatal(tmp_path: Path):
    from cindraleads.errors import ConfigError

    (tmp_path / "scoring.yaml").write_text(
        "triggers:\n  T1_AI_SHIP: {weight: 30, half_life_days: 180}\n"
        "components:\n  trigger: 1.0\n"
        "reachability: {verified: 100, role_account: 70, catch_all: 40, risky: 25, "
        "unverified: 15, none: 0}\n"
    )
    s = settings()
    object.__setattr__(s, "config_dir", tmp_path)
    with pytest.raises(ConfigError, match="human `means` phrase"):
        ScoringConfig.load(s)


def test_no_phrase_reads_as_something_we_found(cfg: ScoringConfig):
    """Rule 4 of the prompt applies to `means` too -- it is prospect-facing text, and
    the promise is "no scan ever starts without a signed RoE"."""
    forbidden = ("we found", "we detected", "vulnerab", "exposed", "we scanned", "insecure")
    for code, spec in cfg.triggers.items():
        lowered = spec.means.lower()
        for phrase in forbidden:
            assert phrase not in lowered, f"{code} phrases a finding, not an observation"


def test_rewording_a_phrase_does_not_force_a_rescore(cfg: ScoringConfig):
    """`means` changes the prose and never the number. Putting it in the fingerprint
    would make a typo fix recompute 141 leads at ~18 s each."""
    from dataclasses import replace

    from cindraleads.scoring import TriggerWeight

    original = cfg.triggers["T1_AI_SHIP"]
    reworded = replace(
        cfg,
        triggers={
            **cfg.triggers,
            "T1_AI_SHIP": TriggerWeight(original.weight, original.half_life_days, "reworded"),
        },
    )
    assert reworded.fingerprint() == cfg.fingerprint()


def test_a_leaked_code_costs_the_angle_not_the_lead():
    """The guard, and the choice it encodes.

    An angle naming our internal taxonomy is unusable -- the card exists to be pasted
    into an email. But the lead itself is already fully decided by arithmetic the model
    never touched, so the prose is discarded and the lead ships without it. Not
    retried: the same prompt would produce the same thing.
    """
    from cindraleads.agents.scorer import _leaked_codes
    from cindraleads.models import LeadProse

    leaky = LeadProse(
        outreach_angle="You published T1_AI_SHIP and T8_HYGIENE_GAP on your public page.",
        rationale="fine",
    )
    assert _leaked_codes(leaky) == ["T1_AI_SHIP", "T8_HYGIENE_GAP"]

    clean = LeadProse(
        outreach_angle="You announced an AI assistant two weeks ago.", rationale="fine"
    )
    assert _leaked_codes(clean) == []


def test_the_bengali_angle_is_checked_too():
    """As prospect-facing as the English one. A card is pasted whole."""
    from cindraleads.agents.scorer import _leaked_codes
    from cindraleads.models import LeadProse

    prose = LeadProse(
        outreach_angle="You announced an AI assistant.",
        rationale="fine",
        bengali_angle="আপনারা T1_AI_SHIP প্রকাশ করেছেন।",
    )
    assert _leaked_codes(prose) == ["T1_AI_SHIP"]


def test_a_company_named_like_a_code_is_not_a_false_positive():
    """`\\b` on both sides. The guard must not eat a legitimate angle."""
    from cindraleads.agents.scorer import _leaked_codes
    from cindraleads.models import LeadProse

    for text in (
        "Your T3 pricing tier launched last week.",
        "You published an AI_ASSISTANT changelog.",
        "T5 Systems announced a funding round.",
    ):
        assert _leaked_codes(LeadProse(outreach_angle=text, rationale="x")) == [], text


def test_the_age_phrase_reads_as_a_person_wrote_it():
    """ "observed 2026-08-17" is a log line. A researcher writes "11 days ago"."""
    from datetime import timedelta

    from cindraleads.agents.scorer import _age_phrase

    now = utcnow()
    assert _age_phrase(now) == "today"
    assert _age_phrase(now - timedelta(days=1)) == "yesterday"
    assert _age_phrase(now - timedelta(days=5)) == "5 days ago"
    assert _age_phrase(now - timedelta(days=21)) == "3 weeks ago"
    assert _age_phrase(now - timedelta(days=95)) == "3 months ago"
    # A future-dated trigger from a bad page must not read as "-3 days ago".
    assert _age_phrase(now + timedelta(days=30)) == "today"
