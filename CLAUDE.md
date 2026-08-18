# CindraLeads

Autonomous lead intelligence for Cindrasec. Runs on a Raspberry Pi 5 (16 GB).

## Absolute rules

1. **PASSIVE-ONLY.** Never write code that scans, probes, brute-forces, or authenticates
   against a prospect. Public-record and self-published lookups only. This is a legal
   boundary and the entire brand promise ("no scan without a signed RoE"), not a
   preference. See `PLAN.md` and `docs/COMPLIANCE.md` (Phase 5).
2. Every lead needs >= 1 Trigger with >= 1 reachable Evidence URL. **No evidence, no lead.**
3. Local model first. Cloud API is a rationed escalation path with a hard daily cap
   (`DAILY_CLOUD_USD_CAP`, default $0.50), persisted in SQLite, surviving restart.
4. Nothing crosses a stage boundary except a validated Pydantic model.
5. Secrets live in `.env`, are redacted in logs, and are never committed.
6. The Dispatcher writes to Discord. It **never** emails a prospect. A human decides.

## Commands

```
make install     # venv + dev extras
make test        # everything
make check       # lint + typecheck + test (what CI runs)
make gate        # Phase 0 acceptance: 100 jobs, kill -9, exactly once
make fmt         # ruff format + autofix
make schema      # regenerate db/schema.sql from db/migrations/
make fixtures    # gather tests/fixtures/html/ (needs open outbound HTTPS)
make bench       # Phase 1 benchmark -> docs/BENCHMARKS.md (RUN ON THE PI)

cindra db migrate | db status | db backup <path>
cindra queue status | queue reclaim | queue enqueue --kind K
cindra harvest [--dry-run] [--limit N]   # Scout -> durable harvest jobs
cindra pipeline                          # harvest -> extract -> resolve -> enrich -> score -> dispatch
cindra work --kinds harvest.query,extract.candidate,... [--drain-inflight]
cindra dispatch-test [--dry-run]         # prove the Discord wiring, any tier
cindra health                            # what the thermal governor sees
cindra queue release [--kind K]          # pull deferred jobs forward
cindra status                            # candidates, companies, live triggers
cindra maintain [--dry-run] [--no-network]  # nightly: retire, decay, resample, purge
cindra reconcile                         # enqueue-only: unenriched + stale scores
cindra explain [--near-misses N]         # scores, penalties, and yield per query template
cindra digest [--dry-run] [--limit N]    # batch the Tier C backlog to Discord
cindra serve [--port 9109]               # /healthz, /metrics, HTML view (localhost)
cindra feedback <lead_id> good|bad
```

## Conventions

Python 3.11+, asyncio, Pydantic v2, ruff + mypy strict, pytest.
Every network call: timeout, retry with jitter, circuit breaker.
Every LLM call: JSON Schema via Ollama `format`, parsed into Pydantic, retried once at
temp 0, then escalated, then dead-lettered. **Never regex a model's prose into a field.**
Prefer stdlib and small arm64-native deps. Justify anything over ~200 MB.

## Approved deviations from the master prompt

These are decided; do not "fix" them back. Rationale in `PLAN.md` Part 2.

- **No `qwen3:1.7b` router.** Three models contend for two Ollama load slots. Two
  resident models only: `qwen3:4b-instruct` + `bge-m3`.
- **`anthropic` SDK, not `claude-agent-sdk`** in prod (100 MB and wants Node, for an
  agent loop we do not use). `claude-agent-sdk` is dev-extra only.
- **Dedupe rungs 1/2/4 ship; the vector rung is gated off** behind config. `sqlite-vec`
  and `company_vectors` exist from day one so enabling it is never a migration.
- **Discord feedback needs a gateway bot** (Phase 8). Webhooks are write-only and
  cannot read reactions; `dispatch_log.discord_message_id` exists for the join.
- **6 fetches per domain per 24 h**, >= 3 s apart (the spec's "<=2/day" contradicted its
  own 5-path fetch list).
- **`T0_INBOUND`** added to the trigger taxonomy so inbound mail becomes a real Lead.

## Where things are

```
config/*.yaml   behaviour - edit these, not code       prompts/        all LLM prompts
db/migrations/  schema source of truth                 db/schema.sql   generated, do not edit
src/cindraleads/  pipeline                             mcp_servers/    tools (library + MCP wrapper)
tests/golden/   regression fixtures for prompt changes docs/RUNBOOK.md what to do at 3am
```

