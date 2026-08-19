"""Phase 8 feedback ingress: Discord reactions become rows in `feedback`.

Split three ways on purpose. `store.py` is the join and the write, and knows nothing
about Discord. `bot.py` is a gateway client that translates reactions into calls on it.
`cli.py`'s `cindra feedback` is the same write by hand.

That split is what makes the loop testable: every rule about which reactions count,
which lead they attach to and what a duplicate means lives in `store.py` and is
exercised without a network, a token, or a guild.
"""

from cindraleads.feedback.store import (
    REACTION_VERDICTS,
    VERDICTS,
    FeedbackResult,
    PrecisionReport,
    lead_for_message,
    precision_report,
    record_reaction,
    record_verdict,
    remove_reaction,
    unjudged_leads,
)

__all__ = [
    "REACTION_VERDICTS",
    "VERDICTS",
    "FeedbackResult",
    "PrecisionReport",
    "lead_for_message",
    "precision_report",
    "record_reaction",
    "record_verdict",
    "remove_reaction",
    "unjudged_leads",
]
