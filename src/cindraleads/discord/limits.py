"""Every Discord constraint, in one place.

Scattered across the embed builders these become five separate opportunities to be
wrong, and the failure mode is a 400 at dispatch time with a card that took ~64 s of
inference to produce. One module, one set of numbers, and a truncation helper that
every field goes through.

The 6000-character total is the one that bites. PLAN.md 2.6: the master prompt asks for
"one embed per lead, max 10 embeds" in the digest, but a full lead card is ~1,100-1,400
characters and ten of them is roughly 12,000 — a guaranteed rejection. Hence two
builders and a page size of 8.
"""

from __future__ import annotations

__all__ = [
    "AUTHOR_NAME",
    "DESCRIPTION",
    "DIGEST_PAGE_SIZE",
    "EMBEDS_PER_MESSAGE",
    "FIELDS_PER_EMBED",
    "FIELD_NAME",
    "FIELD_VALUE",
    "FOOTER_TEXT",
    "TITLE",
    "TOTAL_CHARACTERS",
    "truncate",
]

TITLE = 256
DESCRIPTION = 4096
FIELDS_PER_EMBED = 25
FIELD_NAME = 256
FIELD_VALUE = 1024
FOOTER_TEXT = 2048
AUTHOR_NAME = 256
EMBEDS_PER_MESSAGE = 10
TOTAL_CHARACTERS = 6000

# Eight compact rows, not ten. Leaves headroom under the 6000 total for the summary
# line, which is the part that would otherwise be silently dropped.
DIGEST_PAGE_SIZE = 8

ELLIPSIS = "…"


def truncate(text: str, limit: int, *, keep_links: bool = False) -> str:
    """Cut to `limit`, at a word boundary, with an ellipsis.

    `keep_links` trims from the *front* of the prose instead of the back, so a field
    whose value ends in evidence URLs keeps them. Losing the evidence link to make room
    for more adjectives would defeat the point of the card.
    """
    if len(text) <= limit:
        return text
    if limit <= len(ELLIPSIS):
        return ELLIPSIS[:limit]

    if keep_links:
        tail = text[-(limit - len(ELLIPSIS)) :]
        cut = tail.find(" ")
        return ELLIPSIS + (tail[cut + 1 :] if 0 <= cut < 40 else tail)

    head = text[: limit - len(ELLIPSIS)]
    cut = head.rfind(" ")
    # Only break at a space if one is reasonably near the end; otherwise a single long
    # token would collapse the field to almost nothing.
    return (head[:cut] if cut > limit * 0.6 else head) + ELLIPSIS


def total_characters(embed: dict[str, object]) -> int:
    """What Discord counts toward the 6000 limit: every rendered string in the embed."""
    total = 0
    for key in ("title", "description"):
        value = embed.get(key)
        if isinstance(value, str):
            total += len(value)
    for key in ("footer", "author"):
        block = embed.get(key)
        if isinstance(block, dict):
            for inner in ("text", "name"):
                value = block.get(inner)
                if isinstance(value, str):
                    total += len(value)
    fields = embed.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if isinstance(field, dict):
                total += len(str(field.get("name", ""))) + len(str(field.get("value", "")))
    return total
