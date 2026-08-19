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
cindra feedback <lead_id> good|bad        # manual verdict, same write path as the bot
cindra feedback-bot                       # Discord gateway client (optional unit)
cindra precision-report [--write]         # of what we sent, how much was worth sending
cindra critic [--write]                   # proposals. Applies none of them.
cindra acceptance [--hours 72] [--write]  # what the unattended run actually proved
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

**Scoring recalibrated 2026-08-18, measured before and after on 169 real leads.**
Sendable 7 -> 60, hit rate 5% -> 34%, mean score 6.8 -> 31.8, Tier B 0 -> 8. Two
defects, both found by `cindra explain` and neither visible from the tier counts alone:

- `single_source` inspected only the *top* trigger, so 56 of 57 leads corroborated by
  two or more independent sources carried it anyway -- 96% incidence, which is a
  constant offset rather than a discriminator. It now asks whether the *lead* rests on
  one source, counting **sources not URLs** (three pages of a company's own site are
  one party's word for it). Now 50% incidence, and the report confirms **0 of 84**
  corroborated leads still carry it.
- `no_contact` (-25) charged the same fact as the `reachability` component (0-15), so
  one fact cost 40 points of a 100-point scale with a Tier C floor of 40. Removed; the
  gradient carries it alone. `enrichment_ran` went with it, which loses the
  unknown-versus-absent distinction in reachability -- "we looked and found nobody" and
  "we have not looked" both score 0. A real gap, unmade rather than wrong.

**A scoring change is only half done until the corpus is rescored.** Leads carry a
`scoring_version` (config hash + hand-bumped `ARITHMETIC_VERSION`), `enqueue_stale_scores`
treats a mismatch as stale, and the fingerprint is in the dedupe key -- without that the
rescore collides with the job that already ran and the mechanism reports success having
changed nothing. `cindra reconcile --force` exists for the case a job *ran* but achieved
nothing, which no query can detect.

**Prose is prospect-facing and gets guarded twice.** The first Tier B card ever
dispatched read "You published T1_AI_SHIP and T8_HYGIENE_GAP on your public page": the
prompt was handed the raw code and nothing knew what it meant. Every trigger now has a
`means` phrase in `scoring.yaml` (outside the fingerprint -- it changes prose, never
numbers), and any `T\d+_[A-Z_]+` surviving into an angle is discarded at generation
*and* withheld at dispatch. The second guard matters because leads scored under an older
build keep their bad angle and nothing re-queues them.

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

**Phase 8 code complete; the loop has no real reactions in it yet.** A gateway bot
(`cindraleads-feedback.service`, optional) turns Discord reactions into `feedback` rows,
`cindra precision-report` measures them, and `cindra critic` argues with the config.

Four things shape it:

- **The join is the whole mechanism.** A reaction carries a message id and nothing else
  -- Discord has never heard of a lead. `dispatch_log.discord_message_id` is the only
  bridge, which is why the Dispatcher POSTs with `?wait=true`. A card sent before that
  column was populated can never be reacted to, and the code says so rather than
  guessing at the most recent lead.
- **The bot and `cindra feedback` share one write path.** They did not at first, and
  the CLI inserted unconditionally: marking a lead `good` then `bad` by hand left both
  rows, and the pessimistic resolution in `precision_report` made the correction
  unreachable rather than authoritative. One verdict per person per *question* --
  `contacted` does not overwrite `good`, because they answer different ones.
- **`OPTIONAL_UNITS` exists so a declined bot is not permanently degraded.** Never
  having run one is normal and reported `ok`; having run it and stopped is degraded and
  never critical, because a probe that 503'd on a Discord outage would restart a worker
  that is working. Nothing that drains the queue may be listed there -- a test asserts
  it.
- **The Critic proposes and applies nothing, checked by content.** A test hashes
  `config/*.yaml` before and after a full run, so a future "just apply the obvious ones"
  flag fails regardless of how it is spelled. The reason is not caution about bugs: a
  scoring change that applied itself would be one nobody read, measured against a corpus
  scored under the rules it just replaced.

**Precision is scoped to dispatched leads; the Critic is not.** `precision_report`
answers "of what we sent, how much was worth sending" and joins through `dispatch_log`
inside a window. The Critic argues about weights, and a verdict typed at the CLI on a
lead that never cleared the floor is exactly the signal it needs -- so it counts every
judged lead. Reporting `judged` from one population while arguing from the other
produced a report claiming nothing was judged directly above a proposal citing eighteen
judged leads.

**An interruption is not a failure, and the queue used to charge them to one counter.**
`attempts` was incremented at *claim* time, so a worker killed mid-job looked exactly
like a stage that raised. Three deploys during a slow LLM call dead-lettered a
`score.company` job that had never once failed -- found by `cindra acceptance` on its
first real run, below `/healthz`'s dead-letter warn threshold of 5 and therefore
invisible until something asked "did this run lose work".

`attempts` now counts stage failures only and is charged in `fail()`; `reclaims`
counts orphaned leases and is charged in `reclaim_expired()`, with its own higher
ceiling. **Every claim still ends in exactly one of done, `attempts+1` or
`reclaims+1`** -- that accounting is why the claim-time increment existed, and it is
preserved rather than dropped. The ceilings differ because the evidence does: three
stage failures say the job is broken, three interruptions say we deployed three times.

**`extend_lease` shipped in Phase 0, was tested, and nothing ever called it** -- the
same shape as `digest_pages`. The worker now renews the lease and pets the watchdog
while `prepare()` runs, which is what stops a slow stage being reclaimed out from under
itself. That needs a bound or it defeats both mechanisms: a stage wedged in a socket
read would renew forever. `MAX_STAGE_SECONDS` is the line, and past it the stage is
cancelled and the job fails honestly.

**The Phase 7 gate was re-specified 2026-08-19, because the old one asserted a
heatsink.** "`get_throttled` stays `0x0` for 72 h" was already false twenty minutes
after a cold boot -- sticky bits 17/18/19, bits 0/16 clear, so thermal and not power.
It also required that the thermal governor never once act, on a system that has a
thermal governor. Under real load it engaged, `scorer_prose_failed` logged
`will_retry: true`, and the jobs completed later; the old gate failed the run for
exactly that.

`cindra acceptance [--hours 72]` grades what the software controls -- throughput, no
job lost, no silent unit, one build throughout, and **the governor recovered if it
engaged**. Heat is reported and never graded. Two rules in it:

- **A criterion that cannot be evaluated reports `n/a` and does not pass.** A box with
  no sensor must not read as a box that stayed cool, which is how the `0x0` gate would
  have been satisfied by a machine that never measured anything.
- **A gap is never credited to the state that preceded it.** Crediting the interval
  across an outage would report hours of `nominal` for a window in which nothing ran.

It reads the `metrics` table, which the worker writes to every 60 s anyway -- the
heartbeat now carries `thermal_state`, `temp_c` and `throttled_now`. Before that the
governor kept its state in memory and `/healthz` reported only the instant, so "did it
engage over those 72 hours, and did it come back" died with each poll.

**Known hardware gaps:** root is on microSD (no NVMe present), and sustained
inference reaches ~80 C with the fan at ~6000 RPM. Two unclean shutdowns have already
put 13k NUL bytes in the JSONL log; `PRAGMA integrity_check` on the database still
returns `ok`, which is luck rather than design. journald is volatile on this box, so
nothing survives the reboot you would want to investigate.
