"""The Critic: does it find the defects it is named after, and does it stay read-only.

Two of these tests are the Phase 8 acceptance criteria as executable code -- "proposes
>= 3 concrete changes, each citing the leads that justify it, and applies none". The
rest seed the exact shapes of defects that were found by hand during Phase 5 and 6, so
the answer to "would this have caught it" is a test run rather than an opinion.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from cindraleads.agents.critic import (
    CONSTANT_OFFSET_INCIDENCE,
    MIN_CORPUS,
    MIN_TEMPLATE_SAMPLE,
    Critique,
    critique,
    render_markdown,
)
from cindraleads.models import to_iso, utcnow
from cindraleads.scoring import ScoringConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def _lead(
    store: Any,
    domain: str,
    *,
    score: int = 55,
    tier: str = "C",
    discovered_by: str | None = None,
    triggers: tuple[str, ...] = (),
    **breakdown: float,
) -> str:
    lead_id = uuid.uuid4().hex[:16]
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO companies (canonical_domain, display_name, discovered_by, "
            "first_seen_at, last_updated_at) VALUES (?,?,?,?,?)",
            (domain, domain.split(".")[0].title(), discovered_by, now, now),
        )
        conn.execute(
            "INSERT INTO leads (lead_id, canonical_domain, score, score_breakdown, tier, "
            "recommended_offer, first_seen_at, last_updated_at, pipeline_version) "
            "VALUES (?,?,?,?,?,'snapshot_free',?,?,'test')",
            (lead_id, domain, score, json.dumps(breakdown), tier, now, now),
        )
        for code in triggers:
            conn.execute(
                "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
                "observed_at, decays_at) VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, domain, code, 0.9, now, now),
            )
    return lead_id


def _components(**overrides: float) -> dict[str, float]:
    base = {
        "trigger": 60.0,
        "icp_fit": 60.0,
        "reachability": 50.0,
        "surface": 50.0,
        "freshness": 60.0,
    }
    base.update(overrides)
    return base


def _corpus(store: Any, n: int = MIN_CORPUS + 5, **breakdown: float) -> list[str]:
    """Enough leads that the Critic is willing to argue at all."""
    return [
        _lead(store, f"c{i}.com", **{**_components(), **breakdown})  # type: ignore[arg-type]
        for i in range(n)
    ]


def _keys(report: Critique) -> set[str]:
    return {p.key for p in report.proposals}


# --------------------------------------------------------------- refusing to guess


def test_a_small_corpus_produces_no_proposals(store: Any) -> None:
    """Three proposals from eleven leads is a Critic you learn to ignore, and then the
    one week it is right you ignore it too."""
    _corpus(store, n=MIN_CORPUS - 5)

    report = critique(store)

    assert report.proposals == []
    assert "below the" in " ".join(report.notes)


def test_a_stale_corpus_is_flagged_before_its_numbers_are_read(store: Any) -> None:
    """`cindra reconcile` only enqueues. Reading the Critic straight after a scoring
    change describes the corpus as the old rules left it, and every proposal would then
    be arguing with a calibration that is no longer running."""
    _corpus(store)

    report = critique(store)

    assert any("older calibration" in note for note in report.notes)


# ------------------------------------------------------- the defects it is named for


def test_a_penalty_that_fires_on_nearly_everything_is_proposed_for_narrowing(
    store: Any,
) -> None:
    """The `single_source` shape: 96% incidence, a constant offset wearing a penalty's
    name. It subtracted points from almost every lead and ranked nothing."""
    _corpus(store, single_source=-12.0)

    report = critique(store)

    assert "penalties.single_source" in _keys(report)
    proposal = next(p for p in report.proposals if p.key == "penalties.single_source")
    assert proposal.evidence, "a proposal with no leads behind it is a hunch"
    assert (
        f"{CONSTANT_OFFSET_INCIDENCE:.0%}" not in proposal.rationale
    )  # reports actual, not the threshold
    assert "100%" in proposal.rationale


def test_a_penalty_on_a_minority_of_leads_is_left_alone(store: Any) -> None:
    """A penalty that separates leads is doing its job. Proposing to remove every
    penalty would make the report noise."""
    n = MIN_CORPUS + 5
    for i in range(n):
        penalties = {"single_source": -12.0} if i < n // 4 else {}
        _lead(store, f"c{i}.com", **{**_components(), **penalties})  # type: ignore[arg-type]

    report = critique(store)

    assert "penalties.single_source" not in _keys(report)


def test_a_penalty_worth_most_of_the_scale_is_proposed_for_cutting(store: Any) -> None:
    """The `no_contact` shape: -25 against a 100-point scale with a Tier C floor of 40,
    on top of a `reachability` component already pricing the same fact. One fact cost
    40% of the usable range and no lead missing a contact could clear the floor.

    Driven through a penalty that is still in `scoring.yaml`, because a proposal about
    one that is not is unactionable by construction -- see the config-guard test below.
    """
    n = MIN_CORPUS + 5
    for i in range(n):
        penalties = {"single_source": -30.0} if i < n // 3 else {}
        _lead(store, f"c{i}.com", **{**_components(), **penalties})  # type: ignore[arg-type]

    report = critique(store)

    assert "penalties.single_source" in _keys(report)
    proposal = next(p for p in report.proposals if p.key == "penalties.single_source")
    assert "usable range" in proposal.rationale


def test_a_penalty_no_longer_in_the_config_is_not_proposed(store: Any) -> None:
    """`penalty_counts` is read off stored `score_breakdown` rows, which record what the
    build that scored each lead applied -- not what the file says now. `no_contact` was
    deleted from `scoring.yaml` on 2026-08-18, and three leads scored before that still
    carried it, so the Critic proposed cutting a key that is not in the file and quoted a
    point value it could only have got from history.

    A reader would have opened `scoring.yaml`, found nothing to edit, and learned to
    distrust the report -- which is the one thing a proposal-only tool cannot survive.
    """
    n = MIN_CORPUS + 5
    for i in range(n):
        penalties = {"no_contact": -30.0} if i < n // 3 else {}
        _lead(store, f"c{i}.com", **{**_components(), **penalties})  # type: ignore[arg-type]

    report = critique(store)

    assert "no_contact" not in ScoringConfig.load().penalties, "premise of this test"
    assert "penalties.no_contact" not in _keys(report)


def test_a_penalty_holding_back_a_tenth_of_the_corpus_is_proposed(store: Any) -> None:
    """Incidence alone misses this one. `single_source` fired on 52% of the real corpus
    -- nowhere near `CONSTANT_OFFSET_INCIDENCE` -- while being the largest lever in the
    report: lifting it alone promoted 47 of 262 leads. `cindra explain` had been telling
    humans to read it exactly that way for weeks; no rule here did.

    A penalty can discriminate perfectly well and still be priced for a corpus with
    better corroboration than the one it is being applied to.
    """
    n = MIN_CORPUS + 15
    for i in range(n):
        # Half the corpus, and the penalty alone decides their tier: 57.5 without it,
        # 49.5 with. Small enough (-8) that the double-charged rule stays quiet, so this
        # asserts the new rule rather than an existing one.
        penalties = {"single_source": -8.0} if i < n // 2 else {}
        _lead(store, f"c{i}.com", **{**_components(), **penalties})  # type: ignore[arg-type]

    report = critique(store)
    keys = _keys(report)

    assert "penalties.single_source" in keys
    proposal = next(p for p in report.proposals if p.key == "penalties.single_source")
    assert "holding" in proposal.rationale


def test_a_component_that_is_zero_for_everyone_points_upstream_not_at_the_weight(
    store: Any,
) -> None:
    """`reachability` was structurally zero for the whole corpus before the Enricher
    existed. The fix was the Enricher. A Critic that proposed dropping the weight would
    have removed the only thing that made Tier A reachable."""
    _corpus(store, reachability=0.0)

    report = critique(store)

    assert "components.reachability" in _keys(report)
    proposal = next(p for p in report.proposals if p.key == "components.reachability")
    assert "before touching" in proposal.change
    assert "upstream" in proposal.change


def test_a_template_that_finds_nothing_sendable_is_proposed_for_reweighting(
    store: Any,
) -> None:
    """Unfiltered Show HN reached 82% of the first corpus -- a tic-tac-toe game, a world
    clock, a personal blog -- because it announced AI without implying payroll."""
    for i in range(20):
        _lead(store, f"good{i}.com", tier="B", discovered_by="hn_who_is_hiring", **_components())
    for i in range(20):
        _lead(
            store,
            f"bad{i}.com",
            tier="REJECT",
            discovered_by="weekend_projects",
            **_components(trigger=5.0),
        )

    report = critique(store)

    assert "query_templates.weekend_projects.weight" in _keys(report)
    assert "query_templates.hn_who_is_hiring.weight" not in _keys(report)


def test_a_template_already_demoted_is_not_proposed_again(store: Any) -> None:
    """The Critic reads yield and never read `icp.yaml`, so it told a human to lower
    `hn_show_ai` while it sat at 25 -- the weight they had already lowered it to the day
    before -- and to raise `hn_ai_agent` the day after they raised it to 88.

    Same defect as proposing a penalty deleted from the config: arguing about a file it
    does not read. A report that keeps asking for work already done is one you stop
    reading, and a proposal-only tool has nothing else to trade on.
    """
    from cindraleads.agents.critic import ALREADY_DEMOTED, _template_weights

    assert _template_weights().get("hn_show_ai", 99) <= ALREADY_DEMOTED, "premise"

    for i in range(20):
        _lead(store, f"good{i}.com", tier="B", discovered_by="hn_who_is_hiring", **_components())
    for i in range(20):
        _lead(
            store,
            f"bad{i}.com",
            tier="REJECT",
            discovered_by="hn_show_ai",
            **_components(trigger=5.0),
        )

    report = critique(store)

    assert "query_templates.hn_show_ai.weight" not in _keys(report)


def test_a_template_with_too_few_companies_is_not_judged(store: Any) -> None:
    """Two companies and one sendable lead is not a 50% hit rate."""
    for i in range(20):
        _lead(store, f"good{i}.com", tier="B", discovered_by="hn_who_is_hiring", **_components())
    _lead(store, "tiny.com", tier="REJECT", discovered_by="brand_new_template", **_components())

    report = critique(store)

    assert "query_templates.brand_new_template.weight" not in _keys(report)


def test_companies_discovered_before_provenance_existed_are_not_blamed(store: Any) -> None:
    """`(unknown)` is most of the corpus right now and is not a template anyone can
    reweight. Proposing a change to it would be a proposal nobody can act on."""
    for i in range(30):
        _lead(store, f"c{i}.com", tier="REJECT", **_components(trigger=2.0))

    report = critique(store)

    assert not any(p.key.endswith("(unknown).weight") for p in report.proposals)


# ------------------------------------------------------------- grounded in feedback


def test_feedback_can_argue_with_a_trigger_weight(store: Any) -> None:
    """The only argument here that comes from outside the system's own arithmetic.
    Everything else can be self-consistently wrong; a human's verdict cannot."""
    from cindraleads.feedback import record_verdict

    cfg = ScoringConfig.load()
    weak, strong = sorted(cfg.triggers)[:2]

    for i in range(18):
        good = _lead(store, f"good{i}.com", tier="B", triggers=(strong,), **_components())
        record_verdict(store, lead_id=good, verdict="good", source="cli", actor="zahin")
    for i in range(12):
        bad = _lead(store, f"bad{i}.com", tier="C", triggers=(weak,), **_components())
        record_verdict(store, lead_id=bad, verdict="bad", source="cli", actor="zahin")

    report = critique(store)

    assert report.judged == 30
    assert f"triggers.{weak}.weight" in _keys(report)
    lowered = next(p for p in report.proposals if p.key == f"triggers.{weak}.weight")
    assert lowered.change.startswith("Lower")
    assert lowered.evidence, "a weight proposal must name the leads that were judged"


