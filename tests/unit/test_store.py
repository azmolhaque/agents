"""Store, pragmas and migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cindraleads.errors import MigrationError
from cindraleads.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "db" / "migrations"


def test_migrate_creates_every_table_the_data_model_needs(store):
    tables = set(store.table_names())
    expected = {
        "raw_documents",
        "evidence",
        "companies",
        "contacts",
        "triggers",
        "trigger_evidence",
        "candidates",
        "leads",
        "jobs",
        "dead_letter",
        "dispatch_log",
        "feedback",
        "suppression_list",
        "quarantine",
        "api_budget",
        "metrics",
        "companies_fts",
        "company_vectors",
    }
    assert expected <= tables, f"missing: {expected - tables}"


def test_migrate_is_idempotent(store):
    assert store.migrate() == [], "a second run applies nothing"
    applied = store.applied_migrations()
    # Every file on disk, in order, and nothing else. Asserting the relationship
    # rather than a hardcoded list, so adding a migration does not break this.
    assert applied == [p.stem for p in store.available_migrations()]
    assert applied == sorted(applied)
    assert applied[0] == "0001_init"


def test_wal_and_foreign_keys_are_on(store):
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_dispatch_log_carries_the_discord_message_id(store):
    """PLAN.md 2.1: without this column the Phase 8 Critic cannot join a reaction
    back to a lead, and webhooks cannot supply it retroactively."""
    columns = {
        row["name"] for row in store.conn.execute("PRAGMA table_info(dispatch_log)").fetchall()
    }
    assert "discord_message_id" in columns


def test_company_vectors_exists_even_though_rung_3_ships_disabled(store):
    """PLAN.md 2.3: the table is present from day one so enabling the vector rung
    is a config flip, never a migration."""
    assert "company_vectors" in store.table_names()


def test_fts5_index_is_queryable(store):
    store.conn.execute(
        "INSERT INTO companies_fts (canonical_domain, display_name, legal_name, description) "
        "VALUES ('acme.io', 'Acme Health', 'Acme Ltd', 'patient-facing AI assistant')"
    )
    rows = store.conn.execute(
        "SELECT canonical_domain FROM companies_fts WHERE companies_fts MATCH 'assistant'"
    ).fetchall()
    assert [r["canonical_domain"] for r in rows] == ["acme.io"]


def test_jobs_dedupe_index_is_partial(store):
    """Many jobs legitimately have no dedupe_key; a plain UNIQUE index would let
    only one of them exist."""
    store.conn.execute(
        "INSERT INTO jobs (job_id, kind, payload, available_at, created_at, updated_at) "
        "VALUES ('a','k','{}','t','t','t')"
    )
    store.conn.execute(
        "INSERT INTO jobs (job_id, kind, payload, available_at, created_at, updated_at) "
        "VALUES ('b','k','{}','t','t','t')"
    )
    assert store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_transaction_rolls_back_on_exception(store):
    store.conn.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(ValueError), store.tx() as conn:
        conn.execute("INSERT INTO t VALUES (1)")
        raise ValueError("nope")
    assert store.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_bad_migration_filename_is_rejected(tmp_path: Path):
    (tmp_path / "nope.sql").write_text("SELECT 1;")
    s = Store(tmp_path / "x.db", migrations_dir=tmp_path)
    with pytest.raises(MigrationError, match="NNNN_lower_snake"):
        s.migrate()
    s.close()


def test_migration_managing_its_own_transaction_is_rejected(tmp_path: Path):
    """The runner wraps each migration so the schema_migrations row commits with the
    DDL. A migration that COMMITs internally would break that atomicity."""
    (tmp_path / "0001_bad.sql").write_text("BEGIN; CREATE TABLE t (x); COMMIT;")
    s = Store(tmp_path / "x.db", migrations_dir=tmp_path)
    with pytest.raises(MigrationError, match="must not manage its own transaction"):
        s.migrate()
    s.close()


def test_failed_migration_leaves_nothing_behind(tmp_path: Path):
    (tmp_path / "0001_broken.sql").write_text(
        "CREATE TABLE good (x INTEGER);\nCREATE TABLE bad (;\n"
    )
    s = Store(tmp_path / "x.db", migrations_dir=tmp_path)
    with pytest.raises(MigrationError):
        s.migrate()
    assert "good" not in s.table_names()
    assert s.applied_migrations() == []
    s.close()


def test_backup_does_not_leak_a_connection(store, tmp_path: Path, monkeypatch):
    """`with sqlite3.connect(...)` commits but does NOT close.

    That leak is silent on Python 3.11 and raises ResourceWarning on 3.13, so this
    asserts the property directly rather than relying on the interpreter to notice.
    It matters because the Phase 7 maintenance timer backs up nightly inside a
    process that stays up for weeks.
    """
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    store.backup_to(tmp_path / "b1.db")
    store.backup_to(tmp_path / "b2.db")

    assert len(opened) == 2, "expected one target connection per backup"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_backup_produces_a_readable_copy(store, tmp_path: Path):
    store.conn.execute(
        "INSERT INTO suppression_list (entry_id, kind, value, created_at) "
        "VALUES ('e1','domain','x.io','t')"
    )
    target = tmp_path / "backup" / "copy.db"
    store.backup_to(target)
    restored = Store(target, migrations_dir=MIGRATIONS)
    assert restored.conn.execute("SELECT value FROM suppression_list").fetchone()[0] == "x.io"
    restored.close()


def test_pending_migrations_reports_the_drift(tmp_path: Path):
    """A `git pull` that ships a migration must be detectable before a query fails.

    Without this the symptom is "no such column" from whichever stage touches the new
    field first, which is a long way from the cause.
    """
    store = Store(tmp_path / "m.db", migrations_dir=MIGRATIONS)
    assert store.pending_migrations(), "a fresh database has not applied anything"
    store.migrate()
    assert store.pending_migrations() == []
    store.close()


def test_the_unattended_entry_points_migrate_themselves(tmp_path: Path, monkeypatch):
    """A systemd timer has nobody to read an error message.

    `pipeline` and `work` bring the schema up to date on open; a schema shipped by a
    pull would otherwise leave the pipeline dead until a human noticed.
    """
    import inspect

    from cindraleads import cli

    for name in ("pipeline", "work"):
        source = inspect.getsource(getattr(cli, name))
        assert "_open_store(migrate=True)" in source, f"`cindra {name}` does not self-migrate"


def test_read_only_commands_do_not_migrate_silently():
    """`status` reports drift instead of applying it. Someone asking for a summary is
    not asking for a schema change."""
    import inspect

    from cindraleads import cli

    source = inspect.getsource(cli.status)
    assert "_open_store(migrate=True)" not in source
    assert "pending_migrations" in source
