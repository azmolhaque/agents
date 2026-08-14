"""structlog setup with secret redaction.

Absolute imports mean ``import logging`` inside this package still resolves to the
stdlib, so the module name is safe.

The redaction processor is not decoration. Log files get copied into issues, pasted
into chat, and shipped to a Discord ops channel; a webhook URL in a log line is a
leaked credential. Two mechanisms, both tested in ``tests/unit/test_redaction.py``:

1. **Key-name matching** — a value whose key looks secret is replaced wholesale.
   Matching is on an explicit set plus a few suffixes, deliberately *not* on a bare
   ``key`` substring, because ``dedupe_key`` and ``idempotency_key`` are debugging
   gold and must survive.
2. **Pattern matching** — secrets pasted into a free-text message get scrubbed by
   regex regardless of which key carried them.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

__all__ = ["REDACTED", "configure_logging", "get_logger", "redact_processor", "scrub_text"]

REDACTED = "***REDACTED***"

_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "anthropic_api_key",
        "api_key",
        "apikey",
        "bot_token",
        "discord_bot_token",
        "discord_webhook",
        "discord_webhook_digest",
        "discord_webhook_hot",
        "discord_webhook_ops",
        "discord_webhook_warm",
        "github_token",
        "password",
        "passwd",
        "secret",
        "serpapi_key",
        "token",
        "webhook",
        "webhook_url",
    }
)

_SECRET_SUFFIXES: tuple[str, ...] = ("_token", "_secret", "_password", "_webhook", "_api_key")

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Discord webhooks - the highest-value secret in this system.
    re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\S+"),
    # Anthropic keys.
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    # Generic bearer tokens in a header dump.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
    # GitHub tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    # SerpAPI keys are 64 lowercase hex; anchor on the query parameter so we do not
    # eat legitimate sha256 content hashes, which we very much want to keep logging.
    re.compile(r"(?i)\b(api_key|serp_api_key)=[A-Za-z0-9]{16,}"),
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SECRET_KEYS or lowered.endswith(_SECRET_SUFFIXES)


def scrub_text(value: str) -> str:
    """Replace any known secret shape appearing anywhere in a string."""
    for pattern in _PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _scrub(value: Any, key: str | None = None) -> Any:
    if key is not None and _is_secret_key(key):
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub(v) for v in value]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor. Must run last, so it also catches what earlier
    processors added."""
    return {k: _scrub(v, str(k)) for k, v in event_dict.items()}


def configure_logging(
    *,
    log_dir: Path | None = None,
    level: str = "INFO",
    console: bool = True,
    filename: str = "cindraleads.jsonl",
) -> None:
    """Configure structlog to emit JSON lines, redacted.

    Every line carries whatever is bound into contextvars — the pipeline binds
    ``job_id``, ``lead_id``, ``stage``, ``duration_ms`` and ``cost_units``.
    """
    handlers: list[logging.Handler] = []
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / filename, encoding="utf-8"))
    if console or not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        # Close as well as detach: reconfiguring in a long-lived process (or across
        # tests) otherwise leaks an open file descriptor per call.
        if isinstance(existing, logging.FileHandler):
            existing.close()
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Redaction runs immediately before rendering so nothing added upstream
            # can sneak a secret past it.
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "cindraleads") -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
