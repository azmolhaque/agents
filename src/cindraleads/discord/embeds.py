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

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from cindraleads.dedupe import is_platform_url
from cindraleads.discord import limits
from cindraleads.models import provenance_of, to_iso

__all__ = [
    "TIER_COLORS",
    "CardData",
    "TriggerLine",
    "digest_row",
    "digest_summary",
    "lead_card",
]

# Ember, amber, cyan. Tier A is the one you should feel in your peripheral vision.
TIER_COLORS: dict[str, int] = {
    "A": 0xFF5A36,
    "B": 0xFFC169,
    "C": 0x33E0C8,
    "REJECT": 0x6B7280,
}

TIER_MARK = {"A": "▲", "B": "◆", "C": "•", "REJECT": "✕"}


@dataclass(frozen=True)
class TriggerLine:
    """One trigger as the card shows it.

    This was a bare `(code, confidence, when)` tuple, and the anonymity is most of why
    the card read badly. `confidence` is **provenance, not probability** -- exactly two
    constants, 0.7 for what a 4B read off a page and 0.8 for a public record we looked
    up ourselves -- and printing it as `0.70` told the reader we were 70% sure of *this
    claim about this company*, which is a measurement nobody made. `code` is our
    internal taxonomy and means nothing to the person deciding whether to send.

    So the card now prints what the number means and what the code means, and `means`
    is the same phrase the outreach prompt is handed. One row, read end to end:

        `T10_VENDOR_PRESSURE` a customer has asked them for a pentest report
            · read off their page · 3d ago
    """

    code: str
    confidence: float
    when: str = ""
    #: The `means` phrase from `scoring.yaml`, empty when the config could not be read.
    means: str = ""


@dataclass(frozen=True)
class CardData:
    lead_id: str
    canonical_domain: str
    display_name: str
    tier: str
    score: int
    offer: str
    #: What the card *shows* for the offer. `offer` stays the slug because the guards
    #: key on it; this is the phrase a human reads. Empty falls back to the slug, which
    #: is the old behaviour and is what a card built by hand in a test gets.
    offer_label: str = ""
    triggers: tuple[TriggerLine, ...] = ()
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


def _offer(data: CardData) -> str:
    """What the card shows where the offer goes."""
    return data.offer_label or data.offer


def _fmt_triggers(data: CardData) -> str:
    """One line per trigger: what it is, what it means, how we know, and when."""
    if not data.triggers:
        return "—"
    lines: list[str] = []
    for line in data.triggers:
        parts = [f"`{line.code}`"]
        if line.means:
            parts.append(line.means)
        parts.append(provenance_of(line.confidence))
        if line.when:
            parts.append(line.when)
        lines.append(f"{parts[0]} {' · '.join(parts[1:])}" if len(parts) > 1 else parts[0])
    return "\n".join(lines)


def _display_host(url: str) -> str:
    """The host as the reader would see it in an address bar.

    Deliberately **not** `canonical_domain`, which returns None for a platform host --
    it is a rejection, and the hosts it rejects are exactly the ones the reader most
    needs to see. `www.` is dropped because it is noise and nothing here compares the
    result against anything.
    """
    try:
        host = urlparse(url if "//" in url else f"https://{url}").hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _fmt_evidence(data: CardData) -> str:
    """Markdown links, deduplicated by URL, labelled by host.

    Evidence is the one field that must survive truncation intact — a lead card whose
    links were trimmed to make room for prose is unverifiable, which is the same as
    having no evidence at all.

    **The label was the `source_id`**, so the card read `company_site · dns_public` and
    the reader could not see whose page they were about to open without clicking it.
    That is the Findcheap defect on the card the human sees *first*: its worklist row
    cited `chromewebstore.google.com/detail/findcheap/...` as proof of what findcheap.ai
    had announced, and the fix there -- prefer their own domain, mark a borrowed page --
    stopped at the call list. The Discord card had neither half.

    Marked and not hidden, for the reason the worklist states: a platform link is
    sometimes the only proof a trigger holds, and blanking it would leave the operator
    telling a stranger "you published this" with nothing to check. Marked *inline*
    rather than as a legend under the field, because a card cites several URLs and
    usually only one of them is borrowed.

    Only a platform host is marked. `crt.sh` and `dns.google` are not the prospect's
    page either and are perfectly honest citations -- a public record, which is what the
    trigger line says they are -- and a warning on every card is one nobody reads.
    """
    if not data.evidence:
        return "—"
    seen: set[str] = set()
    parts: list[str] = []
    for label, url in data.evidence:
        if url in seen:
            continue
        seen.add(url)
        host = _display_host(url) or label
        borrowed = " ⚠️ not their page" if is_platform_url(url) else ""
        parts.append(f"[{host}]({url}){borrowed}")
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
                f"{mark} TIER {data.tier} · CindraScore {data.score} · {_offer(data)}",
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
    top = "—"
    if data.triggers:
        line = data.triggers[0]
        top = f"`{line.code}`" + (f" {line.means}" if line.means else "")
    link = ""
    if data.evidence:
        label, url = data.evidence[0]
        host = _display_host(url) or label
        borrowed = " ⚠️" if is_platform_url(url) else ""
        link = f" · [{host}]({url}){borrowed}"

    embed: dict[str, Any] = {
        "color": TIER_COLORS.get(data.tier, TIER_COLORS["C"]),
        "title": limits.truncate(
            f"{TIER_MARK.get(data.tier, '•')} {data.score} · {data.display_name}", limits.TITLE
        ),
        "url": f"https://{data.canonical_domain}",
        "description": limits.truncate(f"{top} · {_offer(data)}{link}\n{data.description}", 400),
    }
    return _fit_total(embed)


def digest_summary(values: Mapping[str, float]) -> str:
    """The run's numbers, under the digest.

    Deliberately includes the rejections and the spend. **A digest that only reports
    what was dispatched cannot tell you the day the pipeline started rejecting
    everything** -- a morning with no Tier C rows reads identically whether nothing
    scored, the credits ran out at 09:00, or the worker has been down since Tuesday.

    Built, documented with that rationale, and then never called: the digest posted its
    pages and nothing else for the life of the project. Tenth instance of
    built-wired-never-connected, after `digest_pages` in this same module.

    Takes the `metrics.snapshot()` mapping rather than a dict assembled for it, because
    a hand-passed dict is a second place to compute "how many leads are live" -- which
    is the exact thing `snapshot` says in its own docstring that it exists to prevent.
    A key it does not carry is not printed; there is no number here that is not read
    from the same place `/metrics` and `/healthz` read theirs.
    """

    def n(key: str) -> int:
        return int(values.get(key, 0))

    tiers = " · ".join(f"{t} {n(f'leads_tier_{t.lower()}')}" for t in ("A", "B", "C"))
    parts = [
        f"companies {n('companies_total')}",
        f"leads {n('leads_total')} ({tiers})",
        f"rejected {n('leads_tier_reject')}",
        f"dispatched {n('dispatches_24h')}/24h",
        f"queue {n('queue_ready')} ready",
        f"cloud ${float(values.get('cloud_usd_24h', 0.0)):.2f}/24h",
    ]
    # Printed only when it is not zero, because a `dead 0` on every digest is a line
    # nobody reads and a `dead 4` under a thin digest is the answer to why it is thin.
    if n("dead_letter_recent"):
        parts.append(f"**dead {n('dead_letter_recent')}/24h**")
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
