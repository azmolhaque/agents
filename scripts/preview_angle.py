#!/usr/bin/env python
"""Print the outreach prompt a company would get, without calling a model.

The prompt is where every prose defect this project has shipped actually lived, and
each one cost hours to find because the only way to see the prompt was to wait for a
score job to reach the front of the queue and then read the card it produced.

  * `T1_AI_SHIP` reached a prospect's inbox because the code was passed where a phrase
    belonged.
  * `ai_llm_assessment` did the same, wrapped in a hardcoded "free" -- a $2k-8k
    engagement given away on every Tier A and B card in the corpus.
  * Four facts the Scorer had already assembled were never passed at all, so every
    angle opened with "you announced an AI feature", true of half the internet.

Every one of those is visible in the prompt text in under a second. None of them was
visible in the code, because the code reads correctly in all three cases -- the comment
above the offending `format()` call described the exact defect it was committing.

So: this renders the real prompt from the real database, for one company, using the
same `_read` and the same template the Scorer uses. It writes nothing, calls nothing,
and does not touch the queue.

    .venv/bin/python scripts/preview_angle.py rtrvr.ai

Read it for what is *populated*. An empty `Verified quotes` block means the evidence
rows for that company carry no snippet; an empty `What they shipped` means `ai_surface`
is empty or its values have no phrase in `scoring.yaml`. Both are silent in a finished
card, which is exactly the problem.
"""

from __future__ import annotations

import argparse
import sys

from cindraleads.agents.scorer import Scorer, _quote_block, _recipient_name
from cindraleads.config import settings
from cindraleads.scoring import score
from cindraleads.store import Store


def _preview(scorer: Scorer, domain: str) -> int:
    facts = scorer._read(domain)
    if facts is None:
        print(f"{domain}: no company row")
        return 1

    if scorer._angle_prompt is None:
        print("no outreach_angle prompt on disk")
        return 1

    result = score(scorer._score_input(facts), scorer.scoring)
    prompt = scorer._angle_prompt.format(
        display_name=facts["display_name"],
        canonical_domain=domain,
        description=facts["description"] or "",
        triggers=scorer._trigger_phrases(scorer._score_input(facts).triggers),
        offer=scorer.scoring.offer_phrase(result.offer),
        country=facts["country"] or "",
        quotes=_quote_block(facts["evidence"]),
        surfaces=", ".join(scorer.scoring.surface_phrases(facts["ai_surface"])),
        published_gaps=", ".join(facts["hygiene_gaps"][:2]),
        recipient=_recipient_name(facts["contacts"]),
    )

    print("=" * 78)
    print(f"{domain}  score {result.score} {result.tier}  offer {result.offer}")
    print("=" * 78)
    # Only the facts block. The rules above it are static and long, and what is being
    # checked here is always whether a field arrived with anything in it.
    marker = "## The company"
    print(prompt[prompt.index(marker) :] if marker in prompt else prompt)

    empty = [
        name
        for name, value in (
            ("description", facts["description"]),
            ("quotes", _quote_block(facts["evidence"])),
            ("surfaces", scorer.scoring.surface_phrases(facts["ai_surface"])),
            ("published_gaps", facts["hygiene_gaps"]),
            ("recipient", _recipient_name(facts["contacts"])),
        )
        if not value
    ]
    if empty:
        print(f"\nEMPTY, so the angle cannot use them: {', '.join(empty)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domains", nargs="+", help="canonical domains")
    args = parser.parse_args()

    cfg = settings()
    store = Store(cfg.db_file, migrations_dir=cfg.migrations_path)
    scorer = Scorer(store=store, config=cfg)
    try:
        return max(_preview(scorer, domain) for domain in args.domains)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
