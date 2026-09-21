"""Settings and YAML config loading.

Secrets are :class:`SecretStr`, so an accidental ``repr`` or f-string prints
``**********`` rather than your Discord webhook. That is a second line of defence —
the first is the redaction processor in ``logging.py``, which is the one under test.
"""

from __future__ import annotations

import functools
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from cindraleads.errors import ConfigError

__all__ = [
    "MAX_STAGE_SECONDS",
    "Settings",
    "find_repo_root",
    "load_prompt",
    "load_yaml",
    "prompt_version",
    "settings",
]

# The longest a single stage may run before we stop believing in it. Generous against
# the measured worst case -- a 64 s p50 page, plus a thermal pause -- because the cost
# of cutting a slow job short is a lost lead and the cost of waiting is a slow lead.
#
# It lives here rather than in `cli.py` because it is not only the worker's business:
# a stage that wants to finish *before* the worker gives up on it has to know the
# number, and a second constant written down independently is the drift this project
# has now paid for three times (`WatchdogSec` against the lease, the prose bound
# against its budget, `MAX_STAGE_SECONDS` against `TimeoutStopSec`).
MAX_STAGE_SECONDS = 900.0

# The rationale header every prompt file carries. Stripped before the model sees it.
_COMMENT_BLOCK = re.compile(r"<!--.*?-->", re.DOTALL)


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
    prompt_dir: Path = Path("prompts")
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
    # Decode runs at ~3.7 tok/s on the Pi, so a bounded extraction takes ~64 s at
    # p50 and ~77 s at p95 (docs/BENCHMARKS.md). 180 s leaves room for a cold
    # model load (~32 s off microSD) on top of a p95 page.
    ollama_timeout_seconds: float = 180.0

    # --- prose ---------------------------------------------------------------
    # Whether a `bengali_angle` may reach a card. **Off, and the default is the
    # decision**, not caution pending a tidy-up.
    #
    # Read as a Bengali speaker rather than as JSON, what a 4B writes is
    # machine-translation garbage: `মুক্ত` (*liberated*) where `বিনামূল্যে` (*free of
    # charge*) belongs, "RoE" transliterated to `রো ই`, "AI" spelled out letter by
    # letter as `এই আই`. That is the precise opposite of the local-trust wedge
    # `T12_LOCAL` and Taka pricing exist to build, and it is a capability limit rather
    # than a bug -- `PROSE_MAX_TOKENS_BENGALI` is about length and nothing in this
    # project has ever checked the Bengali for quality.
    #
    # CLAUDE.md has said "do not send a Bengali card until a native reader has approved
    # the wording" since the first batch was read, and nothing implemented it: five of
    # five cards previewed on 2026-09-21 carried one. A rule in a document is a
    # preference; a rule in the code is a rule.
    #
    # A flag rather than deleting the field, because the honest fix is a human-written
    # template with slots and this is what turns it back on in one line when that
    # exists.
    dispatch_bengali_angle: bool = False

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


def load_prompt(name: str, *, base: Path | None = None) -> str:
    """Load ``prompts/<name>.md``, stripped of its HTML rationale comment.

    Prompts live in files rather than in code so that changing one is a reviewable
    diff with golden fixtures attached, not a string edit buried in a stage.

    The leading ``<!-- ... -->`` block is documentation for whoever edits the prompt
    and would otherwise be tokens the model pays for on every single page. At 3.7
    tok/s of decode and ~64 s per page, prompt bloat is not free.
    """
    cfg = settings()
    directory = base or cfg.resolve(cfg.prompt_dir)
    path = directory / (name if name.endswith(".md") else f"{name}.md")
    if not path.is_file():
        raise ConfigError(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8")
    return _COMMENT_BLOCK.sub("", text).strip()


def prompt_version(base: Path | None = None) -> str:
    """A hash over every prompt file.

    PLAN.md 2.10: ``Lead.pipeline_version`` does not change when a prompt is edited,
    so a prompt tweak would silently invalidate the golden fixtures with nothing in
    the data to show it. This hash is stored alongside extractions instead, which
    makes fixture invalidation automatic and a regression traceable to the edit.
    """
    cfg = settings()
    directory = base or cfg.resolve(cfg.prompt_dir)
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.md")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
