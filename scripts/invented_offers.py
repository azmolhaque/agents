#!/usr/bin/env python3
"""Which stored angles name an offer the lead was never given.

Rule 3 of `outreach_angle.md` says to reproduce the offer text as given and **add
nothing to it**. `scripts/preview_angle.py getsitetell.com` showed one that did not:
the prompt was handed `snapshot_free` -- *"your first external attack-surface and
exposed-secrets Snapshot free as a founding-cohort client"*, the whole offer, nothing
paid in it -- and the stored angle opens

    "I'd like to run an AI/LLM security assessment covering prompt injection,
     chain-of-thought attacks, and model hallucination for your chatbot"

**A fabricated engagement, in text a human pastes into an email.** If the prospect
replies yes we have committed to work nobody quoted. No guard sees it: the offer *is*
`snapshot_free`, so `_free_claim_is_backed` correctly allows the free claim, and
`ai_llm_assessment` never appears as a slug.

This counts rather than guards, because the distribution decides whether a guard is
worth having -- the same call that killed the CT-certificate veto, the `name_similarity`
rule, the `--reprose` third sort key, the `open_roles` threshold and re-enrichment as
the reachability lever. One angle is noise; two hundred is a prompt problem.

**The distinctive tokens are derived, never listed.** A hand-written "what counts as
naming the assessment" would be a second place to describe the offers, which is the
defect this project keeps paying for. For each offer the marker set is the tokens in
its own phrases that appear in *no other* offer's phrases -- so "snapshot" and
"attack-surface", which the paid phrase deliberately shares, are excluded on their own
and cannot produce a match.

Reads the database and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from cindraleads.agents.dispatcher import block_reason, blocked_subjects
from cindraleads.agents.scorer import prose_version
from cindraleads.config import settings
from cindraleads.scoring import ScoringConfig, offers_named
from cindraleads.store import Store

#: The derivation and the threshold live in `cindraleads.scoring`, because the
#: dispatch guard asks the same question and a marker set computed twice is the defect
#: this project keeps paying for. This report is what measured the problem; the guard
#: is what acts on it, and they must not be able to disagree.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5, help="example angles per pairing")
    args = parser.parse_args(argv)

    scoring = ScoringConfig.load()
    markers = scoring.offer_markers

    store = Store(settings().db_path)
    try:
        print("# distinctive markers per offer (derived, not listed)\n")
        for slug, marks in sorted(markers.items()):
            print(f"  {slug:<20} {' '.join(sorted(marks)) or '(none -- cannot be detected)'}")

        rows = store.conn.execute(
            "SELECT l.lead_id, l.canonical_domain, l.tier, l.score, l.compliance, "
            "  l.recommended_offer, l.outreach_angle, l.angle_version "
            "FROM leads l WHERE l.archived = 0 AND l.outreach_angle <> '' "
            "ORDER BY l.score DESC"
        ).fetchall()

        # Borrowed from the Dispatcher rather than restated, and read once per scan
        # rather than per row -- the `_is_unsendable` 759x lesson. `pypi.org` reached
        # the first run of this report at Tier B 65: a host blocked for canonicalization
        # whose company row predates the block, so it keeps a tier and an angle that
        # nothing will ever send. Counting its prose teaches nothing about a card that
        # cannot exist, which is what `judge_next.py` exists to stop.
        suppressed, quarantined = blocked_subjects(store.conn)

        # The question a single count cannot answer, and the one that decides what to
        # build. An angle this build would write again is a **prompt** problem: a guard
        # withholds it and `--reprose` rewrites it into the same refusal, which is the
        # "wrong prescription twice" trap already recorded here. One written by an older
        # build is a backlog, and the guard plus a repair pass is the whole fix.
        current = prose_version()

        pairs: Counter[tuple[str, str]] = Counter()
        examples: dict[tuple[str, str], list[str]] = {}
        by_build: Counter[str] = Counter()
        blocked = 0
        for row in rows:
            if block_reason(row, suppressed, quarantined):
                blocked += 1
                continue
            own = str(row["recommended_offer"])
            for named in offers_named(str(row["outreach_angle"]), markers):
                if named == own:
                    continue
                key = (own, named)
                pairs[key] += 1
                by_build["this build" if row["angle_version"] == current else "older"] += 1
                examples.setdefault(key, []).append(
                    f"{row['score']:>3} {row['tier']}  {row['canonical_domain']}\n"
                    f"      {str(row['outreach_angle'])[:240]}"
                )

        total = len(rows) - blocked
        affected = sum(pairs.values())
        print(f"\n# {affected} of {total} stored angle(s) name an offer the lead was not given")
        print(f"  ({blocked} dispatchable-by-tier lead(s) skipped: the Dispatcher refuses them)")
        if affected:
            fresh = by_build["this build"]
            print(
                f"  {fresh} written by THIS build, {by_build['older']} by an older one -- "
                f"{'the prompt still does it' if fresh else 'reachable by --reprose alone'}"
            )
        if not pairs:
            print("\nNothing to guard. The one case that prompted this report is gone or")
            print("was never representative -- either way a mechanism is not worth building.")
            return 0

        for (own, named), count in pairs.most_common():
            share = 100.0 * count / total if total else 0.0
            print(f"\n## lead was offered {own} · angle names {named} -- {count} ({share:.1f}%)")
            for line in examples[(own, named)][: args.rows]:
                print(f"  {line}\n")
        print(
            "Read these before deciding. A guard here withholds the angle, so a wrong\n"
            "match costs a card its text -- the same trade `_free_claim_is_backed` makes,\n"
            "and the reason that one keys on the price rather than on the word 'free'."
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
