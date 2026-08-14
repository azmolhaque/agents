"""Secrets must never reach a log file.

Phase 0 acceptance: a webhook URL and an API key never appear in *.jsonl output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog

from cindraleads.logging import REDACTED, configure_logging, get_logger, scrub_text

WEBHOOK = "https://discord.com/api/webhooks/1234567890/AbCdEf-GhIjKlMnOpQrStUvWxYz_0123456789"
ANTHROPIC_KEY = "sk-ant-api03-AAAAbbbbCCCCddddEEEEffffGGGG"
GITHUB_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@pytest.fixture(autouse=True)
def _reset_structlog():
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_scrub_text_catches_each_secret_shape():
    for secret in (WEBHOOK, ANTHROPIC_KEY, GITHUB_TOKEN, "Bearer abcdef0123456789xyz"):
        assert secret not in scrub_text(f"leaking {secret} here")


def test_webhook_and_api_key_never_reach_the_log_file(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO", console=False, filename="t.jsonl")
    log = get_logger("test")

    # Every route a secret could plausibly take into a log line.
    log.info("dispatching", webhook_url=WEBHOOK, lead_id="9f2ac41")
    log.info(f"posting to {WEBHOOK}")
    log.info("escalating", anthropic_api_key=ANTHROPIC_KEY)
    log.info("cloning", detail={"nested": {"github_token": GITHUB_TOKEN}})
    log.info("headers", items=["Authorization: Bearer abcdef0123456789xyz"])

    raw = (tmp_path / "t.jsonl").read_text()
    for secret in (WEBHOOK, ANTHROPIC_KEY, GITHUB_TOKEN):
        assert secret not in raw, f"{secret[:20]}... leaked into the log"
    assert REDACTED in raw
    # The non-secret context has to survive, or the logs are useless.
    assert "9f2ac41" in raw


def test_debugging_keys_are_not_over_redacted(tmp_path: Path):
    """`dedupe_key` and `idempotency_key` are debugging gold. A naive 'contains key'
    rule would eat them, so the matcher is an explicit set plus suffixes."""
    configure_logging(log_dir=tmp_path, level="INFO", console=False, filename="t.jsonl")
    log = get_logger("test")
    log.info(
        "enqueued",
        dedupe_key="serpapi:google:abc123",
        idempotency_key="lead42:T1_AI_SHIP:8",
        content_sha256="a" * 64,
    )
    entry = _read_lines(tmp_path / "t.jsonl")[0]
    assert entry["dedupe_key"] == "serpapi:google:abc123"
    assert entry["idempotency_key"] == "lead42:T1_AI_SHIP:8"
    assert entry["content_sha256"] == "a" * 64


def test_log_lines_are_valid_json_with_level_and_timestamp(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO", console=False, filename="t.jsonl")
    get_logger("test").info("hello", stage="extractor", job_id="j1", duration_ms=12)
    entry = _read_lines(tmp_path / "t.jsonl")[0]
    assert entry["event"] == "hello"
    assert entry["level"] == "info"
    assert entry["stage"] == "extractor"
    assert entry["job_id"] == "j1"
    assert "timestamp" in entry
