"""Discord output. The only place the pipeline speaks to a human.

Write-only by design: the Dispatcher posts lead cards and never sends a prospect
anything. A human reads the card and decides who to contact. That human-in-the-loop is
the control the compliance story rests on, and no score replaces it.

Reading *from* Discord needs a gateway bot (PLAN.md decision 1) and arrives in Phase 8;
webhooks cannot receive a reaction.
"""

from cindraleads.discord import embeds, limits
from cindraleads.discord.embeds import CardData, digest_row, digest_summary, lead_card
from cindraleads.discord.webhook import DiscordWebhook, WebhookResult

__all__ = [
    "CardData",
    "DiscordWebhook",
    "WebhookResult",
    "digest_row",
    "digest_summary",
    "embeds",
    "lead_card",
    "limits",
]
