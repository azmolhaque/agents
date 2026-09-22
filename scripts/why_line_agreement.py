#!/usr/bin/env python3
"""How often does the `why:` line cite the trigger the angle is actually about?

The call list prints one trigger and one URL directly beneath the model's prose, and
until now it picked that trigger by **scoring weight**. Matcha's card read
`why: T3_HIRING_SEC` over an angle describing a mail-authentication gap, so the link
the reader is invited to click did not support the sentence above it.

`worklist._trigger_the_angle_argues` now reads the angle instead, matching it against
the `means` phrases the model was handed. It is conservative -- two distinct content
words and a strict winner, otherwise the heaviest trigger is kept -- and it shipped
before it could be measured, because the dev box has no corpus and the failure it can
produce (citing the wrong trigger) is exactly what the weight ordering does
unconditionally today. **This script is how that claim gets checked.**

Read three numbers:

* `agrees` -- the angle and the weight point at the same trigger. Nothing changed.
* `reclassified` -- the angle named a different trigger, and the `why:` line moved.
  Each one is a card that used to cite a link its own text did not support. Read the
  samples: if the new trigger is plainly what the angle is about, the mechanism works.
* `no opinion` -- the angle matched nothing convincingly and the weight still decides.
  A high share here means the matcher is inert, not safe; the threshold or the `means`
  phrases would be the thing to look at.

Reads the database and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from cindraleads.agents.dispatcher import TRIGGER_ORDER, _trigger_means
from cindraleads.config import settings
from cindraleads.store import Store
from cindraleads.worklist import _angle_subject, _trigger_the_angle_argues


def _leads(store: Store, tiers: tuple[str, ...]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in tiers)
    return [
        dict(r)
        for r in store.conn.execute(
            "SELECT l.canonical_domain AS domain, l.tier, l.score, l.outreach_angle AS angle "
            "FROM leads l "
            f"WHERE l.tier IN ({placeholders}) AND l.archived = 0 "
            "  AND l.outreach_angle IS NOT NULL AND l.outreach_angle <> '' "
            "ORDER BY l.score DESC",
            tiers,
        ).fetchall()
    ]


def _provable_codes(store: Store, domain: str) -> list[str]:
    """The live triggers that hold at least one evidence URL -- the same candidate set
    `_top_trigger` picks from, since a trigger with nothing to open cannot be cited."""
    rows = store.conn.execute(
        "SELECT DISTINCT t.code FROM triggers t "
        "JOIN trigger_evidence te ON te.trigger_id = t.trigger_id "
        "JOIN evidence e ON e.evidence_id = te.evidence_id "
        "WHERE t.canonical_domain = ? AND t.active = 1 AND e.url IS NOT NULL",
        (domain,),
    ).fetchall()
    return sorted(str(r["code"]) for r in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tiers", default="A,B", help="comma-separated tiers (default A,B)")
    parser.add_argument("--examples", type=int, default=10, help="samples to print per outcome")
    args = parser.parse_args()

    tiers = tuple(t.strip() for t in args.tiers.split(",") if t.strip())
    cfg = settings()
    store = Store(cfg.db_file, migrations_dir=cfg.migrations_path)
    try:
        leads = _leads(store, tiers)
        means = _trigger_means()
        if not means:
            print("scoring.yaml did not load, so every lead would read as 'no opinion'")
            return 1

        agrees: list[dict[str, Any]] = []
        reclassified: list[dict[str, Any]] = []
        no_opinion: list[dict[str, Any]] = []
        for lead in leads:
            codes = _provable_codes(store, str(lead["domain"]))
            if not codes:
                continue
            heaviest = max(codes, key=lambda c: TRIGGER_ORDER.get(c, 0))
            chosen = _trigger_the_angle_argues(str(lead["angle"]), codes, means)
            lead["heaviest"] = heaviest
            lead["chosen"] = chosen
            lead["candidates"] = len(codes)
            if not chosen:
                no_opinion.append(lead)
            elif chosen == heaviest:
                agrees.append(lead)
            else:
                reclassified.append(lead)
    finally:
        store.close()

    total = len(agrees) + len(reclassified) + len(no_opinion)
    if not total:
        print(f"no tier {'/'.join(tiers)} leads with an angle and a citable trigger")
        return 1

    print(f"{total} lead(s) with an angle and at least one citable trigger\n")
    for label, group in (
        ("agrees with the weight", agrees),
        ("reclassified by the angle", reclassified),
        ("no opinion -- weight decides", no_opinion),
    ):
        share = len(group) / total * 100
        print(f"  {label:<30} {len(group):>5}  {share:>5.1f}%")

    # A single-trigger lead can never disagree, so the honest denominator for
    # "did this mechanism do anything" is the leads that had a choice to make.
    contested = [lead for lead in agrees + reclassified + no_opinion if lead["candidates"] > 1]
    if contested:
        moved = sum(
            1 for lead in contested if lead["chosen"] and lead["chosen"] != lead["heaviest"]
        )
        print(
            f"\n  of {len(contested)} lead(s) with more than one citable trigger, "
            f"{moved} moved ({moved / len(contested) * 100:.0f}%)"
        )

    if reclassified:
        print("\n--- reclassified: the `why:` line moved. Does the angle back it up? ---")
        for lead in reclassified[: args.examples]:
            print(f"\n  {lead['tier']}{lead['score']:>3}  {lead['domain']}")
            print(f"       was: {lead['heaviest']}   now: {lead['chosen']}")
            print(f"       {_angle_subject(str(lead['angle'])).strip()[:220]}")

    if no_opinion:
        print("\n--- no opinion: the weight still decides. Should it have? ---")
        for lead in no_opinion[: args.examples]:
            print(f"\n  {lead['tier']}{lead['score']:>3}  {lead['domain']}  ({lead['heaviest']})")
            print(f"       {_angle_subject(str(lead['angle'])).strip()[:220]}")

    print(
        "\nA high `no opinion` share means the matcher is inert rather than safe.\n"
        "A reclassification whose angle plainly argues the new trigger is the "
        "mechanism working.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
