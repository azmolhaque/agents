#!/usr/bin/env bash
#
# CindraLeads Pi bootstrap. Idempotent - safe to re-run.
#
#   cd ~/cindraleads && ./deploy/install_pi.sh
#
# Brings a Raspberry Pi 5 from "fresh Debian + cloned repo" to "Phase 1 ready":
# apt deps, Ollama, the systemd override, the two resident models, the Python venv,
# the database, and the test suite.
#
# What it deliberately does NOT do without being asked:
#   --tune-boot      edits /boot/firmware/config.txt for NVMe Gen 3. That needs a reboot
#                    and can leave a Pi unbootable if the SD/NVMe setup is unusual, so
#                    it is opt-in and printed as a recommendation otherwise.
#   --install-units  installs and enables the systemd units, which starts a worker that
#                    harvests and posts to Discord on its own. Turning a manual tool
#                    into an unattended one is a decision, not a bootstrap step.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TUNE_BOOT=0
INSTALL_UNITS=0
for arg in "$@"; do
  case "$arg" in
    --tune-boot) TUNE_BOOT=1 ;;
    --install-units) INSTALL_UNITS=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }

# ----------------------------------------------------------------- host checks

say "Host"
ARCH="$(uname -m)"
printf '    arch=%s  kernel=%s\n' "$ARCH" "$(uname -r)"
[ "$ARCH" = "aarch64" ] || warn "not aarch64 - benchmark numbers will not represent the Pi"

if [ -f /proc/device-tree/model ]; then
  printf '    model=%s\n' "$(tr -d '\0' < /proc/device-tree/model)"
fi

TOTAL_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
printf '    ram=%s MB\n' "$TOTAL_MB"
[ "$TOTAL_MB" -ge 7000 ] || warn "under 8 GB RAM; the two-model envelope assumes 16 GB"

# The DB must not live on microSD: SQLite under WAL load will corrupt on it.
DB_DEV="$(findmnt -no SOURCE --target "$REPO_ROOT" 2>/dev/null || echo unknown)"
printf '    repo on %s\n' "$DB_DEV"
case "$DB_DEV" in
  *mmcblk*) warn "repo is on microSD. Move it to NVMe before running unattended - " \
                 "SQLite WAL on SD is the documented corruption path." ;;
  *) ok "not microSD" ;;
esac

# ------------------------------------------------------------------- apt deps

say "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-dev build-essential \
     sqlite3 git curl jq libsqlite3-dev
ok "installed"

PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
printf '    python=%s\n' "$PYV"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' \
  || { echo "python 3.11+ required, found $PYV" >&2; exit 1; }

python3 - <<'PY' || { echo "SQLite is missing FTS5; install libsqlite3 with FTS5" >&2; exit 1; }
import sqlite3
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)")
PY
ok "sqlite has FTS5"

# --------------------------------------------------------------------- ollama

say "Ollama"
if command -v ollama >/dev/null 2>&1; then
  ok "already installed ($(ollama --version 2>/dev/null | head -1))"
else
  curl -fsSL https://ollama.com/install.sh | sh
fi

sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp deploy/ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
ok "systemd override installed"

# The daemon needs a moment to bind after a restart. Polling here is why the manual
# sequence failed with "could not connect to ollama server".
say "Waiting for the Ollama API"
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    ok "API up after ${i}s"
    break
  fi
  [ "$i" -eq 30 ] && { echo "Ollama did not come up. Try: journalctl -u ollama -n 50" >&2; exit 1; }
  sleep 1
done

# ---------------------------------------------------------------------- models

say "Models (PLAN.md 2.2: two resident, no third router model)"
PULLED=(); MISSING=()
for model in qwen3:4b-instruct bge-m3; do
  printf '    pulling %s ...\n' "$model"
  # Not piped: a pipeline's status is the LAST command's, so `... | tail` would
  # report success for every failed pull and hide a missing tag.
  if ollama pull "$model"; then
    PULLED+=("$model")
    ok "$model"
  else
    warn "$model did not resolve"
    MISSING+=("$model")
  fi
done
printf '    resolved: %s\n' "${PULLED[*]:-none}"

if printf '%s\n' "${MISSING[@]:-}" | grep -q 'qwen3'; then
  warn "qwen3:4b-instruct unavailable. Pulling llama3.2:3b as the measured fallback."
  warn "The benchmark will record which model it actually used - do not edit the table."
  ollama pull llama3.2:3b || true
fi

echo
ollama list

# ---------------------------------------------------------------------- python

say "Python environment"
make install
ok "venv ready"

say "Boilerplate stripper (selectolax, 4.9 MB)"
.venv/bin/pip install --quiet -e ".[extract]" && ok "installed" \
  || warn "selectolax failed; the stdlib fallback will be used (slightly worse extraction)"

# -------------------------------------------------------------------- database

say "Database"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  ok "created .env (0600) - fill in keys before Phase 2"
else
  chmod 600 .env
  ok ".env already present"
fi
.venv/bin/cindra db migrate
.venv/bin/cindra db status

# ----------------------------------------------------------------- test suite

say "Test suite + Phase 0 durability gate"
make check
make gate

# --------------------------------------------------------------- systemd units

