"""The nightly pass, and the four ways it could be quietly wrong.

Each block here corresponds to a way maintenance could look like it worked:
retiring a trigger we could not evaluate, deleting evidence a contact still cites,
marking a link dead because robots said no, or retiring a trigger without ever
re-scoring the lead that quotes it.
"""

from __future__ import annotations

import random
import uuid
from datetime import timedelta
from typing import Any

import pytest

from cindraleads.maintenance import (
    RETIREMENT_RULES,
    MaintenanceConfig,
    enqueue_rescore,
    expire_decayed_triggers,
    purge_retention,
    resample_evidence,
    retire_superseded_triggers,
    retire_unevidenced_triggers,
    run_maintenance,
)
from cindraleads.models import DnsHygiene, to_iso, utcnow

# A domain that publishes SPF and an enforcing DMARC: the narrowed rule finds no gap.
CLEAN = DnsHygiene(
    mx_present=True,
    spf="v=spf1 include:_spf.google.com ~all",
    dmarc_policy="reject",
    dnssec=False,
    security_txt=False,
).model_dump_json()

# Receives mail, publishes neither. Still a real gap under the narrowed rule.
WEAK = DnsHygiene(
    mx_present=True, spf=None, dmarc_policy=None, dnssec=False, security_txt=False
).model_dump_json()


# --------------------------------------------------------------------------- helpers


def _company(store: Any, domain: str, *, hygiene: str | None = None, updated: Any = None) -> None:
    stamp = to_iso(updated or utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, dns_hygiene, "
            "first_seen_at, last_updated_at) VALUES (?,?,?,?,?)",
            (domain, domain.split(".")[0].title(), hygiene, stamp, stamp),
        )


def _trigger(
    store: Any,
    domain: str,
    code: str,
    *,
    decays_in_days: float = 30.0,
    evidence: list[tuple[str, int | None]] | None = None,
) -> str:
    now = utcnow()
    trigger_id = uuid.uuid4().hex[:16]
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at, active) VALUES (?,?,?,?,?,?,1)",
            (
                trigger_id,
                domain,
                code,
                0.8,
                to_iso(now),
                to_iso(now + timedelta(days=decays_in_days)),
            ),
        )
        for url, reachable in evidence or []:
            evidence_id = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256, reachable) VALUES (?,?,?,?,?,'',?)",
                (evidence_id, url, "company_site", "snippet", to_iso(now), reachable),
            )
            conn.execute(
                "INSERT INTO trigger_evidence (trigger_id, evidence_id) VALUES (?,?)",
                (trigger_id, evidence_id),
            )
    return trigger_id


def _active(store: Any, trigger_id: str) -> int:
    row = store.conn.execute(
        "SELECT active FROM triggers WHERE trigger_id = ?", (trigger_id,)
    ).fetchone()
    return int(row["active"])


