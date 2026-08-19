#!/usr/bin/env bash
#
# Restore drill (PLAN.md Phase 8): wipe, restore, and prove the pipeline resumes with
# no duplicate dispatches.
#
#   ./scripts/restore_drill.sh [backup.db.gz]
#
# **This never touches your real database.** It restores into a scratch directory, runs
# the checks there, and deletes it. A drill that could destroy the thing it is
# rehearsing for is not a drill.
#
# The question is not "does the file open" -- `backup.sh` already runs an integrity
# check, and a corrupt file fails there. It is: **after a restore, does the system do
# anything twice?** The dispatcher's idempotency lives in `dispatch_log`, which is
# inside the database. Restore a snapshot taken before this morning's cards went out
# and those rows are gone; nothing in Discord is gone, and the next run re-sends every
# one of them. That is the failure mode this rehearses, and the only way to know the
# gap is to measure it against what the channel already has.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${REPO_ROOT}/var/backups"
PY="${REPO_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

SNAPSHOT="${1:-}"
if [ -z "$SNAPSHOT" ]; then
  SNAPSHOT="$(ls -1t "${BACKUP_DIR}"/cindraleads-*.db.gz 2>/dev/null | head -1 || true)"
fi
[ -n "$SNAPSHOT" ] || { echo "no backup found in ${BACKUP_DIR}; run scripts/backup.sh first" >&2; exit 1; }
[ -f "$SNAPSHOT" ] || { echo "no such backup: $SNAPSHOT" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
RESTORED="${WORK}/restored.db"

echo "== restoring $(basename "$SNAPSHOT")"
gunzip -c "$SNAPSHOT" > "$RESTORED"

echo "== integrity"
"$PY" - "$RESTORED" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
if verdict != "ok":
    sys.exit(f"FAILED: {verdict}")
print("   ok")
PY

echo "== schema is current"
# A snapshot older than the newest migration restores into a database the running code
# cannot query. Migrating the *restored copy* is part of the drill, not a workaround:
# if it fails here it would have failed at 3am with the real one.
DB_PATH="$RESTORED" "${REPO_ROOT}/.venv/bin/cindra" db migrate

echo "== what a restore would cost"
"$PY" - "$RESTORED" "${DB_PATH:-${REPO_ROOT}/var/cindraleads.db}" <<'PY'
import sqlite3, sys

restored, live = sys.argv[1], sys.argv[2]


def counts(path: str) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("companies", "leads", "triggers", "dispatch_log", "feedback")
        }
    finally:
        conn.close()


old = counts(restored)
try:
    new = counts(live)
except sqlite3.OperationalError:
    new = {}

for table, n in old.items():
    current = new.get(table)
    if current is None:
        print(f"   {table:<14} {n}")
        continue
    gap = current - n
    note = ""
    if table == "dispatch_log" and gap > 0:
        # The whole point of the drill. Those cards are in Discord and their
        # idempotency rows are not in the snapshot, so a restore re-sends them.
        note = f"  <-- {gap} card(s) would be re-sent; suppress or re-dispatch by hand"
    print(f"   {table:<14} {n} restored / {current} live ({gap:+d}){note}")
PY

echo
echo "Drill complete. Nothing was written outside ${WORK}, which is now gone."
echo "To restore for real:"
echo "  sudo systemctl stop cindraleads-worker cindraleads-feedback"
echo "  gunzip -c ${SNAPSHOT} > var/cindraleads.db"
echo "  cindra db migrate && cindra status"
echo "  # then check the dispatch_log gap above against your Discord channels"
echo "  sudo systemctl start cindraleads-worker cindraleads-feedback"