say "Systemd units (Phase 7)"
if [ "$INSTALL_UNITS" = "1" ]; then
  UNIT_TMP="$(mktemp -d)"
  trap 'rm -rf "$UNIT_TMP"' EXIT
  for unit in deploy/systemd/cindraleads-*.service deploy/systemd/cindraleads-*.timer; do
    # The units are written for user `pi` at /home/pi/cindraleads. Rewrite to whoever
    # is actually running this, rather than shipping units that only work on one box.
    sed -e "s|/home/pi/cindraleads|${REPO_ROOT}|g" \
        -e "s|^User=pi$|User=${USER}|" \
        -e "s|^Group=pi$|Group=${USER}|" \
        "$unit" > "${UNIT_TMP}/$(basename "$unit")"
  done
  sudo cp "${UNIT_TMP}"/cindraleads-*.service "${UNIT_TMP}"/cindraleads-*.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now cindraleads-worker cindraleads-health
  sudo systemctl enable --now cindraleads-harvest.timer cindraleads-reconcile.timer \
       cindraleads-digest.timer cindraleads-maintenance.timer
  ok "units installed and enabled"
  systemctl list-timers 'cindraleads-*' --no-pager || true
else
  warn "not installing systemd units. To run unattended:"
  echo "        ./deploy/install_pi.sh --install-units"
  echo "        # or follow docs/RUNBOOK.md section 4"
fi

# ------------------------------------------------------------------ boot tune

say "NVMe / boot tuning"
CONFIG=/boot/firmware/config.txt
if [ "$TUNE_BOOT" = "1" ]; then
  if [ -f "$CONFIG" ]; then
    sudo cp "$CONFIG" "${CONFIG}.cindraleads.bak"
    if grep -q '^dtparam=pciex1_gen=' "$CONFIG"; then
      sudo sed -i 's/^dtparam=pciex1_gen=.*/dtparam=pciex1_gen=3/' "$CONFIG"
    else
      echo 'dtparam=pciex1_gen=3' | sudo tee -a "$CONFIG" >/dev/null
    fi
    ok "set dtparam=pciex1_gen=3 (backup at ${CONFIG}.cindraleads.bak) - REBOOT REQUIRED"
  else
    warn "$CONFIG not found; skipping"
  fi
else
  warn "not touching $CONFIG (needs a reboot). To enable NVMe Gen 3 later:"
  echo "        sudo sed -i 's/^dtparam=pciex1_gen=.*/dtparam=pciex1_gen=3/' $CONFIG"
  echo "        # or append 'dtparam=pciex1_gen=3' if the line is absent, then reboot"
  echo "        Or re-run: ./deploy/install_pi.sh --tune-boot"
fi

# ---------------------------------------------------------------- thermal read

say "Thermal sensors"
if command -v vcgencmd >/dev/null 2>&1 && vcgencmd measure_temp >/dev/null 2>&1; then
  TEMP_RAW="$(vcgencmd measure_temp)"
  THROTTLE_RAW="$(vcgencmd get_throttled)"
  ok "$TEMP_RAW"

  # Decode rather than printing the hex word. A bare "throttled=0xe0000" looks fine
  # at a glance and is not: bits 16-19 mean it HAS throttled since boot. The whole
  # reason thermal.py exists is that throttling is otherwise invisible, so the
  # bootstrap must not be the place that hides it.
  .venv/bin/python - "$THROTTLE_RAW" <<'PY'
import sys
from cindraleads.thermal import parse_throttled

flags = parse_throttled(sys.argv[1])
active = sorted(k for k, v in flags.items() if v and k.endswith("_now") or (v and k == "currently_throttled"))
historic = sorted(k for k, v in flags.items() if v and k.endswith("_occurred"))

if not flags:
    print("    \033[33mwarn\033[0m could not parse: " + sys.argv[1])
elif active:
    print("    \033[31mCRITICAL\033[0m throttled RIGHT NOW: " + ", ".join(active))
    print("             The governor will drop to 1 worker and pause inference.")
elif historic:
    print("    \033[33mwarn\033[0m not throttled now, but HAS been since boot:")
    for name in historic:
        print(f"             - {name}")
    if "under_voltage_occurred" in historic:
        print("             under-voltage => power supply. Use the official 27W USB-C PSU.")
    else:
        print("             no under-voltage, so this is COOLING, not power.")
        print("             Phase 7 requires zero throttle events over 72h. An active")
        print("             cooler is required by the hardware budget; check yours.")
    print("             These bits are sticky until reboot. For a clean baseline:")
    print("               sudo reboot && vcgencmd get_throttled   # expect 0x0")
else:
    print("    \033[32mok\033[0m   throttled=0x0, no throttling since boot")
PY
else
  warn "vcgencmd unreadable. The service user needs the 'video' group:"
  echo "        sudo usermod -aG video \$USER   # then log out and back in"
  warn "Without it the governor cannot see temperature and holds the safe state."
fi

# ------------------------------------------------------------------ next steps

say "Bootstrap complete"
cat <<'NEXT'
    Next, to close the Phase 1 gate:

      make fixtures     # gather tests/fixtures/html/ (robots-respecting, ~2 min)
      make bench        # measure, then overwrite docs/BENCHMARKS.md

    Then commit the regenerated benchmark file:

      git add docs/BENCHMARKS.md tests/fixtures/
      git commit -m "Phase 1: measured benchmarks on the Pi"
      git push
NEXT