## Testing rules

- Never change a prompt without re-running the golden fixtures.
- Every rule in the compliance section gets its own test. CI fails on a missing one.
- The durability drill (`make gate`) spawns real processes and sends real SIGKILLs.
  If you make it pass by weakening it, you have deleted the reason the queue exists.

## Current state

**Phases 0-6 code complete.** The pipeline produces Tier A leads end to end.

The pipeline runs end to end: `Scout -> Harvester -> Extractor -> Resolver -> Enricher
-> Scorer -> Dispatcher`, six durable job kinds driven by one async worker loop. Every stage is two-phase --
`prepare()` does network I/O outside any transaction, `commit()` writes inside the one
that also enqueues follow-on jobs and completes the current job. A stage that fails,
by returning `ok=False` or by raising, rolls its own writes back.

Two rules are enforced mechanically rather than trusted:
- A snippet that does not literally appear in the fetched page is dropped, and a
  candidate with no surviving snippet keeps no trigger claims.
- The Extractor holds an LLM and a fetcher and nothing else, so a successful prompt
  injection can only produce a wrong extraction. The regex tripwire in `injection.py`
  is a detection signal, not the defence.

**Phase 0 complete.** Models, store, durable queue, structlog + redaction, CLI skeleton, CI.

**Phase 1 measured on the Pi, gate passing on a 3-page sample.** See
`docs/BENCHMARKS.md` (generated, never hand-edited). Measured 2026-08-15:

| | qwen3:4b-instruct on Pi 5 |
| --- | --- |
| Schema validity | **100%** (gate is >= 95%) |
| Prefill | 42.2 tok/s |
| **Decode** | **3.7 tok/s** ← the binding constraint |
| p50 page | 64 s |
| Peak temp | 79.6 C, no active throttle |
| Cold model load | ~32 s off microSD |

**Decode costs ~11x more per token than prefill.** Every tuning decision follows from
that: output is bounded in the schema (`maxLength`/`maxItems` become grammar rules), and
the prompt budget is 1500 chars because 4000 cost 150 s/page against 64 s.

Two numbers that are latency-tuned against a *schema-validity* gate and must be re-tuned
in Phase 3 against *field accuracy*: `textextract.extract_text(max_chars=1500)` and the
`CompanyExtraction` field bounds. A short budget drops footers, which is where headcount
and location live.

**Phase 2 gate passed on the Pi (2026-08-15).** 21 discovery queries, 151 extract jobs,
and a repeat run at zero network calls (`skipped_cached=10`, free plans dropped to 0).
Three defects were found doing it, all of which made a broken run *look* like a passing
one; the tests named after them are the reason they cannot come back.

**Still open before Phase 3 is formally closed — all of it needs the Pi:**

- The three-way accuracy gate (PLAN.md 2.11): schema validity >= 98%, critical fields
  >= 90%, soft fields >= 70%. Nothing has been measured against a real model yet;
  every extract test here uses a stub backend and asserts pipeline judgement, not
  extraction quality.
- Re-tune `PROMPT_CHAR_BUDGET` and the `CompanyExtraction` bounds against field
  accuracy rather than latency.
- Duplicate rate < 2% on real data using rungs 1/2/4.
- Phase 1's full 24-page benchmark (`make bench`, ~26 min).

**Phase 4 done, and it is what made Tier A reachable.** The same company that scored
**52 (Tier C)** before enrichment scores **74 (Tier A)** after, verified end to end over
real HTTP: reachability 0 -> 100 from a contact on the company's own page, surface
60 -> 75 from a published DMARC gap. `reachability` is 15% of the score and `surface`
another 10%; before the Enricher both were structurally zero and no lead could clear the
threshold no matter how good it was.

Enrichment is passive throughout: CT logs, public DNS, RDAP, the company's own pages and
their ATS board's public JSON. `contacts.py` imports no socket library at all, which is
the strongest available form of "never SMTP VRFY/RCPT" -- a test asserts it.

**`cindra maintain` is the only thing in the system that looks backwards.** Every stage
moves work forward and never revisits a row it wrote, so decay, retirement, evidence
reachability and retention all live in one nightly pass (PLAN.md 2.7,
`deploy/systemd/cindraleads-maintenance.*`).