class _Queue:
    """Records enqueues without needing the real queue's dedupe table semantics."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def enqueue(self, kind: str, payload: dict[str, Any], **kwargs: Any) -> str:
        self.calls.append((kind, payload, kwargs.get("dedupe_key")))
        return uuid.uuid4().hex


# ------------------------------------------------------------------ config validation


def test_config_loads_from_the_repo() -> None:
    cfg = MaintenanceConfig.load()
    assert cfg.window("contacts_days") > 0
    assert 0.0 <= cfg.resample_fraction <= 1.0


def test_unknown_retention_window_is_an_error() -> None:
    from cindraleads.errors import ConfigError

    with pytest.raises(ConfigError, match="no retention window"):
        MaintenanceConfig.load().window("nonexistent_days")


def test_done_jobs_window_outlives_any_dedupe_bucket() -> None:
    """Purging a `done` job un-dedupes the work it represents.

    `JobQueue.enqueue` matches `dedupe_key` across every job including completed ones,
    so this window is not housekeeping -- it is the upper bound on how long a harvest
    query stays de-duplicated. `dedupe_key_for` buckets by the plan's cache TTL, so a
    retention window shorter than the longest bucket would let a query re-run inside
    its own bucket and re-spend a SerpAPI credit.

    The margin is 2x rather than 1x because the bucket boundary is absolute-time, not
    per-job: a job completed at the very start of a 30 day bucket must still be there
    at the end of it.
    """
    from cindraleads.sources.registry import SourceRegistry

    sources = SourceRegistry.from_config().sources.values()
    longest_bucket_days = max(float(source.cache_ttl_hours) for source in sources) / 24
    assert MaintenanceConfig.load().window("done_jobs_days") >= longest_bucket_days * 2


# ------------------------------------------------------------------ rule retirement


def test_superseded_trigger_is_retired(store: Any) -> None:
    _company(store, "clean.io", hygiene=CLEAN)
    trigger_id = _trigger(store, "clean.io", "T8_HYGIENE_GAP")

    count, by_code, domains = retire_superseded_triggers(store)

    assert count == 1
    assert by_code == {"T8_HYGIENE_GAP": 1}
    assert domains == ["clean.io"]
    assert _active(store, trigger_id) == 0


def test_still_justified_trigger_survives(store: Any) -> None:
    _company(store, "weak.io", hygiene=WEAK)
    trigger_id = _trigger(store, "weak.io", "T8_HYGIENE_GAP")

    count, _, _ = retire_superseded_triggers(store)

    assert count == 0
    assert _active(store, trigger_id) == 1


def test_unreadable_hygiene_never_retires(store: Any) -> None:
    """The whole reason `RuleCheck` returns `bool | None`.

    A company we never enriched has `dns_hygiene = NULL`, which is indistinguishable
    from a company with no gaps if you only look at the truthiness. Retiring on that
    deletes a true claim because of a missing lookup -- the same failure mode as
    reporting "no SPF" after a resolver timeout.
    """
    _company(store, "unknown.io", hygiene=None)
    _company(store, "corrupt.io", hygiene="{not json")
    unknown = _trigger(store, "unknown.io", "T8_HYGIENE_GAP")
    corrupt = _trigger(store, "corrupt.io", "T8_HYGIENE_GAP")

    count, _, _ = retire_superseded_triggers(store)

    assert count == 0
    assert _active(store, unknown) == 1
    assert _active(store, corrupt) == 1


def test_retirement_only_touches_registered_codes(store: Any) -> None:
    """A rule with no entry in `RETIREMENT_RULES` is not a rule this pass evaluates."""
    _company(store, "clean.io", hygiene=CLEAN)
    untouched = _trigger(store, "clean.io", "T1_AI_SHIP")

    retire_superseded_triggers(store)

    assert _active(store, untouched) == 1


def test_dry_run_changes_nothing(store: Any) -> None:
    _company(store, "clean.io", hygiene=CLEAN)
    trigger_id = _trigger(store, "clean.io", "T8_HYGIENE_GAP")

    count, _, _ = retire_superseded_triggers(store, dry_run=True)

    assert count == 1
    assert _active(store, trigger_id) == 1


def test_every_retirement_rule_names_a_real_trigger() -> None:
    """A typo'd code is a rule that silently never fires."""
    from cindraleads.scoring import ScoringConfig

    known = set(ScoringConfig.load().triggers)
    assert set(RETIREMENT_RULES) <= known, sorted(set(RETIREMENT_RULES) - known)


# ------------------------------------------------------------------------ decay


def test_decayed_trigger_is_deactivated(store: Any) -> None:
    _company(store, "old.io")
    stale = _trigger(store, "old.io", "T1_AI_SHIP", decays_in_days=-1)
    fresh = _trigger(store, "old.io", "T2_FUNDING", decays_in_days=10)

    assert expire_decayed_triggers(store) == 1
    assert _active(store, stale) == 0
    assert _active(store, fresh) == 1
    # Idempotent: a second pass finds nothing left to flip.
    assert expire_decayed_triggers(store) == 0


# ------------------------------------------------------------------ evidence checks


class _Registry:
    def __init__(self, enabled: set[str]) -> None:
        self._enabled = enabled

    def get(self, source_id: str) -> Any:
        if source_id not in self._enabled:
            return None
        return type("Source", (), {"enabled": True})()


