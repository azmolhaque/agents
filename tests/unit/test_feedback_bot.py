"""The gateway client's translation layer, and the two ends that have to agree.

`bot.py` is deliberately thin -- it turns a gateway event into one call on
`feedback/store.py` and decides nothing -- so these tests are about the translation and
about the wiring between the CLI, the unit file and the health endpoint. The rules
themselves are tested in `test_feedback.py`, without discord.py installed.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from cindraleads.feedback.bot import HEARTBEAT_SECONDS, handle_raw_reaction
from cindraleads.metrics import HEARTBEAT_UNITS
from cindraleads.models import to_iso, utcnow

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT = REPO_ROOT / "deploy/systemd/cindraleads-feedback.service"


class _Emoji:
    """`discord.PartialEmoji`'s only relevant behaviour: what `str()` gives you."""

    def __init__(self, rendered: str) -> None:
        self._rendered = rendered

    def __str__(self) -> str:
        return self._rendered


class _Event:
    """A stand-in for `discord.RawReactionActionEvent`."""

    def __init__(self, message_id: str, emoji: str, user_id: int = 42) -> None:
        self.message_id = message_id
        self.user_id = user_id
        self.emoji = _Emoji(emoji)


def _dispatched(store: Any, lead_id: str = "lead-1", message_id: str = "msg-1") -> None:
    now = to_iso(utcnow())
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO dispatch_log (dispatch_id, lead_id, channel, tier, score, "
            "idempotency_key, discord_message_id, dispatched_at) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, lead_id, "warm", "B", 60, uuid.uuid4().hex, message_id, now),
        )


def _verdicts(store: Any) -> list[str]:
    return [r["verdict"] for r in store.conn.execute("SELECT verdict FROM feedback").fetchall()]


def test_a_raw_event_becomes_a_feedback_row(store: Any) -> None:
    _dispatched(store)

    handle_raw_reaction(store, _Event("msg-1", "✅"), removed=False)

    assert _verdicts(store) == ["good"]


def test_removing_a_reaction_retracts_it(store: Any) -> None:
    _dispatched(store)
    handle_raw_reaction(store, _Event("msg-1", "✅"), removed=False)

    handle_raw_reaction(store, _Event("msg-1", "✅"), removed=True)

    assert _verdicts(store) == []


def test_the_reactor_is_identified_by_id_not_display_name(store: Any) -> None:
    """Display names change and are not unique in a guild. The supersede rule is keyed
    on the actor, so two people sharing a nickname would overwrite each other."""
    _dispatched(store)

    handle_raw_reaction(store, _Event("msg-1", "✅", user_id=99), removed=False)

    actor = store.conn.execute("SELECT actor FROM feedback").fetchone()["actor"]
    assert actor == "99"


def test_a_custom_server_emoji_is_ignored(store: Any) -> None:
    """`str(PartialEmoji)` renders a custom emoji as `<:name:id>`, which matches no
    verdict. A server's own `:shipit:` means nothing to the precision figure."""
    _dispatched(store)

    handle_raw_reaction(store, _Event("msg-1", "<:shipit:12345>"), removed=False)

    assert _verdicts(store) == []


def test_a_malformed_event_does_not_kill_the_gateway_loop(store: Any) -> None:
    """discord.py has no supervisor for an exception raised inside a handler -- it logs
    and carries on, but a bug that raises on every event would produce a bot that looks
    connected and records nothing. Swallowing it here keeps the connection and gets the
    reason into our own log."""

    class _Broken:
        message_id = "msg-1"
        user_id = 1

        @property
        def emoji(self) -> Any:
            raise RuntimeError("gateway sent something unexpected")

    handle_raw_reaction(store, _Broken(), removed=False)  # type: ignore[arg-type]

    assert _verdicts(store) == []


# ------------------------------------------------------------- the ends must agree


def test_the_bot_heartbeats_well_inside_its_silence_budget() -> None:
    """A gateway client is silent by nature: a week with no reactions looks exactly like
    a week with a revoked token, and the heartbeat is the only thing that tells them
    apart. One that ticked slower than its own budget would alarm on a healthy bot."""
    assert HEARTBEAT_UNITS["feedback"] * 3600 / 2 > HEARTBEAT_SECONDS


def test_the_unit_stops_restarting_on_the_exit_code_the_cli_uses() -> None:
    """`Restart=always` plus a missing token is a 30-second traceback loop forever. The
    CLI exits 78 (EX_CONFIG) for "cannot possibly succeed"; if the unit stops honouring
    that, the reason scrolls out of the journal before anyone reads it."""
    from cindraleads.cli import EX_CONFIG

    assert f"RestartPreventExitStatus={EX_CONFIG}" in UNIT.read_text(encoding="utf-8")


def test_the_unit_runs_the_command_that_exists() -> None:
    assert "cindra feedback-bot" in UNIT.read_text(encoding="utf-8")
