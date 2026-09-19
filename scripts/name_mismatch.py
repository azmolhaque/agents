#!/usr/bin/env python3
"""How often does a company's name disagree with its own domain, and is that a problem?

CLAUDE.md has asked for this measurement for weeks and it has never been run:

    The measurement to run before building anything: how many companies have a name
    that fails a `name_similarity` check against their own domain, and what share of
    those are real. Until that exists the list is the patch and it is known to be
    losing.

It is losing. `PLATFORM_HOSTS` gained four hosts in a single session -- `brecorder.com`,
`startupstorymedia.com`, `ostechnix.com`, plus the app stores -- and each one was added
*after* a card had already been dispatched. One maintenance pass immediately turned up
five more shapes the list does not cover: `phoronix.com` and `theconversation.com`
(publishers), `reactorcore.itch.io` (a game host), `blog.lukesalamone.com` (a personal
blog) and `bollywoodle.app` (a Bollywood guessing game).

The tempting general rule is "the display name does not match the canonical domain", and
**`Rover · rtrvr.ai` kills it** -- one of the best leads in the corpus, a real company
whose name genuinely does not resemble its domain. So this script measures and does not
judge: it buckets the corpus by similarity and prints real rows in each bucket, and a
human decides whether a low-similarity band is mostly junk or mostly Rover.

Read the `sendable` column. That is the question. If the 0-30 band is 80 companies with
two sendable leads, a quarantine-for-review rule pays for itself; if it holds twenty
good leads, it does not and the denylist stays the answer.

Reads the database and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from cindraleads.config import settings
from cindraleads.dedupe import name_similarity
from cindraleads.store import Store

# The bands a human can act on differently, not equal-width buckets. 0-30 is "shares
# almost nothing"; 70+ is "obviously the same company spelled two ways".
BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 30, "shares almost nothing"),
    (30, 50, "faint"),
    (50, 70, "partial"),
    (70, 101, "clearly the same name"),
)


def _domain_stem(domain: str) -> str:
    """The registrable label, without the suffix.

    `traccia.ai` -> `traccia`. Comparing against the whole domain would score every
    `.ai` company down for sharing two characters with every other one.
    """
    return domain.split(".")[0] if domain else ""


def _rows(store: Store) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in store.conn.execute(
            "SELECT c.canonical_domain AS domain, c.display_name AS name, "
            "       c.industry AS industry, "
            "       COALESCE(l.tier, '-') AS tier, COALESCE(l.score, 0) AS score "
            "FROM companies c "
            "LEFT JOIN leads l ON l.canonical_domain = c.canonical_domain "
            "WHERE c.display_name IS NOT NULL AND c.display_name <> '' "
            "ORDER BY c.canonical_domain"
        ).fetchall()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--examples", type=int, default=8, help="rows to print per band (default 8)"
    )
    args = parser.parse_args()

    cfg = settings()
    store = Store(cfg.db_file, migrations_dir=cfg.migrations_path)
    try:
        rows = _rows(store)
    finally:
        store.close()

    if not rows:
        print("no companies with a display name")
        return 1

    for row in rows:
        row["similarity"] = name_similarity(str(row["name"]), _domain_stem(str(row["domain"])))

    print(f"{len(rows)} company/companies with a name to compare\n")
    print(f"{'band':<26} {'companies':>9} {'sendable':>9} {'share':>7}")
    print("-" * 54)

    for low, high, label in BANDS:
        band = [r for r in rows if low <= r["similarity"] < high]
        if not band:
            continue
        sendable = [r for r in band if r["tier"] in ("A", "B", "C")]
        share = (len(sendable) / len(band)) * 100
        print(f"{f'{low}-{high - 1} {label}':<26} {len(band):>9} {len(sendable):>9} {share:>6.0f}%")

    print(
        "\nThe `sendable` column is the whole question: a band that is mostly junk is\n"
        "worth quarantining for review, and a band holding real leads is not. Nothing\n"
        "here rejects anything -- `Rover · rtrvr.ai` is a real company whose name does\n"
        "not resemble its domain, and a veto on mismatch alone would have dropped it.\n"
    )

    for low, high, label in BANDS:
        band = sorted(
            (r for r in rows if low <= r["similarity"] < high), key=lambda r: r["similarity"]
        )
        if not band:
            continue
        print(f"\n--- {low}-{high - 1} ({label}), {len(band)} total, worst first ---")
        for row in band[: args.examples]:
            tier = f"{row['tier']}{row['score']:>3}" if row["tier"] != "-" else "  -"
            print(
                f"  {row['similarity']:>5.1f}  {tier}  {str(row['name'])[:34]:<34} "
                f"{row['domain']:<28} {str(row['industry'] or '')[:24]}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
