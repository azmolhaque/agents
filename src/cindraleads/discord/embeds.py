"""Building the lead card.

Two builders, sharing one limits module (PLAN.md 2.6):

* `lead_card` — the full card from the master prompt's section 10, one per message, for
  Tier A and B. Everything you need to decide whether to write to someone.
* `digest_row` — a compact row, eight per message, for the Tier C daily roll-up. Same
  facts, ruthlessly shortened, because ten full cards is ~12,000 characters against a
  6,000 limit.

**The card never claims we scanned anything.** Not in the angle, not in the surface
field, not by implication. That is a legal boundary and the entire brand promise, so
the wording here is as load-bearing as the code: "publicly visible", "as published",
"from public records". A card that reads like a pentest report is a compliance failure
even though nothing was scanned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cindraleads.discord import limits
from cindraleads.models import to_iso

__all__ = ["TIER_COLORS", "CardData", "digest_row", "digest_summary", "lead_card"]

# Ember, amber, cyan. Tier A is the one you should feel in your peripheral vision.
TIER_COLORS: dict[str, int] = {
    "A": 0xFF5A36,
    "B": 0xFFC169,
    "C": 0x33E0C8,
    "REJECT": 0x6B7280,
}

TIER_MARK = {"A": "▲", "B": "◆", "C": "•", "REJECT": "✕"}


@dataclass(frozen=True)
class CardData:
    lead_id: str
    canonical_domain: str
    display_name: str
    tier: str
    score: int
    offer: str
    triggers: tuple[tuple[str, float, str], ...] = ()  # (code, confidence, when)
    evidence: tuple[tuple[str, str], ...] = ()  # (label, url)
    description: str = ""
    outreach_angle: str = ""
    bengali_angle: str | None = None
    contacts: tuple[str, ...] = ()
    surface_notes: tuple[str, ...] = ()
    compliance_basis: str = "legitimate_interest_b2b"
    compliance_passed: bool = True
    pipeline_version: str = ""
    observed_at: datetime | None = None
    extra_fields: tuple[tuple[str, str], ...] = field(default=())


def _fmt_triggers(data: CardData) -> str:
    if not data.triggers:
        return "—"
    return "\n".join(
        f"`{code}` {confidence:.2f}" + (f" · {when}" if when else "")
        for code, confidence, when in data.triggers
    )


def _fmt_evidence(data: CardData) -> str:
    """Markdown links, deduplicated by URL.

    Evidence is the one field that must survive truncation intact — a lead card whose
    links were trimmed to make room for prose is unverifiable, which is the same as
    having no evidence at all.
    """
    if not data.evidence:
        return "—"
    seen: set[str] = set()
    parts: list[str] = []
    for label, url in data.evidence:
        if url in seen:
            continue
        seen.add(url)
        parts.append(f"[{label}]({url})")
    return " · ".join(parts)


def lead_card(data: CardData) -> dict[str, Any]:
    """The full card, for Tier A and B. One per message."""
    mark = TIER_MARK.get(data.tier, "•")
    fields: list[dict[str, Any]] = [
        {
            "name": "🔥 Triggers",
            "value": limits.truncate(_fmt_triggers(data), limits.FIELD_VALUE),
            "inline": False,
        }
    ]

    if data.outreach_angle:
        fields.append(
            {
                "name": "🎯 Angle",
                "value": limits.truncate(data.outreach_angle, limits.FIELD_VALUE),
                "inline": False,
            }
        )
    if data.bengali_angle:
        fields.append(
            {
                "name": "🇧🇩 বাংলা",
                "value": limits.truncate(data.bengali_angle, limits.FIELD_VALUE),
                "inline": False,
            }
        )
    if data.contacts:
        fields.append(
            {
                "name": "👤 Contact",
                "value": limits.truncate("\n".join(data.contacts), limits.FIELD_VALUE),
                "inline": False,
            }
        )
    if data.surface_notes:
        fields.append(
            {
                # "Publicly visible", not "found" or "detected". Nothing was scanned and
                # the card must not imply otherwise.
                "name": "🛰 Publicly visible",
                "value": limits.truncate(" · ".join(data.surface_notes), limits.FIELD_VALUE),
                "inline": False,
            }
        )

    fields.append(
        {
            "name": "📎 Evidence",
            "value": limits.truncate(_fmt_evidence(data), limits.FIELD_VALUE, keep_links=True),
            "inline": False,
        }
    )
    fields.append(
        {
            "name": "⚖️ Compliance",
            "value": limits.truncate(
                f"{'PASS' if data.compliance_passed else 'VETO'} · basis: {data.compliance_basis}"
                " · passive-only ✓ · no scan performed",
                limits.FIELD_VALUE,
            ),
            "inline": False,
        }
    )
    for name, value in data.extra_fields:
        fields.append(
            {
                "name": limits.truncate(name, limits.FIELD_NAME),
                "value": limits.truncate(value, limits.FIELD_VALUE),
                "inline": False,
            }
        )

    stamp = to_iso(data.observed_at) if data.observed_at else ""
    embed: dict[str, Any] = {
        "color": TIER_COLORS.get(data.tier, TIER_COLORS["C"]),
        "author": {
            "name": limits.truncate(
                f"{mark} TIER {data.tier} · CindraScore {data.score} · {data.offer}",
                limits.AUTHOR_NAME,
            )
        },
        "title": limits.truncate(f"{data.display_name} · {data.canonical_domain}", limits.TITLE),
        "url": f"https://{data.canonical_domain}",
        "description": limits.truncate(data.description or "—", limits.DESCRIPTION),
        "fields": fields[: limits.FIELDS_PER_EMBED],
        "footer": {
            "text": limits.truncate(
                f"lead_id {data.lead_id} · {data.pipeline_version} · {stamp}", limits.FOOTER_TEXT
            )
        },
    }
    return _fit_total(embed)


def digest_row(data: CardData) -> dict[str, Any]:
    """A compact row for the Tier C roll-up. Eight per message.

    Title, score, top trigger, one evidence link. Roughly 450 characters, which is what
    makes eight of them fit where ten full cards cannot.
    """
    top = data.triggers[0][0] if data.triggers else "—"
    link = ""
    if data.evidence:
        label, url = data.evidence[0]
        link = f" · [{label}]({url})"

    embed: dict[str, Any] = {
        "color": TIER_COLORS.get(data.tier, TIER_COLORS["C"]),
        "title": limits.truncate(
            f"{TIER_MARK.get(data.tier, '•')} {data.score} · {data.display_name}", limits.TITLE
        ),
        "url": f"https://{data.canonical_domain}",
        "description": limits.truncate(f"`{top}` · {data.offer}{link}\n{data.description}", 400),
    }
    return _fit_total(embed)


def digest_summary(stats: dict[str, Any]) -> str:
    """The run's numbers, under the digest.

    Deliberately includes the rejections and the spend. A digest that only reports
    what was dispatched cannot tell you the day the pipeline started rejecting
    everything.
    """
    parts = [
        f"harvested {stats.get('harvested', 0)}",
        f"candidates {stats.get('candidates', 0)}",
        f"companies {stats.get('companies', 0)}",
        f"dispatched {stats.get('dispatched', 0)}",
        f"vetoed {stats.get('vetoed', 0)}",
        f"credits {stats.get('credits', 0)}",
        f"cloud ${float(stats.get('usd', 0)):.2f}",
    ]
    if stats.get("peak_temp_c"):
        parts.append(f"peak {stats['peak_temp_c']}°C")
    return " · ".join(parts)


def _fit_total(embed: dict[str, Any]) -> dict[str, Any]:
    """Last-resort enforcement of the 6000-character total.

    Every field is already individually bounded, but the *sum* is a separate limit and
    a card with many fields can satisfy all the per-field caps and still be rejected.
    Trims the description first, then drops optional fields from the end — evidence and
    compliance are moved ahead of the cut, because a card without its evidence link is
    not worth sending.
    """
    if limits.total_characters(embed) <= limits.TOTAL_CHARACTERS:
        return embed

    description = str(embed.get("description", ""))
    overflow = limits.total_characters(embed) - limits.TOTAL_CHARACTERS
    if len(description) > overflow + 16:
        embed["description"] = limits.truncate(description, len(description) - overflow - 16)
        if limits.total_characters(embed) <= limits.TOTAL_CHARACTERS:
            return embed

    fields = list(embed.get("fields") or [])
    protected = [f for f in fields if str(f.get("name", "")).startswith(("📎", "⚖️"))]
    optional = [f for f in fields if f not in protected]
    while optional and limits.total_characters(embed) > limits.TOTAL_CHARACTERS:
        optional.pop()
        embed["fields"] = optional + protected
    embed["fields"] = optional + protected
    return embed
