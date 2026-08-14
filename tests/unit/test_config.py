"""Settings loading and secret handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from cindraleads.config import Settings, find_repo_root, load_yaml, settings
from cindraleads.errors import ConfigError


def test_repo_root_is_the_directory_with_pyproject():
    assert (find_repo_root() / "pyproject.toml").is_file()


def test_defaults_reflect_the_approved_fetch_budget():
    """PLAN.md 2.5, approved: 6 requests per domain per 24h, >= 3s apart."""
    cfg = Settings()
    assert cfg.fetch_budget_per_domain_24h == 6
    assert cfg.fetch_min_interval_seconds == 3.0


def test_daily_cloud_cap_matches_the_budget_guard():
    assert Settings().daily_cloud_usd_cap == 0.50


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("FETCH_BUDGET_PER_DOMAIN_24H", "2")
    settings.cache_clear()
    cfg = Settings()
    assert cfg.log_level == "DEBUG"
    assert cfg.fetch_budget_per_domain_24h == 2


def test_secrets_do_not_render_in_repr(monkeypatch: pytest.MonkeyPatch):
    """Second line of defence behind the log redactor: even a stray f-string or a
    traceback that prints Settings must not spill a webhook."""
    webhook = "https://discord.com/api/webhooks/123/SECRETVALUE12345"
    monkeypatch.setenv("DISCORD_WEBHOOK_HOT", webhook)
    settings.cache_clear()
    cfg = Settings()
    assert "SECRETVALUE12345" not in repr(cfg)
    assert "SECRETVALUE12345" not in str(cfg.discord_webhook_hot)
    assert cfg.discord_webhook_hot is not None
    assert cfg.discord_webhook_hot.get_secret_value() == webhook


def test_relative_paths_resolve_against_the_repo_root():
    cfg = Settings()
    assert cfg.resolve(Path("db/migrations")).is_absolute()
    assert cfg.resolve(Path("/tmp/abs")) == Path("/tmp/abs")


def test_load_yaml_reads_a_mapping(tmp_path: Path):
    (tmp_path / "icp.yaml").write_text("primary:\n  - seed saas\nweights:\n  T1_AI_SHIP: 30\n")
    loaded = load_yaml("icp", base=tmp_path)
    assert loaded["weights"]["T1_AI_SHIP"] == 30


def test_load_yaml_rejects_a_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_yaml("nope", base=tmp_path)


def test_load_yaml_rejects_a_non_mapping(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_yaml("bad", base=tmp_path)


def test_load_yaml_rejects_malformed_yaml(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("key: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_yaml("bad", base=tmp_path)


def test_empty_yaml_is_an_empty_mapping(tmp_path: Path):
    (tmp_path / "empty.yaml").write_text("")
    assert load_yaml("empty", base=tmp_path) == {}