It exists because narrowing `mail_auth_weakness` did not un-write the 95 T8 rows the
loose rule had already produced -- they kept a 60-day decay and kept feeding scores.
**Editing a derived-trigger rule is half a change; the other half is an entry in
`RETIREMENT_RULES` so the pass re-runs it over what the old rule wrote.** Retirement
also has to enqueue its own re-scoring: `enqueue_stale_scores` reconciles on
`MAX(observed_at) > lead.last_updated_at`, and retiring a trigger moves neither.

Reachability is three-valued on purpose. `evidence.reachable` is 1, 0 or NULL, and a
robots denial, an exhausted domain budget and a timeout all leave NULL. Only a 4xx
(never 401/403/429) sets 0, and a trigger is retired for dead evidence only when every
URL it cites is *known* dead.

**Still unmeasured: extraction accuracy.** Every extract test uses a stub model. Nothing
has been checked against a real one, so `employee_band` and `display_name` correctness
are unknown. That is the Phase 3 gate (PLAN.md 2.11) and needs ~50 hand-labelled pages.

**Phase 7 code complete; the 72 h unattended run is not yet done.** Two long-running
units (`worker`, `health`) and four timers (`harvest`, `reconcile`, `digest`,
`maintenance`), installed by `./deploy/install_pi.sh --install-units`. `docs/RUNBOOK.md`
is the 3am document.

**Timers only ever enqueue; the worker drains.** A timer that also drained would race
the worker for the same jobs and load a second copy of the model on a box sized for two.
That is why `cindra reconcile` exists separately from `cindra pipeline`.

Three things that shape the rest of Phase 7:

- **Metrics are computed from SQLite at scrape time, never accumulated in process
  memory.** Harvest, digest and maintenance are short-lived processes; an in-process
  counter would die with each one and a scrape would report whatever the last process
  to exit had done. `prometheus_client` is deliberately unused -- the text format is a
  dozen lines, and a metrics endpoint that fails to start on a missing optional extra
  is worse than none.
- **The health endpoint's whole job is telling *idle* from *stopped*.** Zero ready jobs
  is both a healthy finished system and one whose harvest timer died on Tuesday; the
  job table cannot distinguish them, so `HEARTBEAT_UNITS` does. **A new timer needs an
  entry there or it is a blind spot** -- a test asserts every `*.timer` has one, and it
  caught exactly that when `enrich.timer` was renamed to `reconcile.timer`.
- **Degraded is not failure.** Ollama down, budget spent, SoC hot are all designed-for
  states: `/healthz` returns 200 and only a stuck queue, a dead-letter pile or a silent
  unit gives 503. A probe that failed on heat would restart the worker into the heat.

**Tier C now batches.** `digest_pages` existed and was tested but nothing called it, so
Tier C posted one message per lead. The per-lead stage now sends only A and B; `cindra
digest` reconciles the rest against `dispatch_log` daily, so a missed morning costs a
day's delay and not a day's leads.

**Discovery is weighted by what a hit *proves*, not by what it announces.** The
first corpus reached 148 companies at 82% T1_AI_SHIP -- a tic-tac-toe game, a world
clock, a personal blog -- because unfiltered Show HN sat at weight 95 and the HN
hiring thread at 72. With a 12-plan budget per run the project sources consumed it
before the company sources were reached. The question every template weight now
answers: does a hit here imply payroll or investors? A public ATS board does, a
funding announcement does, an org-owned repo usually does, a Show HN post does not.

`companies.discovered_by` records the template that found a company first (never
overwritten -- a re-sighting does not reassign discovery), and `cindra explain` reports
sendable-per-template. **Before it existed, no query change was checkable.** A weight in
`icp.yaml` is a guess until that table disagrees with it.

`organizations_only` defaults ON for GitHub. `stack_risk_repos` documented "Restricted
to organizations" for weeks while calling an unfiltered search, so every personal
langchain project became a candidate.

**A `git pull` does not change what is running.** The worker and health units are
long-lived Python processes; they keep the modules imported at boot while the new code
sits on disk, draining jobs and reporting healthy. Deploying is
`git pull && cindra db migrate && sudo systemctl restart cindraleads-worker cindraleads-health`.
`/healthz` reports the gap as `worker:build` -- the worker stamps `source_mtime` on its
heartbeat and health compares it against the newest `.py` on disk. Timers are exempt:
each firing is a fresh process.

**Known hardware gaps:** root is on microSD (no NVMe present), and sustained
inference reaches ~80 C with the fan at ~6000 RPM. The Phase 7 acceptance run requires
`get_throttled` to stay `0x0` for 72 h, which this hardware has not yet demonstrated.
