#!/usr/bin/env python3
"""Which leads to judge next, so the Critic can say something it could not before.

The Critic argues about a trigger's weight only once `MIN_JUDGED_PER_TRIGGER` leads
carrying it have a human verdict, and it prints how far each one still is. What it
cannot do is name the leads that would close the gap -- so the operator is told "needs
2 more" and left to find them.

Finding them by hand is where this went wrong three times in one session. Ad-hoc SQL
over `leads` returned `arxiv.org` at Tier A (compliance veto, refused by the Dispatcher
since `_blocked` shipped), `jetbrains.com` (whose stored angle is the original
hardcoded free-offer defect) and `schneier.com` (a security consultancy) -- because
**a stored tier is not a dispatch decision**, and then named `suppressed_domains`,
which is not a table.

So this borrows the real predicates rather than restating them: `blocked_subjects` and
`block_reason` from the Dispatcher, `MIN_JUDGED_PER_TRIGGER` from the Critic. A report
that split the rows differently from the code would recommend judging leads the system
would never send, which is worse than no report -- the verdict would be real and the
lesson drawn from it would be about a lead that cannot exist.

Reads the database and writes nothing.
"""

from __future__ import annotations

import argparse
import sys

from cindraleads.agents.critic import MIN_JUDGED_PER_TRIGGER
from cindraleads.agents.dispatcher import _trigger_means, block_reason, blocked_subjects
from cindraleads.config import settings
from cindraleads.dedupe import display_name_or_domain
from cindraleads.store import Store

#: Tiers a human would actually email. Tier C goes out in the digest and is judged
#: there; the call list is A and B, and a verdict is worth most on a lead that was
#: about to be sent.
SENDABLE_TIERS = ("A", "B")


def _short_triggers(store: Store) -> dict[str, int]:
    """Triggers with at least one verdict but fewer than the Critic needs.

    Deliberately not every trigger in the taxonomy: one nobody has judged at all is a
    cold start the operator may not want, while one sitting at three is a single
    verdict away from unlocking an argument. The Critic's own table prints both; this
    picks the ones where the next verdict does the most.
    """
    rows = store.conn.execute(
        "WITH judged AS ("
        "  SELECT l.lead_id, l.canonical_domain FROM leads l "
        "  JOIN feedback f ON f.lead_id = l.lead_id "
        "  WHERE f.verdict IN ('good','bad') GROUP BY l.lead_id) "
        "SELECT t.code AS code, COUNT(DISTINCT j.lead_id) AS judged "
        "FROM judged j JOIN triggers t ON t.canonical_domain = j.canonical_domain "
        "WHERE t.active = 1 GROUP BY t.code"
    ).fetchall()
    return {
        str(r["code"]): MIN_JUDGED_PER_TRIGGER - int(r["judged"])
        for r in rows
        if int(r["judged"]) < MIN_JUDGED_PER_TRIGGER
    }


def _candidates(store: Store, codes: list[str], limit: int) -> list[dict[str, object]]:
    """Unjudged, dispatchable leads carrying one of those triggers.

    The compliance verdict, the suppression list and the quarantine table are asked
    through `block_reason`, which is the same function the Dispatcher and the worklist
    ask. Everything it refuses is dropped here rather than shown with a warning: this
    report exists to spend the operator's attention well, and a lead nothing will send
    is attention spent on a card that cannot teach anything.
    """
    if not codes:
        return []
    placeholders = ",".join("?" for _ in codes)
    rows = store.conn.execute(
        "SELECT DISTINCT l.lead_id, l.canonical_domain, l.tier, l.score, l.compliance, "
        "  c.display_name "
        "FROM leads l "
        "JOIN companies c ON c.canonical_domain = l.canonical_domain "
        "JOIN triggers t ON t.canonical_domain = l.canonical_domain "
        "LEFT JOIN feedback f ON f.lead_id = l.lead_id AND f.verdict IN ('good','bad') "
        f"WHERE t.active = 1 AND t.code IN ({placeholders}) "
        f"  AND l.tier IN ({','.join('?' for _ in SENDABLE_TIERS)}) "
        "  AND l.archived = 0 AND f.lead_id IS NULL "
        "ORDER BY l.score DESC",
        (*codes, *SENDABLE_TIERS),
    ).fetchall()

    suppressed, quarantined = blocked_subjects(store.conn)
    out: list[dict[str, object]] = []
    for row in rows:
        if block_reason(row, suppressed, quarantined):
            continue
        codes_here = [
            str(r["code"])
            for r in store.conn.execute(
                "SELECT DISTINCT code FROM triggers WHERE canonical_domain = ? AND active = 1 "
                f"AND code IN ({placeholders})",
                (row["canonical_domain"], *codes),
            )
        ]
        out.append(
            {
                "lead_id": str(row["lead_id"]),
                "domain": str(row["canonical_domain"]),
                # `display_name_or_domain`, not `name or domain`. The literal
                # four-character string "null" is truthy, survives every
                # `IS NOT NULL` filter in the system, and this report printed
                # `null · culture.sbs` on its first real run -- in the script
                # written to stop restating predicates the code already owns.
                # Fourth reader; the other three were already correct.
                "name": display_name_or_domain(row["display_name"], str(row["canonical_domain"])),
                "tier": str(row["tier"]),
                "score": int(row["score"]),
                "codes": codes_here,
            }
        )
        if len(out) >= limit:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    store = Store(settings().db_path)
    try:
        short = _short_triggers(store)
        if not short:
            print(
                "Every trigger with a verdict has reached "
                f"{MIN_JUDGED_PER_TRIGGER}. `cindra critic` can argue about all of "
                "them; nothing here is blocked on more judging."
            )
            return 0

        # The same phrase map the card and the call list print, so a trigger reads
        # the same wherever the operator meets it -- and it swallows a config
        # failure the way `_trigger_means` already does, because a report that
        # dies on a bad YAML is one that cannot tell you the YAML is bad.
        means = _trigger_means()
        print(f"# triggers one or more verdicts short of {MIN_JUDGED_PER_TRIGGER}\n")
        for code, gap in sorted(short.items(), key=lambda kv: kv[1]):
            print(f"  {code:<22} needs {gap} more   {means.get(code, '')}")

        rows = _candidates(store, sorted(short), args.limit)
        if not rows:
            print(
                "\nNo unjudged, dispatchable Tier A/B lead carries any of them. The gap "
                "closes when discovery finds one, not by judging harder."
            )
            return 0

        print(f"\n# {len(rows)} lead(s) that would close a gap, best first\n")
        for row in rows:
            print(f"  {row['score']:>3} {row['tier']}  {row['name']} · {row['domain']}")
            print(f"      closes: {', '.join(row['codes'])}")  # type: ignore[arg-type]
            print(f"      cindra feedback {row['lead_id']} good|bad\n")
        print(
            "Read each card in `cindra worklist` before judging it. `good` asks whether "
            "the lead\nwas worth surfacing, not whether they replied -- a prospect who "
            "says no was still\na correct lead to surface."
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