class _Egress:
    """Answers per-URL, and records exactly which URLs were asked about."""

    def __init__(self, answers: dict[str, Any], enabled: set[str] | None = None) -> None:
        self.answers = answers
        self.registry = _Registry(enabled if enabled is not None else {"company_site"})
        self.asked: list[str] = []

    async def fetch(self, source_id: str, url: str, **kwargs: Any) -> Any:
        self.asked.append(url)
        answer = self.answers.get(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _status_error(code: int) -> Exception:
    import httpx

    request = httpx.Request("GET", "https://example.test/")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.asyncio
async def test_404_marks_evidence_dead(store: Any) -> None:
    _company(store, "acme.io")
    _trigger(store, "acme.io", "T1_AI_SHIP", evidence=[("https://acme.io/gone", None)])
    egress = _Egress({"https://acme.io/gone": _status_error(404)})
    cfg = MaintenanceConfig.load()

    sampled, checked, dead = await resample_evidence(
        store, egress, config=cfg, rng=random.Random(0)
    )

    assert (sampled, checked, dead) == (1, 1, 1)
    row = store.conn.execute("SELECT reachable, last_checked_at FROM evidence").fetchone()
    assert row["reachable"] == 0
    assert row["last_checked_at"]


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503])
@pytest.mark.asyncio
async def test_inconclusive_status_leaves_reachability_unknown(store: Any, code: int) -> None:
    """401/403 mean "you may not see it", 5xx means "not right now".

    None of those are "the page is gone", and recording them as such would retire a
    trigger whose evidence a human can open perfectly well.
    """
    _company(store, "acme.io")
    _trigger(store, "acme.io", "T1_AI_SHIP", evidence=[("https://acme.io/walled", None)])
    egress = _Egress({"https://acme.io/walled": _status_error(code)})

    sampled, checked, dead = await resample_evidence(
        store, egress, config=MaintenanceConfig.load(), rng=random.Random(0)
    )

    assert (sampled, checked, dead) == (1, 0, 0)
    assert store.conn.execute("SELECT reachable FROM evidence").fetchone()["reachable"] is None


@pytest.mark.asyncio
async def test_robots_denial_is_not_a_dead_link(store: Any) -> None:
    from cindraleads.sources.http import FetchDenied

    _company(store, "acme.io")
    _trigger(store, "acme.io", "T1_AI_SHIP", evidence=[("https://acme.io/x", None)])
    egress = _Egress({"https://acme.io/x": FetchDenied("robots.txt", "https://acme.io/x")})

    sampled, checked, dead = await resample_evidence(
        store, egress, config=MaintenanceConfig.load(), rng=random.Random(0)
    )

    assert (sampled, checked, dead) == (1, 0, 0)
    assert store.conn.execute("SELECT reachable FROM evidence").fetchone()["reachable"] is None


@pytest.mark.asyncio
async def test_unregistered_source_is_never_fetched(store: Any) -> None:
    """`dns_public` and `ats_board` are provenance labels, not registry entries.

    Fetching under one would route a request through a legality class nobody declared
    for it, which is exactly the hole the single-chokepoint design exists to close.
    """
    _company(store, "acme.io")
    _trigger(store, "acme.io", "T8_HYGIENE_GAP", evidence=[("https://dns.google/q", None)])
    with store.tx() as conn:
        conn.execute("UPDATE evidence SET source_id = 'dns_public'")
    egress = _Egress({}, enabled={"company_site"})

    sampled, checked, _ = await resample_evidence(
        store, egress, config=MaintenanceConfig.load(), rng=random.Random(0)
    )

    assert (sampled, checked) == (1, 0)
    assert egress.asked == []


@pytest.mark.asyncio
async def test_evidence_for_a_dead_trigger_is_not_resampled(store: Any) -> None:
    """A request spent on a claim nothing makes any more is a request wasted."""
    _company(store, "acme.io")
    _trigger(store, "acme.io", "T1_AI_SHIP", decays_in_days=-1, evidence=[("https://a.io/x", None)])
    egress = _Egress({})

    sampled, checked, _ = await resample_evidence(
        store, egress, config=MaintenanceConfig.load(), rng=random.Random(0)
    )

    assert (sampled, checked, egress.asked) == (0, 0, [])


