#!/usr/bin/env bash
#
# Move CindraLeads off microSD onto NVMe.
#
#   ./scripts/move_to_nvme.sh --check              # what would happen, touching nothing
#   ./scripts/move_to_nvme.sh --target /mnt/nvme   # do it
#
# **Nothing is deleted.** The microSD copy is left exactly where it is and the script
# tells you how to remove it once the new one has run for a day. A move that deletes
# its own fallback is not a move, it is a bet.
#
# Why this exists: SQLite WAL on a microSD is the documented corruption path, and this
# box has already proved the mechanism is live -- two unclean shutdowns put 13k NUL
# bytes in the JSONL log. `PRAGMA integrity_check` on the database still says `ok`,
# which is luck. The log is the cheap casualty; the database is the expensive one.
#
# The order matters and is the whole design:
#
#   1. Refuse early. No NVMe, no mount, no fstab entry -> stop before touching anything.
#   2. Stop every unit FIRST. Copying a live WAL database gives you a file that opens
#      fine and is missing the last transactions, silently.
#   3. Checkpoint and verify at the source, so a corrupt database is caught here rather
#      than faithfully copied and discovered next week.
#   4. Copy, then verify at the destination by comparing row counts, not by trusting rsync.
#   5. Rebuild the venv rather than copying it. A virtualenv bakes absolute paths into
#      its shebangs and pyvenv.cfg; a copied one runs the old interpreter from the old
#      path, which is a worse failure than an obvious one because it works.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET=""
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --target) TARGET="${2:-}"; shift ;;
    -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '    \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ 1. refuse early

say "Hardware"
if ! lsblk -dno NAME | grep -q '^nvme'; then
  echo
  die "no NVMe device found (lsblk shows no nvme*). Nothing to move to.
       This Pi's CLAUDE.md records 'no NVMe present' -- if you have just fitted one,
       check it is seated and that /boot/firmware/config.txt has:
           dtparam=pciex1_gen=3
       (./deploy/install_pi.sh --tune-boot writes that for you), then reboot."
fi
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT | grep -E 'nvme|NAME' || true

CURRENT_DEV="$(findmnt -no SOURCE --target "$REPO_ROOT")"
ok "repo currently on ${CURRENT_DEV}"
case "$CURRENT_DEV" in
  /dev/nvme*) ok "already on NVMe -- nothing to do"; exit 0 ;;
esac

if [ -z "$TARGET" ]; then
  echo
  warn "no --target given. Pick the NVMe mountpoint from the table above, e.g."
  echo "        ./scripts/move_to_nvme.sh --check --target /mnt/nvme"
  [ "$CHECK_ONLY" = "1" ] || exit 2
  exit 0
fi

say "Target"
[ -d "$TARGET" ] || die "$TARGET does not exist. Mount the NVMe there first."
TARGET_DEV="$(findmnt -no SOURCE --target "$TARGET")"
case "$TARGET_DEV" in
  /dev/nvme*) ok "$TARGET is on ${TARGET_DEV}" ;;
  *) die "$TARGET is on ${TARGET_DEV}, which is not an NVMe device.
       Moving there would achieve nothing." ;;
esac
[ -w "$TARGET" ] || die "$TARGET is not writable by $(whoami)"

# A move that does not survive a reboot is a move that fails at the worst moment: the
# units come up, find no database at the configured path, and create an empty one.
if ! findmnt -no SOURCE --fstab --target "$TARGET" >/dev/null 2>&1; then
  warn "$TARGET has no /etc/fstab entry -- it will NOT be mounted after a reboot."
  warn "add one before starting the 72 h run, e.g.:"
  echo "        echo \"UUID=\$(blkid -s UUID -o value ${TARGET_DEV})  ${TARGET}  ext4  defaults,noatime  0  2\" | sudo tee -a /etc/fstab"
fi

DEST="${TARGET}/$(basename "$REPO_ROOT")"
[ -e "$DEST" ] && die "$DEST already exists. Remove it or choose another target."

DB="${REPO_ROOT}/var/cindraleads.db"
NEED_KB="$(du -sk --exclude=.venv "$REPO_ROOT" | cut -f1)"
FREE_KB="$(df -Pk "$TARGET" | awk 'NR==2 {print $4}')"
ok "need ~$((NEED_KB / 1024)) MB (excluding .venv), free $((FREE_KB / 1024)) MB"
[ "$FREE_KB" -gt $((NEED_KB * 2)) ] || die "not enough free space on $TARGET (want 2x for headroom)"

