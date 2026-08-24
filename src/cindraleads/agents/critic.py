"""The weekly Critic. It reads the corpus, argues for changes, and applies none.

**It has no write access to `scoring.yaml` and this is structural, not a promise.** The
module imports no YAML writer and never opens a config path for writing; a test asserts
both, and a second test runs a full critique against a seeded database and compares the
config files byte for byte afterwards. The reason is not caution about bugs. A scoring
change that applies itself is a scoring change nobody read, on a corpus scored under the
rules it just replaced -- and the only mechanism that would have caught it, comparing
this week's precision against last week's, is the very thing being edited. A human
applies the diff, bumps `ARITHMETIC_VERSION`, and reconciles. That sequence is three
steps for a reason.

**Every proposal cites the leads that justify it.** A proposal is a claim about the
corpus, and a claim with no rows behind it is a hunch with a percentage attached. The
`evidence` field is lead ids, not prose, so the person reading can pull the cards up and
disagree.

What it looks at, and why each one has already produced a real defect:

- **A penalty that fires on nearly everything is a constant offset, not a
  discriminator.** `single_source` sat at 96% incidence, subtracting the same points
  from almost every lead and separating nothing. Found by hand once; found here now.
- **A penalty and a component charging the same fact.** `no_contact` (-25) and
  `reachability` (0-15) both priced "we found nobody to talk to", so one fact cost 40
  points of a 100-point scale.
- **Discovery templates that produce nothing sendable.** Unfiltered Show HN reached 82%
  of the corpus and almost none of it had payroll. A weight in `icp.yaml` is a guess
  until `discovered_by` disagrees with it.
- **Trigger weights the feedback disagrees with.** The only proposal grounded in human
  judgement rather than in the system's own arithmetic, and therefore the only one that
  can catch the whole scale being miscalibrated in one direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from cindraleads.diagnose import LeadRow, ScoreDiagnosis, diagnose, read_leads
from cindraleads.logging import get_logger
from cindraleads.models import utcnow
from cindraleads.scoring import ScoringConfig
from cindraleads.store import Store

__all__ = ["Critique", "Proposal", "critique", "render_markdown"]

log = get_logger("cindraleads.critic")

# Below this many leads the corpus cannot support an argument, and a Critic that
# proposes anyway teaches you to ignore it. Phase 8's acceptance asks for >= 3
# proposals; it does not ask for three proposals from eleven leads.
MIN_CORPUS = 25

# A penalty firing on this share of leads is subtracting a constant. `single_source` was
# at 0.96 when it was found by hand.
CONSTANT_OFFSET_INCIDENCE = 0.85

# A template needs at least this many companies before its hit rate means anything. Two
# companies and one sendable lead is not a 50% hit rate.
MIN_TEMPLATE_SAMPLE = 8

# Judged leads needed before feedback can argue with a trigger weight. Deliberately
# small -- the point is to surface a disagreement early and let a human weigh it, not
# to reach significance.
MIN_JUDGED_PER_TRIGGER = 4

# A penalty whose removal would promote this share of the corpus a tier is miscalibrated
# for that corpus, whatever its incidence. `single_source` sat at 52% -- well under
# CONSTANT_OFFSET_INCIDENCE, so no rule caught it -- while holding back 47 of 262 leads,
# the largest single lever in the report. `cindra explain` has always told humans to
# read it that way ("if 'lifting it alone promotes' is large, the penalty is
# miscalibrated"); this is the Critic applying its own advice.
HELD_BACK_SHARE = 0.10

# ...but only for a penalty that is also widespread. "Promotes a tenth of the corpus" is
# sensitive to where scores happen to sit relative to a threshold, so on its own it fires
# on any penalty applied to leads near a boundary -- including one that is simply doing
# its job. The pair is the signal: fires on a large minority AND decides the tier for a
# tenth of everything. `single_source` at 52% and 18% sits in exactly the band between
# an ordinary discriminator and CONSTANT_OFFSET_INCIDENCE, which is why no rule saw it.
HELD_BACK_INCIDENCE = 0.40


@dataclass(frozen=True)
class Proposal:
    """One concrete change, the file it belongs in, and the rows that argue for it."""

    target: str  # "scoring.yaml" | "icp.yaml"
    key: str  # the setting, e.g. "penalties.single_source"
    change: str  # what to do, concretely enough to type
    rationale: str
    evidence: tuple[str, ...] = ()  # lead ids, template ids -- things you can go look at
    confidence: str = "medium"  # low | medium | high

    def render(self) -> str:
        cited = ", ".join(self.evidence[:8]) or "(corpus-wide)"
        more = f" (+{len(self.evidence) - 8} more)" if len(self.evidence) > 8 else ""
        return (
            f"### `{self.target}` — `{self.key}`\n\n"
            f"**Change:** {self.change}\n\n"
            f"**Why:** {self.rationale}\n\n"
            f"**Evidence ({self.confidence} confidence):** {cited}{more}\n"
        )


@dataclass
class Critique:
    generated_at: datetime
    corpus: int = 0
    judged: int = 0
    proposals: list[Proposal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def applied(self) -> int:
        """Always zero. Present so the report says so out loud rather than by omission."""
        return 0


def critique(store: Store, *, config: ScoringConfig | None = None) -> Critique:
    """Read the corpus and every verdict a human has left; return proposals, change nothing.

    Unwindowed on purpose, unlike `precision_report`. That figure answers "how are we
    doing lately" and a stale week would mislead it; this one argues about weights, and
    a week of reactions is a handful of leads -- narrowing to it would mean either no
    proposals or proposals resting on four rows.
    """
    cfg = config or ScoringConfig.load()
    report = Critique(generated_at=utcnow())

    leads = read_leads(store, cfg)
    diag = diagnose(store, config=cfg)
    report.corpus = len(leads)

    if len(leads) < MIN_CORPUS:
        report.notes.append(
            f"Only {len(leads)} scored leads; below the {MIN_CORPUS} needed to argue "
            f"about calibration. No proposals."
        )
        return report

    if diag.stale_calibration:
        # Reading a stale corpus is not fatal but it is not honest either: the numbers
        # describe rules that are no longer running, so a proposal derived from them
        # argues with the past. Say so at the top rather than in a footnote.
        report.notes.append(
            f"{diag.stale_calibration} of {len(leads)} leads were scored under an older "
            f"calibration. Run `cindra reconcile` and re-read before acting on the "
            f"numbers below."
        )

    report.proposals += _constant_offset_penalties(diag, leads, cfg)
    report.proposals += _held_back_penalties(diag, cfg)
    report.proposals += _double_charged_penalties(diag, cfg)
    report.proposals += _unproductive_templates(diag)
    report.proposals += _outperforming_templates(diag)
    report.proposals += _feedback_disagreements(store, cfg, report=report)
    report.proposals += _dead_components(diag, cfg)

    log.info(
        "critique_generated",
        corpus=report.corpus,
        judged=report.judged,
        proposals=len(report.proposals),
        applied=report.applied,
    )
    return report


# ------------------------------------------------------------------- the arguments


def _constant_offset_penalties(
    diag: ScoreDiagnosis, leads: list[LeadRow], cfg: ScoringConfig
) -> list[Proposal]:
    """A penalty that fires on nearly every lead is a constant subtracted from the scale."""
    out: list[Proposal] = []
    total = max(1, diag.total)
    for name, count in sorted(diag.penalty_counts.items(), key=lambda kv: -kv[1]):
        incidence = count / total
        if incidence < CONSTANT_OFFSET_INCIDENCE or not _still_configured(name, cfg):
            continue
        cited = tuple(lead.lead_id for lead in leads if name in lead.penalties)
        out.append(
            Proposal(
                target="scoring.yaml",
                key=f"penalties.{name}",
                change=(
                    f"Narrow the rule so it separates leads, or fold its cost into the "
                    f"tier thresholds and remove it. As written it subtracts "
                    f"{abs(diag.penalty_cost.get(name, 0.0)) / max(1, count):.0f} points "
                    f"from {count} of {total} leads."
                ),
                rationale=(
                    f"`{name}` fires on {incidence:.0%} of the corpus. A penalty that "
                    f"applies to nearly everything moves the whole distribution down "
                    f"and ranks nothing -- the tier floor then does the work the "
                    f"penalty was supposed to do. This is exactly the shape "
                    f"`single_source` had at 96% before it was rewritten to count "
                    f"sources rather than the top trigger."
                ),
                evidence=cited,
                confidence="high" if count >= 20 else "medium",
            )
        )
    return out


def _still_configured(name: str, cfg: ScoringConfig) -> bool:
    """Whether a penalty seen in the corpus still exists in the running config.

    `penalty_counts` is read off stored `score_breakdown` rows, which record what the
    build that scored each lead applied -- not what the current file says. `no_contact`
    was deleted from `scoring.yaml` on 2026-08-18 and three leads scored before that
    still carry it, so the Critic proposed cutting a key that is not in the file, citing
    a point value it could only have got from history. A reader would have opened
    `scoring.yaml`, found nothing to edit, and learned to distrust the report.

    The staleness was even visible: those leads were among the four the same report
    flagged as scored under an older calibration, at the top of its own output.
    """
    return name in cfg.penalties


def _held_back_penalties(diag: ScoreDiagnosis, cfg: ScoringConfig) -> list[Proposal]:
    """A penalty that is the only thing standing between many leads and a better tier.

    Incidence alone misses this. `single_source` fired on 52% of the corpus -- nowhere
    near `CONSTANT_OFFSET_INCIDENCE` -- while being the single largest lever available:
    lifting it promoted 47 of 262 leads. A penalty can be a perfectly good discriminator
    and still be priced too high for the corpus it is discriminating over, and that is
    a different question from whether it fires too often.
    """
    out: list[Proposal] = []
    total = max(1, diag.total)
    for name, promoted in sorted(diag.promoted_by_lifting.items(), key=lambda kv: -kv[1]):
        share = promoted / total
        incidence = diag.penalty_counts.get(name, 0) / total
        if share < HELD_BACK_SHARE or incidence < HELD_BACK_INCIDENCE:
            continue
        if incidence >= CONSTANT_OFFSET_INCIDENCE or not _still_configured(name, cfg):
            # Above that line it is a constant offset and the other rule says so better.
            continue
        cost = abs(cfg.penalties.get(name, 0.0))
        out.append(
            Proposal(
                target="scoring.yaml",
                key=f"penalties.{name}",
                change=(
                    f"Halve `{name}` to about {cost / 2:.0f} points, or make it "
                    f"proportional to how thin the corroboration actually is."
                ),
                rationale=(
                    f"`{name}` is the only thing holding {promoted} of {total} leads "
                    f"({share:.0%}) out of a higher tier -- lifting it alone promotes "
                    f"them. It fires on {diag.penalty_counts.get(name, 0)} lead(s), so "
                    f"it is still discriminating between leads rather than subtracting a "
                    f"constant; it is simply priced for a corpus with better "
                    f"corroboration than this one has. Re-price it or improve what it "
                    f"measures, but at this share it is deciding the tier distribution "
                    f"on its own."
                ),
                evidence=(),
                confidence="medium",
            )
        )
    return out


def _double_charged_penalties(diag: ScoreDiagnosis, cfg: ScoringConfig) -> list[Proposal]:
    """A penalty whose cost rivals the whole usable range of the scale.

    `no_contact` was -25 against a 100-point scale with a Tier C floor of 40, on top of
    a `reachability` component that already priced the same fact. One fact cost 40% of
    the scale, and no lead missing a contact could clear the floor no matter what else
    was true of it.
    """
    out: list[Proposal] = []
    floor = diag.floor
    headroom = max(1.0, 100.0 - floor)
    for name, count in diag.penalty_counts.items():
        if not count or not _still_configured(name, cfg):
            continue
        per_lead = abs(diag.penalty_cost.get(name, 0.0)) / count
        if per_lead < headroom * 0.4:
            continue
        promoted = diag.promoted_by_lifting.get(name, 0)
        out.append(
            Proposal(
                target="scoring.yaml",
                key=f"penalties.{name}",
                change=(
                    f"Cut `{name}` to at most {headroom * 0.25:.0f} points, or remove it "
                    f"and let the gradient component covering the same fact carry it "
                    f"alone."
                ),
                rationale=(
                    f"`{name}` costs {per_lead:.0f} points against {headroom:.0f} points "
                    f"of usable range above the Tier C floor of {floor:.0f}. Lifting it "
                    f"alone promotes {promoted} lead(s) a tier. A single penalty worth "
                    f"more than a quarter of the range is a veto wearing a score's "
                    f"clothes, and if a component already grades the same fact the lead "
                    f"is charged twice for it."
                ),
                evidence=(),
                confidence="medium",
            )
        )
    return out


def _unproductive_templates(diag: ScoreDiagnosis) -> list[Proposal]:
    """Discovery weights are guesses until `discovered_by` disagrees with them."""
    out: list[Proposal] = []
    measured = [t for t in diag.by_template if t.companies >= MIN_TEMPLATE_SAMPLE]
    if len(measured) < 2:
        return out

    corpus_rate = sum(t.sendable for t in measured) / max(1, sum(t.companies for t in measured))
    for tpl in measured:
        if tpl.template_id == "(unknown)":
            continue  # discovered before the column existed; nothing to reweight
        if tpl.hit_rate >= corpus_rate * 0.5:
            continue
        out.append(
            Proposal(
                target="icp.yaml",
                key=f"query_templates.{tpl.template_id}.weight",
                change=(
                    "Lower the weight, add a filter that implies payroll or investors, "
                    "or retire it. With a 12-plan budget per run this template is "
                    "consuming a slot a better one could use."
                ),
                rationale=(
                    f"{tpl.companies} companies discovered, {tpl.sendable} sendable "
                    f"({tpl.hit_rate:.0%} against a corpus rate of {corpus_rate:.0%}), "
                    f"mean score {tpl.mean_score:.1f}. The question a weight answers is "
                    f"not what a hit announces but what it proves: a public ATS board "
                    f"implies payroll, a Show HN post implies a weekend."
                ),
                evidence=(tpl.template_id,),
                confidence="high" if tpl.companies >= 25 else "low",
            )
        )
    return out


def _outperforming_templates(diag: ScoreDiagnosis) -> list[Proposal]:
    """The other half of the same question, and it was missing.

    Every other rule here proposes taking something away. With a fixed 12-plan budget
    per run, promoting a winner and demoting a loser are the same decision seen from
    two ends -- and only one of them was ever suggested. `hn_ai_agent` sat at weight 52
    while converting 73% against a 33% corpus rate, below several templates it
    outperformed threefold, and nothing in this report would ever have said so.

    Deliberately stricter than the demotion rule: it needs twice the corpus rate *and*
    a mean above it, because raising a weight spends a slot that is currently producing
    something, whereas lowering one only stops waste.
    """
    out: list[Proposal] = []
    measured = [
        t
        for t in diag.by_template
        if t.companies >= MIN_TEMPLATE_SAMPLE and t.template_id != "(unknown)"
    ]
    if len(measured) < 2:
        return out

    corpus_rate = sum(t.sendable for t in measured) / max(1, sum(t.companies for t in measured))
    corpus_mean = sum(t.mean_score * t.companies for t in measured) / max(
        1, sum(t.companies for t in measured)
    )
    for tpl in measured:
        if tpl.hit_rate < corpus_rate * 2 or tpl.mean_score <= corpus_mean:
            continue
        out.append(
            Proposal(
                target="icp.yaml",
                key=f"query_templates.{tpl.template_id}.weight",
                change=(
                    "Raise the weight so this template wins a plan slot more often, and "
                    "consider raising its `max_hits` -- it is being rationed below what "
                    "it earns."
                ),
                rationale=(
                    f"{tpl.companies} companies discovered, {tpl.sendable} sendable "
                    f"({tpl.hit_rate:.0%} against a corpus rate of {corpus_rate:.0%}), "
                    f"mean score {tpl.mean_score:.1f} against {corpus_mean:.1f}. Every "
                    f"other proposal here takes a slot away from something; with a fixed "
                    f"plan budget, giving one to the best converter is the same decision "
                    f"from the other end."
                ),
                evidence=(tpl.template_id,),
                confidence="high" if tpl.companies >= 25 else "low",
            )
        )
    return out


def _dead_components(diag: ScoreDiagnosis, cfg: ScoringConfig) -> list[Proposal]:
    """A weighted component that is zero for everyone is weight spent on nothing.

    Not necessarily a scoring bug -- `reachability` was structurally zero for the whole
    corpus before the Enricher existed, and the fix was the Enricher, not the weight.
    Which is why this proposes investigating the upstream stage first.
    """
    out: list[Proposal] = []
    for name, weight in cfg.components.items():
        if weight <= 0:
            continue
        zeros = diag.component_zero_counts.get(name, 0)
        if not diag.total or zeros < diag.total:
            continue
        out.append(
            Proposal(
                target="scoring.yaml",
                key=f"components.{name}",
                change=(
                    f"Find out why the stage feeding `{name}` produces nothing before "
                    f"touching its {weight:.0%} weight. If the input genuinely does not "
                    f"exist yet, redistribute the weight; if it does, this is an "
                    f"upstream bug and the weight is fine."
                ),
                rationale=(
                    f"`{name}` is zero on all {diag.total} leads while carrying "
                    f"{weight:.0%} of the score, so {weight:.0%} of the scale is "
                    f"unreachable and every lead is capped below 100 by that much. "
                    f"`reachability` looked exactly like this before Phase 4, and the "
                    f"answer was to build the Enricher rather than to drop the weight."
                ),
                evidence=(),
                confidence="high",
            )
        )
    return out


def _feedback_disagreements(
    store: Store, cfg: ScoringConfig, *, report: Critique
) -> list[Proposal]:
    """Where human verdicts and trigger weights point in opposite directions.

    The only argument here grounded outside the system's own arithmetic. Everything else
    can be self-consistently wrong; this cannot, which is why the feedback loop is worth
    a whole systemd unit.
    """
    # Deliberately *not* `precision_report`, which is scoped to leads that were
    # dispatched inside a window -- the right population for "of what we sent, how much
    # was worth sending", and the wrong one here. A verdict typed at the CLI on a lead
    # that never cleared the tier floor is exactly the signal a weight argument needs,
    # and the per-trigger query below counts it. Reporting `judged` from one population
    # and arguing from another put two different numbers under one label, and the
    # symptom was a report claiming nothing was judged directly above a proposal citing
    # eighteen judged leads.
    judged_rows = store.conn.execute(
        "SELECT l.lead_id, "
        "  MAX(CASE WHEN f.verdict = 'bad' THEN 1 ELSE 0 END) AS any_bad, "
        "  MAX(CASE WHEN f.verdict = 'good' THEN 1 ELSE 0 END) AS any_good "
        "FROM leads l JOIN feedback f ON f.lead_id = l.lead_id "
        "WHERE f.verdict IN ('good','bad') GROUP BY l.lead_id"
    ).fetchall()

    report.judged = len(judged_rows)
    if not judged_rows:
        report.notes.append(
            "No lead has been judged by a human, so nothing here is grounded in a "
            "verdict. React to a few cards, or run `cindra feedback <lead_id> "
            "good|bad`, and the next run can argue about weights."
        )
        return []

    # Pessimistic on disagreement, matching `precision_report`: over-reporting precision
    # tunes the whole system toward the wrong target, and chasing one bad lead costs an
    # hour. The two figures must at least agree on what "good" means.
    corpus_precision = sum(
        1 for row in judged_rows if not int(row["any_bad"]) and int(row["any_good"])
    ) / len(judged_rows)

    rows = store.conn.execute(
        # Per trigger code: how the leads carrying it were judged. Counted per lead, and
        # pessimistically, for the same reason `precision_report` is -- a lead two people
        # disagreed about must not be counted twice and must not be counted good.
        "WITH judged AS ("
        "  SELECT l.lead_id, l.canonical_domain, "
        "    MAX(CASE WHEN f.verdict = 'bad' THEN 1 ELSE 0 END) AS any_bad, "
        "    MAX(CASE WHEN f.verdict = 'good' THEN 1 ELSE 0 END) AS any_good "
        "  FROM leads l JOIN feedback f ON f.lead_id = l.lead_id "
        "  WHERE f.verdict IN ('good','bad') GROUP BY l.lead_id) "
        "SELECT t.code AS code, COUNT(DISTINCT j.lead_id) AS judged, "
        "  SUM(CASE WHEN j.any_bad = 0 AND j.any_good = 1 THEN 1 ELSE 0 END) AS good, "
        "  GROUP_CONCAT(DISTINCT j.lead_id) AS lead_ids "
        "FROM judged j JOIN triggers t ON t.canonical_domain = j.canonical_domain "
        "WHERE t.active = 1 GROUP BY t.code"
    ).fetchall()

    out: list[Proposal] = []
    for row in rows:
        judged = int(row["judged"])
        if judged < MIN_JUDGED_PER_TRIGGER:
            continue
        good = int(row["good"] or 0)
        rate = good / judged
        weight = cfg.triggers.get(str(row["code"]))
        if weight is None:
            continue
        cited = tuple(str(row["lead_ids"] or "").split(","))

        if rate < corpus_precision * 0.6:
            out.append(
                Proposal(
                    target="scoring.yaml",
                    key=f"triggers.{row['code']}.weight",
                    change=f"Lower `{row['code']}` from {weight.weight}.",
                    rationale=(
                        f"{good} of {judged} leads carrying `{row['code']}` were judged "
                        f"good ({rate:.0%}), against {corpus_precision:.0%} across all "
                        f"judged leads. The trigger is being weighted as a buying signal "
                        f"and read as noise by the person who has to act on it."
                    ),
                    evidence=cited,
                    confidence="low" if judged < 10 else "medium",
                )
            )
        elif rate > 0.8 and corpus_precision and rate > corpus_precision * 1.4:
            out.append(
                Proposal(
                    target="scoring.yaml",
                    key=f"triggers.{row['code']}.weight",
                    change=f"Raise `{row['code']}` from {weight.weight}.",
                    rationale=(
                        f"{good} of {judged} leads carrying `{row['code']}` were judged "
                        f"good ({rate:.0%}) against a corpus rate of "
                        f"{corpus_precision:.0%}. It is outperforming its weight, and "
                        f"raising it moves leads that already work up the queue rather "
                        f"than widening it."
                    ),
                    evidence=cited,
                    confidence="low" if judged < 10 else "medium",
                )
            )
    return out


# ------------------------------------------------------------------------- render


def render_markdown(report: Critique) -> str:
    """The document a human reads, edits from, and applies by hand."""
    lines = [
        f"# Critic — {report.generated_at:%Y-%m-%d}",
        "",
        f"Corpus: **{report.corpus}** scored leads · judged by a human: **{report.judged}**",
        "",
        f"Proposals: **{len(report.proposals)}** · applied automatically: "
        f"**{report.applied}** (by design — a scoring change is a human's to make, and "
        f"is only half done until `cindra reconcile --force` rescores the corpus under it)",
        "",
    ]
    for note in report.notes:
        lines += [f"> {note}", ""]

    if not report.proposals:
        lines += ["Nothing to propose. The corpus does not disagree with the config."]
        return "\n".join(lines) + "\n"

    for target in ("scoring.yaml", "icp.yaml"):
        group = [p for p in report.proposals if p.target == target]
        if not group:
            continue
        lines += [f"## {target}", ""]
        lines += [p.render() for p in group]

    lines += [
        "---",
        "",
        "**To apply any of these:** edit the file by hand, bump `ARITHMETIC_VERSION` in",
        "`scoring.py` if the arithmetic changed, then `cindra reconcile --force`. Without",
        "the rescore the corpus keeps its old scores and the change reports success",
        "having done nothing.",
    ]
    return "\n".join(lines) + "\n"