@pytest.mark.asyncio
async def test_recently_checked_evidence_is_skipped(store: Any) -> None:
    _company(store, "acme.io")
    _trigger(store, "acme.io", "T1_AI_SHIP", evidence=[("https://acme.io/x", 1)])
    with store.tx() as conn:
        conn.execute("UPDATE evidence SET last_checked_at = ?", (to_iso(utcnow()),))
    egress = _Egress({})

    sampled, checked, _ = await resample_evidence(
        store, egress, config=MaintenanceConfig.load(), rng=random.Random(0)
    )

    assert (sampled, checked, egress.asked) == (0, 0, [])


# --------------------------------------------------------- retiring on dead evidence


def test_trigger_with_all_evidence_dead_is_retired(store: Any) -> None:
    _company(store, "acme.io")
    doomed = _trigger(
        store, "acme.io", "T1_AI_SHIP", evidence=[("https://a.io/1", 0), ("https://a.io/2", 0)]
    )

    count, domains = retire_unevidenced_triggers(store)

    assert (count, domains) == (1, ["acme.io"])
    assert _active(store, doomed) == 0


def test_one_surviving_link_keeps_the_trigger(store: Any) -> None:
    _company(store, "acme.io")
    kept = _trigger(
        store, "acme.io", "T1_AI_SHIP", evidence=[("https://a.io/1", 0), ("https://a.io/2", 1)]
    )

    assert retire_unevidenced_triggers(store) == (0, [])
    assert _active(store, kept) == 1


def test_one_unchecked_link_keeps_the_trigger(store: Any) -> None:
    """Unknown is not dead. A trigger wrongly retired never resurfaces."""
    _company(store, "acme.io")
    kept = _trigger(
        store, "acme.io", "T1_AI_SHIP", evidence=[("https://a.io/1", 0), ("https://a.io/2", None)]
    )

    assert retire_unevidenced_triggers(store) == (0, [])
    assert _active(store, kept) == 1


def test_trigger_with_no_evidence_at_all_is_left_alone(store: Any) -> None:
    """`NOT EXISTS (reachable IS NULL OR reachable = 1)` is vacuously true for a
    trigger with zero evidence rows. Without the matching EXISTS clause this pass
    would silently deactivate every trigger the Resolver wrote before its evidence
    landed."""
    _company(store, "acme.io")
    bare = _trigger(store, "acme.io", "T1_AI_SHIP", evidence=[])

    assert retire_unevidenced_triggers(store) == (0, [])
    assert _active(store, bare) == 1


# ------------------------------------------------------------------------ retention


def test_purge_respects_the_windows(store: Any) -> None:
    cfg = MaintenanceConfig.load()
    old = utcnow() - timedelta(days=cfg.window("raw_documents_days") + 5)
    recent = utcnow() - timedelta(days=1)
    with store.tx() as conn:
        for sha, when in (("old", old), ("new", recent)):
            conn.execute(
                "INSERT INTO raw_documents (content_sha256, url, source_id, legality_class, "
                "fetched_at) VALUES (?,?,?,?,?)",
                (sha, f"https://x.io/{sha}", "company_site", "public_web", to_iso(when)),
            )

    removed = purge_retention(store, config=cfg)

    assert removed["raw_documents"] == 1
    assert store.conn.execute("SELECT content_sha256 FROM raw_documents").fetchone()[0] == "new"


def test_purge_keeps_pending_candidates_at_any_age(store: Any) -> None:
    cfg = MaintenanceConfig.load()
    ancient = to_iso(utcnow() - timedelta(days=cfg.window("candidates_days") + 30))
    with store.tx() as conn:
        rows = (("pending", "new"), ("waiting", "extracted"), ("gone", "resolved"))
        for candidate_id, status in rows:
            conn.execute(
                "INSERT INTO candidates (candidate_id, content_sha256, raw_payload, status, "
                "created_at) VALUES (?,?,'{}',?,?)",
                (candidate_id, candidate_id, status, ancient),
            )

    removed = purge_retention(store, config=cfg)

    assert removed["candidates"] == 1
    survivors = {r[0] for r in store.conn.execute("SELECT candidate_id FROM candidates")}
    assert survivors == {"pending", "waiting"}


