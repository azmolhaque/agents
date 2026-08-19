"""The Discord gateway client. Reactions in, nothing out.

This is the thinnest possible layer over `store.py`: it translates a gateway event into
one function call and does no deciding of its own. That is deliberate, because it is
the one component that cannot be tested offline end to end -- every rule worth getting
wrong lives one module over, where a test can reach it without a token.

**Read-only, structurally.** The intents it requests are `guilds` and `guild_reactions`
and nothing else, so the connection is not authorised to read message content and the
client is never handed a channel to write to. `send`ing is not forbidden by convention
here; the bot simply never obtains the objects that would let it. The Dispatcher writes
to Discord, this listens, and nothing in between emails a prospect.

**Raw events, not the cached ones.** `on_reaction_add` fires only for messages in the
client's cache, which after a restart is empty -- so reacting to yesterday's card would
be silently ignored while reacting to today's worked. Every card is yesterday's card
eventually. `on_raw_reaction_add` fires for any message in a watched channel regardless
of cache, and carries the message id, which is the only field the join needs.

**Its death must not stall the pipeline.** It runs as its own unit
(`cindraleads-feedback.service`) holding no queue lease and no model, so killing it
costs feedback and nothing else -- a test in `tests/chaos` asserts the pipeline still
drains with it stopped.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from cindraleads.config import settings
from cindraleads.errors import CindraError
from cindraleads.logging import get_logger
from cindraleads.metrics import record_heartbeat
from cindraleads.store import Store

__all__ = ["ReactionEvent", "handle_raw_reaction", "run_bot"]

log = get_logger("cindraleads.feedback.bot")

# How often the bot records that it is alive, in seconds. A gateway client is silent by
# nature -- a week with no reactions looks exactly like a week with a revoked token --
# so the heartbeat is the only thing that tells `/healthz` the two apart.
HEARTBEAT_SECONDS = 300.0


class ReactionEvent(Protocol):
    """The three fields of `discord.RawReactionActionEvent` this module reads.

    Stated as a Protocol so the handler can be exercised with a plain object. Depending
    on the concrete discord.py type here would make the only interesting branch --
    "which verdict, for which lead" -- untestable without the optional dependency
    installed.
    """

    @property
    def message_id(self) -> int: ...

    @property
    def user_id(self) -> int: ...

    @property
    def emoji(self) -> Any: ...


def handle_raw_reaction(store: Store, event: ReactionEvent, *, removed: bool) -> None:
    """One gateway event, one call into the store.

    `event.emoji` is a `PartialEmoji`, whose `str()` is the unicode character for a
    standard emoji and `<:name:id>` for a custom one. Custom emoji therefore never
    match `REACTION_VERDICTS` and are ignored, which is the intended behaviour: a
    server's own `:shipit:` means nothing to the precision figure.
    """
    from cindraleads.feedback.store import record_reaction, remove_reaction

    action = remove_reaction if removed else record_reaction
    # The whole translation is inside the guard, reading the event included. discord.py
    # has no supervisor for an exception raised in a handler, so a bug here produces a
    # bot that stays connected and silently records nothing -- and the payload's fields
    # are as much external input as its contents are.
    try:
        message_id = str(event.message_id)
        result = action(
            store, message_id=message_id, emoji=str(event.emoji), actor=str(event.user_id)
        )
    except Exception as exc:
        log.warning("feedback_event_failed", error=str(exc), removed=removed)
        return

    if not result.recorded:
        log.debug("feedback_event_ignored", message_id=message_id, reason=result.reason)


def _build_client(store: Store) -> Any:
    """Construct the discord.py client, importing it only when actually running.

    The import is local so `cindra` works on a box without the `feedback` extra
    installed -- the bot is one optional unit, not a dependency of the pipeline.
    """
    try:
        import discord
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise CindraError(
            "the feedback bot needs discord.py: pip install -e '.[feedback]'"
        ) from exc

    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_reactions = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:  # pragma: no cover - needs a gateway
        log.info("feedback_bot_ready", user=str(client.user))

    @client.event
    async def on_raw_reaction_add(payload: Any) -> None:  # pragma: no cover
        if client.user is not None and payload.user_id == client.user.id:
            return
        handle_raw_reaction(store, payload, removed=False)

    @client.event
    async def on_raw_reaction_remove(payload: Any) -> None:  # pragma: no cover
        if client.user is not None and payload.user_id == client.user.id:
            return
        handle_raw_reaction(store, payload, removed=True)

    return client


async def _heartbeat(store: Store) -> None:  # pragma: no cover - a timing loop
    while True:
        record_heartbeat(store, "feedback")
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def run_bot(store: Store) -> None:  # pragma: no cover - needs a gateway
    """Connect and stay connected. Returns only on shutdown.

    discord.py owns the reconnect loop, which is the main reason to use it rather than
    a raw websocket: a Pi on domestic broadband disconnects, and a gateway resume is
    fiddly to get right.
    """
    token = settings().discord_bot_token
    if token is None:
        raise CindraError("DISCORD_BOT_TOKEN is not set; the feedback bot cannot start")

    client = _build_client(store)
    beat = asyncio.create_task(_heartbeat(store))
    try:
        record_heartbeat(store, "feedback")
        await client.start(token.get_secret_value())
    finally:
        beat.cancel()
        await client.close()
