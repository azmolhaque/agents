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

from cindraleads.agents.dispatcher import block_reason, blocked_subjects
from cindraleads.agents.scorer import prose_version
from cindraleads.config import settings
from cindraleads.scoring import ScoringConfig
from cindraleads.store import Store

#: How many of an offer's distinctive tokens an angle must carry before this counts it
#: as naming that offer. One is too loose -- "agent" alone appears in ordinary prose
#: about an AI company -- and the Site Tell angle carries four.
MARKER_HITS = 2

#: Words that carry no offer identity wherever they appear. The derivation below
#: subtracts everything the rest of the prompt uses, but a phrase that is the only one
#: to say "as" or "two" would otherwise donate those as markers -- and `as` really was
#: in `snapshot_free`'s set on the first real run.
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
        "as",
        "is",
        "are",
        "be",
        "will",
        "we",
        "us",
        "our",
        "they",
        "their",
        "them",
        "re",
        "two",
        "three",
        "later",
        "than",
        "but",
        "if",
        "so",
        "by",
        "about",
        "into",
        "over",
        "up",
        "out",
        "all",
        "any",
        "each",
        "more",
        "most",
        "like",
        "run",
        "runs",
        "running",
    }
)

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in _FILLER and len(t) > 1}


def distinctive_markers(scoring: ScoringConfig) -> dict[str, set[str]]:
    """Per offer, the tokens nothing else in the prompt uses.

    **The first real run got this wrong and the distribution said so.** Markers were
    derived by subtracting only what *other offers* share, and the report then flagged
    274 of 1141 angles. Three sample rows settled it: `matterhaul.com` reproduces its
    `snapshot_free` text faithfully and matched `ai_llm_assessment` on `ai` and
    `agent` -- words from the **trigger** phrases T1_AI_SHIP and T11_STACK_RISK, which
    sit in the same prompt and appear in nearly every angle this corpus produces. The
    report was measuring "does this angle mention AI", which for an AI-company corpus
    is a constant, the tenth appearance of that tell here.

    So the subtraction is against everything the model is handed beside the offer: the
    other offers, every trigger `means` phrase and every `ai_surface` phrase, in both
    currencies because `offer_phrase` picks by country. What survives is a word that
    can only have come from this offer's own text.

    An offer may legitimately end up with nothing left -- the paid phrases name the
    free Snapshot on purpose, so `free`, `first`, `attack`, `surface` and `snapshot`
    are shared by design. The report prints that rather than pretending, because a
    marker set this cannot distinguish is a question it must not answer.
    """
    phrases: dict[str, set[str]] = {}
    for slug, offer in scoring.offers.items():
        text = " ".join(
            str(offer.get(key) or "")
            for key in ("means", "means_bd")  # both currencies
        )
        phrases[slug] = _tokens(text)

    # Everything else the prompt says, which an honest angle may quote freely.
    elsewhere: set[str] = set()
    for rule in scoring.triggers.values():
        elsewhere |= _tokens(
            " ".join(str(getattr(rule, key, "") or "") for key in ("means", "means_bd"))
        )
    elsewhere |= _tokens(" ".join(scoring.surface_phrases(tuple(scoring.surfaces))))

    return {
        slug: own - elsewhere - set().union(*(other for s, other in phrases.items() if s != slug))
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