UNITS="cindraleads-worker cindraleads-health cindraleads-feedback"
TIMERS="cindraleads-harvest.timer cindraleads-reconcile.timer cindraleads-digest.timer cindraleads-maintenance.timer"

if [ "$CHECK_ONLY" = "1" ]; then
  say "Dry run — nothing was changed"
  echo "    would stop:    $UNITS $TIMERS"
  echo "    would copy:    $REPO_ROOT  ->  $DEST   (excluding .venv, rebuilt at the destination)"
  echo "    would keep:    $REPO_ROOT  untouched, as the fallback"
  echo "    you then run:  cd $DEST && make install && ./deploy/install_pi.sh --install-units"
  exit 0
fi

# --------------------------------------------------------- 2. stop everything first

say "Stopping units"
# shellcheck disable=SC2086
sudo systemctl stop $TIMERS $UNITS 2>/dev/null || true
# The worker finishes its current job on SIGTERM, and an LLM stage can take minutes.
# Copying while it is still committing is the exact failure this whole script is about.
for _ in $(seq 1 60); do
  systemctl is-active --quiet cindraleads-worker || break
  sleep 2
done
systemctl is-active --quiet cindraleads-worker && die "worker did not stop; refusing to copy a live database"
ok "all units stopped"

# ------------------------------------------------- 3. checkpoint and verify at source

say "Source database"
if [ -f "$DB" ]; then
  PY="${REPO_ROOT}/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
  SRC_COUNTS="$("$PY" - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
# Fold the WAL back into the main file so the copy is one self-consistent artifact
# rather than three files that must travel together and arrive in the right order.
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
if verdict != "ok":
    sys.exit(f"integrity_check FAILED before the move: {verdict}")
print(",".join(
    f"{t}={conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}"
    for t in ("companies", "leads", "triggers", "evidence", "dispatch_log", "jobs")
))
PY
)"
  ok "checkpointed and verified: ${SRC_COUNTS}"
else
  warn "no database at $DB (nothing to verify)"
  SRC_COUNTS=""
fi

# ------------------------------------------------------------------------ 4. copy

say "Copying"
# --exclude .venv: a virtualenv bakes absolute paths into its shebangs and pyvenv.cfg.
# A copied one silently runs the interpreter from the old path, which is worse than an
# obvious break. It is rebuilt at the destination by `make install`.
rsync -a --info=progress2 --exclude='.venv/' --exclude='__pycache__/' \
      "${REPO_ROOT}/" "${DEST}/"
ok "copied to $DEST"

if [ -n "$SRC_COUNTS" ]; then
  say "Destination database"
  DEST_COUNTS="$("$PY" - "${DEST}/var/cindraleads.db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
if verdict != "ok":
    sys.exit(f"integrity_check FAILED after the move: {verdict}")
print(",".join(
    f"{t}={conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}"
    for t in ("companies", "leads", "triggers", "evidence", "dispatch_log", "jobs")
))
PY
)"
  # Row counts, not rsync's word for it. rsync reports what it transferred; this
  # reports what arrived and opens.
  [ "$SRC_COUNTS" = "$DEST_COUNTS" ] || die "row counts differ!
       source: $SRC_COUNTS
       dest:   $DEST_COUNTS
       The microSD copy is untouched. Do not start the units against $DEST."
  ok "verified identical: ${DEST_COUNTS}"
fi

# ----------------------------------------------------------------------- 5. finish

say "Done copying. The rest needs you, because it rebuilds and re-points services"
cat <<EOF

    The microSD copy at ${REPO_ROOT} is UNTOUCHED and every unit is stopped.
    Nothing is running yet. To finish:

      cd ${DEST}
      make install                              # rebuild the venv at this path
      ./deploy/install_pi.sh --install-units    # rewrites unit paths to ${DEST}
      cindra db migrate && cindra status        # sanity
      curl -s localhost:9109/healthz | head

    Then watch it for a day. Once you are satisfied:

      rm -rf ${REPO_ROOT}

    If anything is wrong, the way back is to re-run install_pi.sh from
    ${REPO_ROOT} -- the old copy is complete and still current.

EOF