def test_purge_does_not_break_on_evidence_a_contact_cites(store: Any) -> None:
    """`contacts.evidence_id` is a foreign key with no ON DELETE.

    Deleting the evidence first raises, and because the whole purge is one transaction
    that means *nothing* gets purged -- a retention job that silently never runs.
    """
    cfg = MaintenanceConfig.load()
    ancient = utcnow() - timedelta(days=cfg.window("contacts_days") + 30)
    _company(store, "acme.io", updated=ancient)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
            "content_sha256) VALUES ('e1','https://acme.io/team','company_site','x',?,'')",
            (to_iso(ancient),),
        )
        conn.execute(
            "INSERT INTO contacts (contact_id, canonical_domain, email, evidence_id, "
            "first_seen_at) VALUES ('c1','acme.io','a@acme.io','e1',?)",
            (to_iso(ancient),),
        )

    removed = purge_retention(store, config=cfg)

    assert removed["contacts"] == 1
    assert removed["evidence"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_purge_keeps_contacts_at_a_live_company(store: Any) -> None:
    """Age alone is not the test. A company with a live trigger is one we have a
    reason to contact, and the contact is the point of the whole pipeline."""
    cfg = MaintenanceConfig.load()
    ancient = utcnow() - timedelta(days=cfg.window("contacts_days") + 30)
    _company(store, "acme.io", updated=ancient)
    _trigger(store, "acme.io", "T1_AI_SHIP", decays_in_days=30)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO contacts (contact_id, canonical_domain, email, first_seen_at) "
            "VALUES ('c1','acme.io','a@acme.io',?)",
            (to_iso(ancient),),
        )

    assert purge_retention(store, config=cfg)["contacts"] == 0


def test_purge_dry_run_counts_without_deleting(store: Any) -> None:
    cfg = MaintenanceConfig.load()
    old = to_iso(utcnow() - timedelta(days=cfg.window("dead_letter_days") + 5))
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO dead_letter (job_id, kind, payload, attempts, died_at) "
            "VALUES ('j1','extract.candidate','{}',3,?)",
            (old,),
        )

    assert purge_retention(store, config=cfg, dry_run=True)["dead_letter"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 1


# ------------------------------------------------------------------------ rescoring


def test_retirement_queues_a_rescore(store: Any) -> None:
    """The reason this exists at all.

    `enqueue_stale_scores` reconciles on `MAX(observed_at) > lead.last_updated_at`.
    Retiring a trigger moves neither timestamp, so the score reconciler cannot see the
    change and the card keeps quoting a trigger that no longer holds.
    """
    queue = _Queue()

    assert enqueue_rescore(store, queue, ["a.io", "b.io", "a.io"], reason="maintenance:x") == 2

    kinds = {call[0] for call in queue.calls}
    assert kinds == {"score.company"}
    assert {call[1]["canonical_domain"] for call in queue.calls} == {"a.io", "b.io"}


def test_rescore_is_idempotent_within_a_reason(store: Any, queue: Any) -> None:
    """Two maintenance runs the same night must not double-queue one company."""
    _company(store, "a.io")

    first = enqueue_rescore(store, queue, ["a.io"], reason="maintenance:2026-08-16")
    second = enqueue_rescore(store, queue, ["a.io"], reason="maintenance:2026-08-16")
    tomorrow = enqueue_rescore(store, queue, ["a.io"], reason="maintenance:2026-08-17")

    assert (first, second, tomorrow) == (1, 0, 1)
    assert (
        store.conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'score.company'").fetchone()[0]
        == 2
    )


# --------------------------------------------------------------------- orchestration


@pytest.mark.asyncio
async def test_full_run_retires_and_rescores(store: Any) -> None:
    _company(store, "clean.io", hygiene=CLEAN)
    _company(store, "weak.io", hygiene=WEAK)
    retired = _trigger(store, "clean.io", "T8_HYGIENE_GAP")
    kept = _trigger(store, "weak.io", "T8_HYGIENE_GAP")
    queue = _Queue()

    report = await run_maintenance(store, queue=queue, egress=None, cache=None)

    assert report.superseded == 1
    assert report.superseded_codes == {"T8_HYGIENE_GAP": 1}
    assert report.rescored == 1
    assert _active(store, retired) == 0
    assert _active(store, kept) == 1
    assert [call[1]["canonical_domain"] for call in queue.calls] == ["clean.io"]


@pytest.mark.asyncio
async def test_run_without_network_still_purges(store: Any) -> None:
    """A maintenance pass that refuses to reclaim disk because the network is down
    fails during exactly the outage it exists to survive."""
    cfg = MaintenanceConfig.load()
    old = to_iso(utcnow() - timedelta(days=cfg.window("metrics_days") + 5))
    with store.tx() as conn:
        conn.execute("INSERT INTO metrics (name, value, recorded_at) VALUES ('x', 1.0, ?)", (old,))

    report = await run_maintenance(store, queue=None, egress=None, cache=None, config=cfg)

    assert report.purged["metrics"] == 1
    assert report.evidence_checked == 0


@pytest.mark.asyncio
async def test_quiet_run_reports_no_change(store: Any) -> None:
    report = await run_maintenance(store, queue=_Queue(), egress=None, cache=None)
    assert not report.changed


def test_a_re_dated_trigger_is_pulled_back_to_its_first_sighting(store):
    """Written off as unrecoverable before anyone checked the database.

    The backfill re-read pages and moved `observed_at` forward, so a four-day-old
    announcement rendered as "you announced an AI feature (today)". Re-extraction
    *inserts* evidence rows and never deletes them, and `trigger_evidence` accumulates,
    so the original sighting was still there under the trigger it belongs to.
    """
    from cindraleads.maintenance import restore_first_observation

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, first_seen_at, "
            "last_updated_at) VALUES ('acme.io','Acme','2026-08-01','2026-08-01')"
        )
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at, rationale, active) "
            "VALUES ('t1','acme.io','T1_AI_SHIP',0.7,?,?,'',1)",
            ("2026-09-02T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        )
        # Two sightings of the same page: the original, and the backfill's re-read.
        for eid, seen in (("e1", "2026-08-29T00:00:00+00:00"), ("e2", "2026-09-02T00:00:00+00:00")):
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,?,?,?,?)",
                (eid, "https://acme.io/", "company_site", "we shipped an agent", seen, "abc"),
            )
            conn.execute(
                "INSERT INTO trigger_evidence (trigger_id, evidence_id) VALUES (?,?)", ("t1", eid)
            )

    assert restore_first_observation(store, dry_run=True) == 1, "dry run counts, changes nothing"
    assert (
        store.conn.execute("SELECT observed_at FROM triggers").fetchone()["observed_at"]
        == "2026-09-02T00:00:00+00:00"
    )

    assert restore_first_observation(store) == 1
    assert (
        store.conn.execute("SELECT observed_at FROM triggers").fetchone()["observed_at"]
        == "2026-08-29T00:00:00+00:00"
    )

    assert restore_first_observation(store) == 0, "and it is idempotent"