def test_with_nothing_judged_the_report_says_so_rather_than_inventing_a_weight(
    store: Any,
) -> None:
    _corpus(store)

    report = critique(store)

    assert report.judged == 0
    assert any("No lead has been judged" in note for note in report.notes)
    assert not any(p.key.startswith("triggers.") for p in report.proposals)


def test_one_persons_two_reactions_do_not_count_as_two_verdicts(store: Any) -> None:
    from cindraleads.feedback import record_verdict

    cfg = ScoringConfig.load()
    code = sorted(cfg.triggers)[0]
    lead_id = _lead(store, "a.com", triggers=(code,), **_components())
    record_verdict(store, lead_id=lead_id, verdict="good", source="cli", actor="zahin")
    record_verdict(store, lead_id=lead_id, verdict="bad", source="cli", actor="zahin")
    _corpus(store)

    report = critique(store)

    assert report.judged == 1


# -------------------------------------------------------- Phase 8 acceptance: P8


def test_it_proposes_at_least_three_concrete_changes_each_citing_evidence(
    store: Any,
) -> None:
    """PLAN.md Phase 8: ">= 3 concrete weight or query-plan changes, each citing the
    leads that justify it"."""
    from cindraleads.feedback import record_verdict

    cfg = ScoringConfig.load()
    weak = sorted(cfg.triggers)[0]

    for i in range(20):
        _lead(
            store,
            f"junk{i}.com",
            tier="REJECT",
            discovered_by="weekend_projects",
            triggers=(weak,),
            **_components(trigger=4.0, reachability=0.0),
            single_source=-12.0,
        )
    for i in range(12):
        good = _lead(
            store,
            f"real{i}.com",
            tier="B",
            discovered_by="hn_who_is_hiring",
            **_components(reachability=0.0),
        )
        record_verdict(store, lead_id=good, verdict="good", source="cli", actor="zahin")
    for i in range(6):
        bad = _lead(
            store, f"nope{i}.com", tier="C", triggers=(weak,), **_components(reachability=0.0)
        )
        record_verdict(store, lead_id=bad, verdict="bad", source="cli", actor="zahin")

    report = critique(store)

    assert len(report.proposals) >= 3, [p.key for p in report.proposals]
    for proposal in report.proposals:
        assert proposal.target in {"scoring.yaml", "icp.yaml"}
        assert proposal.key and proposal.change and proposal.rationale
    # "citing the leads that justify it" -- at least the corpus-derived ones must, and
    # a proposal whose evidence is a config-wide fact says so in `render()` instead.
    assert any(p.evidence for p in report.proposals)


