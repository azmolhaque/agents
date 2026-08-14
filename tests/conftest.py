from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point every test at a throwaway database and never at a real .env.

    Settings is an lru_cache singleton, so it has to be cleared between tests or the
    first test's tmp_path leaks into all the others.
    """
    from cindraleads.config import settings

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "log"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ENVIRONMENT", "test")
    for secret in (
        "SERPAPI_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "DISCORD_WEBHOOK_HOT",
        "DISCORD_WEBHOOK_WARM",
        "DISCORD_WEBHOOK_DIGEST",
        "DISCORD_WEBHOOK_OPS",
        "DISCORD_BOT_TOKEN",
    ):
        monkeypatch.delenv(secret, raising=False)
    monkeypatch.chdir(tmp_path)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    from cindraleads.store import Store

    s = Store(tmp_path / "test.db", migrations_dir=REPO_ROOT / "db" / "migrations")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def queue(store):  # type: ignore[no-untyped-def]
    from cindraleads.queue import JobQueue

    return JobQueue(store)


@pytest.fixture
def repo_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Environment for spawning `cindra` subprocesses against a temp database."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return env
