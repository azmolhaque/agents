"""Posting to a Discord webhook.

Small on purpose. The interesting parts are the two things that go wrong in production:

* **429s.** Discord tells you exactly how long to wait in `retry_after`. Guessing
  instead is how you get rate-limited harder, so the value is obeyed, and the retry
  budget is finite so a persistently angry endpoint dead-letters rather than spinning.
* **`?wait=true`.** Without it the POST returns 204 and no body, and the message id is
  gone forever. Phase 8's Critic joins Discord reactions back to `lead_id` through
  `dispatch_log.discord_message_id`, so the id has to be captured at send time or the
  feedback loop has no key to join on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from cindraleads.logging import get_logger

__all__ = ["DiscordWebhook", "WebhookResult"]

log = get_logger("cindraleads.discord")


@dataclass(frozen=True)
class WebhookResult:
    ok: bool
    message_id: str | None = None
    status: int | None = None
    error: str | None = None


@dataclass
class DiscordWebhook:
    client: httpx.AsyncClient
    max_attempts: int = 4

    async def post(
        self, url: str, payload: dict[str, Any], *, timeout: float = 15.0
    ) -> WebhookResult:
        """POST one message. Returns the created message id on success."""
        last: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.client.post(
                    url, json=payload, params={"wait": "true"}, timeout=timeout
                )
            except (httpx.HTTPError, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(min(2**attempt, 30))
                continue

            if response.status_code == 429:
                # Obey the server. `retry_after` is seconds, and it means it.
                retry_after = _retry_after(response)
                log.warning("discord_rate_limited", retry_after=retry_after, attempt=attempt)
                await asyncio.sleep(retry_after)
                last = "429 rate limited"
                continue

            if 500 <= response.status_code < 600:
                last = f"HTTP {response.status_code}"
                await asyncio.sleep(min(2**attempt, 30))
                continue

            if response.status_code >= 400:
                # A 400 means the embed is malformed. Retrying sends the same bytes and
                # gets the same answer, so it goes straight to the caller.
                body = response.text[:300]
                log.error("discord_rejected", status=response.status_code, body=body)
                return WebhookResult(
                    ok=False,
                    status=response.status_code,
                    error=f"HTTP {response.status_code}: {body}",
                )

            message_id = None
            try:
                message_id = str((response.json() or {}).get("id") or "") or None
            except ValueError:
                # 204, or a body that is not JSON. The post succeeded; we simply have
                # no id to join reactions on later.
                log.info("discord_no_message_id", status=response.status_code)
            return WebhookResult(ok=True, message_id=message_id, status=response.status_code)

        return WebhookResult(ok=False, error=last or "exhausted retries")


def _retry_after(response: httpx.Response) -> float:
    try:
        body = response.json()
        if isinstance(body, dict) and "retry_after" in body:
            return max(0.0, float(body["retry_after"]))
    except (ValueError, TypeError):
        pass
    header = response.headers.get("Retry-After")
    try:
        return max(0.0, float(header)) if header else 1.0
    except ValueError:
        return 1.0
