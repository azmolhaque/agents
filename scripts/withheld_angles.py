#!/usr/bin/env python3
"""Which stored angles would the Dispatcher refuse, and for which reason?

`cindra reconcile --reprose` prints a count of unsendable angles. A count is the size
of a job; it is not a diagnosis, and this project has now twice written a confident
explanation of a number before reading the rows behind it -- `apple.com`, which turned
out not to be Apple, and the "second worker" that was two process lifetimes.

So: the two reasons `angle_withheld_reason` can give, split by offer, with real rows
under each. Read it for whether the split is what you expect. The free-claim half is a
model that dropped the price out of a paid offer phrase; the internal-code half is an
angle naming `T1_AI_SHIP` or `ai_llm_assessment` in text meant for a prospect, which is
a different defect with a different fix.

Scoped to every stored angle, not just the stale ones, because "the guard refuses this
today" is a fact about the corpus and the backlog is a fact about build stamps -- and
the difference between those two populations is itself worth seeing.

Reads the database and writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

import httpx

from cindraleads.agents.dispatcher import (
    Dispatcher,
    _free_claim_is_backed,
    angle_withheld_reason,
)
from cindraleads.agents.scorer import prose_version
from cindraleads.config import settings
from cindraleads.discord import DiscordWebhook
from cindraleads.scoring import ScoringConfig
from cindraleads.store import Store

SENDABLE = "sendable"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=int, default=6, help="rows to print per reason")
    args = parser.parse_args()

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"withheld_angles must never post: {request.url}")

    cfg = settings()
    store = Store(cfg.db_file, migrations_dir=cfg.migrations_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
    try:
        # Read once. `_free_claim_is_backed` loads this when it is not handed one, and
        # over a thousand rows that is a thousand YAML parses.
        scoring = ScoringConfig.load()
        current = prose_version()
        rows = [
            dict(r)
            for r in store.conn.execute(
                "SELECT l.lead_id AS lead_id, l.canonical_domain AS canonical_domain, "
                "       l.tier, l.score, "
                "       l.recommended_offer AS offer, l.outreach_angle AS angle, "
                "       COALESCE(l.angle_version, '') AS stamp, "
                "       l.compliance AS compliance, c.country AS country "
                "FROM leads l JOIN companies c ON c.canonical_domain = l.canonical_domain "
                "WHERE l.archived = 0 AND COALESCE(l.outreach_angle, '') <> '' "
                "ORDER BY l.score DESC"
            ).fetchall()
        ]

        # The real `_blocked`, borrowed the way `preview_card.py` borrows `read_lead`:
        # a transport that raises on any request and a non-empty `webhooks` so
        # `__post_init__` does not read the configured secrets. It needs only
        # `compliance` and `canonical_domain` off the row, so `read_lead` -- which
        # assembles triggers and evidence per lead -- is not worth paying for a
        # thousand times.
        reader = Dispatcher(
            store=store,
            webhook=DiscordWebhook(client=client),
            config=cfg,
            webhooks={"_report_never_posts": ""},
        )
        for row in rows:
            row["reason"] = (
                angle_withheld_reason(
                    str(row["angle"]),
                    allow_free=_free_claim_is_backed(
                        str(row["angle"]),
                        str(row["offer"] or ""),
                        str(row["country"] or "") or None,
                        scoring,
                    ),
                )
                or SENDABLE
            )
            row["stale"] = row["stamp"] != current
            row["blocked"] = reader._blocked(row)
    finally:
        asyncio.run(client.aclose())
        store.close()

    if not rows:
        print("no stored angles")
        return 1

    by_reason: Counter[str] = Counter(str(row["reason"]) for row in rows)
    print(f"{len(rows)} lead(s) carry an angle\n")
    for reason, count in by_reason.most_common():
        share = count / len(rows) * 100
        print(f"  {count:>5}  {share:>5.1f}%  {reason}")

    # The two populations the reconcile line reports on, side by side. A withheld angle
    # written by the *current* build is the interesting cell: re-prosing cannot reach it
    # and the prompt is what would need to change.
    print(f"\n{'':<38} {'stale':>7} {'current':>8}")
    for reason, _ in by_reason.most_common():
        group = [row for row in rows if row["reason"] == reason]
        stale = sum(1 for row in group if row["stale"])
        print(f"  {reason:<36} {stale:>7} {len(group) - stale:>8}")
    print(
        "\n  A withheld angle stamped by the *current* build is one `--reprose` will\n"
        "  rewrite into the same refusal. That column is a prompt problem, not a\n"
        "  backlog one.\n"
    )

    # Decode spent on a lead that can never reach a card is decode spent twice over:
    # `--reprose` puts unsendable angles at the front, and `arxiv.org` -- compliance
    # VETO, refused by the Dispatcher since the guard shipped -- sits at the top of
    # this very report at Tier A 74. Rewriting its angle costs ~18 s and produces
    # nothing. Counted rather than assumed, because one such lead is noise and fifty
    # is a wasted pass.
    doomed = [row for row in rows if row["reason"] != SENDABLE and row["blocked"]]
    if doomed:
        print(f"\n  of the withheld angles, {len(doomed)} belong to a lead the Dispatcher")
        print("  refuses anyway -- re-prosing them buys nothing:")
        for reason, count in Counter(str(row["blocked"]) for row in doomed).most_common():
            print(f"    {count:>5}  {reason}")
    else:
        print("\n  every withheld angle belongs to a lead that could be dispatched")

    print(f"\n{'':<20} {'offer':<22} {'reason'}")
    for (offer, reason), count in Counter(
        (str(row["offer"] or ""), str(row["reason"])) for row in rows if row["reason"] != SENDABLE
    ).most_common():
        print(f"  {count:>5}  {offer:<24} {reason}")

    for reason in by_reason:
        if reason == SENDABLE:
            continue
        group = [row for row in rows if row["reason"] == reason]
        print(f"\n--- {reason} ({len(group)}), highest scoring first ---")
        for row in group[: args.examples]:
            stamp = "stale" if row["stale"] else "CURRENT BUILD"
            flags = f"{stamp}, {row['blocked']}" if row["blocked"] else stamp
            print(f"\n  {row['tier']}{row['score']:>3}  {row['canonical_domain']}  [{flags}]")
            print(f"       {str(row['angle'])[:260]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
