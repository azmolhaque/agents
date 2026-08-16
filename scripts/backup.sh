#!/usr/bin/env bash
#
# Consistent SQLite backup with rotation. Run from the maintenance timer or by hand.
#
#   ./scripts/backup.sh [destination_dir] [keep_count]
#
# Never `cp`. Copying a WAL-mode database while the worker is running gives you a file
# that opens fine and is missing the last transactions -- the failure is silent, and
# you find out during the restore you needed it for. The online backup API takes a
# consistent snapshot against a live writer, which is the entire reason it exists.
#
# Driven through the venv's Python rather than the `sqlite3` CLI: same backup API, one
# fewer thing that has to be installed for the nightly job to work.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-${REPO_ROOT}/var/backups}"
KEEP="${2:-7}"
DB="${DB_PATH:-${REPO_ROOT}/var/cindraleads.db}"
PY="${REPO_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }
mkdir -p "$DEST"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST}/cindraleads-${STAMP}.db"

# Back up, then verify, then report. The verify runs before the rotation below deletes
# the previous snapshot -- an unchecked backup is a hope, and rotating on the strength
# of one is how you end up with seven copies of a corrupt file.
"$PY" - "$DB" "$OUT" <<'PY'
import sqlite3, sys

source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()

check = sqlite3.connect(target)
try:
    verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    if verdict != "ok":
        sys.exit(f"integrity check FAILED: {verdict}")
    rows = check.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
finally:
    check.close()
print(f"{rows} companies")
PY

gzip -f "$OUT"
echo "wrote ${OUT}.gz"

# Rotate oldest-first, and only ever inside DEST.
mapfile -t OLD < <(ls -1t "${DEST}"/cindraleads-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))")
for path in "${OLD[@]:-}"; do
  [ -n "$path" ] || continue
  rm -f -- "$path"
  echo "rotated out $(basename "$path")"
done
