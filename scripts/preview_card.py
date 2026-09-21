#!/usr/bin/env python
"""Print the Discord card a lead would be sent as, without sending it.

The sibling of `preview_angle.py`, and it exists for the same reason: every card defect
this project has shipped was visible in the rendered output and invisible in the code
that produced it.

  * The evidence field read `company_site · dns_public`, so the operator could not see
    whose page they were about to open -- and Findcheap's proof was a Chrome Web Store
    listing.
  * `T1_AI_SHIP 0.70` printed a constant as though it were a measurement.
  * `T8_HYGIENE_GAP · 0d ago` dated a DNS lookup as something the prospect did today.

None of those is visible in `_fmt_triggers`; all three are obvious in one rendered card.
And the only way to see one used to be waiting for a score job to reach the front of the
queue and a dispatch to fire -- which on an empty queue never happens at all.

So this renders the real embed, through the real `build_card`, from the real database.
It posts nothing, writes nothing and does not touch the queue: the webhook it builds
carries a transport that raises on any request, so a tool for *reading* cards is not one
keystroke away from sending one.

    .venv/bin/python scripts/preview_card.py rtrvr.ai
    .venv/bin/python scripts/preview_card.py --top 5

Read it for what is *populated*. An empty description means the company has no
`description`; a trigger line with no phrase after the code means `scoring.yaml` has no
`means` for it. Both are silent in a finished card, which is the whole problem.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from cindraleads.config import settings
from cindraleads.store import Store


def _leads(store: Store, domain: str | None, top: int) -> list[str]:
    if domain:
        row = store.conn.execute(
            "SELECT lead_id FROM leads WHERE canonical_domain = ?", (domain,)
        ).fetchone()
        return [str(row["lead_id"])] if row else []
    return [
        str(r["lead_id"])
        for r in store.conn.execute(
            "SELECT lead_id FROM leads WHERE tier IN ('A','B','C') AND archived = 0 "
            "ORDER BY score DESC LIMIT ?",
            (top,),
        ).fetchall()
    ]


def _render(embed: dict[str, Any]) -> str:
    """The embed as Discord lays it out, in plain text.

    Deliberately prints every field rather than a summary. A card is judged on what it
    says, and a preview that abbreviates is a different card.
    """
    out: list[str] = ["=" * 78]
    author = str((embed.get("author") or {}).get("name", ""))
    if author:
        out.append(author)
    out.append(str(embed.get("title", "")))
    out.append(str(embed.get("url", "")))
    out.append("")
    out.append(str(embed.get("description", "")))
    for field in embed.get("fields") or []:
        out.append("")
        out.append(f"--- {field['name']} ---")
        out.append(str(field["value"]))
    footer = str((embed.get("footer") or {}).get("text", ""))
    if footer:
        out.append("")
        out.append(footer)
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", nargs="?", help="canonical domain of one lead")
    parser.add_argument("--top", type=int, default=3, help="highest-scoring N (default 3)")
    args = parser.parse_args()

    # Imported here so `--help` works without a config on the path.
    import asyncio

    import httpx

    from cindraleads.agents.dispatcher import Dispatcher, build_card
    from cindraleads.discord import DiscordWebhook

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"preview_card must never post: {request.url}")

    cfg = settings()
    store = Store(cfg.db_file, migrations_dir=cfg.migrations_path)
    try:
        lead_ids = _leads(store, args.domain, args.top)
        if not lead_ids:
            print(f"no lead for {args.domain}" if args.domain else "no leads")
            return 1
        # A Dispatcher only to borrow `read_lead`, which assembles the triggers and
        # evidence exactly as dispatch does. Two guards, because one is not enough:
        # the transport raises on any request, and `webhooks` is non-empty so
        # `__post_init__` does not read the configured secrets -- `{}` would be, since
        # it tests falsy and reads them anyway. `webhook_for` finds nothing truthy and
        # returns None. A tool for *reading* cards must not be one keystroke from
        # sending one.
        client = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
        reader = Dispatcher(
            store=store,
            webhook=DiscordWebhook(client=client),
            config=cfg,
            webhooks={"_preview_never_posts": ""},
        )
        try:
            for lead_id in lead_ids:
                lead = reader.read_lead(lead_id)
                if lead is None:
                    continue
                # Said out loud, because the card does not say it. `arxiv.org` renders
                # a complete Tier A card at score 74 with `Compliance: VETO` in a field
                # that looks like every other field, and nothing tells the reader the
                # Dispatcher will refuse it. A preview that shows an unsendable card as
                # though it were sendable answers a different question from the one
                # being asked of it.
                blocked = reader._blocked(lead)
                if blocked:
                    print(f"### WOULD NOT BE DISPATCHED -- {blocked}")
                print(_render(build_card(lead)))
                print()
        finally:
            # Closed explicitly. An unclosed client is collected later and, under
            # `filterwarnings = ["error"]` on 3.13, charged to whichever test happens to
            # be running -- which is exactly how three leaked `Store` connections were
            # reported as a failure in an unrelated assertion.
            asyncio.run(client.aclose())
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