def test_a_standing_fact_keeps_its_refreshed_date(store):
    """The Enricher's triggers are re-derived from a live lookup, and a DMARC gap that
    is still open today should read as current. Pinning those to a first sighting would
    decay away a fact that never stopped being true.

    Excluded by construction rather than by a list of codes: only the Extractor stamps
    `content_sha256`, because only a page sighting is an event with a date."""
    from cindraleads.maintenance import restore_first_observation

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO companies (canonical_domain, display_name, first_seen_at, "
            "last_updated_at) VALUES ('acme.io','Acme','2026-08-01','2026-08-01')"
        )
        conn.execute(
            "INSERT INTO triggers (trigger_id, canonical_domain, code, confidence, "
            "observed_at, decays_at, rationale, active) "
            "VALUES ('t8','acme.io','T8_HYGIENE_GAP',0.7,?,?,'',1)",
            ("2026-09-02T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        )
        for eid, seen in (("d1", "2026-08-01T00:00:00+00:00"), ("d2", "2026-09-02T00:00:00+00:00")):
            conn.execute(
                "INSERT INTO evidence (evidence_id, url, source_id, snippet, observed_at, "
                "content_sha256) VALUES (?,?,?,?,?,'')",
                (eid, "https://acme.io/", "dns_public", "DMARC p=none", seen),
            )
            conn.execute(
                "INSERT INTO trigger_evidence (trigger_id, evidence_id) VALUES (?,?)", ("t8", eid)
            )

    assert restore_first_observation(store) == 0
    assert (
        store.conn.execute("SELECT observed_at FROM triggers").fetchone()["observed_at"]
        == "2026-09-02T00:00:00+00:00"
    )
