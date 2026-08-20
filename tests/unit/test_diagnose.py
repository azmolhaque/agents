"""The score diagnostic, and the two ways it could mislead.

It exists to answer one question -- bad calibration or weak corpus -- so the tests are
about whether its evidence actually distinguishes those. A diagnostic that reported
the same shape for both would be worse than none, because it would be trusted.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from cindraleads.diagnose import diagnose, read_leads
from cindraleads.models import to_iso, utcnow
from cindraleads.scoring import ScoringConfig


def _lead(store: Any, domain: str, *, score: int, tier: str, **breakdown: float) -> str:
    lead_id = uuid.uuid4().hex[:16]
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO companies (canonical_domain, display_name, first_seen_at, "
            "last_updated_at) VALUES (?,?,?,?)",
            (domain, domain.split(".")[0].title(), now, now),
        )
        conn.execute(
            "INSERT INTO leads (lead_id, canonical_domain, score, score_breakdown, tier, "
            "recommended_offer, first_seen_at, last_updated_at, pipeline_version) "
            "VALUES (?,?,?,?,?,'snapshot_free',?,?,'test')",
            (lead_id, domain, score, json.dumps(breakdown), tier, now, now),
        )
    return lead_id


def _strong(**overrides: float) -> dict[str, float]:
    """Component values that clear Tier C comfortably before penalties."""
    base = {
        "trigger": 80.0,
        "icp_fit": 70.0,
        "reachability": 60.0,
        "surface": 60.0,
        "freshness": 90.0,
    }
    base.update(overrides)
    return base


def _weak(**overrides: float) -> dict[str, float]:
    base = {"trigger": 8.0, "icp_fit": 30.0, "reachability": 0.0, "surface": 0.0, "freshness": 20.0}
    base.update(overrides)
    return base


# ----------------------------------------------------------------------- reading


def test_components_and_penalties_are_told_apart(store: Any) -> None:
    """`score_breakdown` is one flat dict of both. The split is by the component names
    in `scoring.yaml`, so a component added tomorrow is not misfiled as a penalty."""
    _lead(store, "acme.io", score=45, tier="C", **_strong(), no_contact=-25.0)
    cfg = ScoringConfig.load()

    rows = read_leads(store, cfg)

    assert len(rows) == 1
    assert set(rows[0].components) == set(cfg.components)
    assert rows[0].penalties == {"no_contact": -25.0}


def test_an_unreadable_breakdown_does_not_crash_the_report(store: Any) -> None:
    _lead(store, "acme.io", score=45, tier="C", **_strong())
    with store.tx() as conn:
        conn.execute("UPDATE leads SET score_breakdown = 'not json'")

    report = diagnose(store)
    assert report.total == 1


def test_an_empty_corpus_reports_nothing_rather_than_dividing_by_zero(store: Any) -> None:
    report = diagnose(store)
    assert report.total == 0
    assert report.component_means == {}


# --------------------------------------------------------- the two diagnoses


def test_a_penalty_holding_back_a_good_corpus_is_visible(store: Any) -> None:
    """The calibration case. Strong components, one penalty, everything rejected --
    and lifting that penalty alone rescues all of them."""
    for n in range(10):
        _lead(
            store,
            f"good{n}.io",
            score=0,
            tier="REJECT",
            **_strong(),
            no_contact=-25.0,
            single_source=-15.0,
        )

    report = diagnose(store)

    assert report.tiers["REJECT"] == 10
    assert report.dispatchable == 0
    # With no penalties every one of them clears a tier.
    assert report.tiers_unpenalised.get("REJECT", 0) == 0
    assert report.promoted_by_lifting.get("no_contact", 0) > 0


def test_a_weak_corpus_is_not_blamed_on_penalties(store: Any) -> None:
    """The other case, and the one that must not be confused with the first.

    Floor-level components and no penalty applied: lifting nothing helps, and the
    report has to say so rather than implying a config edit would fix it.
    """
    for n in range(10):
        _lead(store, f"weak{n}.io", score=12, tier="REJECT", **_weak())

    report = diagnose(store)

    assert report.penalty_counts == {}
    assert report.promoted_by_lifting == {}
    # Still rejected even with every penalty lifted, because there were none.
    assert report.tiers_unpenalised.get("REJECT", 0) == 10
    assert report.component_means["trigger"] < 20


def test_a_penalty_that_changes_nothing_is_reported_as_such(store: Any) -> None:
    """A penalty on a lead too weak to be rescued by lifting it must not be counted as
    a promotion. Otherwise every penalty looks miscalibrated on a bad corpus."""
    for n in range(5):
        _lead(store, f"hopeless{n}.io", score=0, tier="REJECT", **_weak(), no_contact=-25.0)

    report = diagnose(store)

    assert report.penalty_counts["no_contact"] == 5
    assert report.promoted_by_lifting.get("no_contact", 0) == 0


# ------------------------------------------------------------------ arithmetic


def test_the_counterfactual_recovers_raw_below_zero(store: Any) -> None:
    """The stored score is clamped at zero, so a lead whose arithmetic came to -8 and
    one that came to exactly 0 are indistinguishable in the `score` column -- and that
    difference is precisely what says whether lifting a penalty could reach it.

    Both leads below store 0. Only the one whose true raw is close to the floor is
    promotable, and the report must not treat them alike.
    """
    _lead(store, "close.io", score=0, tier="REJECT", **_strong(), no_contact=-25.0)
    _lead(store, "hopeless.io", score=0, tier="REJECT", **_weak(), no_contact=-25.0)

    report = diagnose(store)

    assert report.promoted_by_lifting.get("no_contact") == 1, (
        "only the lead whose true raw clears the floor should count as promotable"
    )


def test_near_misses_rank_by_true_distance_not_stored_score(store: Any) -> None:
    """Both store 0. Ranking by the clamped number would put a hopeless lead level
    with a genuinely marginal one."""
    _lead(store, "close.io", score=0, tier="REJECT", **_strong(), no_contact=-25.0)
    _lead(store, "hopeless.io", score=0, tier="REJECT", **_weak(), no_contact=-25.0)

    report = diagnose(store)

    assert [row.domain for row, _, _ in report.near_misses] == ["close.io", "hopeless.io"]
    gaps = [gap for _, gap, _ in report.near_misses]
    assert gaps[0] < gaps[1]


def test_the_blocker_names_the_worst_penalty(store: Any) -> None:
    _lead(
        store, "acme.io", score=0, tier="REJECT", **_strong(), no_contact=-25.0, single_source=-15.0
    )

    report = diagnose(store)
    _, _, blocker = report.near_misses[0]

    assert "no_contact" in blocker, "the heaviest penalty is the one worth naming"


def test_a_lead_with_no_penalty_is_blamed_on_its_weakest_component(store: Any) -> None:
    """Nothing to lift, so the useful thing to say is which component is empty."""
    _lead(store, "acme.io", score=12, tier="REJECT", **_weak(reachability=0.0))

    report = diagnose(store)
    _, _, blocker = report.near_misses[0]

    assert "weak" in blocker


def test_only_rejected_leads_appear_in_near_misses(store: Any) -> None:
    _lead(store, "sendable.io", score=61, tier="B", **_strong())
    _lead(store, "rejected.io", score=20, tier="REJECT", **_weak())

    report = diagnose(store)

    assert [row.domain for row, _, _ in report.near_misses] == ["rejected.io"]
    assert report.dispatchable == 1


def test_zero_components_are_counted_separately_from_low_ones(store: Any) -> None:
    """A component that is structurally zero across the corpus -- reachability before
    the Enricher existed -- is a different finding from one that is merely low."""
    for n in range(4):
        _lead(store, f"c{n}.io", score=20, tier="REJECT", **_weak(reachability=0.0, surface=5.0))

    report = diagnose(store)

    assert report.component_zero_counts["reachability"] == 4
    assert report.component_zero_counts.get("surface", 0) == 0


def test_the_report_writes_nothing(store: Any) -> None:
    """It informs a calibration decision; it must never make one."""
    _lead(store, "acme.io", score=45, tier="C", **_strong(), no_contact=-25.0)
    before = {
        table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("leads", "companies", "jobs", "triggers", "metrics", "dispatch_log")
    }

    diagnose(store)

    after = {
        table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before
    }
    assert after == before
    assert store.conn.execute("SELECT score, tier FROM leads").fetchone()[0] == 45


# --------------------------------------------------- evidence breadth vs the rule


def _trigger_with_evidence(store: Any, domain: str, code: str, sources: list[str]) -> None:
    """One live trigger citing one URL per named source."""
    now = to_iso(utcnow())
    trigger_id = uuid.uuid4().hex[:16]
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at, active) VALUES (?,?,?,0.7,?,'2099-01-01T00:00:00+00:00',1)",
            (trigger_id, domain, code, now),
        )
        for source in sources:
            evidence_id = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,?,'x',?,'')",
                (evidence_id, f"https://{domain}/{source}", source, now),
            )
            conn.execute(
                "INSERT INTO trigger_evidence (trigger_id, evidence_id) VALUES (?,?)",
                (trigger_id, evidence_id),
            )


def test_breadth_counts_sources_across_every_trigger(store: Any) -> None:
    """The gap the report exists to expose: `single_source` inspects only the top
    trigger, so a lead resting on three independent sources still carries it."""
    _lead(store, "acme.io", score=0, tier="REJECT", **_strong(), single_source=-15.0)
    _trigger_with_evidence(store, "acme.io", "T1_AI_SHIP", ["company_site"])
    _trigger_with_evidence(store, "acme.io", "T8_HYGIENE_GAP", ["dns_public"])
    _trigger_with_evidence(store, "acme.io", "T2_FUNDING", ["serpapi_news"])

    report = diagnose(store)

    assert report.corroborated == 1
    assert report.penalised_but_corroborated == 1
    assert report.promoted_if_corroboration_counted == 1


def test_a_genuinely_single_sourced_lead_is_not_exonerated(store: Any) -> None:
    """Three pages of a company's own site are three URLs and one party's account of
    itself. Counting URLs instead of sources would call that corroboration."""
    _lead(store, "acme.io", score=0, tier="REJECT", **_strong(), single_source=-15.0)
    _trigger_with_evidence(store, "acme.io", "T1_AI_SHIP", ["company_site"])
    _trigger_with_evidence(store, "acme.io", "T4_HIRING_AI_ONLY", ["company_site"])

    report = diagnose(store)

    assert report.corroborated == 0
    assert report.penalised_but_corroborated == 0


def test_a_decayed_trigger_does_not_count_as_corroboration(store: Any) -> None:
    """Breadth has to mean live evidence. A retired trigger still in the table would
    otherwise make a thin lead look independently confirmed."""
    _lead(store, "acme.io", score=0, tier="REJECT", **_strong(), single_source=-15.0)
    _trigger_with_evidence(store, "acme.io", "T1_AI_SHIP", ["company_site"])
    _trigger_with_evidence(store, "acme.io", "T8_HYGIENE_GAP", ["dns_public"])
    with store.tx() as conn:
        conn.execute("UPDATE triggers SET active = 0 WHERE code = 'T8_HYGIENE_GAP'")

    report = diagnose(store)

    assert report.corroborated == 0


def test_breadth_is_reported_never_applied(store: Any) -> None:
    """It informs a rule change; it must not be one. The stored score is untouched."""
    _lead(store, "acme.io", score=0, tier="REJECT", **_strong(), single_source=-15.0)
    _trigger_with_evidence(store, "acme.io", "T1_AI_SHIP", ["company_site"])
    _trigger_with_evidence(store, "acme.io", "T2_FUNDING", ["serpapi_news"])

    report = diagnose(store)

    assert report.promoted_if_corroboration_counted == 1
    row = store.conn.execute("SELECT score, tier FROM leads").fetchone()
    assert (row[0], row[1]) == (0, "REJECT"), "diagnosing must not rescore"


# ------------------------------------------------------- reporting its own freshness


def test_a_stale_calibration_is_flagged(store: Any) -> None:
    """`cindra reconcile` only enqueues, so reading the report straight after a
    calibration change shows the corpus exactly as the old rules left it. Without this
    the fix looks like it did nothing, and every number below describes a config that
    is no longer running."""
    _lead(store, "acme.io", score=45, tier="C", **_strong(), no_contact=-25.0)
    with store.tx() as conn:
        conn.execute("UPDATE leads SET scoring_version = 'from-an-older-config'")

    report = diagnose(store)

    assert report.stale_calibration == 1
    assert report.is_current is False


def test_a_rescored_corpus_reports_current(store: Any) -> None:
    _lead(store, "acme.io", score=45, tier="C", **_strong(), no_contact=-25.0)
    with store.tx() as conn:
        conn.execute("UPDATE leads SET scoring_version = ?", (ScoringConfig.load().fingerprint(),))

    report = diagnose(store)

    assert report.stale_calibration == 0
    assert report.is_current is True


def test_a_lead_predating_the_column_counts_as_stale(store: Any) -> None:
    _lead(store, "acme.io", score=45, tier="C", **_strong())

    assert diagnose(store).stale_calibration == 1


# ------------------------------------------------------ discovery yield by template


def _discovered(store: Any, domain: str, template: str | None, *, tier: str, score: int) -> None:
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, discovered_by, "
            "first_seen_at, last_updated_at) VALUES (?,?,?,?,?)",
            (domain, domain, template, now, now),
        )
        conn.execute(
            "INSERT INTO leads (lead_id, canonical_domain, score, score_breakdown, tier, "
            "recommended_offer, first_seen_at, last_updated_at, pipeline_version) "
            "VALUES (?,?,?,'{}',?,'snapshot_free',?,?,'t')",
            (uuid.uuid4().hex[:16], domain, score, tier, now, now),
        )


def test_yield_separates_a_good_template_from_a_bad_one(store: Any) -> None:
    """The number that makes a query rewrite evidence-based instead of a guess."""
    from cindraleads.diagnose import template_yield

    for n in range(4):
        _discovered(store, f"hire{n}.io", "hn_who_is_hiring", tier="C", score=45)
    for n in range(6):
        _discovered(store, f"toy{n}.io", "hn_show_ai", tier="REJECT", score=10)

    rows = {r.template_id: r for r in template_yield(store)}

    assert rows["hn_who_is_hiring"].sendable == 4
    assert rows["hn_who_is_hiring"].hit_rate == 1.0
    assert rows["hn_show_ai"].sendable == 0
    assert rows["hn_show_ai"].hit_rate == 0.0
    assert rows["hn_show_ai"].companies == 6


def test_companies_found_before_provenance_are_shown_not_dropped(store: Any) -> None:
    """They are most of the corpus right now. Excluding them would make a handful of
    new rows look like the whole picture."""
    from cindraleads.diagnose import template_yield

    _discovered(store, "old.io", None, tier="REJECT", score=10)
    _discovered(store, "new.io", "hn_funding", tier="C", score=45)

    rows = {r.template_id: r for r in template_yield(store)}

    assert rows["(unknown)"].companies == 1
    assert rows["hn_funding"].companies == 1


def test_an_unscored_company_counts_as_found_but_not_sendable(store: Any) -> None:
    """Otherwise a template that finds companies the pipeline has not reached yet
    looks like a template that finds nothing."""
    from cindraleads.diagnose import template_yield

    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, discovered_by, "
            "first_seen_at, last_updated_at) VALUES ('pending.io','Pending','hn_funding',?,?)",
            (now, now),
        )

    row = next(r for r in template_yield(store) if r.template_id == "hn_funding")
    assert (row.companies, row.scored, row.sendable) == (1, 0, 0)


# ------------------------------------------- templates that produce nothing at all


def _harvest_run(store: Any, template_id: str, *, hits: int, candidates: int, dropped: int) -> None:
    from cindraleads.agents.harvester import HARVEST_YIELD_METRIC

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO metrics (name, value, labels, recorded_at) VALUES (?,?,?,?)",
            (
                HARVEST_YIELD_METRIC,
                float(candidates),
                json.dumps(
                    {
                        "template_id": template_id,
                        "engine": "serpapi_jobs",
                        "hits": hits,
                        "candidates": candidates,
                        "dropped_platform": dropped,
                    },
                    separators=(",", ":"),
                ),
                to_iso(utcnow()),
            ),
        )


def test_a_template_that_produces_no_company_is_still_visible(store: Any) -> None:
    """The blind spot this table exists to close.

    `by_template` groups `companies.discovered_by`, so a template returning nothing but
    platform URLs has no row there at all -- it reads as one that was never tried rather
    than one that fails every run. Two were doing exactly that at weights 98 and 94,
    ten hits and ten dropped each time, spending SerpAPI credits hourly.
    """
    from cindraleads.diagnose import harvest_yield

    for _ in range(6):
        _harvest_run(store, "serpapi_marketplace", hits=10, candidates=0, dropped=10)
    _harvest_run(store, "hn_who_is_hiring", hits=50, candidates=31, dropped=19)

    found = {y.template_id: y for y in harvest_yield(store)}

    assert found["serpapi_marketplace"].is_barren is True
    assert found["serpapi_marketplace"].runs == 6
    assert found["serpapi_marketplace"].drop_rate == 1.0
    assert found["hn_who_is_hiring"].is_barren is False


def test_the_worst_template_is_reported_first(store: Any) -> None:
    """Ordered by candidates ascending, so the failing ones are what you read."""
    from cindraleads.diagnose import harvest_yield

    _harvest_run(store, "productive", hits=50, candidates=31, dropped=19)
    _harvest_run(store, "barren", hits=10, candidates=0, dropped=10)

    assert next(y.template_id for y in harvest_yield(store)) == "barren"


def test_a_template_that_simply_found_nothing_is_not_called_barren(store: Any) -> None:
    """Zero hits is a quiet week or a cached query, not a broken template. Only a
    template that *found* things and converted none of them has a problem."""
    from cindraleads.diagnose import harvest_yield

    _harvest_run(store, "quiet", hits=0, candidates=0, dropped=0)

    assert harvest_yield(store)[0].is_barren is False
