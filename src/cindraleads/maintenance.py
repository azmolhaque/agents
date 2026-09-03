"""The nightly pass that looks backwards.

Every stage in the pipeline moves work forward and never revisits a row it wrote. That
is the right shape for a pipeline and the wrong shape for a database that accumulates
claims about real companies, so everything backward-looking lives here: decay,
retirement, reachability, and retention.

Four of these exist because of a specific way the system can be quietly wrong.

**A tightened rule does not un-write its old rows.** `mail_auth_weakness` was narrowed
after T8_HYGIENE_GAP fired on 85 of 93 companies, but the 95 rows the loose rule had
already written kept their 60 day decay and kept contributing to scores. Editing a rule
is only half the change; the other half is re-running it over what it already produced.
`RETIREMENT_RULES` is where that second half lives, and any derived trigger whose
definition tightens needs an entry in it.

**Decay is only enforced at read time.** Every query filters `decays_at > now`, so a
decayed trigger is invisible but still counted as `active = 1` in any honest census.
Flipping the flag costs one UPDATE and makes `cindra status` mean what it says.

**Evidence rots.** A lead card is a promise that a human can click the link and see the
claim. Nothing re-checks that after the day it was extracted, so a card dispatched in
week six can cite a 404. A 10% nightly sample is the compromise: re-fetching every URL
we hold would be a recurring crawl of every prospect, which is precisely what
passive-only forbids.

**Retention is a legal obligation with no natural trigger.** Nothing in the pipeline
has a reason to delete a contact row, so without a scheduled purge the answer to "how
long do you keep personal data" is "forever".

Reachability is deliberately three-valued. `reachable` is 1, 0, or NULL, and NULL means
"we did not find out" -- a robots denial, an exhausted domain budget and a timeout all
leave it NULL. Only a 4xx makes it 0. A trigger is retired for dead evidence only when
every URL it cites is *known* dead, never when we merely failed to look.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from cindraleads.config import Settings, load_yaml, settings
from cindraleads.dedupe import canonical_domain
from cindraleads.dns_hygiene import mail_auth_weakness
from cindraleads.errors import ConfigError
from cindraleads.logging import get_logger
from cindraleads.models import DnsHygiene, to_iso, utcnow
from cindraleads.store import Store

__all__ = [
    "RETIREMENT_RULES",
    "MaintenanceConfig",
    "MaintenanceReport",
    "enqueue_rescore",
    "expire_decayed_triggers",
    "purge_retention",
    "resample_evidence",
    "restore_first_observation",
    "retire_superseded_triggers",
    "retire_unevidenced_triggers",
    "run_maintenance",
    "suppress_platform_companies",
]

log = get_logger("cindraleads.maintenance")

SCORE_KIND = "score.company"


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class MaintenanceConfig:
    retention: dict[str, float]
    cache_sweep_days: float
    resample_fraction: float
    resample_max: int
    recheck_after_days: float

    @classmethod
    def load(cls, config: Settings | None = None) -> MaintenanceConfig:
        cfg = config or settings()
        data = load_yaml("maintenance", base=cfg.resolve(cfg.config_dir))
        retention = {str(k): float(v) for k, v in (data.get("retention") or {}).items()}
        if not retention:
            raise ConfigError("maintenance.yaml needs a non-empty 'retention' mapping")
        negative = sorted(k for k, v in retention.items() if v <= 0)
        if negative:
            # A zero would purge everything written today, on the first run, silently.
            raise ConfigError(f"maintenance.yaml retention windows must be > 0: {negative}")

        cache = data.get("cache") or {}
        evidence = data.get("evidence") or {}
        fraction = float(evidence.get("resample_fraction", 0.1))
        if not 0.0 <= fraction <= 1.0:
            raise ConfigError(f"resample_fraction must be in [0, 1], got {fraction}")
        return cls(
            retention=retention,
            cache_sweep_days=float(cache.get("sweep_older_than_days", 30)),
            resample_fraction=fraction,
            resample_max=int(evidence.get("resample_max", 40)),
            recheck_after_days=float(evidence.get("recheck_after_days", 14)),
        )

    def window(self, name: str) -> float:
        try:
            return self.retention[name]
        except KeyError as exc:  # a typo must not silently mean "never purge"
            raise ConfigError(f"maintenance.yaml has no retention window '{name}'") from exc


@dataclass
class MaintenanceReport:
    """What the run actually changed. Every field is a count of rows touched."""

    superseded: int = 0
    superseded_codes: dict[str, int] = field(default_factory=dict)
    platform_suppressed: int = 0
    redated: int = 0
    decayed: int = 0
    evidence_sampled: int = 0
    evidence_checked: int = 0
    evidence_dead: int = 0
    unevidenced: int = 0
    purged: dict[str, int] = field(default_factory=dict)
    cache_rows: int = 0
    cache_files: int = 0
    rescored: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.superseded
            or self.platform_suppressed
            or self.redated
            or self.decayed
            or self.evidence_dead
            or self.unevidenced
            or self.cache_rows
            or self.cache_files
            or sum(self.purged.values())
        )


# ---------------------------------------------------------------- rule retirement

# A predicate over a `companies` row: True if the trigger is still justified, False if
# the current rule would no longer write it, and **None if we cannot tell**. None is
# not a convenience -- a company whose `dns_hygiene` we never stored looks identical to
# one with no gaps, and retiring on that would delete a true claim because of a missing
# lookup. The same reasoning as `dnspython_available()` returning None-valued fields.
RuleCheck = Callable[[sqlite3.Row], bool | None]


def _t8_still_justified(company: sqlite3.Row) -> bool | None:
    raw = company["dns_hygiene"]
    if not raw:
        return None
    try:
        hygiene = DnsHygiene.model_validate_json(str(raw))
    except ValueError:
        return None
    return bool(mail_auth_weakness(hygiene))


RETIREMENT_RULES: dict[str, RuleCheck] = {
    "T8_HYGIENE_GAP": _t8_still_justified,
}


def retire_superseded_triggers(
    store: Store, *, dry_run: bool = False
) -> tuple[int, dict[str, int], list[str]]:
    """Deactivate triggers the *current* rule would not write.

    Returns ``(count, per_code_counts, affected_domains)``. The domains matter as much
    as the count: a retired trigger changes a score, and nothing else would notice.
    """
    if not RETIREMENT_RULES:
        return 0, {}, []

    codes = tuple(RETIREMENT_RULES)
    placeholders = ",".join("?" * len(codes))
    rows = store.conn.execute(
        f"SELECT t.trigger_id, t.code, t.canonical_domain, c.* "
        f"FROM triggers t JOIN companies c ON c.canonical_domain = t.canonical_domain "
        f"WHERE t.active = 1 AND t.code IN ({placeholders})",
        codes,
    ).fetchall()

    doomed: list[tuple[str, str, str]] = []
    for row in rows:
        verdict = RETIREMENT_RULES[str(row["code"])](row)
        if verdict is False:
            doomed.append((str(row["trigger_id"]), str(row["code"]), str(row["canonical_domain"])))

    per_code: dict[str, int] = {}
    domains: list[str] = []
    for _, code, domain in doomed:
        per_code[code] = per_code.get(code, 0) + 1
        if domain not in domains:
            domains.append(domain)

    if doomed and not dry_run:
        with store.tx() as conn:
            conn.executemany(
                "UPDATE triggers SET active = 0 WHERE trigger_id = ?",
                [(trigger_id,) for trigger_id, _, _ in doomed],
            )
        log.info("triggers_superseded", count=len(doomed), by_code=per_code)
    return len(doomed), per_code, domains


# Below this, the difference is the gap between the extract job and the resolve job
# rather than anything about the prospect. Half a day is far under the smallest unit
# `_age_phrase` renders ("today" vs "yesterday") and far over any queue latency.
REDATE_TOLERANCE_DAYS = 0.5


def suppress_platform_companies(store: Store, *, dry_run: bool = False) -> tuple[int, list[str]]:
    """Retire companies whose domain has since become a platform host.

    **Adding a host to `PLATFORM_HOSTS` is half a change.** It stops the next candidate
    and does nothing about the rows already written, so `France 24`, `Chaya ·
    dhakatribune.com`, `WeeTracker` and `linecast · terminaltrove.com` were still on the
    near-miss list a day after the hosts that produce them were blocked -- and
    `The Financial Times · ft.com` had joined them. Same shape as `RETIREMENT_RULES`:
    the rule changed, and something has to re-run it over what the old rule produced.

    A denylist will keep growing, because publishers and directories are a category
    rather than a list. What this fixes is the *second* cost of a late addition: without
    it every entry needs a human to remember `cindra suppress` for each company it
    already let through, which is exactly the manual step nobody performs twice.

    Suppression rather than deletion, and through the table the rest of the system
    already consults: the Scout skips a suppressed domain at plan time, `worklist` joins
    it live, and `is_suppressed` is -100 in the arithmetic. The company row and its
    evidence stay, so the record of what we thought survives, which deletion would lose.
    """
    rows = store.conn.execute("SELECT canonical_domain FROM companies").fetchall()
    stale = sorted(
        domain
        for row in rows
        if (domain := str(row["canonical_domain"] or "").strip())
        and canonical_domain(f"https://{domain}/") is None
    )
    if not stale:
        return 0, []

    already = {
        str(r["value"]).lower()
        for r in store.conn.execute(
            "SELECT value FROM suppression_list WHERE kind = 'domain'"
        ).fetchall()
    }
    fresh = [d for d in stale if d.lower() not in already]
    if dry_run or not fresh:
        return len(fresh), fresh

    now = to_iso(utcnow())
    with store.tx() as conn:
        for domain in fresh:
            conn.execute(
                "INSERT OR REPLACE INTO suppression_list (entry_id, kind, value, reason, "
                "created_at) VALUES (?,?,?,?,?)",
                (
                    uuid.uuid4().hex[:16],
                    "domain",
                    domain,
                    "platform or publisher host, not a company",
                    now,
                ),
            )
    log.info("platform_companies_suppressed", count=len(fresh), domains=fresh[:10])
    return len(fresh), fresh


def restore_first_observation(store: Store, *, dry_run: bool = False) -> int:
    """Pull `observed_at` back to the earliest page sighting that supports it.

    The re-extraction backfill re-read pages and both `_write_triggers` paths moved
    `observed_at` unconditionally, so a four-day-old Sparrow-2 announcement became one
    made today -- rendered to the prospect as "you announced an AI feature (today)".
    The Resolver no longer does that. **This is the other half**, and it was written
    off as unrecoverable before anyone checked: re-extraction *inserts* evidence rows
    and never deletes them, and `trigger_evidence` accumulates, so the original
    sighting is still in the database under the trigger it belongs to.

    Scoped to page-verified evidence by the same discriminator the quotes use. Only the
    Extractor stamps `content_sha256`, and only a page sighting is an event with a date
    the prospect would recognise. The Enricher's triggers are standing facts re-derived
    from a live lookup -- a DMARC gap that is still open today *should* read as current,
    and pinning those to their first sighting would decay away a fact that never stopped
    being true. They have no page evidence, so they are excluded by construction rather
    than by a list of codes that would drift.

    Only ever moves a date *backwards*, only when the evidence disagrees by more than
    `REDATE_TOLERANCE_DAYS`, and only to a time we can point at a page for.
    """
    # Scoped to the URL that currently justifies the date, not to all evidence ever
    # joined. Two reasons, and the second is the one that bites:
    #
    #  * Extraction and resolution are separate jobs, so `evidence.observed_at` is
    #    always earlier than `triggers.observed_at` by the queue latency between them.
    #    An unscoped `MIN` therefore fires on essentially every trigger in the corpus --
    #    838 of them on the first dry run -- which drowns the day-scale damage in
    #    minute-scale noise and makes the count useless as a measure of anything.
    #  * It would fight the Resolver. That rule says a *different* URL is new news and
    #    moves the date forward; an unscoped pull-back would drag it to the oldest
    #    evidence every night and quietly undo the fix.
    #
    # Taking the newest sighting's URL and the earliest time we saw *that* page gives
    # both: the backfill's re-read of one page collapses back to when the page first
    # appeared, and a genuine second announcement keeps its own date.
    sql = (
        "SELECT t.trigger_id, MIN(e.observed_at) AS first_seen "
        "FROM triggers t "
        "JOIN trigger_evidence te ON te.trigger_id = t.trigger_id "
        "JOIN evidence e ON e.evidence_id = te.evidence_id "
        "WHERE t.active = 1 AND COALESCE(e.content_sha256, '') != '' "
        "  AND e.url = ("
        "    SELECT e2.url FROM trigger_evidence te2 "
        "    JOIN evidence e2 ON e2.evidence_id = te2.evidence_id "
        "    WHERE te2.trigger_id = t.trigger_id "
        "      AND COALESCE(e2.content_sha256, '') != '' "
        "    ORDER BY e2.observed_at DESC LIMIT 1) "
        "GROUP BY t.trigger_id "
        # Only material moves. A trigger dated a few minutes after the page it cites is
        # the extract-to-resolve gap, not a claim about what the prospect did, and
        # rewriting 800 rows a night to chase it would bury the ones that matter.
        "HAVING julianday(t.observed_at) - julianday(MIN(e.observed_at)) > ?"
    )
    rows = store.conn.execute(sql, (REDATE_TOLERANCE_DAYS,)).fetchall()
    if dry_run or not rows:
        return len(rows)

    with store.tx() as conn:
        for row in rows:
            conn.execute(
                "UPDATE triggers SET observed_at = ? WHERE trigger_id = ?",
                (str(row["first_seen"]), str(row["trigger_id"])),
            )
    log.info("triggers_redated", count=len(rows))
    return len(rows)


def expire_decayed_triggers(store: Store, *, dry_run: bool = False) -> int:
    """Flip `active` on triggers past `decays_at`.

    Read paths already filter on `decays_at`, so this changes no scoring decision. It
    changes what a census says, and a census that counts 211 live triggers when 90 of
    them expired last month is a lie told by an index.
    """
    now = to_iso(utcnow())
    if dry_run:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM triggers WHERE active = 1 AND decays_at <= ?", (now,)
        ).fetchone()
        return int(row["n"])
    with store.tx() as conn:
        cursor = conn.execute(
            "UPDATE triggers SET active = 0 WHERE active = 1 AND decays_at <= ?", (now,)
        )
        count = int(cursor.rowcount)
    if count:
        log.info("triggers_decayed", count=count)
    return count


# ------------------------------------------------------------ evidence reachability


async def resample_evidence(
    store: Store,
    egress: Any,
    *,
    config: MaintenanceConfig,
    rng: random.Random | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """HEAD-equivalent re-check of a sample of live evidence URLs.

    Returns ``(sampled, checked, found_dead)``, and the gap between the first two is
    the interesting number. On the first real run 24 URLs were sampled and only 14
    produced an answer: the rest were unregistered sources, robots denials, budget
    exhaustion, or a crt.sh outage. Collapsing those into one figure would report
    "re-checked 24" for a pass that verified 14, which is the kind of number that reads
    as reassurance and is not.

    Every request goes through the egress chokepoint like any other, which means it is
    subject to robots, the per-domain budget and the minimum interval -- a re-check is
    not a privileged request. A URL whose `source_id` is not a registered source is
    skipped rather than fetched under a borrowed legality class: the source label on an
    evidence row is a provenance note, and inventing a registry entry from it would
    route a request through a policy nobody wrote for it.

    Only evidence attached to a live trigger is sampled. Re-checking a URL that no
    longer supports any claim spends a request on nothing.
    """
    picker = rng or random.Random()
    now = utcnow()
    stale_before = to_iso(now - timedelta(days=config.recheck_after_days))

    candidates = store.conn.execute(
        "SELECT DISTINCT e.evidence_id, e.url, e.source_id FROM evidence e "
        "JOIN trigger_evidence te ON te.evidence_id = e.evidence_id "
        "JOIN triggers t ON t.trigger_id = te.trigger_id "
        "WHERE t.active = 1 AND t.decays_at > ? "
        "AND (e.last_checked_at IS NULL OR e.last_checked_at < ?)",
        (to_iso(now), stale_before),
    ).fetchall()
    if not candidates:
        return 0, 0, 0

    take = min(config.resample_max, max(1, round(len(candidates) * config.resample_fraction)))
    sample = picker.sample(list(candidates), min(take, len(candidates)))
    if dry_run:
        # Sampled, but nothing was checked -- and the dry-run figure is an upper bound
        # in a second way: retirement has not run, so the live-trigger pool it samples
        # from still contains evidence the real pass would have retired first.
        return len(sample), 0, 0

    checked = 0
    dead = 0
    for row in sample:
        verdict = await _probe_evidence(egress, str(row["source_id"]), str(row["url"]))
        if verdict is None:
            continue  # did not find out; leave `reachable` as it was
        checked += 1
        if verdict is False:
            dead += 1
        with store.tx() as conn:
            conn.execute(
                "UPDATE evidence SET reachable = ?, last_checked_at = ? WHERE evidence_id = ?",
                (1 if verdict else 0, to_iso(utcnow()), str(row["evidence_id"])),
            )
    if sample:
        log.info(
            "evidence_resampled",
            sampled=len(sample),
            checked=checked,
            dead=dead,
            inconclusive=len(sample) - checked,
        )
    return len(sample), checked, dead


async def _probe_evidence(egress: Any, source_id: str, url: str) -> bool | None:
    """True reachable, False definitively gone, None we did not find out."""
    from cindraleads.sources.http import FetchDenied

    try:
        registered = egress.registry.get(source_id)
    except Exception:
        registered = None
    if registered is None or not getattr(registered, "enabled", False):
        log.debug("evidence_skip_unregistered", source_id=source_id, url=url)
        return None

    try:
        await egress.fetch(source_id, url)
    except FetchDenied as denied:
        # robots, budget, interval. Policy said not now; that is not a dead link.
        log.debug("evidence_skip_denied", url=url, reason=denied.reason)
        return None
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 429):
            # 401/403 mean "you may not see it", not "it is not there". A card citing a
            # login-walled URL is a different problem from one citing a 404.
            return False
        log.debug("evidence_check_inconclusive", url=url, error=type(exc).__name__)
        return None
    return True


def retire_unevidenced_triggers(store: Store, *, dry_run: bool = False) -> tuple[int, list[str]]:
    """Deactivate triggers whose every cited URL is known dead.

    "No evidence, no lead" is enforced at write time by the Extractor. This is the same
    rule enforced against the passage of time.

    The condition is deliberately hard to satisfy: the trigger must cite at least one
    URL, and there must be no URL among them that is reachable *or unknown*. One
    timeout on one link is enough to keep the trigger alive, which is the correct bias
    -- a trigger wrongly retired is a lead we never call, and nothing surfaces it again.
    """
    rows = store.conn.execute(
        "SELECT t.trigger_id, t.canonical_domain FROM triggers t "
        "WHERE t.active = 1 "
        "AND EXISTS (SELECT 1 FROM trigger_evidence te WHERE te.trigger_id = t.trigger_id) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM trigger_evidence te JOIN evidence e ON e.evidence_id = te.evidence_id "
        "  WHERE te.trigger_id = t.trigger_id AND (e.reachable IS NULL OR e.reachable = 1)"
        ")"
    ).fetchall()
    if not rows:
        return 0, []

    domains: list[str] = []
    for row in rows:
        domain = str(row["canonical_domain"])
        if domain not in domains:
            domains.append(domain)
    if not dry_run:
        with store.tx() as conn:
            conn.executemany(
                "UPDATE triggers SET active = 0 WHERE trigger_id = ?",
                [(str(r["trigger_id"]),) for r in rows],
            )
        log.info("triggers_unevidenced", count=len(rows), domains=len(domains))
    return len(rows), domains


# ------------------------------------------------------------------------ retention


def purge_retention(
    store: Store, *, config: MaintenanceConfig, dry_run: bool = False
) -> dict[str, int]:
    """Delete what we have no basis to keep. Returns rows removed per table."""
    now = utcnow()

    def cutoff(window: str) -> str:
        return to_iso(now - timedelta(days=config.window(window)))

    # (label, delete statement, params). Counted with a matching SELECT under dry-run
    # so `--dry-run` reports the real number rather than a guess.
    plans: list[tuple[str, str, str, tuple[Any, ...]]] = [
        (
            "raw_documents",
            "DELETE FROM raw_documents WHERE fetched_at < ?",
            "SELECT COUNT(*) AS n FROM raw_documents WHERE fetched_at < ?",
            (cutoff("raw_documents_days"),),
        ),
        (
            # Terminal only. `new` and `extracted` are work in flight at any age.
            "candidates",
            "DELETE FROM candidates WHERE created_at < ? AND status IN "
            "('resolved','unresolvable','skipped','quarantined')",
            "SELECT COUNT(*) AS n FROM candidates WHERE created_at < ? AND status IN "
            "('resolved','unresolvable','skipped','quarantined')",
            (cutoff("candidates_days"),),
        ),
        (
            "dead_letter",
            "DELETE FROM dead_letter WHERE died_at < ?",
            "SELECT COUNT(*) AS n FROM dead_letter WHERE died_at < ?",
            (cutoff("dead_letter_days"),),
        ),
        (
            "metrics",
            "DELETE FROM metrics WHERE recorded_at < ?",
            "SELECT COUNT(*) AS n FROM metrics WHERE recorded_at < ?",
            (cutoff("metrics_days"),),
        ),
        (
            # Completed jobs are the queue's dedupe memory. The window is far longer
            # than any dedupe time bucket, so this can never make work re-run.
            "jobs",
            "DELETE FROM jobs WHERE status = 'done' AND updated_at < ?",
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'done' AND updated_at < ?",
            (cutoff("done_jobs_days"),),
        ),
        (
            # Personal data, and the only purge here that is a legal obligation rather
            # than housekeeping: a named person at a company we have no live reason to
            # contact is data we cannot justify holding. Ordered before the evidence
            # purge so the row that cites a contact's source page goes first -- the
            # other order leaves the evidence row pinned by a foreign key.
            "contacts",
            "DELETE FROM contacts WHERE canonical_domain IN ("
            "  SELECT c.canonical_domain FROM companies c"
            "  WHERE c.last_updated_at < ? AND NOT EXISTS ("
            "    SELECT 1 FROM triggers t WHERE t.canonical_domain = c.canonical_domain"
            "    AND t.active = 1)"
            ")",
            "SELECT COUNT(*) AS n FROM contacts WHERE canonical_domain IN ("
            "  SELECT c.canonical_domain FROM companies c"
            "  WHERE c.last_updated_at < ? AND NOT EXISTS ("
            "    SELECT 1 FROM triggers t WHERE t.canonical_domain = c.canonical_domain"
            "    AND t.active = 1)"
            ")",
            (cutoff("contacts_days"),),
        ),
        (
            # Orphaned both ways: no trigger cites it *and* no contact was sourced from
            # it. `contacts.evidence_id` is a foreign key with no ON DELETE, so missing
            # the second clause turns the whole purge into a constraint failure and
            # nothing at all gets deleted.
            "evidence",
            "DELETE FROM evidence WHERE observed_at < ? "
            "AND evidence_id NOT IN (SELECT evidence_id FROM trigger_evidence) "
            "AND evidence_id NOT IN "
            "(SELECT evidence_id FROM contacts WHERE evidence_id IS NOT NULL)",
            "SELECT COUNT(*) AS n FROM evidence WHERE observed_at < ? "
            "AND evidence_id NOT IN (SELECT evidence_id FROM trigger_evidence) "
            "AND evidence_id NOT IN "
            "(SELECT evidence_id FROM contacts WHERE evidence_id IS NOT NULL)",
            (cutoff("orphan_evidence_days"),),
        ),
    ]

    removed: dict[str, int] = {}
    if dry_run:
        for label, _, counter, params in plans:
            removed[label] = int(store.conn.execute(counter, params).fetchone()["n"])
        return removed

    with store.tx() as conn:
        for label, statement, _, params in plans:
            removed[label] = int(conn.execute(statement, params).rowcount)
    total = sum(removed.values())
    if total:
        log.info("retention_purged", total=total, **{k: v for k, v in removed.items() if v})
    return removed


# ------------------------------------------------------------------------- rescoring


def enqueue_rescore(
    store: Store, queue: Any, domains: Sequence[str], *, reason: str, dry_run: bool = False
) -> int:
    """Queue a re-score for companies whose trigger set changed underneath them.

    `enqueue_stale_scores` cannot cover this. It reconciles on
    `MAX(observed_at) > lead.last_updated_at`, and retiring a trigger moves neither --
    the remaining triggers keep their timestamps and the lead keeps its own. A
    retirement that never reaches a score is a retirement that never reaches a card.

    The dedupe key carries `reason` and the domain set, so re-running maintenance the
    same night enqueues nothing while a genuinely new retirement does.
    """
    unique = sorted(set(domains))
    if not unique or dry_run:
        return len(unique)

    queued = 0
    with store.tx() as conn:
        for domain in unique:
            digest = hashlib.sha256(f"{domain}|{reason}".encode()).hexdigest()[:16]
            key = f"score:{digest}"
            existing = conn.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ? LIMIT 1", (key,)
            ).fetchone()
            queue.enqueue(SCORE_KIND, {"canonical_domain": domain}, dedupe_key=key, conn=conn)
            if existing is None:
                queued += 1
    if queued:
        log.info("rescore_enqueued", count=queued, reason=reason)
    return queued


# ----------------------------------------------------------------------- orchestrator


async def run_maintenance(
    store: Store,
    *,
    queue: Any = None,
    egress: Any = None,
    cache: Any = None,
    config: MaintenanceConfig | None = None,
    rng: random.Random | None = None,
    dry_run: bool = False,
) -> MaintenanceReport:
    """The whole nightly pass, in dependency order.

    Retirement runs before resampling so a request is never spent re-checking evidence
    for a trigger that is about to be retired anyway, and rescoring runs last so it
    sees every retirement from every source in one job per company.

    `egress` and `cache` are optional: with either absent the run does everything that
    does not need them. A maintenance pass that refuses to purge because the network is
    down would let the disk fill during exactly the outage it should survive.
    """
    cfg = config or MaintenanceConfig.load()
    report = MaintenanceReport()
    dirty: list[str] = []

    report.superseded, report.superseded_codes, superseded_domains = retire_superseded_triggers(
        store, dry_run=dry_run
    )
    dirty.extend(superseded_domains)

    # Before the date repair and before decay: a company that is not a company should
    # not have its triggers carefully re-dated first.
    report.platform_suppressed, platform_domains = suppress_platform_companies(
        store, dry_run=dry_run
    )
    dirty.extend(platform_domains)

    # Before decay, because it moves dates backwards and a trigger pulled back past
    # its own `decays_at` should expire in the same pass rather than next night.
    report.redated = restore_first_observation(store, dry_run=dry_run)

    report.decayed = expire_decayed_triggers(store, dry_run=dry_run)

    if egress is not None:
        (
            report.evidence_sampled,
            report.evidence_checked,
            report.evidence_dead,
        ) = await resample_evidence(store, egress, config=cfg, rng=rng, dry_run=dry_run)
        if report.evidence_dead:
            report.unevidenced, dead_domains = retire_unevidenced_triggers(store, dry_run=dry_run)
            dirty.extend(dead_domains)

    report.purged = purge_retention(store, config=cfg, dry_run=dry_run)

    if cache is not None:
        report.cache_rows, report.cache_files = (
            (0, 0) if dry_run else cache.purge_expired(older_than_days=cfg.cache_sweep_days)
        )

    if queue is not None and dirty:
        # One stamp per calendar day: two runs the same night must not double-queue,
        # but a retirement tomorrow must get its own job.
        stamp = to_iso(utcnow())[:10]
        report.rescored = enqueue_rescore(
            store, queue, dirty, reason=f"maintenance:{stamp}", dry_run=dry_run
        )

    log.info(
        "maintenance_complete",
        dry_run=dry_run,
        superseded=report.superseded,
        decayed=report.decayed,
        evidence_sampled=report.evidence_sampled,
        evidence_checked=report.evidence_checked,
        evidence_dead=report.evidence_dead,
        unevidenced=report.unevidenced,
        purged=sum(report.purged.values()),
        rescored=report.rescored,
    )
    return report
