# Runbook

What to do at 3am. Ordered by what you would actually check, not by subsystem.

Everything below assumes the repo at `~/cindraleads` and the venv at `.venv`. Every
`cindra` invocation is `~/cindraleads/.venv/bin/cindra`.

---

## 0. The one command

```bash
curl -s localhost:9109/healthz | jq
```

`status` is `ok`, `degraded`, or `critical`, and every check says why in a sentence.
`/healthz` answers 503 only on `critical`, so `curl -f` works as a probe. Prefer this
over `systemctl status`: a running worker with a harvest timer that stopped firing
three days ago looks perfectly healthy to systemd.

No jq, or the endpoint is down:

```bash
systemctl --user status cindraleads-health   # or without --user, see Install
cindra status                                # the same numbers, from the CLI
```

**`degraded` is not an outage.** Ollama down, budget spent, SoC hot, and a unit that
has never run are all states the pipeline survives by design. Read the detail line and
decide; do not restart reflexively.

---

## 1. Symptom → cause

### No cards in Discord

In order of likelihood:

| Check | Command | Meaning |
| --- | --- | --- |
| Is anything scoring above the tier floor? | `cindra status` | Tier A ≥ 72, B ≥ 55, C ≥ 40. Everything at REJECT is a scoring problem, not a delivery one. |
| Is the webhook configured? | `cindra dispatch-test --dry-run` | Prints the embed without posting. Then drop `--dry-run` to prove the wiring end to end. |
| Is the worker draining? | `curl -s localhost:9109/healthz \| jq '.metrics'` | `queue_ready` climbing with `dispatches_24h` at zero means jobs are queued and nothing is running them. |
| Did the digest fire? | `systemctl status cindraleads-digest.timer` | Tier C only goes out on the 08:30 timer. A and B do not wait for it. |

`cindra dispatch-test` exists precisely to separate "the webhook is wrong" from "no
lead qualified" — those need different fixes and look identical from Discord.

### The queue is deep and not moving

```bash
cindra queue status
journalctl -u cindraleads-worker -n 50
```

- **`deferred` high, `ready` at zero** — jobs held until later. Usually the per-domain
  fetch budget or the thermal governor. `cindra queue release` pulls them forward if
  you are impatient and know why they were deferred.
- **`in_flight` stuck above zero with no progress** — a worker died holding leases.
  `cindra queue reclaim` returns them; they become claimable when the lease expires
  anyway, so this only saves you the wait.
- **`dead` climbing** — jobs that exhausted their retries. `cindra status` prints the
  last five errors. These do not retry on their own.

### `LLM inference is paused by the thermal governor`

```bash
cindra health
```

Three different problems wear this message:

- **`state=hot`** — genuine heat. Inference pauses, fetching continues, jobs defer and
  retry. This is the system working. Check the fan.
- **`state=throttled`** with `under_voltage` — **power, not heat.** Use the official
  27 W USB-C supply. A cheap one browns out under inference load.
- **`vcgencmd on PATH: False`** — the governor is blind and holds its last state. The
  service user needs the `video` group:
  ```bash
  sudo usermod -aG video "$USER"   # log out and back in
  ```
  The unit files set `SupplementaryGroups=video` for exactly this.

### Ollama is down

```bash
systemctl status ollama
curl -sf localhost:11434/api/tags | jq '.models[].name'
```

The worker is not `Requires=` on Ollama on purpose: it keeps harvesting and enriching
and defers anything needing a model. Restart Ollama and the deferred jobs drain on
their own. Nothing needs to be re-queued.

A cold model load off microSD is ~32 s. The first extraction after a restart being
slow is not a fault.

### Disk full

```bash
df -h ~/cindraleads/var
du -sh ~/cindraleads/var/*
```

`no space left on device` is recoverable and deletes still work while writes fail:

```bash
cindra maintain                  # cache sweep + retention purge
rm -rf ~/cindraleads/var/cache   # nuclear; it is a cache, it refills
```

The cache holds zstd page bodies under `var/cache/<sha>/`, purged at 30 days by
`cindra maintain`. Backups under `var/backups/` rotate at 7.

### SerpAPI budget exhausted

Not an error. The guard is a 24 h rolling window persisted in SQLite, so it refills
continuously rather than at midnight, and it survives restarts. Free-tier discovery
continues; only the costed queries stop. `cindra harvest --dry-run` shows what would
be planned and what it would cost.

### Everything looks fine but no new companies

Almost always cache saturation, not a fault:

```bash
cindra harvest --dry-run
```

`skipped_cached: 12` means every query is still inside its cache TTL (12–72 h by
source). The hourly timer exists to catch each window the moment it reopens, which is
why running harvest by hand rarely finds anything.

---

## 2. Routine operations

```bash
# what is the system doing
cindra status
curl -s localhost:9109/metrics | grep -v '^#'
xdg-open http://localhost:9109/            # read-only HTML view

# make it do something now, out of schedule
sudo systemctl start cindraleads-harvest.service
sudo systemctl start cindraleads-reconcile.service
sudo systemctl start cindraleads-digest.service
sudo systemctl start cindraleads-maintenance.service

# when do the timers next fire
systemctl list-timers 'cindraleads-*'

# logs
journalctl -u cindraleads-worker -f
journalctl -u cindraleads-worker --since '1 hour ago' -o cat | jq -c 'select(.level=="error")'
```

