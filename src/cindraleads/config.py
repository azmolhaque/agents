"""Settings and YAML config loading.

Secrets are :class:`SecretStr`, so an accidental ``repr`` or f-string prints
``**********`` rather than your Discord webhook. That is a second line of defence —
the first is the redaction processor in ``logging.py``, which is the one under test.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from cindraleads.errors import ConfigError

__all__ = ["Settings", "find_repo_root", "load_yaml", "settings"]


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up looking for ``pyproject.toml``.

    Editable installs keep ``db/`` and ``config/`` outside the package, so we resolve
    them relative to the repo rather than to ``__file__``'s package directory.
    """
    here = (start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError(f"could not locate repo root (no pyproject.toml above {here})")


class Settings(BaseSettings):
    """Runtime configuration. Every field is overridable by an env var of the same name."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "dev"
    log_level: str = "INFO"

    db_path: Path = Path("var/cindraleads.db")
    log_dir: Path = Path("var/log")
    cache_dir: Path = Path("var/cache")
    config_dir: Path = Path("config")
    migrations_dir: Path = Path("db/migrations")

    # SQLite page cache in KiB, negative means KiB rather than pages.
    # 64 MiB by default; the 1 GB ceiling in the hardware budget is a ceiling, not a target.
    sqlite_cache_kib: int = 65536
    sqlite_busy_timeout_ms: int = 5000

    # --- rationed resources ------------------------------------------------
    daily_cloud_usd_cap: float = 0.50
    serpapi_quota_safety_fraction: float = 0.85
    # PLAN.md 2.5, approved: 6 requests per domain per rolling 24h, >= 3s apart.
    fetch_budget_per_domain_24h: int = 6
    fetch_min_interval_seconds: float = 3.0

    # --- secrets -----------------------------------------------------------
    serpapi_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    discord_webhook_hot: SecretStr | None = None
    discord_webhook_warm: SecretStr | None = None
    discord_webhook_digest: SecretStr | None = None
    discord_webhook_ops: SecretStr | None = None
    discord_bot_token: SecretStr | None = None

    repo_root: Path = Field(default_factory=find_repo_root)

    def resolve(self, path: Path) -> Path:
        """Absolutize a configured path against the repo root."""
        return path if path.is_absolute() else (self.repo_root / path)

    @property
    def db_file(self) -> Path:
        return self.resolve(self.db_path)

    @property
    def migrations_path(self) -> Path:
        return self.resolve(self.migrations_dir)

    def ensure_dirs(self) -> None:
        for path in (self.db_file.parent, self.resolve(self.log_dir), self.resolve(self.cache_dir)):
            path.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide settings singleton. Cached so ``.env`` is read once."""
    return Settings()


def load_yaml(name: str, *, base: Path | None = None) -> dict[str, Any]:
    """Load ``config/<name>.yaml``.

    Behaviour lives in these files, not in code — adding a source means adding a row
    in ``sources.yaml``, not editing the pipeline.
    """
    cfg = settings()
    directory = base or cfg.resolve(cfg.config_dir)
    path = directory / (name if name.endswith((".yaml", ".yml")) else f"{name}.yaml")
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return loaded
