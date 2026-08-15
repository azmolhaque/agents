"""SQLite access and migrations.

One file, WAL mode, no broker. Power loss on a Pi is a *when*, not an *if*, so the
unit of durability is the SQLite transaction and nothing else.

Transactions are explicit. The connection runs with ``isolation_level=None`` so
Python's sqlite3 module never opens or commits a transaction behind our back — the
exactly-once guarantee depends on the caller controlling exactly where COMMIT lands.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

from cindraleads.config import Settings, settings
from cindraleads.errors import MigrationError, StoreError
from cindraleads.models import to_iso, utcnow

__all__ = ["Store"]

_MIGRATION_NAME = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


class Store:
    """Owns one SQLite connection and the migration runner."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        config: Settings | None = None,
        migrations_dir: Path | None = None,
    ) -> None:
        self._config = config or settings()
        self.db_path = Path(db_path) if db_path is not None else self._config.db_file
        self._migrations_dir = migrations_dir or self._config.migrations_path
        self._conn: sqlite3.Connection | None = None

    # -------------------------------------------------------------- lifecycle

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def _open(self) -> sqlite3.Connection:
        if self.db_path.parent and str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # explicit transactions only
            timeout=self._config.sqlite_busy_timeout_ms / 1000,
        )
        conn.row_factory = sqlite3.Row
        cfg = self._config
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={cfg.sqlite_busy_timeout_ms}")
        conn.execute(f"PRAGMA cache_size=-{cfg.sqlite_cache_kib}")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ----------------------------------------------------------- transactions

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction.

        ``BEGIN IMMEDIATE`` takes the write lock up front, so two workers racing to
        claim the same job serialize here instead of one of them failing later with
        SQLITE_BUSY at COMMIT time.

        Everything a stage does — its side effects *and* the queue completion — must
        happen inside one of these. If the process is killed mid-block, SQLite rolls
        the whole thing back and the job's lease simply expires.
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            # BaseException, not Exception: a SIGTERM-driven KeyboardInterrupt or a
            # cancelled asyncio task must roll back too, not leave a lock held.
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read-only scope. WAL means this never blocks a writer."""
        yield self.conn

    # ------------------------------------------------------------- migrations

    def _ensure_migration_table(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )

    def applied_migrations(self) -> list[str]:
        self._ensure_migration_table()
        rows = self.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [str(row["version"]) for row in rows]

    def available_migrations(self) -> list[Path]:
        directory = self._migrations_dir
        if not directory.is_dir():
            raise MigrationError(f"migrations directory not found: {directory}")
        files = sorted(p for p in directory.glob("*.sql") if p.is_file())
        for path in files:
            if not _MIGRATION_NAME.match(path.name):
                raise MigrationError(f"migration {path.name!r} must match NNNN_lower_snake.sql")
        return files

    def migrate(self) -> list[str]:
        """Apply pending migrations. Returns the versions applied this call.

        Each migration is wrapped in a single transaction *including* its
        ``schema_migrations`` row — SQLite DDL is transactional, so a crash halfway
        through leaves the database exactly as it was, never half-migrated.
        """
        self._ensure_migration_table()
        already = set(self.applied_migrations())
        applied: list[str] = []
        for path in self.available_migrations():
            version = path.stem
            if version in already:
                continue
            body = path.read_text(encoding="utf-8")
            if "COMMIT" in body.upper():
                raise MigrationError(
                    f"{path.name} must not manage its own transaction; the runner does that"
                )
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{body}\n"
                "INSERT INTO schema_migrations (version, applied_at) "
                f"VALUES ('{version}', '{to_iso(utcnow())}');\n"
                "COMMIT;"
            )
            try:
                self.conn.executescript(script)
            except sqlite3.Error as exc:
                # executescript aborts at the failing statement; the surrounding
                # BEGIN means nothing from this migration survives. The rollback may
                # itself be a no-op if SQLite already unwound the transaction.
                with contextlib.suppress(sqlite3.Error):
                    self.conn.execute("ROLLBACK")
                raise MigrationError(f"{path.name} failed to apply: {exc}") from exc
            applied.append(version)
        return applied

    # ------------------------------------------------------------- utilities

    def table_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def dump_schema(self) -> str:
        rows = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        ).fetchall()
        return "\n".join(f"{row['sql']};" for row in rows) + "\n"

    def backup_to(self, destination: Path) -> None:
        """Online backup. Safe to run against a live WAL database.

        Note the explicit ``close()``. ``with sqlite3.connect(...)`` commits or rolls
        back the transaction but does **not** close the connection — a stdlib gotcha
        that leaks a file descriptor per call. That is invisible on Python 3.11 and
        raises ResourceWarning on 3.13, and on the Pi this runs nightly from the
        maintenance timer inside a process that stays up for weeks.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        target: sqlite3.Connection | None = None
        try:
            target = sqlite3.connect(destination)
            with target:
                self.conn.backup(target)
        except sqlite3.Error as exc:
            raise StoreError(f"backup to {destination} failed: {exc}") from exc
        finally:
            if target is not None:
                target.close()