Logs are structlog JSON, one object per line, with secrets redacted by a processor.
`-o cat` strips systemd's prefix so `jq` can read them.

### After every `git pull` — restart the worker

```bash
git pull && cindra db migrate && sudo systemctl restart cindraleads-worker cindraleads-health
```

**A pull alone changes nothing that is running.** Python imports its modules once, at
process start. The worker is long-lived, so it keeps executing the build it loaded at
boot while the new code sits on disk — and it goes on draining jobs and reporting
itself healthy the entire time, which is what makes this so easy to miss. A scoring
change was deployed four times before anyone noticed the process applying it was four
builds old.

`/healthz` now reports it as `worker:build`, so if you forget, the endpoint tells you.
The timers are unaffected: each firing is a fresh process and picks up new code by
itself. Only the two long-running units need the restart.

### Restarting safely

```bash
sudo systemctl restart cindraleads-worker
```

`SIGTERM` sets a flag and the loop finishes its current job. `TimeoutStopSec=180`
clears a 64 s extraction plus a cold model load. A restart mid-extraction is safe
regardless — the job's lease expires and it runs again whole — but waiting is free.

---

## 3. Backup and restore

Backups run nightly after maintenance, keeping 7:

```bash
./scripts/backup.sh                    # manual, same thing
./scripts/backup.sh /mnt/usb 30        # elsewhere, keep 30
```

It uses the online backup API, never `cp`. **Copying a WAL-mode database while the
worker runs produces a file that opens fine and is missing the last transactions** —
silently, and you find out during the restore you needed it for. Every snapshot is
`PRAGMA integrity_check`ed before the rotation deletes its predecessor.

### Restore

```bash
sudo systemctl stop cindraleads-worker cindraleads-health
gunzip -c var/backups/cindraleads-<stamp>.db.gz > var/cindraleads.db
cindra db migrate                      # the backup may predate a migration
cindra status
sudo systemctl start cindraleads-worker cindraleads-health
```

A restore cannot cause duplicate cards: `dispatch_log` restores with the database, and
the idempotency key is `(lead_id, sorted(triggers), score // 10)`. Leads dispatched
after the snapshot will re-send — that is the correct behaviour, since from the
database's point of view they never went out.

---

## 4. Install

```bash
sudo cp deploy/systemd/cindraleads-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cindraleads-worker cindraleads-health
sudo systemctl enable --now cindraleads-{harvest,reconcile,digest,maintenance}.timer
systemctl list-timers 'cindraleads-*'
```

The units assume user `pi` and `/home/pi/cindraleads`. Adjust `User=`, `Group=`,
`WorkingDirectory=`, `EnvironmentFile=` and the `ExecStart=` paths if yours differ —
`sed -i "s|/home/pi/cindraleads|$HOME/cindraleads|g; s|^User=pi|User=$USER|"` over the
copies is usually enough.

### What runs when

| Unit | Schedule | Does |
| --- | --- | --- |
| `worker.service` | always | Drains every job kind. The only thing that runs stages. |
| `health.service` | always | `/healthz`, `/metrics`, HTML view on 127.0.0.1:9109. |
| `harvest.timer` | hourly | Plans discovery queries and **enqueues**. |
| `reconcile.timer` | 30 min | Queues unenriched companies and stale scores. |
| `digest.timer` | 08:30 | Posts the Tier C backlog. |
| `maintenance.timer` | 03:20 | Retire, decay, resample, purge, backup. |

**Timers only ever enqueue; the worker drains.** A timer that also drained would race
the worker for the same jobs and, worse, load a second copy of the model on a box
sized for two.

### Two failure modes specific to this layout

- **Worker restart-looping.** `StartLimitBurst=5` in 5 minutes stops it rather than
  hammering Ollama forever. `systemctl reset-failed cindraleads-worker` after fixing.
- **Watchdog kills.** `WatchdogSec=180`; the loop pets it every iteration including
  empty polls, so a kill means genuinely wedged — usually a socket read with no
  timeout. The journal shows the last job it claimed.

---

## 5. Things that look broken and are not

| Looks like | Actually |
| --- | --- |
| `harvest: planned 1, 0 new jobs` | Every query is inside its cache window. Working as intended. |
| `skipped_cached: 12` | Same. This is what a *second* run should look like. |
| `dns_gaps: 3` but no T8 trigger | `mail_auth_weakness` is narrower than `hygiene_gaps`. DNSSEC and `security.txt` show on a card but are not worth a trigger. |
| `evidence: inconclusive 10` in maintenance | Robots denials, unregistered sources, timeouts. Unknown is not dead, and only a 4xx marks a URL unreachable. |
| Score dropped after `cindra maintain` | A superseded trigger was retired and the lead re-scored. The old number was the stale one. |
| `crtsh` in `sources_failed` | crt.sh 502s often. The breaker opens after 3 failures and T7 cannot fire until it recovers. Everything else enriches normally. |
| Enrichment took 33 s for one company | Five site paths at ≥3 s apart is 15 s of deliberate politeness. |
| A 403 on `/.well-known/security.txt` | Very common. Recorded as "we could not see it", never as "they do not have one". |