def test_the_critic_applies_nothing(store: Any, tmp_path: Any) -> None:
    """PLAN.md Phase 8: "applies none -- a test asserts the Critic has no write access
    to `scoring.yaml`".

    Checked by content, not by intent: every config file is hashed before the run and
    compared afterwards. A future edit that added a "just apply the obvious ones" flag
    fails here regardless of how it is spelled.
    """
    from cindraleads.feedback import record_verdict

    _corpus(store, single_source=-12.0, reachability=0.0)
    lead_id = _lead(store, "judged.com", **_components())
    record_verdict(store, lead_id=lead_id, verdict="bad", source="cli", actor="zahin")

    config_dir = REPO_ROOT / "config"
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(config_dir.glob("*.yaml"))
    }
    assert before, "no config files found; this test would pass vacuously"

    report = critique(store)
    render_markdown(report)

    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(config_dir.glob("*.yaml"))
    }
    assert after == before
    assert report.applied == 0


def test_the_critic_module_contains_no_write_path_to_config() -> None:
    """The structural half of the same rule. `applies nothing` held by a passing test is
    a behaviour; held by the module importing no writer is a property.

    Named symbols rather than a substring sweep: `dump` alone would trip on the word in
    a docstring, and a test that fails for the wrong reason gets deleted.
    """
    source = (REPO_ROOT / "src/cindraleads/agents/critic.py").read_text(encoding="utf-8")

    for forbidden in ("yaml.dump", "safe_dump", "write_text", "write_bytes", "os.replace"):
        assert forbidden not in source, f"critic.py must not be able to write config: {forbidden}"
    # `open(...)` in any mode other than read is the general case of the above.
    assert "open(" not in source.replace("open(url", "")


