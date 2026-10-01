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
import re
import sys
from collections import Counter

from cindraleads.config import settings
from cindraleads.scoring import ScoringConfig
from cindraleads.store import Store

#: How many of an offer's distinctive tokens an angle must carry before this counts it
#: as naming that offer. One is too loose -- "agent" alone appears in ordinary prose
#: about an AI company -- and the Site Tell angle carries four.
MARKER_HITS = 2

#: Words that carry no offer identity. Short and deliberately so: the derivation below
#: already removes everything two offers share, so this only has to drop the filler
#: that survives because exactly one phrase happens to use it.
_FILLER = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "after",
        "it",
        "that",
        "or",
        "your",
        "you",
        "first",
        "from",
        "starting",
        "one",
    }
)

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in _FILLER and len(t) > 1}


def distinctive_markers(scoring: ScoringConfig) -> dict[str, set[str]]:
    """Per offer, the tokens no other offer's phrasing uses.

    Both currencies, because `offer_phrase` picks by country and an angle written for
    a BD lead was handed the Taka wording. Prices are tokenised too and drop out on
    their own: every paid phrase names one, so no digit string is distinctive except
    the amounts, which is correct -- quoting another offer's price is exactly the thing
    worth seeing.
    """
    phrases: dict[str, set[str]] = {}
    for slug, offer in scoring.offers.items():
        text = " ".join(
            str(offer.get(key) or "")
            for key in ("means", "means_bd")  # both currencies
        )
        phrases[slug] = _tokens(text)
    return {
        slug: own - set().union(*(other for s, other in phrases.items() if s != slug))
        for slug, own in phrases.items()
    }


def offers_named(angle: str, markers: dict[str, set[str]]) -> list[str]:
    """Every offer whose distinctive tokens this angle carries, its own included."""
    seen = _tokens(angle)
    return sorted(slug for slug, marks in markers.items() if len(seen & marks) >= MARKER_HITS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5, help="example angles per pairing")
    args = parser.parse_args(argv)

    scoring = ScoringConfig.load()
    markers = distinctive_markers(scoring)

    store = Store(settings().db_path)
    try:
        print("# distinctive markers per offer (derived, not listed)\n")
        for slug, marks in sorted(markers.items()):
            print(f"  {slug:<20} {' '.join(sorted(marks)) or '(none -- cannot be detected)'}")

        rows = store.conn.execute(
            "SELECT l.lead_id, l.canonical_domain, l.tier, l.score, "
            "  l.recommended_offer, l.outreach_angle "
            "FROM leads l WHERE l.archived = 0 AND l.outreach_angle <> '' "
            "ORDER BY l.score DESC"
        ).fetchall()

        pairs: Counter[tuple[str, str]] = Counter()
        examples: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            own = str(row["recommended_offer"])
            for named in offers_named(str(row["outreach_angle"]), markers):
                if named == own:
                    continue
                key = (own, named)
                pairs[key] += 1
                examples.setdefault(key, []).append(
                    f"{row['score']:>3} {row['tier']}  {row['canonical_domain']}\n"
                    f"      {str(row['outreach_angle'])[:240]}"
                )

        total = len(rows)
        affected = sum(pairs.values())
        print(f"\n# {affected} of {total} stored angle(s) name an offer the lead was not given")
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