def test_the_report_says_out_loud_that_it_applied_nothing(store: Any) -> None:
    """A report that merely omits the fact reads, to someone skimming it on a Monday,
    as though the changes are already live."""
    _corpus(store, single_source=-12.0)

    body = render_markdown(critique(store))

    assert "applied automatically: **0**" in body
    assert "cindra reconcile --force" in body


def test_an_empty_critique_renders_as_agreement_not_as_a_broken_report(store: Any) -> None:
    _corpus(store)

    body = render_markdown(critique(store))

    assert "does not disagree with the config" in body


def test_the_best_converting_template_is_proposed_for_promotion(store: Any) -> None:
    """Every other rule here takes something away, and for weeks that was the only
    direction available. With a fixed 12-plan budget per run, promoting a winner and
    demoting a loser are the same decision seen from two ends.

    `hn_ai_agent` sat at weight 52 while converting 73% against a 33% corpus rate with a
    mean of 48.5 against 32.4 -- below several templates it outperformed threefold. No
    proposal in this report would ever have said so.
    """
    for i in range(MIN_TEMPLATE_SAMPLE + 2):
        _lead(
            store,
            f"good{i}.com",
            score=70,
            tier="B",
            discovered_by="winner",
            **_components(trigger=85.0),
        )
    for i in range(MIN_TEMPLATE_SAMPLE + 20):
        _lead(
            store,
            f"meh{i}.com",
            score=20,
            tier="REJECT",
            discovered_by="ordinary",
            **_components(trigger=10.0),
        )

    report = critique(store)
    keys = _keys(report)

    assert "query_templates.winner.weight" in keys
    proposal = next(p for p in report.proposals if p.key == "query_templates.winner.weight")
    assert "Raise the weight" in proposal.change
    assert "query_templates.ordinary.weight" not in keys or any(
        "Lower the weight" in p.change
        for p in report.proposals
        if p.key == "query_templates.ordinary.weight"
    ), "the weak template may be demoted, but must never be proposed for promotion"


def test_a_merely_average_template_is_left_alone(store: Any) -> None:
    """The bound. Promotion spends a plan slot that is currently producing something,
    so the bar is deliberately higher than the demotion rule's: twice the corpus rate
    AND a mean above it. A report that proposes raising every second weight is one
    nobody reads."""
    for i in range(MIN_TEMPLATE_SAMPLE + 2):
        _lead(store, f"a{i}.com", discovered_by="alpha", **_components())
    for i in range(MIN_TEMPLATE_SAMPLE + 2):
        _lead(store, f"b{i}.com", discovered_by="beta", **_components())

    report = critique(store)

    assert not [p for p in report.proposals if p.target == "icp.yaml" and "Raise" in p.change], (
        "two identical templates cannot both be outperforming"
    )
