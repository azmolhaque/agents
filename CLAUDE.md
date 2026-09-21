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
cindra reconcile                         # enqueue-only: lost/superseded extracts, unenriched, stale scores
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

**`maxLength` bounds characters; `max_tokens` bounds tokens; the two are only the same
number in English.** Verified directly -- a field bounded at 20 returned exactly 20
characters, cut mid-phrase, so the grammar does hold. What it does not do is keep the
model inside the *budget*: a 400-character `bengali_angle` is legal grammar worth ~1200
tokens, and against a 400-token budget the decode ran out inside a string the grammar
was still happy with. JSON EOF, whole object lost, `EOF while parsing a string at line 3
column 908` -- a byte column, which is how 300 Bengali characters reads as 908.

**A bound and a budget are one decision made in two files, and nothing at runtime checks
they agree.** `test_the_bengali_bound_is_the_one_the_budget_was_sized_for` is that check.
A ceiling is also not a cost -- decode stops at the stop token -- so sizing `max_tokens`
tightly to the bound saves nothing and converts a long answer into a lost one.

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

**A thermal pause is a defer in every stage that runs a model, and the Extractor was
missed.** It turned every `SchemaValidationError` into a stage error, so a pause
incremented `attempts` and retried within seconds -- three pauses in one minute
dead-lettered 11 candidates that had never once been shown to the model. Fixed in the
Scorer first; nothing asked whether another stage had the same shape, and it did.

**Fixing the cause does not recover what the bug buried.** Those candidates sat in
`candidates` with status `new` and no live job, invisible to every reconciler --
`enqueue_unenriched` and `enqueue_stale_scores` both start from `companies`, and a
candidate that never extracted never became one. `enqueue_unextracted` is the third
reconciler and the only one that recovers *lost* work rather than late work.

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
- **A count that only climbs cannot answer a present-tense question.** `dead_letter`
  is append-only and nothing purges it, so the all-time total held `/healthz` at
  degraded indefinitely over four jobs buried by two bugs that were already fixed --
  the pre-0006 attempt accounting and the watchdog crash loop. `/healthz` now grades
  `dead_letter_recent` (24 h) and reports the total alongside it; `cindra acceptance`
  still grades "no job lost" over the window a human chose. A probe that stays
  degraded after the fault is gone is one you learn to ignore.
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

**The decode budget for prose is sized by language, and it is part of the prose
version.** `LeadProse` allows 1080 characters and 400 tokens covered that in English;
Bengali is several tokens per *character* in this tokenizer, so a BD lead ran out
mid-string and produced JSON ending in the middle of a value. Three Tier B cards reached
Discord with a dash where the angle belongs.

Raising the budget was half the change. Nothing could find those three leads again: the
arithmetic had not moved so `scoring_version` matched, no trigger had moved so
`last_updated_at` was current, and the score job had completed successfully -- **a failed
prose call is not a failed score.** `prompt_version` would not have caught it either,
because the fix was a constant in `scorer.py` and that hash covers prompt files.
`prose_version()` now hashes both, the Scorer stamps it on every lead it writes, and
`enqueue_stale_scores` re-queues a lead that is angle-less **and** stamped by an older
build. Both halves are load bearing: without the first it re-decodes angles that are
already fine, and without the second a lead whose prose leaks trigger codes -- discarded
on purpose, and the same prompt will do it again -- asks forever.

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

**`discovered_by` was NULL for every company ever recorded, and nothing noticed for
days.** The Harvester puts `template_id` on the extract job; the Resolver reads it out
of the *candidate's stored payload* to write the column. The Extractor sat between them
and forwarded neither. Both ends had tests and both passed -- nothing crossed the seam,
which is the only place the bug could live. `cindra explain` reported `(unknown) 201`
and read as "these predate the column" rather than "this never works".

The lesson is the one this project keeps relearning: **a field threaded through three
stages needs a test that drives all three.** Same shape as `digest_pages` and
`extend_lease` -- built, tested in isolation, never actually wired.

`organizations_only` defaults ON for GitHub. `stack_risk_repos` documented "Restricted
to organizations" for weeks while calling an unfiltered search, so every personal
langchain project became a candidate.

**A retry that completes successfully is invisible to everything built to catch a
loop.** A prose failure is not a stage failure -- the lead is scored and stored, the
job returns `ok` and completes -- so `attempts` never increments, `max_attempts` never
applies, nothing dead-letters and `/healthz` reads ok. Fourteen leads were re-decoding
every twenty minutes with no counter anywhere that could say so. `MAX_PROSE_ATTEMPTS`
is the ceiling, carried in the follow-on payload because it is per-attempt state and a
lead re-scored for another reason should start over.

**A thermal pause is charged to its own counter, for the same reason the queue splits
`attempts` from `reclaims`.** Three failed calls say the prompt or the budget is wrong;
three pauses say the box was hot for an hour, which is designed-for. On one counter a
single hot spell spends the whole allowance and the lead is angle-less *permanently* --
silently, because by then `prose_version` matches and `enqueue_stale_scores` is right to
report nothing to do. `MAX_PROSE_PAUSES` is 12, four hours; past that the governor is
not having a spell, it is the steady state.

They were looping because **`_RECOVERABLE` matched a fact about the configuration
rather than about the failure.** "no escalation backend" is appended to every
exhausted-ladder message on a box with no cloud tier, which is this box, always -- so a
deterministic JSON truncation read as transient. A marker present in 100% of cases is a
constant, not a discriminator, exactly like `single_source` at 96% incidence.

**A `git pull` does not change what is running.** The worker and health units are
long-lived Python processes; they keep the modules imported at boot while the new code
sits on disk, draining jobs and reporting healthy. Deploying is
`git pull && cindra db migrate && sudo systemctl daemon-reload && sudo systemctl restart
cindraleads-worker cindraleads-health`.

**A unit change is not deployed by `git pull`, and `daemon-reload` does not rescue it.**
`deploy/systemd/*.service` is a *template*: `install_pi.sh` rewrites the paths and user
and copies the result into `/etc/systemd/system/`. So the live unit is a copy, a pull
never touches it, and a reload faithfully reloads the old one. `TimeoutStopSec=960` was
pulled, reloaded and restarted, and `systemctl show` still reported `3min` through all
three. Deploying a unit change is:

```
./deploy/install_pi.sh --install-units
systemctl show cindraleads-worker -p TimeoutStopUSec   # verify, do not assume
```

Third layer of the same defect: a long-lived worker pins the prompt it imported, systemd
pins the unit it cached, and the installed unit is a copy of a file nobody edits twice.
`source_mtime` catches none of it -- it scans `src/`, `prompts/` and `config/`, and a
`.service` file is in none of them.
`/healthz` reports the gap as `worker:build` -- the worker stamps `source_mtime` on its
heartbeat and health compares it against the newest file on disk. Timers are exempt:
each firing is a fresh process.

**That check scanned `*.py` only, and the two directories this project changes most are
not code.** `config/*.yaml` and `prompts/` are read exactly once, at stage construction
-- `load_prompt` in the Extractor's `__post_init__`, `icp.yaml` in `Scout.from_config`,
`scoring.yaml` for the Scorer's lifetime -- so a long-lived worker pins an edited prompt
exactly as firmly as an edited module. `source_mtime` now covers all three trees,
resolved through `Settings` rather than guessed from `__file__`. **A staleness probe
blind to the file you just edited is worse than none, because it answers confidently.**

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

**The Critic argued about a penalty that no longer exists.** `penalty_counts` is read
off stored `score_breakdown` rows -- what the build that scored each lead applied, not
what the file says now. `no_contact` was deleted from `scoring.yaml` on 2026-08-18 and
three stale leads still carried it, so the report proposed editing a key that is not in
the file, quoting a point value from history. It even flagged those leads as stale at
the top of its own output and argued from them anyway. **Every penalty rule now checks
the running config first.**

Meanwhile `single_source` -- 52% incidence, holding 47 of 262 leads out of a better
tier, the largest lever in the report -- drew no proposal, because incidence was the
only thing being asked about and 52% is far under `CONSTANT_OFFSET_INCIDENCE`.
`HELD_BACK_SHARE` closes the band, but **only paired with an incidence floor**: "promotes
a tenth of the corpus" alone fires on any penalty applied near a tier boundary, including
one doing exactly its job.

**A hit that is a platform URL implies nothing, whatever the source implies.**
`hn_who_is_hiring` sat at weight 96 as the strongest free company signal and returned 19
hits, 16 platform drops, 0 candidates -- because "Ask HN: Who is hiring" is *one story*
whose URL is news.ycombinator.com, with the companies in its comments. `hn_show_ai`
converts 100 hits into 35 candidates for the opposite reason: a Show HN story carries an
external `url`. The distinction was never the source, it was whether a hit carries a
company's own domain.

**`comments: true` makes a hit a comment**, which is what closed it. The HN permalink
stays the hit URL -- that is what we actually saw, dated and quotable -- and the company
URL inside the comment goes in `raw["homepage"]`, where `extraction_target` already
looks. So the Extractor reads the company's page while the citation points at the
comment; citing acme.io as evidence for "they are hiring" would be a claim their landing
page may not make. A comment naming no domain is skipped, never guessed at. Bounded at
3 threads x 40 comments and **sliced client-side as well as requested** -- a bound the
remote enforces is not a bound, and each accepted comment is ~64 s of decode.

**The mock passed and the mock was wrong.** `hn.algolia.com` is blocked by the dev
session's network policy, so the tests drive a mock encoding the assumed response shape
-- and a mock cannot know the *query* is wrong. Two live calls from the Pi found what
786 green tests could not: `query` is full text, so "Ask HN Who is hiring" was matching
"Who wants to fund DB research?" and "Do you know how much head hunters cost?". **This
template had never once found the thread it is named after**, which is the real reason
it produced nothing; reading comments would have faithfully read the wrong threads.
`tags: story,author_whoishiring` selects the monthly bot post exactly.

**Most links in that thread are an ATS, and an ATS host is worse than useless.**
Measured on the first 10: 7 were `teamtailor.com`, `wellfound.com`, `careerpuck.com`,
`kula.ai`, `applicantstack.com`, `uctalent.io` or `forms.gle`, against 3 real company
domains. Each hosts many companies behind a slug, so
`arborealmanagement.na.teamtailor.com` canonicalizes to `teamtailor.com` and every
company on that ATS would merge onto one bogus row -- rung 1 doing exactly its job to
data that should never have reached it. They are in `PLATFORM_HOSTS` now.

**Algolia has no boolean operator, and five templates had returned zero hits for the
life of the project.** Not zero candidates -- zero *hits*. `query` is typo-tolerant full
text where every word must match, so `raises seed OR "Series A" OR "we raised"` requires
seven tokens in one story and finds nothing, while `Series A` fills a page. `Dhaka
startup` is the one to remember: two tokens, still zero, because both must co-occur. It
is not length, it is requiring several rare terms at once -- **one concept per
template**. SerpAPI and GitHub do support boolean and keep theirs.

That is why the corpus was 79% T1_AI_SHIP + T8_HYGIENE_GAP with the trigger mean pinned
at 35 against the 75 Tier A needs. T2, T3, T4, T6 and T12 could not exist in quantity
because the only free queries that find them matched nothing. It read for weeks as
"discovery is hard" rather than "five queries are malformed".

**Weight decides order; `max_hits` decides share, and share is what a fixed decode
budget rations.** Unfiltered Show HN was 250 hits and 85 candidates over five runs --
more than every other template combined -- while converting 1 of 17 companies into a
sendable lead (6% against a 33% baseline, mean 23.6 against 32.3). Demotion alone never
touched it, because it returns a full page whenever it runs at all.

**`is_barren` needs a minimum sample or it condemns templates nobody measured.** It
flagged a template with one hit across two runs in the same words as one with nineteen,
and the advice attached is "lower the weight or retire it".

**Precision is scoped to dispatched leads; the Critic is not.** `precision_report`
answers "of what we sent, how much was worth sending" and joins through `dispatch_log`
inside a window. The Critic argues about weights, and a verdict typed at the CLI on a
lead that never cleared the floor is exactly the signal it needs -- so it counts every
judged lead. Reporting `judged` from one population while arguing from the other
produced a report claiming nothing was judged directly above a proposal citing eighteen
judged leads.

**Tier A is not an enrichment problem, and `cindra explain` now proves it rather
than arguing it.** The report re-tiers every lead with `reachability` set to a perfect
100. If Tier A is still zero there, contacts cannot reach it -- and at the current means
they cannot: solving the weighted sum for the Tier A floor of 75 needs `trigger >= 81`
against a corpus mean of 35. That is a discovery problem. The counterfactual is printed
directly under the tier table so the next person does not spend a week on the wrong
component, the way this one nearly did.

**A template that produces nothing is invisible to the table built to judge
templates.** `cindra explain`'s yield view groups `companies.discovered_by`, so a query
returning only platform URLs has no row and reads as one never tried. `serpapi_marketplace`
(weight 98) and `serpapi_jobs` (94) were each returning 10 hits, dropping 10 as platform
URLs and producing zero candidates on every run, spending SerpAPI credits hourly. The
Harvester now persists per-run yield to `metrics` and `explain` reports it worst-first,
flagging any template that found hits and converted none. `dropped_platform` itself is
correct -- a LinkedIn URL has no company behind it -- the defect was that nobody could see
how often it fired.

**Most contacts were in the markup, and `extract_text` throws markup away.** The
Enricher found contacts for 23 of 201 companies, so `reachability` -- 15% of the score --
was zero on 173 of 195 leads and ten of them sat 0.8 points under the Tier C floor. The
cause was not that the addresses were absent: a page whose contact is a "Get in touch"
button publishes it in `href="mailto:..."`, and the text extractor keeps only what a
visitor sees. `emails_from_markup` reads the raw body; the obfuscation rule is untouched,
because `hello [at] acme.io` is a request not to be harvested and a `mailto:` link is the
opposite.

The security.txt `Contact:` line is read for the same reason -- we already fetched the
file to decide whether one exists and discarded the body. RFC 9116 makes the field
mandatory, so it is a free address, and the most relevant one available: the mailbox the
company nominated for security correspondence.

**The contact loop exists only to find a contact, and it kept fetching after it had
one.** `site.text` has exactly one consumer -- `extract_contacts` -- so every page after
the first address can only spend budget, and the old loop always ran all four. One
company could burn the whole 6-per-24h allowance that tomorrow's evidence re-check also
needs. Stopping early is what makes room for `/privacy`, `/imprint` and `/impressum`,
which are the highest-yield pages available because they are *legal obligations*: GDPR
Art. 13 requires a controller contact and an Impressum is mandatory in DE/AT/CH, so they
are populated even on sites that publish nothing else.

**security.txt is fetched first, and that is a fix.** It ran after the page loop, so a
domain whose budget ran out mid-loop never got one -- and `security_txt` feeds
`hygiene_gaps`, so an exhausted budget silently cost a *trigger* as well as a contact.

**RDAP is deliberately not a contact source.** Registrant records are redacted post-GDPR
and the abuse contact belongs to the registrar, not the prospect -- presenting it as a
company contact would be wrong rather than merely useless.

**Improving the Enricher is half a change.** `enriched_at` records that we looked, not
what we could see at the time, so none of this reaches the 201 companies already marked
enriched until the 30-day sweep. `cindra reconcile --force` now re-queues enrichment as
well as scoring. Same shape as `RETIREMENT_RULES`.

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

**Two deadlines run during a stage and only one of them is ours.** The lease is ours;
the watchdog is systemd's, and it is the one that bites. `WatchdogSec=180` means the
worker must ping every 90 s. The renewal interval was `lease / 3`, which at the unit's
`--lease 600` is 200 s -- so every stage slower than three minutes sat inside
`asyncio.wait` without petting and took SIGABRT at 180. Twelve crash-loop restarts,
presenting as low throughput and worker gaps rather than as arithmetic.
`_renewal_interval` now takes the nearer of the two and halves the watchdog's, because
`Watchdog.pet()` rate-limits itself and waking exactly on the interval lets jitter push
a ping past its own gate.

The renewal loop also writes the heartbeat. The main loop writes it every 60 s and does
not run while a stage does, so a stage slower than `HEARTBEAT_GAP_SECONDS` would be
reported by `cindra acceptance` as a gap -- the signal that means the worker died.

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

**`description` and `industry` were in the extraction schema and nowhere in the prompt,
so the model nulled them 583 times out of 583.** Rule 2 tells it that an unstated field
is null, and a bare `str | None` with no rule and no enum has nothing to go on. The cost
was not two empty columns: `industry` is the text `not_government_or_cni`,
`not_a_competitor` and `not_an_excluded_sector` all match on, so **three compliance rules
have only ever seen `display_name`** -- which is why nyc.gov passed as "New York City
Public Interest Tech". `description` is handed to the prose prompt, so every outreach
angle was written without knowing what the company does.

Invisible to every test, because a stubbed model returns whatever the fixture says. Only
counting the corpus could show it -- `SUM(industry IS NOT NULL)` over `companies`. The
guard is now mechanical: every optional free-text field in `CompanyExtraction` must be
named in `prompts/extract_company.md`. `trigger_codes` and `evidence_snippets` are
exempt and populate fine, because an enum and a self-describing list name carry their
own semantics through the schema.

Naming them was not enough on its own: rule 2 required every field to be "supported by
text that literally appears on the page", and the model obeyed rule 2 over rule 10.
Probed against the real model, it quoted the company's own tagline verbatim into
`evidence_snippets` and returned `description: null` in the same object -- it had the
information and the rules forbade writing it. Rule 2 now carves out the two summary
fields, and a test asserts the carve-out, because re-tightening it would silently empty
both columns with every test still green.

**Two prompt fixes shipped and the next 37 companies came out null as well, which was
neither fix being wrong.** `scripts/probe_extraction.py` settled it: holding the prompt
byte-identical and varying only the grammar, the *shipping* schema fills both fields on
the first attempt. The prompt on disk was correct and the worker was still holding the
one it loaded at boot -- see the `source_mtime` note above, which is the same defect
seen from the other end. **Two hypotheses were argued from the corpus before anything
asked the model; the probe cost three minutes.** Nothing records the bytes a model
returns, so an omitted key and an explicit `null` reach every log as the same `None`,
and they are different defects with different fixes.

**And fixing a prompt does not un-write what the broken one produced.**
`enqueue_stale_extractions` is the fourth reconciler and re-extracts on two predicates,
the pair `prose_version` needed: the company is missing `description` or `industry`,
**and** its extraction is stamped by an older `prompt_version`. Without the first it
re-decodes hundreds of pages that are already fine; without the second a bare login page
that genuinely says nothing asks again forever. Bounded at `DEFAULT_RESTALE_LIMIT` per
pass because the cost is decode -- a whole corpus is hours of inference in the queue a
fresh harvest drains, and a backfill nobody is waiting on must never be what a new lead
waits behind.

**It shipped filtering on `candidates.status = 'extracted'`, which no row that has a
company can ever hold.** The Extractor writes `extracted`; the Resolver overwrites it
with `resolved` in the very next stage, and a `companies` row exists only because the
Resolver ran -- so the filter and the join were mutually exclusive and the query
returned zero rows for every possible database state. `cindra reconcile` said "queued 0
for re-extraction" against 583 null rows and read as *nothing to do*. Its test passed
because the fixture wrote `extracted` next to a company row by hand, a pair the pipeline
cannot produce. **Third time now** -- `discovered_by`'s test hand-wrote the one key the
Harvester never sets, and the HN mock encoded an assumed response shape. The rebuilt
test drives the real Extractor and the real Resolver and fails against the old query.

**A bound per pass is not a bound when something else decides how often a pass runs.**
`reconcile.timer` fires every 30 minutes and 50 re-extracts is ~46 minutes of decode, so
adding 50 a pass outruns the worker by construction -- the backlog grows every half hour
and a freshly harvested lead ends up behind hundreds of backfill jobs, which is the one
thing the limit existed to prevent. It counts jobs still *outstanding* and tops up to
the limit instead. A backfill job is otherwise indistinguishable from a fresh extract --
same kind, same shape -- so it carries a `backfill` flag, and the defer path carries the
flag too or a thermal pause silently frees budget the next pass spends again.

**A publisher is not a platform host, it is worse than one.** A worklist about to be
emailed had lead #9 reading `Shikho · techcrunch.com`, contact `aisha@techcrunch.com`,
evidence a TechCrunch article about a third company's earphones. Every stage worked: the
Extractor read the article and took its *subject* as the company, the Resolver
canonicalized the article URL to the publisher, the Enricher found a real journalist's
address on the publisher's own site. A GitHub repo canonicalizes to something that
resolves to nothing and is dropped downstream; **an article canonicalizes to a live
organization with a working mailbox**, so nothing downstream objects and the card
pitches an assessment to a reporter about a story she wrote. Publishers and wire
services are in `PLATFORM_HOSTS` now.

**The offer slugs leak exactly the way the trigger codes did.** `Offer` is a `Literal`
of four identifiers handed to the prose prompt with nothing that knows what they mean --
where `T1_AI_SHIP` stood before `means` existed. Eight of ten cards read "an AI-LLM
assessment" and the ninth read "I'd like to run an ai_llm_assessment for you": *usually*
is why this is a guard and not a prompt fix. Both guards now match snake_case as well as
`T\d+_[A-Z_]+`, underscores only -- a card saying "watch" or "snapshot" in running
English is fine and matching those words would withhold half the corpus. The pattern is
in `prose_version`, because widening what counts as a leak makes an angle the old build
accepted one this build re-asks for.

**The enterprise veto has never once fired.** `under_employee_ceiling` returns True when
`employee_band` is None -- "silence is not evidence of size" -- and `_icp_component`
scores an unknown band at 55 of 100 for the same reason. Both defaults are right for the
case they were written for. Measured 2026-08-30: **`employee_band` was non-null for 1 of
616 companies**, so both mechanisms have been off for the life of the project and every
company is scored as though it might be 11-50. That is how OpenAI reached the top ten of
a call list at 65 with `disclosure@openai.com` attached.

Unlike `description`, the prompt cannot fix this: a tagline is on the page and a
headcount genuinely is not, and rule 3 forbidding the model from inventing a number is
correct and stays. The Enricher was already fetching the company's public ATS board and
already counting it -- `analyze_postings` reads the postings for hiring triggers and
drops the list. `companies.open_roles` keeps that count as the fact it is, and the band
is derived at read time so a threshold edit re-scores through `scoring_version` rather
than leaving a stale column for `RETIREMENT_RULES` to chase.

**The inference only ever costs points, and that asymmetry is the design.** A wrong
"small" puts an enterprise in front of a human as a Tier A lead, which is the failure
being fixed; a wrong "large" drops a real prospect into the digest, where it is still
read. So only the large bands are inferable and a low count infers nothing -- three open
roles is a five-person startup or a hundred-person company hiring quietly. A stated band
always beats an inferred one.

**A publisher denylist cannot be complete, and the obvious general rule is unsafe.**
One report after the first 25 hosts shipped, the corpus produced `Chaya ·
dhakatribune.com` -- the TechCrunch defect again, in Bangladesh, which is 40% of the
ICP's geography and so the likeliest place for it to recur -- plus `France 24`,
`WeeTracker`, and `linecast · terminaltrove.com`, a *directory* rather than a publisher
and a third shape of the same failure.

The tempting general rule is "the display name does not match the canonical domain".
**`Rover · rtrvr.ai` kills it**: one of the best leads in the corpus, a real company
whose name genuinely does not resemble its domain. A mismatch is a signal worth
quarantining for review, never a veto, and nothing here should auto-reject on it.

The measurement to run before building anything: how many companies have a name that
fails a `name_similarity` check against their own domain, and what share of those are
real. Until that exists the list is the patch and it is known to be losing.

**Adding a host to the list is half a change.** It stops the next candidate and does
nothing about the rows already written: a day after the regional publishers were
blocked, `France 24`, `Chaya`, `WeeTracker` and `linecast · terminaltrove.com` were all
still on the near-miss list and `The Financial Times · ft.com` had joined them.
`suppress_platform_companies` in `cindra maintain` re-runs the rule over what the old
one produced, the same shape as `RETIREMENT_RULES`. It suppresses rather than deletes,
through the table the rest of the system already consults -- the Scout skips a
suppressed domain at plan time, `worklist` joins it live, `is_suppressed` is -100 in the
arithmetic -- so the company row and its evidence survive and the record of what we
believed is kept. **The list will keep growing; what this removes is the manual step
after each addition, which is the one nobody performs twice.**

**Only the Snapshot is free, and for the life of the project every card said otherwise.**
Rule 2 of `outreach_angle.md` read *Write "I'd like to run X for you, free, under a
signed RoE"*, with X substituted blindly from `recommended_offer`. For any company with
T1_AI_SHIP and an AI surface -- 487 of 1201 live triggers -- that is
`ai_llm_assessment`, a BDT 40k-1.5L / $2k-8k engagement. **Every Tier A and B card
offered it at no charge, in writing**, and eight of the first ten on a call list said so
in text a human was about to paste into an email.

The slug reaching the prompt bare was the same defect as `T1_AI_SHIP` before `means`
existed -- and the comment two lines above that call said exactly that while passing
`result.offer` anyway. `scoring.yaml` now carries an `offers` map with a `means` phrase
and a `free` flag, load fails closed on a missing phrase, and a paid phrase still names
the free Snapshot as the small first step so the ask stays tiny without giving the
engagement away. `offers` is outside the fingerprint for the same reason `means` is: it
changes prose, never a number.

**The prose prompt was handed five facts while thirteen sat next to it.** `_facts`
builds name, domain, description, industry, country, employee_band, ai_surface,
subdomain_count, hygiene_gaps, contacts, triggers, evidence and evidence_urls. The
`format()` call passed six. So every card opened "you announced an AI feature" -- true
of half the internet -- while the *verified quote from the company's own page*, the
specific surface they shipped, the concrete DNS gap and the reader's own name were each
one argument away.

Four now reach it. The quotes are the strongest of them and the safest: a snippet only
survives the Extractor if it appears in the fetched page character for character, so a
4B quoting one cannot invent a claim. Prefill is 42 tok/s against 3.7 for decode, so
~100 extra prompt tokens costs ~2 s on an 18 s call -- specificity is nearly free at
this end and it is the whole difference between a mail that is read and one that is not.

`ai_surface` values are identifiers and got a phrase map like `means` and `offers`, the
third time. Unlike `offers` it does **not** fail closed: those come from the model
rather than a `Literal`, so an unknown value is dropped instead of taking the config
down -- vague costs a clause, a slug in a prospect's inbox costs the mail.

**A `{placeholder}` the Scorer does not supply raises inside `prepare()`**, which would
fail every score job in the queue on a stage designed so prose failures are survivable.
A test now asserts the prompt's placeholders and the format kwargs are the same set, in
both directions: the reverse is the quieter half, and it is exactly how four facts sat
unused for a month.

**Every prose defect this project shipped was visible in the prompt text and invisible
in the code.** `T1_AI_SHIP` in a prospect's inbox, `ai_llm_assessment` wrapped in a
hardcoded "free", four assembled facts never passed -- and in the last case the comment
directly above the offending `format()` call described the exact defect it was
committing. `scripts/preview_angle.py <domain>` renders the real prompt from the real
database with no inference and no queue, so "did the fact arrive" is a one-second
question instead of a wait for a score job to reach the front of 770. Read it for what
is *populated*: an empty block is silent in a finished card, which is the whole problem.

**A quote must be something they wrote, and the first rendered prompt proved it was
not.** The Enricher writes evidence rows too, and their snippets are *ours*: "85
certificate names, 20 new in 30d", "no SPF record published", a contact address. Nobody
published those sentences. That certificate line was handed to the model as a verified
quote from Tavus's own page, and a model told to reproduce a quote verbatim writes a
sentence that reads as the result of a scan -- the one promise this project rests on,
breached in prose, on a card meant to be pasted into an email.

`content_sha256` is the discriminator and it was already in the schema: only the
Extractor stamps it, because only the Extractor literal-matched the string against a
page whose bytes it hashed. An empty hash means we composed the sentence. **The preview
found this in one second and no card had shipped yet** -- which is the argument for
rendering a prompt rather than reading the code that builds it.

**Re-reading a page is not the company doing something again.** `observed_at` reaches
the prospect as "you announced an AI feature (today)", so moving it on every
re-observation asserts an act on a date. The re-extraction backfill re-read tavus.io and
turned a four-day-old Sparrow-2 announcement into one made today -- **the same defect as
dating a DNS lookup as though the prospect acted that morning, arriving by a route that
did not exist when that one was fixed.** Caught by the preview, before any card shipped.

The discriminator is the evidence URL, not the code: a company that announces again in
September is genuinely fresh, and the codes are coarse enough that both announcements
share one row. Same URL, same story, whatever the page says today. `decays_at` is pushed
out either way -- the trigger is still true, and freezing that would retire a live fact
for the crime of being re-read. The Enricher's `_trigger` is deliberately left alone:
its triggers are standing facts re-derived from a fresh lookup, and freezing those would
decay away a DMARC gap that is still open.

**The rows already re-dated were written off as unrecoverable before anyone checked the
database, and they were not.** Re-extraction *inserts* evidence rows and never deletes
them, and `trigger_evidence` accumulates -- so the original sighting is still there
under the trigger it belongs to. `restore_first_observation` in `cindra maintain` pulls
`observed_at` back to `MIN(evidence.observed_at)` and is the other half, the same shape
as `RETIREMENT_RULES`. Scoped by `content_sha256` rather than by a list of codes that
would drift: only the Extractor stamps it, and only a page sighting is an event with a
date. It runs before decay, so a trigger pulled back past its own `decays_at` expires in
the same pass.

**Its first dry run reported 838 of 1201 live triggers, which was the query being wrong
rather than the damage being that large.** Extraction and resolution are separate jobs,
so `evidence.observed_at` is always earlier than `triggers.observed_at` by the queue
latency between them, and an unscoped `MIN` fires on nearly every trigger by minutes.
Worse, it would have fought the Resolver nightly: that rule moves the date forward for a
*new* URL, and pulling back to the oldest evidence ever joined would undo it. It now
takes the newest sighting's URL and the earliest time we saw *that* page, and ignores
differences under `REDATE_TOLERANCE_DAYS`. **A repair whose count is dominated by noise
cannot tell you whether it worked.**

**Two deadlines run during a stage, and it happened a second time.** `MAX_STAGE_SECONDS`
is 900 and the worker unit's `TimeoutStopSec` was 180, so systemd stopped waiting five
times sooner than the worker was permitted to take -- any deploy landing during a slow
stage was SIGKILL rather than a shutdown. Measured over 24 h: **3 builds, 1 announced
exit, 2 worker gaps**, and the two missing goodbyes are the two gaps. `no_job_lost`
still passed, because the lease reclaim is exactly what covers this, but the job sat
unclaimed for up to `--lease 600` and charged a reclaim against a ceiling meant for
genuinely broken work. The box being warm or hot 76% of the window is what stretches an
ordinary stage past 180 s in the first place.

Identical in shape to `WatchdogSec` against the lease, and to the prose bound against
its token budget: **one decision made in two files with nothing at runtime checking they
agree.** `test_systemd_waits_longer_than_a_stage_may_run` is that check, and it fails
against the old value.

**`full_name` has one reader and zero writers, and the obvious fix does not work.**
`worklist` reads it, `has_named_contact` is worth +10 of the reachability component,
`persona_for` routes on it, and `_recipient_name` feeds it to the outreach prompt as
`recipient` -- and nothing ever assigns a value. **289 contacts, 0 names**, so every
angle opens cold and that bonus has never fired. Fifth instance of
built-wired-never-connected, after `digest_pages`, `extend_lease`, `open_roles` and
`discovered_by`.

Deriving the name from the local part was tried and **reverted after one measurement**.
The argument was that a separator means the company published first-and-last, so
`sarah.chen@` is structural rather than a guess. Against the real corpus that rule
produced 13 names, of which **seven were role mailboxes wearing a separator** --
`customer-service@`, `cyber.security@`, `analyst-relations@`, `info-us@`,
`security-alert@` -- four were journalists at publishers we suppress, and **two** were a
real person at a real prospect. A 54% false-positive rate, and "Hi Cyber," to the FT is
the same defect as "Hi Jdoe" that the strictness existed to prevent.

It was also backwards. The real names in the corpus are the *bare* tokens the rule
excluded -- `aarik@`, `abu@`, `beni@`, `bon@` -- sitting alongside `dev@`, `demo@`,
`bugs@`, `blog@`, `alerts@`, which are equally bare and not people. No regex separates a
given name from a role word without a name list.

**And the ceiling is not the filter.** 200 of 289 contacts are role mailboxes: this
corpus barely publishes personal addresses, so a perfect rule wins two contacts. Names
need the team page, and a team page needs a fetch and a parser -- not a cleverer regex
on an address. Do not retry the address route.

**A veto is arithmetic, and the veto list was never in the stamp.** `anti_icp` is -100,
so `exclude_sectors` decides whether a lead scores 66 or 0 -- but it lives in `icp.yaml`
and `ScoringConfig.fingerprint()` hashes `scoring.yaml`, so **editing the veto list has
never invalidated a single stored lead.** Found through `schneier.com`: Tier B at 66
with `industry = "security consultancy"`, a sector already on the list, because the lead
was scored before `industry` was populated and nothing could find it afterwards -- the
calibration matched, no trigger had moved, the angle was present.

`calibration_version(scoring, gate)` is now the stamp, computed in **one** function used
by the Scorer that writes it and `enqueue_stale_scores` that compares against it. Two
copies would mark every lead stale on every pass and rescore the corpus forever -- the
same one-decision-in-two-files shape as the prose bound against its budget and
`MAX_STAGE_SECONDS` against `TimeoutStopSec`. `suppressed_domains` is deliberately
excluded: it changes on every `cindra suppress`, and rescoring 780 leads to reject one
domain is the wrong trade when `worklist` joins it live and the Scout checks it at plan
time.

**The publication exclusion is the general rule the host denylist could not be.**
`thenewway.ai` is an AI news blog on its own domain, so `ghost.io` in `PLATFORM_HOSTS`
never saw it; the page says what it is and `industry` finally carries that. Multi-word
terms on purpose -- bare "media" would veto a social-media platform and bare "news" a
news-reader app, both real prospects. The labels came from the corpus rather than
imagination, and extending them is a query:
`SELECT industry, COUNT(*) FROM companies GROUP BY industry ORDER BY 2 DESC`.

That query is also a warning: **`developer tools` is 153 of 482.** `industry` is the
model's loose summary, not a taxonomy, so it is a usable compliance input only for
labels specific enough to mean one thing.

**One writer, three readers, and the third was missed.** `calibration_version` was
introduced so the Scorer that stamps `leads.scoring_version` and `enqueue_stale_scores`
that compares against it could not drift -- and `diagnose()` was still comparing against
`ScoringConfig.fingerprint()`, the arithmetic half. So the moment the gate joined the
stamp, **every lead the new build scored read as stale to the report built to say so**:
`832 of 833 lead(s) were scored by a DIFFERENT calibration`, over a corpus that was
current, telling the operator to wait for a rescore that had already run. The Critic
reads the same field and discounted its own proposals on it.

**832 of 833 is the shape of a constant, not a finding** -- the same tell as
`single_source` at 96% incidence and "no escalation backend" in 100% of exhausted
ladders. A number that high is a claim about the measurement, and it was read as a claim
about the corpus for a week.

`test_a_rescored_corpus_reports_current` used to set the column by hand to the value the
*reader* wanted, so it passed against a broken production path. It now drives the real
Scorer and reads its stamp back, and it fails against the old comparison. Fourth
instance, after `discovered_by`, `enqueue_stale_extractions` and the HN mock: **a test
that supplies the input the code expects proves nothing.**

**Slow is a way of failing, and the fan-out only ever handled the other way.**
`return_exceptions=True` means a source that *raises* costs its own field; a source that
never answers cost the whole company. `prepare()` ran until the worker cancelled it --
`enrich.company: CindraError: stage enrich.company exceeded 900s and was cancelled` --
the job failed, and an attempt was charged against the dead-letter ceiling for a
prospect whose only fault was a slow host. Nothing about that is exotic: six site
fetches at three retries, a 30 s timeout and up to 60 s of backoff each is ~1260 s with
no bug at all. The fan-out now keeps whatever answered inside `ENRICH_DEADLINE_SECONDS`
and reports the rest as failed sources, which is what "one failing source must not fail
the company" was always supposed to mean.

**A ceiling that one code path is not subject to is not a ceiling.** `_backoff` is
capped at `backoff_max_seconds` precisely because there is a length of time past which
we would rather fail a fetch than hold a worker -- and the `Retry-After` branch slept
for whatever the remote asked for, uncapped. Past the ceiling we now stop retrying
rather than retry sooner: the server named a number, and asking again inside it is the
hammering the header exists to prevent. `nan` parses as a float and compares False
against every bound, so `_retry_after` rejects non-finite values before they reach a
sleep that would never end.

`ENRICH_DEADLINE_SECONDS` is derived from `MAX_STAGE_SECONDS`, which moved to `config`
so a stage can bound itself against the same number instead of a copy of it. Fourth
instance of one-decision-in-two-files, after the prose bound against its budget,
`WatchdogSec` against the lease, and `MAX_STAGE_SECONDS` against `TimeoutStopSec`.

**"No evidence, no lead" was enforced at write time and never against the calendar.**
A lead whose every trigger has decayed keeps its tier, its score and its outreach angle
forever, and stays dispatchable -- `worklist`, `cindra digest` and the Dispatcher all
read `FROM leads` and filter on `tier`, and not one of them joins a trigger. So a Tier B
card can invite a human to cite a fact the system no longer claims is current, which is
absolute rule 2 breached by the passage of time.

**Three mechanisms that look like they cover it, and none does.**
`enqueue_stale_scores` joins `triggers` on `active = 1 AND decays_at > now`, so a lead
with no live trigger produces no row -- not stale, *invisible*. Queue one anyway and the
Scorer returns `skipped="no live trigger"` and `commit` returns ok **without touching
the lead**, so the row survives a re-score that reports success. And
`retire_unevidenced_triggers` retires triggers for dead links, never the lead above
them. Every part worked; nothing owned the question.

`retire_unevidenced_leads` in `cindra maintain` is that owner, running last of the
retirements because it asks what is left after decay, supersession and dead links have
each had their say. It retires rather than deletes -- the row and its breakdown survive,
the same choice as `suppress_platform_companies` -- and deliberately does **not** move
`last_updated_at`, so a genuinely new trigger satisfies `MAX(t.observed_at) >
l.last_updated_at` and scores the lead back up. Self-healing, not a one-way door. The
`evidence_expired` marker is written straight into `score_breakdown` and is deliberately
**not** in `scoring.yaml`: the Scorer never computes it, so a config entry would claim an
arithmetic that does not run and would invalidate `calibration_version` for the whole
corpus to store a number nothing reads.

Found at 5 leads, all already REJECT, so nothing had leaked -- **but the mechanism never
looked at the tier.** A defect that is currently harmless by luck is still the defect.

**`dead_letter` is an archive and `cindra status` reports it as a present tense.** Dated
2026-09-15, all 26 rows: **11 on one day (2026-08-21)** from the thermal pause charged as
an extraction failure, **3 on 2026-08-19** from pre-`extend_lease` lease expiry, and
**12 network failures spread evenly over 31 days** at ~0.4/day. Every bug-caused dead
letter in this system is closed history -- 25 days clean on the first, 27 on the second.
The count only climbs, so it reads as a growing pile of current faults. `/healthz`
already grades `dead_letter_recent`; nothing else does.

**`recent errors` listed jobs that had already recovered.** It selects on
`last_error IS NOT NULL`, and `last_error` survives a successful retry, so the list means
"errored at some point" under a heading that claims "is failing". Two `enrich.company`
rows sat at the top of it reading as a live fault -- both `done`, one of them three weeks
old -- and a full round of diagnosis went into asking whether a bound shipped that
morning was holding. The answer was in the two columns the line did not print. It now
prints the date and the state. Still not filtered to failures: a job that failed twice
and then succeeded is exactly the flakiness worth seeing, it just must not look identical
to one that is still broken.

**A certificate that does not verify is an answer, not a fault.** The Extractor already
treats a 4xx that way -- "it will say the same thing on every retry, so failing the job
would spend three attempts and a dead-letter row establishing that" -- and an expired or
self-signed certificate is the same kind of permanent. It was taking the timeout branch.
Detected on the exception *chain* rather than the message, because httpx raises
`ConnectError` from the underlying `ssl.SSLError` and by the time it reaches a log the
type is gone.

**`no_job_lost` graded a background rate nobody can fix.** An unreachable prospect is not
lost work: the job ran, reached the network, and the host did not answer after every
retry. At ~0.4/day those alone fail a 72 h window, which is exactly the defect that
retired the `get_throttled == 0x0` criterion -- grading something outside the software.
The gate now grades `jobs_lost` and reports the unreachable count beside it.

Two rules hold it honest, and they are the whole reason this is not just a weakened gate:
**the exemption is an allow-list of remote-origin markers, never a deny-list** -- an
unrecognised error still counts as lost, so a new failure mode cannot quietly acquire an
excuse -- and **both numbers are always printed**, because an exemption nobody can see is
a weakened gate, and a rising unreachable count is how a broken uplink *on this end*
would present.

The tempting version was to convert a final-attempt timeout into `skipped` so no
dead-letter row is written at all. **Rejected:** if the timeouts are ever ours, that rule
discards hundreds of real candidates into a `skipped` bucket nobody watches, and the
census looks healthier the worse things get. Keep the row, narrow the claim.

**Throughput passed for the first time on 2026-09-15: 21.0 Tier A+B/day against a
target of 15, 53 dispatched in 24 h.** It had been 0.0/day. What moved it was the
`reconcile --force` re-enrichment reaching the 880 companies whose `enriched_at` recorded
only that we had looked, under the old contact loop -- `reachability` is 15% of the score
and it was structurally zero on 583 of 833 leads.

**Fixing one gate started failing another.** Raising `TimeoutStopSec` to 960 s, so a
deploy landing mid-stage is a shutdown rather than a SIGKILL, means a *clean* stop now
legitimately takes up to 16 minutes -- three times `HEARTBEAT_GAP_SECONDS`, which is 300.
`no_silent_unit` reads every gap as "the worker died on Tuesday and nobody noticed", so a
clean deploy during a slow stage now fails it: **4 clean exits and 2 worker gaps in one
window**, where the earlier note had gaps tracking the *missing* goodbyes exactly.

The discriminator was already in the heartbeat and already being read. `exiting=True` is
the last beat the worker writes before returning, `worker_restarts` counts them -- and
the gap loop never looked at the flag sitting immediately before the gap. **Sixth
instance of built-wired-never-connected**, after `digest_pages`, `extend_lease`,
`open_roles`, `discovered_by` and `full_name`.

`ANNOUNCED_STOP_SECONDS` is derived from `MAX_STAGE_SECONDS`, because what bounds an
honest shutdown is how long the worker is permitted to spend finishing the job it holds.
Bounded rather than waived: a worker that said goodbye and stayed away for an hour is
exactly the outage the criterion exists for, and the goodbye is not a blank cheque.

**`one_build_throughout` failing at 3 builds is the gate working.** It asks whether the
window was unattended, and a window with three deploys in it was not. That one needs a
quiet period, not a code change.

**The free-offer claim has now been got wrong in both directions, and the second one
was mine.**

First: rule 2 of `outreach_angle.md` hardcoded *Write "I'd like to run X for you, free"*
with X substituted blindly from `recommended_offer`, so every company with an AI surface
was offered a $2k-8k assessment at no charge, in writing. Real defect, correctly fixed.

Then the opposite. Reading **only** cindrasec.com's `schema.org makesOffer` block --
which prices the Snapshot at $250-$600 and encodes no free tier -- I declared
`snapshot_free: {free: true}` false, stripped "free" out of all four phrases, called it
"the worst defect this project has shipped", and shipped that to the Pi. **The page says
"first Snapshot free" in fourteen places**: the meta description, the hero CTA, a pill on
the Snapshot card, the assurance strip, an entire Founding Cohort section, the pricing
banner, the FAQ and the billing note ("genuinely free -- no card, no obligation to
continue"). The flag was right all along; only its *precision* was wrong.

`makesOffer` carries list prices. **A cohort promotion is not a list price, and absence
from one machine-readable block is not absence from the business.** The principle -- a
claim about money is checkable or it is not made -- was right. The execution checked one
source, confidently, and that is the same failure this project keeps paying for in a new
costume: the HN mock encoded an assumed response shape, `test_only_the_snapshot_is_free`
encoded the author's memory, and this encoded one `<script type="ld+json">`.

What the corpus actually gets: the *first* Snapshot free, founding cohort, while pilot
slots last, under a signed RoE. `company.yaml` records `first_free` with its condition
and its source alongside the list price, so **when the cohort closes one flag turns it
off in every card in the same deploy**. `test_the_free_flag_is_backed_by_the_site` ties
`scoring.yaml`'s flag to a zero price *or* a free first engagement, in both directions.

**The dispatch guard had to be narrowed in the same commit or the original defect came
back through its own fix.** It asked "is anything free anywhere", which discriminated
perfectly while the answer was no -- and the moment one genuinely free offer existed, a
blanket allowance would let an angle promise a free $2k-8k assessment again. It keys on
the lead's own `recommended_offer` now.

**The card asked a stranger for $250-$8000 on the strength of nothing.** The outreach
prompt described us as "a B2B security studio" and stopped, while two pieces of
third-party-verifiable work sat published on our own site reaching no prospect: a Google
VRP report (authentication bypass on a forgotten subdomain of an acquisition, triaged
P2/S2, decommissioned nine days later) and a measured prompt-injection study (256 trials
per attack, the same technique swinging 4.6x in success by changing only the goal). For
cold outreach from an unknown studio that is the largest single lever available, and at
42 tok/s prefill against 3.7 decode it costs about a second.

`proof` in `company.yaml` is **matched, not generic**, and the match is most of the
value: a company that just shipped an agent gets the injection measurement, a company
with subdomain sprawl gets the finding that *was itself* a forgotten subdomain. Ordered
by the lead's own trigger order so the proof answers the reason the card exists, and
empty when nothing fits -- a proof clause that does not fit reads as a form letter,
which is what the card is trying not to be.

**Every claim is deliberately under-stated, and a test enforces it.** The Photomath
writeup spends its length explaining why the $0 reward was *correct* and why "reachable
through Google" is not "run by Google". A card reading "we found a critical
vulnerability in Google" would contradict our own published analysis, in writing, to a
reader one click away from it. `test_no_proof_claim_overstates_what_the_writeup_says`
bans "critical", "severe", "bounty", "rewarded" and requires the URL to be on our own
site.

**The strongest sentence in the business reached no card at all.** `cindrasec.com/healthcare/`
positions the whole vertical on one credential -- *"security assessment for hospitals,
diagnostic chains, clinics and health-tech, **by a registered nurse turned security
researcher**"* -- and `icp.yaml` has listed healthtech as primary ICP for the life of the
project. No competitor can make that claim, and a healthtech prospect was getting a
generic prompt-injection statistic instead.

`proof` matches on **industry first**, then trigger, then surface, because specificity is
the whole value: a healthtech company that also shipped an agent should hear about the
nurse, not about garak trial counts. `industry` is the model's loose summary rather than
a taxonomy, so the match is substring-against-a-term-list -- usable precisely because
"clinic", "patient" and "diagnostic" mean one thing.

**Rule 5 was a legal boundary paraphrased from memory.** `legal/Rules-of-Engagement.md`
v3.0 says it in one line -- *"Nothing is scanned until both parties sign"* -- and the
prompt carried my wording instead. Same failure mode as the free-offer defect, on a more
serious claim. The RoE's own sentences are in `company.yaml` now. The rule also needed a
carve-out it never had: it forbids implying we scanned *their* systems, and the proof
line describes work published about someone else.

**`test_the_prompt_asks_for_nothing_the_scorer_does_not_supply` restated the code from
the same memory as the code.** Its `supplied` set was a hand-maintained list, so a kwarg
added to `scorer.py` and forgotten there passed silently -- the half of a two-way check
that matters least often and most. It reads the `format()` call out of the source with
`ast` now. Fifth instance of the same lesson, after `discovered_by`,
`enqueue_stale_extractions`, the HN mock and the free-offer flag.

**And the instrument built to catch prose defects had its own copy of the call.**
`scripts/preview_angle.py` duplicated the `format()` kwargs, so the tool whose entire
purpose is rendering the *real* prompt was rendering a different one -- and the moment
`{proof}` was added it stopped rendering at all: `KeyError: 'proof'`, on the first run
after the deploy. The `ast` check written one commit earlier to catch exactly a missing
kwarg parsed only `scorer.py` and saw nothing.

Seventh instance of one decision in two files, and the sharpest: **the duplicate was
inside the detector.** `Scorer.angle_kwargs` is now the only place those keys are named,
the script calls it, and `test_nothing_else_builds_the_outreach_prompt_itself` walks
`src/` and `scripts/` for any other `_angle_prompt.format(...)` with keywords. Deleted
rather than synchronised -- two call sites that must agree will stop agreeing.

**Resolved: the RoE form's "Free Pilot" is the founding-cohort Snapshot.**
`legal/Rules-of-Engagement.md` lists `☐ Free Pilot ☐ Snapshot ☐ Watch ☐ AI/LLM` and the
homepage explains it -- "we're onboarding a small number of pilot clients at no cost to
build honest, redacted case studies". It needed no new `Offer` literal; it is
`snapshot_free`, which is what the slug said. Flagging it as unresolved was right;
concluding from `makesOffer` that nothing was free was not.

**A lead whose angle is present but *wrong* is invisible to every query in the system.**
The calibration matches, no trigger has moved, and the angle is not blank -- so
`enqueue_stale_scores` is right to report nothing to do, because it only ever re-proses
a lead that has no angle at all. That predicate is correct for automatic reconciliation:
re-decoding an angle that is already fine costs ~18 s each, and a rule that fired on
every wording change would spend the whole queue on cosmetics.

It became a real backlog the day the offer wording changed twice. The corpus now holds
angles written under **three** regimes -- the original free-Snapshot wording, the
paid-only wording that was wrong, and the current one. The middle batch is the awkward
one: it is not a *lie*, so no dispatch guard will ever withhold it; it simply
undersells by omitting a free offer. Nothing asks.

`cindra reconcile --reprose` is the second human override, for the case `--force` does
not reach -- `--force` bypasses the dedupe key, and the problem here is the *predicate*,
not the key. Bounded at `REPROSE_LIMIT` per pass for the same reason
`DEFAULT_RESTALE_LIMIT` is: a whole corpus is hours of decode, and a backfill nobody is
waiting on must never be what a new lead waits behind. The number it prints is what was
queued, not what remains.

Not automatic, and that is the design. **"Some angles are worse than others" is a
judgement about copy that no predicate can make.**

**A repository is the `terminaltrove.com` shape, and `arxiv.org` reached Tier A at 74**
-- the highest-scoring lead in the corpus. `hn_ai_agent` surfaced a paper and the
Extractor read the host it was posted on. Not a publisher writing *about* companies but
a repository hosting other people's work, and it scores well for the same reason a
publisher does: a real organisation, a live site, a working mailbox, and an "AI feature"
on every page. A non-profit run by a university library is also in no ICP this project
has -- the part no trigger could ever have caught. Preprint servers, journals,
`huggingface.co` and `researchgate.net` are in `PLATFORM_HOSTS` now.

**CT certificate count looked like a free enterprise veto and the distribution killed
it.** The proposal was to mirror `inferred_band_from_open_roles` with a large-band
inference from `subdomain_count_ct` -- a public record we already collect, strong at the
extremes, and safe under the same asymmetry (only large bands inferable, a low count
infers nothing). Measured 2026-09-15 across 926 companies:

| certs | companies | sendable |
| --- | --- | --- |
| unknown | 182 | 16 |
| 0-24 | 653 | 60 |
| 25-99 | 64 | 16 |
| 100-299 | 19 | 10 |
| 300-999 | 6 | 0 |
| 1000-4999 | 2 | 1 |
| **5000+** | **0** | **0** |

**The 5000+ band is empty**, on a corpus that unquestionably contains large companies.
An absent bucket is a claim about the measurement before it is a claim about the world --
the same tell as `832 of 833` and `single_source` at 96%. Whatever the cause, a veto
keyed on a signal that is blank for the biggest companies would have been
anti-correlated with what it exists to catch, and would have shipped looking reasonable.

**I then explained the empty bucket with `apple.com` and got the company wrong and the
mechanism half right.** Its actual row:

    apple.com | YouCal - AI Calorie Tracker | productivity software | certs 0

**Not Apple.** An App Store listing canonicalized to the store, the `teamtailor.com`
shape in its purest form -- so the row proved nothing about enterprises, and I had
written a paragraph of confident explanation about a company that was not there.

The truncation mechanism itself turned out to be real, and worse than described.
`defaults.max_bytes` is 900,000, `body` is truncated at it **before** parsing, and one
maintenance pass shows both halves: a fetch logging exactly `bytes: 900000`, and a
crt.sh response for one ordinary company already at 522,908. But the failure does not
leave NULL as I claimed -- `_safe_json` returns None, `growth` returned `(0, 0)`, and
the Enricher wrote that. **A body we could not read was recorded as "this company has
zero subdomains."**

That is the three-valued distinction `evidence.reachable`, `SecurityTxt.present` and
`ThermalWindow.measured` each make, missing from the one field that feeds a size
judgement -- and missing in the direction that matters, because the larger the estate
the likelier the truncation. `growth` returns `None` now; the Enricher's `_subdomains`
already treated None as a failed source, so the column simply stays NULL and nothing
downstream needed changing. **The answer only had to stop being a number.**

Asking for the distribution before picking a threshold cost ten seconds and saved a
shipped mechanism that would silently never have fired. Explaining it without reading
the row cost a paragraph of confident invention in this file.

**`inferred_band_from_open_roles` cannot be tightened either, and the same census says
so.** The proposal was to lower the `40 -> "1000+"` threshold, because `thoughtworks.com`
(~10,000 people) sits at 39 and misses by one role. Measured: 40 already catches
`latitude.so` (40), `supabase.com` (60) and `temporal.io` (71) -- none of which is
within an order of magnitude of 1000. **Open roles measure hiring intensity, not
headcount**, and a funded startup out-posts a consultancy. Lowering the threshold makes
it wrong more often, not less. Left alone, and the asymmetry is why that is survivable:
a wrong "large" only drops a real prospect into the digest, where it is still read.

**The Bengali site checked out, and checking it was the point.** `/bn/` is a faithful
translation: **প্রথম Snapshot ফ্রি**, the same founding-cohort pill, the same four
offers, and the same four prices. Nothing to correct -- which is worth recording,
because the last two times a second source was consulted it disagreed with the config.

What it *did* show is that every card quoted USD. cindrasec.com prices in both and its
currency toggle **defaults to Taka for Bangladesh**, which is 40% of the ICP's
geography. `offer_phrase(offer, country)` returns the Taka phrase for a BD lead --
BDT 15,000-25,000, 5,000-12,000/mo, 40,000-1,50,000, 7,000-15,000, straight from the
site.

`means_bd` is optional and falls back to English rather than failing closed, unlike
`means` itself: a missing translation should cost a card its currency, not take the
config down -- the same call as `surfaces` against `offers`. `snapshot_free` is the live
case, free and so carrying no price at all.

The currency follows the **country, not the language**. `bengali_angle` is written for
BD leads only, but the offer line feeds the English angle too, and a human emailing a
Dhaka company sends one message rather than two.

**`lwn.net` arrived through the evidence resampler, not the near-miss list.** A
maintenance pass fetched `lwn.net/Articles/924577/` as company evidence -- the publisher
shape again, by a route nothing was watching. Worth remembering that
`suppress_platform_companies` and `cindra explain`'s near-miss table both read the
*lead* side; a host can sit in `evidence` for a long time before it becomes a company.

**`country` is NULL for 827 of 908 companies -- 91% -- and three prose mechanisms are
gated on it.** `bengali_angle` is requested only when the country is BD,
`_prose_budget` grants the larger Bengali decode on the same condition, and
`offer_phrase` quotes Taka on it. **A Bangladeshi company on a `.com.bd` domain was
getting an English-only card**: the local-trust wedge switched off for exactly the
companies it exists for.

The extraction rule is not the bug. Rule 6 says `country` only "when the page names a
location", almost no landing page does, and a 4B asked to guess one invents it -- the
stray `EU`, `NU` and `N` values already in the column show what unguarded free text
produces. Same shape as `employee_band` at 1 of 616: the information is genuinely not on
the page.

**The domain says it, and the Scorer already trusted the domain.** `_icp_component`
awards `local_bonus` on `local_tlds` OR `country in {BD, LK, NP, PK}` -- so a `.com.bd`
suffix was good enough to score as local and not good enough to choose the prospect's
language. One decision made in two places with the prose half left empty, and
`test_the_local_tlds_that_score_as_local_can_also_be_named` is the check that they
agree.

`country_from_domain` fills a null and never overrides a stated value, derived at read
time so editing the map re-scores through `scoring_version` -- the `band_from_open_roles`
shape exactly. Only unambiguous ccTLDs: `.io` and `.ai` are deliberately absent, because
inferring British Indian Ocean Territory from a generic suffix would quote the wrong
currency at a real prospect.

**Worth noting how thin the Taka work was without this.** Shipped one commit earlier, it
reached BD companies recommended a *paid* offer -- and of 9 BD companies, the one
sendable lead drew `snapshot_free`, whose Taka and English phrases are identical because
it is free and carries no price. The feature was correct and touched nothing. **A
feature keyed on a column is worth exactly what the column is populated with**, and this
project has now paid that four times: `employee_band`, `description`, `full_name`,
`country`.

**A test dated by a literal fails on a day nobody changed anything.**
`test_crtsh_growth_separates_recent_from_total` pinned `2026-08-10` as "recent" against
a 30-day window; it was true the week it was written and quietly stopped being true a
month later. The timestamp is relative now. A test that fails without a cause is how a
suite gets ignored.

**Discovery is eight-fourteenths Hacker News, and HN is an American forum.** 15 South
Asian companies in 908 -- 1.6% against `icp.yaml`'s `bd_south_asia: 0.4`, a 25x gap --
fed by exactly one template, `hn_south_asia`, a full-text search for "Bangladesh" that
mostly returns *articles about* Bangladesh whose publishers (`dhakatribune.com`,
`thedailystar.net`, `prothomalo.com`) are correctly in `PLATFORM_HOSTS`. That block was
right and it removed most of what the template was finding.

`github_orgs` is the free alternative and it asks a different question from every other
template in the file. The others ask "what happened recently"; this asks "who is
*there*", because the measured problem is not that South Asian companies lack triggers,
it is that they are not in the corpus for a trigger to attach to. GitHub's **users**
endpoint has both qualifiers repo search lacks -- `type:org` and `location:` -- so the
"is this a company" filter that costs `search_repos` a post-fetch filter is free in the
query. The org page is the hit URL and the company's site goes in `raw["homepage"]`,
the same split as the HN comment expansion and for the same reason: cite what you read.
An org with no website is skipped, never guessed at.

**Be honest about what a hit proves here: less than most.** An org account with a site
is a team, not a payroll -- an OSS collective and a university lab both qualify -- and
T12_LOCAL is the weakest trigger in the taxonomy. The value is the doorway: the
Extractor then reads the company's own homepage and the Enricher runs against their real
domain, which is where a T1 or a DMARC gap is actually found. Weights 70 and 62 are
guesses, and `cindra explain`'s yield row is what settles them.

**Measured 2026-09-19, one day in: it works, and the predicted failure is real at 38%.**
29 companies against `hn_south_asia`'s 6 for the life of the project, and the hits are
the best-converting in the file -- 18 hits to 15-16 candidates, ~85%, against 33% for
`hn_ai_agent` and 2% for `hn_south_asia`. That ratio is the whole thesis restated: an
org with a website *is* a company domain, so almost nothing is dropped as a platform URL.
The corpus finally contains SSLCOMMERZ, Brain Station 23, Vivasoft, Themeum, Technext --
real Bangladeshi software companies, not a tic-tac-toe game from Show HN.

And ~11 of the 29 are not prospects: two universities, a foundation, a student rover
team at BRAC, and five individual educators' personal brands. **The note above predicted
exactly this and it should be read as a warning that came true, not as one that was
heeded** -- shipping the template with the risk written down did not stop the risk.

`not_academic` closes the university half, suffix-first for the same reason
`not_government_or_cni` is: `iutoic-dhaka.edu` arrived as "Department of Computer Science
and Engineering", which contains no word any rule looks for. **The nonprofit half is
deliberately left open.** Bare "foundation" would veto a "Foundation Health", which is
primary ICP -- the trap that killed bare "media" in the publication list and the
name-does-not-match-domain rule that `Rover · rtrvr.ai` killed. A rule that cannot be
made safe is left unwritten, and `test_the_nonprofits_the_academic_rule_deliberately_misses`
records the gap so the next person does not think it is closed.

**Two data-quality findings from the same 29 rows, neither of them a rule.**
`templatecookie.com` has `display_name` "LAUTANTOTO" -- an Indonesian gambling brand on a
Bangladeshi template company's domain, so the site is spammed or parked, and a card would
open by addressing them as a casino. And `hasinhayder.com` came out as
"লার্ন উইথ হাসিন হাFRINGদার", with "FRING" spliced into the middle of a Bengali name.
Both would reach a prospect's inbox as written. Neither is caught by anything.

**`ComplianceGate.fingerprint` hashed the veto *config* under a docstring promising the
veto *rules*, and adding `not_academic` proved it.** A new rule is worth -100 to every
university in the corpus and moves neither `excluded_sectors` nor `max_employees`, so
`calibration_version` matched, `enqueue_stale_scores` had nothing to do, and
`mbstu.ac.bd` would have kept its score forever. **That is the identical defect this
function was written to fix -- `exclude_sectors` invisible to `scoring.yaml`'s hash --
recurring one level up inside the fix.** Found by reading the docstring against its own
last line, which is the cheapest audit available and had never been done.

`sorted(RULES)` and the suffix/word tuples are in the hash now, because adding `ac.pk`
to `_ACADEMIC_SUFFIXES` is as much a veto change as adding a sector. A change to a rule's
*body* that touches no constant still escapes; that is stated rather than papered over,
because `inspect.getsource` would invalidate 959 leads on a reworded comment and a
staleness signal that cries wolf is one nobody reads.

It is a separate **engine** rather than a parameter on `github_api` because a source id
is a cache namespace: both would key through `GitHubClient.cache_key`, which builds from
the repo-search URL, so an org plan would read a repo plan's cached body. Two
templates rather than one merged query for the same measurement reason -- a merged query
could never report which half worked.

**`GITHUB_TOKEN` was in `.env.example`, in `Settings`, in the redaction list, in two
`auth_env` lines and in `GitHubClient(token=...)`, and no request has ever carried it.**
Every hop of the chain was built except the last, so GitHub search has run at 60
requests an hour for the life of the project -- fine at one request per template, and
not fine at the 21 an org plan spends. Eighth instance of built-wired-never-connected,
after `digest_pages`, `extend_lease`, `open_roles`, `discovered_by`, `full_name`, the
heartbeat `exiting` flag and `_facts`.

`auth_env` alone was a declaration nothing acted on. `auth_scheme: bearer` is the half
that makes the egress attach the header, and `auth_tokens_for` resolves `GITHUB_TOKEN`
to `Settings.github_token` **by convention rather than by a list**, because a list is a
second place to register a source and therefore a second place to forget one. The
credential is handled exactly like `secret_params` -- on the wire, never in the cache
key (a rotated token would orphan every cached document) and never in a log.
`test_every_bearer_source_has_a_settings_field_to_read` drives the real resolver against
the real registry, and a missing token is info and not an error: unauthenticated GitHub
is slower, not broken, which is every dev checkout.

**CI caught the follow-on defect two minutes after the push and nobody read it.** Run
#139, `check (py3.13)` failed and `check (py3.11)` passed, at 21:17 on 2026-09-15. It
was found four days later by a human running `install_pi.sh` on the hardware. The 3.13
matrix entry exists precisely for this and its comment says so; **a guard that fires
into a log nobody opens is not a guard.** Green locally is not green, and the last step
of a push is reading the run it triggered.

The defect itself: three auth tests each opened a `Store` and none closed it. On 3.13 --
what Debian 13 and so the Pi ships, and what 3.11 does not do -- a collected
`sqlite3.Connection` raises `ResourceWarning: unclosed database`, `filterwarnings =
["error"]` promotes it, and pytest charges an unraisable warning to **whichever test is
running when the collector gets to it**. So a passing assertion was reported as the
failing test, in the same shape as `recent errors` listing jobs that had already
recovered: the line named something true and attributed it to the wrong row.

Both rigs are fixtures now, closing in teardown rather than at the end of a test body --
that line does not run when an assertion fails, which would turn one real failure into
two, in different files. `_counting_rig` in `test_enrich.py` had the same latent shape
and was converted with it.

**And "I cannot reproduce it locally" was wrong.** The dev `.venv` is 3.11 and
`/usr/bin/python3.13` was on the same box the whole time; a throwaway `uv venv --python
3.13` reproduced the failure exactly and then proved the fix against it. Asserting the
limitation cost more than testing it would have, which is the `makesOffer` lesson in a
new costume: **check the second source before describing what it must say.**

**There was never a second worker.** `ps` on 2026-09-19 shows one `cindra work`, one
`cindra serve`, one `feedback-bot` -- and `worker_id` is `f"{nodename}:{getpid()}"`,
which is unique only *within a boot*. So `cindrasec-node:1723` and `:55895` are two
process **lifetimes**, which one `systemctl restart` produces, and reading them as two
concurrent processes was never supported by the field. Several rounds of diagnosis went
into hunting a process that did not exist -- the `apple.com` shape again: a confident
explanation built on a row nobody checked.

The reverse is worse and silent: PIDs are small and predictable right after boot, so the
same number genuinely recurs across reboots and two unrelated lifetimes would collapse
into one id. `worker_identity()` puts `/proc/sys/kernel/random/boot_id` between them and
degrades to the old `hostname:pid` when there is none, because a fabricated constant
would claim every lifetime was the same one -- the failure it exists to prevent,
restated as a default. Nothing matches on the value; it is provenance, so the format is
free to say more. It was assembled in two places in `cli.py`, and
`test_nothing_else_assembles_a_worker_identity` is the guard.

**The 43-minute stall is now unrecoverable, and the reason is recorded here as a
standing cost.** journald is volatile on this box and it rebooted on 2026-09-19, so the
window is gone. Three asks for `ps` and `dmesg` produced a clean answer about *today*
and nothing about the day in question. **A diagnostic that only exists at the moment of
asking cannot answer a question about last week** -- which is the argument for
persistent journald, not for asking a fourth time.

**And the box is rebooting uncleanly.** `EXT4-fs (mmcblk0p2): orphan cleanup on readonly
fs` at mount is a dirty filesystem, which is the third such shutdown. No `i/o error` and
no mmc fault in that boot's ring buffer -- but `dmesg` after a reboot covers only the
current boot, so that is 2 h of evidence about the card and not a verdict on it.
`PRAGMA integrity_check` returned `ok` again on 2026-09-19, three unclean shutdowns in.
Still luck rather than design, and the install script's own warning names the reason:
SQLite WAL on microSD is the documented corruption path.

**`install_pi.sh --install-units` did not deploy a unit change either, which is the
third layer of one defect.** `enable --now` starts a *stopped* unit and does nothing to a
running one -- so on every box past the first install the script copied new units,
reloaded them, printed `units installed and enabled`, and left the worker holding the
code and the unit config it started with. The two layers already recorded above are that
a `git pull` does not change what is running and that `daemon-reload` does not rescue a
unit change; **nobody asked whether the script written to fix that fixed it.** `enable`
for boot persistence and `restart` to pick it up, which is safe because `TimeoutStopSec`
is 960 s and a stage in flight finishes and writes its `exiting` heartbeat. Timers keep
`--now`: each firing is a fresh process, so there is no long-lived import to invalidate.

`test_installing_units_restarts_the_long_lived_ones` reads the script text, for the same
reason `test_systemd_waits_longer_than_a_stage_may_run` reads the unit file rather than
restating its number.

**`/healthz`'s `worker:build` is what makes this self-reporting.** The worker stamps
`source_mtime` on its heartbeat and health compares it against the newest file across
`src/`, `prompts/` and `config/`, so a pull without a restart is visible rather than
inferred. That probe was built for exactly this and is the first thing to read after a
deploy -- the question "is the running process this build" has an endpoint, and it had
been answered by argument three times running.

**The 72 h unattended run is not achievable on this grid, and the gate was measuring
the grid.** Heartbeat coverage per day, read 2026-09-19: `09-19` 6.6 h, `09-16` 23.4 h,
`09-15` 26.9 h, `09-14` 13.8 h — and **17 and 18 September have no rows at all.** Load
shedding, plus the unclean shutdowns already recorded. So a 72 h window covered 12.5 h
of running, and two criteria graded the electricity supply rather than the software:

- `no_silent_unit` counted the outage as a worker gap. A power cut has no `exiting`
  beat, so it read as "the worker died on Tuesday" — a failure no code change can fix,
  which is exactly why `get_throttled == 0x0` was retired and why an unreachable
  prospect was taken out of `no_job_lost`.
- `throughput` divided by wall-clock: 0.7/day for a worker that was managing ~11.5 per
  hour-of-running. **A rate whose denominator is mostly darkness is not a rate.**

**The discriminator was already in the heartbeat again — for the third time.** `worker_id`
carries the boot token as of `worker_identity()`, so a gap the machine rebooted across
is one it was switched off for. Same shape as `exiting=True` sitting unread immediately
before a gap, and as `_facts` building thirteen values while six were passed.

Both exemptions follow the `no_job_lost` discipline exactly: **positive evidence, never
absence of it** — the boot token must be known on *both* sides and different, so a beat
predating the token (None) buys no alibi and a worker that genuinely died while the box
stayed up still fails — and **both numbers are always printed**, the rate beside the
hours it was measured over, because an exemption nobody can see is a weakened gate and
hours lost to power is the number that argues for a UPS rather than for a code change.

`MIN_THROUGHPUT_HOURS` is 6: below that the criterion reports `n/a` and does **not**
pass. One lucky dispatch in a one-hour window is 24/day, so without a floor a run could
prove throughput by being too short to measure it. Short windows are the only ones this
grid allows; short *enough* is still too short, and the report has to say which it was.

**A test asserted the box was cool, on the project that retired that exact gate.**
`test_healthz_and_metrics_answer_over_http` checked `payload["status"] == "ok"`. Every
other test in that file stubs the governor with `_Governor()`; this one cannot, because
`serve` builds the handler and the handler calls `assess` with no thermal argument -- so
it polled the real SoC. Green on a dev box with no `vcgencmd`, red on the Pi at 78 C,
which is the *designed* state. Second test here to encode the dev machine, after the one
that pinned a literal date, and the same family as the 3.13 `ResourceWarning`: **green
locally is a claim about the laptop.**

It now asserts what it is for -- the server binds, answers over real HTTP, and every
check *we* control is `ok` -- while `thermal` and `disk` are named as the machine's.
Verified by forcing a hot governor and re-running: the old assertion fails, the new one
passes, and all ten controllable checks stay green.

**`install_pi.sh` aborted before installing units twice, silently both times.** `set -e`
exits in the middle of a hundred lines of output and the systemd block is near the
bottom, so a failing `make gate` ends the run before the one step the flag exists for --
and both times that read as "the install finished with a test failure" rather than "the
install did not happen". An `ERR` trap now names what did not run. `ERR` and not `EXIT`,
because the units block sets its own `EXIT` trap for a temp dir and a second one would
silently replace it.

**The first real call list, read card by card before sending: 7 problems in 10.** The
guardrails held -- no trigger code, no offer slug, no invented quote, the free-Snapshot
wording correct on every card -- and the *content* is where it went wrong. Worth
recording as the answer to "are we ready to send": the things built to stop us saying
something forbidden work; nothing had yet checked whether we were saying something true.

- **Findcheap cited `chromewebstore.google.com/detail/findcheap/...`** as proof of what
  findcheap.ai announced. `_top_trigger` took whichever evidence row the join returned
  and `PLATFORM_HOSTS` is applied when a *company* is canonicalized, nowhere near an
  evidence URL. **The join has no ORDER BY and SQLite walks it by `evidence_id`**, which
  is a random hex, so the cited URL is a coin flip per company -- which is why it hit
  production and not the first version of the test. Own domain first, then any
  non-platform URL, and a platform link only when it is all we hold, shown rather than
  blanked so the operator can see the proof is weak.
- **Matcha's `why:` says T3_HIRING_SEC and its angle describes a mail-auth gap.**
  `_top_trigger` returns the heaviest trigger and the model opened with a different one,
  so the URL printed beside the text does not support the text. Not yet fixed.
- **Findcheap's angle says "announced an AI feature today"** on a Chrome Web Store
  listing -- the re-dating shape again, by a route `restore_first_observation` scopes
  out (it keys on `content_sha256`, and this evidence is not a page sighting we hashed).
- **Pamir opens "Pamir announced an AI feature 10 days ago; you published..."** -- third
  person then second in one sentence.
- **Pamir and Vigilator both list three triggers**, against a prompt that says at most
  one because "a list of three reads as a report".
- **Tavus, the top lead at 85, still has an empty `description`** -- so the strongest
  card in the corpus is written without knowing what the company does, after both the
  prompt fix and the re-extraction backfill.
- **ThunderPhone's best contact is `legal@`**, which `_best_contact` ranks on status
  and never on what the local part implies.

**Preferring their own page was half the fix, and the next call list said so.**
Findcheap still cited the Chrome Web Store after the change, because that *is* the only
evidence its T1 trigger holds -- the intended behaviour, chosen so a weakly-evidenced
trigger is visible rather than blanked. Except nothing made it visible: an unmarked
store listing renders exactly like the company's own announcement. The card now says
`[!] not their page -- verify before sending`, and a second test asserts the marker is
absent when the URL *is* theirs, because a warning on every card is one nobody reads.

**Every card opens with the weakest trigger it has, and the cause is structural.** The
prompt says "the first one is the reason to write" and the list is ordered by scoring
weight -- so T1_AI_SHIP at 30 always leads, and T1_AI_SHIP is "you announced an AI
feature", which this file already calls true of half the internet. Both previews read in
detail show the same burial:

- **Tavus** carries a funding round from *3 days ago* under an AI feature from 3 weeks
  ago, and the card never mentions the funding.
- **Traccia** carries T10_VENDOR_PRESSURE -- *a customer asked them for a pentest
  report*, the highest-intent signal in the taxonomy -- and the card leads with the AI
  feature and a DMARC gap instead.

**Weight answers "how much is this lead worth"; an opening line needs "what does this
prospect already know they need".** Those are different questions and one ordering is
serving both. Not changed here: re-ordering what the prompt sees invalidates prose for
the whole corpus through `prose_version`, and picking the opener is a judgement about
copy -- but the current order is a *default nobody chose*, not a decision.

**`description` was empty on both companies previewed** -- Tavus at 85 and Traccia at 67
-- after the prompt fix and the re-extraction backfill. Two of two is a sample, not a
proof, but it is the second time this column has been declared fixed.

**And one quote handed to the model was scraped UI chrome**: Traccia's second verified
snippet is `"Unified Agent Registry traccia · governance console $ traccia agents list
142 agents · 3 environments Customer Support ·"`. It passed the literal-match rule
because it genuinely appears on the page -- the rule proves *we did not invent it*, and
was never a claim that it reads like a sentence a human wrote.

**The Bengali angle is not fit to send, and no guard fixes the reason.** Read as a
Bengali speaker rather than as JSON, the dispatched cards are machine-translation
garbage in the one language whose whole purpose is sounding local:

- `স্বাধীন রো ই সঙ্গে` -- "RoE" transliterated phonetically as *ro-i*, meaningless.
- `এই আই` and `এই-এলএম` -- "AI" and "AI-LLM" spelled out letter by letter.
- `এটি আপনার জন্য মুক্ত হবে` -- **মুক্ত means *liberated*, not *free of charge*.** The
  word for that is বিনামূল্যে. The free-offer claim, which two commits have now been
  spent getting exactly right in English, is simply wrong in Bengali.
- `যার গুরুত্ব বিভক্ত হয়েছে` for "with gaps in it" -- not a sentence anyone would write.

**This is a capability limit, not a bug.** A 4B writing marketing Bengali produces text
that reads as foreign to a native speaker, which is the precise opposite of the
local-trust wedge `T12_LOCAL` and Taka pricing exist to build. `PROSE_MAX_TOKENS_BENGALI`
is about *length*; nothing in this project has ever checked the Bengali for *quality*,
and every test of it asserts a stub. **Do not send a Bengali card until a native reader
has approved the wording** -- and the honest fix is a human-written template with slots,
not a bigger budget.

**Two things in those cards were mechanical, and are now guarded.**
`shovels.ai` ended in twenty-odd dandas -- `। । । । ।` -- a 4B degenerating into
repetition at the end of a decode rather than truncating, so no token budget touches it.
And `moza · jigjoy.ai` opened *"You announced an AI feature today; you published code
using an LLM agent framework today"* on a card whose own trigger list reads **16d ago**:
the model invented a date the prompt had supplied correctly, and the card contradicts
itself where the reader can see it.

The prompt already forbids both. It says "Where there is no time, do not invent one ...
'today' would be wrong", and it says so because this had happened before. **A rule in the
prompt is a preference; a rule in the code is a rule** -- the same conclusion `means`,
the offer slugs and the free-offer flag each reached by the same route. Both guards are
in `prose_version`, because widening what counts as unusable makes an angle the old
build accepted one this build must re-ask for.

The recency guard counts **dated triggers only**: a derived trigger is a standing fact
re-derived from a fresh lookup and carries no date, so it can neither support a "today"
nor refute one, and counting it would discard an angle for disagreeing with a claim
nothing made.

**The denylist gained four hosts in one session and one maintenance pass found five
more shapes it does not cover.** `phoronix.com` and `theconversation.com` (publishers --
the latter fetched as company evidence for an article about *Star Trek*),
`reactorcore.itch.io` (a game host), `blog.lukesalamone.com` (a personal blog) and
`bollywoodle.app` (a Bollywood guessing game). Every host added this session was added
*after* a card had already been dispatched.

`scripts/name_mismatch.py` is the measurement this file has asked for for weeks and that
four list edits in one session were spent avoiding. It buckets the corpus by
`name_similarity(display_name, domain-stem)` and prints the **sendable count per band**,
because that is the question -- a band that is mostly junk is worth quarantining for
review, and a band holding real leads is not. It rejects nothing.

**Measured 2026-09-19 over 1029 companies, and the answer is no.** The rule this file
has been circling for weeks does not work:

| band | companies | sendable | share |
| --- | --- | --- | --- |
| 0-29 shares almost nothing | 98 | 28 | **29%** |
| 30-49 faint | 52 | 20 | 38% |
| 50-69 partial | 74 | 24 | 32% |
| 70-100 clearly the same name | 805 | 309 | 38% |

**The bands barely differ, and the worst one is 29% against a 38% corpus baseline.**
Name-domain similarity is very nearly uncorrelated with lead quality. Quarantining the
bottom band would put **28 real leads** behind manual review to catch some publishers.
Same shape as the CT-certificate veto: the distribution killed a mechanism that would
have shipped looking reasonable, and asking cost ten seconds.

`Rover · rtrvr.ai` scores 60, in the *partial* band. So even the counterexample this
file recorded was aimed at the wrong place -- the hazard at the bottom is **acronyms**
(`Electronic Frontier Foundation` against `eff.org` is 19.4 and perfectly honest), not
Rover. Both the rule and the objection to it were wrong; only the measurement settled it.

**What the run did find, by making a human read 32 rows:**

- **`deepgram · fly.dev` and `Zhin.js · js.org`** -- the *merging* shape, which is worse
  than a publisher. Fly.io's shared app domain and a free-subdomain service for JS
  projects, so every company hosted there collapses onto one row. `teamtailor.com`
  exactly. In `PLATFORM_HOSTS` now, with `apnews.com`.
- **`null · bopbook.com`, industry `null`** -- the literal four-character *string*,
  which is why it passed the `display_name IS NOT NULL AND <> ''` filter. A card would
  greet them as "null". Not yet fixed.
- **Personal domains are their own shape**: `Ablaut · romainflorentz.com`,
  `Sourcery · sameerhimati.com`, `ODYSSEY EXPLORER · georgemasto.com`. Someone's own
  name as the host, which is a person and not a B2B prospect -- and no host list can
  enumerate those.

So the list stays the patch, and the general rule is still unfound. What changed is that
it is now unfound *for a measured reason* rather than an argued one, and the next
proposal has a baseline to beat.

**`PLATFORM_HOSTS` is two lists with two different membership rules, and only one of
them can be finished.** The publisher half has no closed rule -- that is what the
`name_similarity` measurement above just confirmed -- so it grows one dispatched card at
a time and always will. The other half does: **does this host put unrelated
organisations behind a path or a subdomain?** That is answerable for any candidate.

It is also the *dangerous* half. A publisher produces one bogus company row; `pypi.org`
would collapse every Python package's author onto a single one, which is rung 1 doing
its job to data that should never have reached it -- the `teamtailor.com` case this
project already paid for.

That half was half-enumerated: `vercel.app`, `netlify.app`, `github.io`, `herokuapp.com`
and `readthedocs.io` were listed, while `fly.dev`, `js.org`, every package registry and
`doi.org` were not -- and the gaps were arriving through the evidence resampler fetching
`pypi.org/project/altastata` and a DOI that redirected into a Nature paper `nature.com`
was already blocked against. Finished in one pass, with the bound pinned beside it:
`traccia.ai`, `sslcommerz.com`, `brainstation-23.com` and a `.com.bd` domain must still
resolve, because a wrong entry here silently deletes every lead on that host.

**A competitor's blog is a third shape and nothing catches it.**
`ebuildersecurity.se/en/cyber-news/ryde-data-breach-...` was fetched as company evidence:
a Swedish security vendor's news post about somebody else's breach. `not_a_competitor`
reads `industry` and `display_name`, and the extraction describes **Ryde** -- the
breached company -- so the competitor rule never sees a competitor. The TechCrunch defect
pointed at a rival.

**`enqueue_stale_scores` took a `limit` and neither caller passed one.** Ninth instance
of built-wired-never-connected, and this one had its own evidence sitting in the query:
the `ORDER BY` puts genuinely new triggers first, recalibrations behind them and angle
repairs last, which **only means anything with a limit** -- take every row and the order
decides nothing but the sequence in which the whole corpus is queued. The comment above
it states the problem outright ("a config edit makes the whole corpus stale at once, and
at ~18 s a lead that is hours of queue -- long enough that a funding round found this
morning would sit behind it") and then nothing bounded it. `REPROSE_LIMIT` existed for
`--reprose` only, which is the path that needs it least.

It cost a real outage. Adding `not_nonprofit` moved `calibration_version`, one
`reconcile --force` put **2389 score jobs** in front of a worker doing ~13 s a job on a
box powered six hours a day, and every completing enrichment enqueues another -- so
`pending` held at exactly 2947 across two readings while `done` climbed by 61. Four to
seven days in which no newly harvested lead could reach a card.

`DEFAULT_RESCORE_LIMIT` is 50, same number and same reasoning as `DEFAULT_RESTALE_LIMIT`:
at 30-minute timer intervals that is ~100/hour, which the worker keeps up with, and the
ordering means a genuinely new trigger sorts to the front of every pass. The test is on
the **call sites** rather than the function, read out of `cli.py` with `ast`, because the
function was never the part that was broken.

**And `--force` conflates two things that cost differently.** It re-runs the arithmetic
*and* re-decodes the prose, when a compliance-rule change moves only the first. 3100
prose calls at ~18 s to apply a veto that rejects a handful of companies and repair
perhaps fifty angles is the wrong trade, and there is no flag for "re-score without
re-prosing". Recommending `--force` after a veto change was a mistake for that reason,
not because the rescore was unnecessary.

**The queue drained and nothing was lost.** 2026-09-21, two days after
`DEFAULT_RESCORE_LIMIT` shipped: `pending` 2947 -> **28**, `done` +2839, `in_flight` 0,
and **`dead` unchanged at 32** across the whole run. The bound was the fix -- reconcile
had been re-queuing the corpus every 30 minutes faster than the worker could drain it.

**And the cards it produced showed a worse defect than the backlog.**
`CallFirst · callfirst.app` and `Gleamit · gleamit.app`, two unrelated consumer phone
apps, both reached Tier B at 55 with **byte-identical trigger sets**: T10_VENDOR_PRESSURE
0.70, T5_COMPLIANCE 0.70, T8_HYGIENE_GAP 0.80, all "0d ago". Two different companies
cannot honestly produce the same three triggers at the same three confidences.

`extractor.py` explained it in one line: when the model named no trigger, **the
Harvester's `targets` stood in.** `hn_pentest_pressure` searches "SOC 2" and declares
`[T10_VENDOR_PRESSURE, T5_COMPLIANCE]`, so every page it surfaced that the model declined
to label got both -- **the highest-intent trigger in the taxonomy, asserted by a config
row.** A card would have told CallFirst "you have been asked by a customer for a pentest
report" on the strength of a page about blocking social media apps.

**The evidence gate did not catch it, and the reason generalises.** `trigger_codes =
claimed if evidence_ids else []` asks whether *any* snippet verified, never whether a
snippet supports *this* trigger -- evidence and claims are two lists joined many-to-many,
so the literal-match rule proves only that the quote is real. For a model-named trigger
that is tolerable: it read the page and named both. For a fallback **nothing read the
page at all**, so the one check standing between a template's intent and a claim about a
stranger was structurally incapable of noticing.

The fallback is gone. A page the model cannot label now has no trigger and the lead is
dropped by "no evidence, no lead" -- the correct outcome, and what the fallback was
quietly preventing. `0.7` confidence is hardcoded in the Resolver for every trigger, so
the matching `0.70`s were a constant, not a coincidence: **the same tell as `832 of 833`
and `single_source` at 96%, this time visible on the face of a card.**

**A test dated by a literal failed again, for the third time, and not from this change.**
`test_re_reading_a_page_does_not_re_date_what_the_company_did` pinned `decays_at =
2026-09-20` as "comfortably in the future"; on 2026-09-21 the seeded trigger was
*expired*, the Resolver took its other branch, and the suite went red on a day nobody
touched the code. Both dates are relative now. Confirmed independent by stashing the
change and watching it still fail -- after `test_crtsh_growth_separates_recent_from_total`
and the health test that asserted a cool SoC.

**The card is the first thing a human reads and it was written for the machine.** Three
defects, all in what the Discord embed *says* rather than in what reached it:

- **`triggers.confidence` is provenance, not probability, and the card printed it as a
  number.** Exactly two values are ever written -- 0.7 by the Resolver for what a 4B
  read off a page, 0.8 by the Enricher for a public record it looked up itself, whose
  own comment said so. Neither varies by company. Rendered `T1_AI_SHIP 0.70`, that is a
  constant wearing a measurement's clothes, and it reads as "70% sure about *this*
  claim". **The same tell as `single_source` at 96% and `832 of 833`**, this time on the
  face of a card. `PAGE_READING_CONFIDENCE`/`PUBLIC_RECORD_CONFIDENCE` name the two, and
  the card prints "read off their page" or "public record".
- **The evidence label was the `source_id`**, so the field read `company_site ·
  dns_public` and the operator could not see whose page they were about to open. That is
  the Findcheap defect on the card the human sees *first*: the worklist learned to
  prefer their own domain and to mark a borrowed one, and **the fix stopped at the call
  list.** The card labels by host now and marks a platform URL inline. Only a platform
  URL -- `crt.sh` and `dns.google` are not their page either and are honest citations,
  and a warning on every card is one nobody reads.
- **A derived trigger was dated.** `T8_HYGIENE_GAP · 0d ago` on a domain whose DMARC has
  read `p=none` for years, because `observed_at` is when *we* looked. `DERIVED_TRIGGERS`
  has existed since the prose learned this; the card never asked.

And `T10_VENDOR_PRESSURE` meant nothing to the person deciding whether to send. `means`
was built for exactly that and reached only the prose prompt -- **the position the codes
themselves were in before `means` existed.** A row now reads end to end:
`` `T10_VENDOR_PRESSURE` a customer has asked them for a pentest report · read off their
page · 3d ago ``.

`CardData.triggers` was a bare `(code, confidence, when)` tuple, and the anonymity is
most of why: a positional triple whose middle element is a constant about provenance is
one nobody re-reads. It is a `TriggerLine` with named fields, and
`test_nothing_writes_a_trigger_confidence_as_a_bare_number` parses both writers with
`ast` and fails on a float literal in a `triggers` INSERT -- the write site, not the
constants, because a literal is how a third value enters the column with no phrase for
it. Every new test was checked against the old renderers first.

`scripts/preview_card.py <domain>` is the sibling of `preview_angle.py`, and the reason
is the same one this file keeps writing down: **all three card defects were obvious in
one rendered card and invisible in `_fmt_triggers`.** Before it, seeing a card meant
waiting for a score job to reach the front of the queue and a dispatch to fire -- which
on a drained queue never happens at all, so a format change could not be checked on the
box it was deployed to. It renders the real embed through the real `build_card` and
posts nothing: the webhook carries a transport that raises on any request, **and**
`webhooks` is non-empty, because `{}` tests falsy and `__post_init__` reads the
configured secrets instead. A tool for *reading* cards must not be one keystroke from
sending one.

`test_the_card_preview_renders_the_real_card` drives the script's own `main()` and
compares against `build_card`, because `preview_angle.py` -- built for exactly this --
stopped rendering at all the day `{proof}` was added, and that was found by running it
rather than by a test.

**Writing its test leaked a connection, and 3.13 named the wrong test four times.**
`test_egress.py::test_the_domain_budget_survives_a_new_client` on one run,
`test_the_configured_cap_applies_without_anyone_registering_a_guard` on the next three,
each passing in isolation, 3.11 green throughout. **Identical to the three unclosed
`Store` connections**, and the tell is the same: *the failing test moves between runs*.

The cause was not what I first said it was. I blamed the script's unclosed
`httpx.AsyncClient`, fixed that, and the suite stayed red -- the warning names
`sqlite3.Connection`, which I had not read. `PYTHONTRACEMALLOC` pointed at the *rig's
own* store, opened in the fixture: the test patched `store.close` to a no-op so `main()`
could not close a store the rig still needed, and **`monkeypatch` undoes after `rig`
tears down**, so the rig's `store.close()` called the no-op and closed nothing. The
no-op was unnecessary in the first place -- `Store.close` clears `_conn`, so the next
read reopens. Third time in this file that a confident explanation was written before
the row was read, after `apple.com` and the second worker; the traceback cost one
command.

**The first five cards rendered through the preview showed three defects the preview
was built to find, and the worst one was that the guard withheld the compliant text
and shipped the other.**

Every one of the five logged `card_prose_withheld_free_claim` against its English
angle -- correctly: they were written under the old free-everything wording and their
offer is `ai_llm_assessment`, a $2k-8k engagement. So the English field was dropped
and the card went out carrying **only** the Bengali, which said
`আমি একটি মুক্ত পরীক্ষা প্রস্তাব করছি` -- the same promise, in the one language
`_FREE_CLAIM` could not read. **A guard that covers one language on a card that
carries two is not a guard**, and the failure direction is the cruel one: it removed
the safe text and kept the dangerous text.

- **`\b` is meaningless in Bengali, so the obvious patch silently does nothing.**
  Vowel signs are category Mc/Mn and are not word characters, so `\bবিনামূল্যে\b`
  never matches the word itself -- it *ends* in one -- while `\bমুক্ত\b` matches
  happily inside `মুক্তিযুদ্ধ`. Word boundaries give the false negative **and** the
  false positive. The terms are substrings on purpose, and the asymmetry is the whole
  argument: a wrong match costs a card its angle, a miss puts a price commitment in a
  prospect's inbox.
- **The Bengali angle is now withheld entirely, behind `DISPATCH_BENGALI_ANGLE`,
  default off.** This file has said "do not send a Bengali card until a native reader
  has approved the wording" since the first batch was read, and nothing implemented
  it. A rule in a document is a preference; **a rule in the code is a rule** -- the
  fourth time that sentence has been written here, after `means`, the offer slugs and
  the recency guard. A flag rather than deleting the field, because the honest fix is
  a human-written template with slots and this is the line that ships it.
- **The prompt asks for Bengali "only when country is BD" and all five companies are
  American.** `country` is NULL for 91% of the corpus, so the condition has nothing to
  test against and a 4B fills the field regardless. Same shape, same lesson.

**`arxiv.org` rendered `⚖️ Compliance: VETO` on a Tier A card at score 74 and nothing
refused to send it.** `compliance_passed` was read, printed and never acted on: the
gate quarantines a vetoed lead while `_upsert_lead` still stores the tier the
arithmetic computed, and every dispatch predicate was about *tier*. The card said the
right thing in a field nothing read.

`_blocked` asks three genuinely different questions -- the stored verdict ("was this
allowed when we scored it"), and the quarantine and suppression tables ("may I write to
them *now*"). The tables change without moving a lead row: `suppressed_domains` is
deliberately outside `calibration_version`, so a suppressed company keeps its tier
forever and no rescore is ever coming. `worklist` already joined both live and says so
in its own comment; **the Dispatcher is the other reader of the same question and
joined neither**, which is how a suppression stopped the operator's call list and not
the Discord card.

And it is asked on **both** routes. `send_digest` is the other path to Discord and Tier
C is the larger population, so a gate covering only the per-lead stage would have left
most of the corpus unguarded -- the `digest_pages` shape again, from the gate side.
Against the old code that test reports `digest_sent pages=1 sent=1` for a vetoed lead.

**Nothing covered any of this, because every renderer test builds `CardData` directly**
and the four defects all live in `_card_data` and `prepare`. 968 tests passed over a
card that was shipping a free $2k-8k engagement in Bengali.

**`cindra reconcile --reprose` reported `queued 0` and would have done so forever.**
I recommended it as the repair for the angle backlog; it could not see a single one of
the leads it exists for.

`_upsert_lead` writes `prompt_version=excluded.prompt_version` **unconditionally**,
while `outreach_angle` is preserved when the incoming one is empty. Both halves are
deliberate and both are right: the angle survives a run where the model was
unavailable, and the stamp moves so a lead whose prose *fails* stops asking rather
than being re-queued on every reconcile forever. Together they mean a row can hold an
angle from one build and a stamp from another -- so **`prompt_version` records the
build that last touched the row, not the build that wrote the angle**, and there is no
way to recover the second fact from the first.

`--reprose`'s entire predicate was `prompt_version != prose_version()`. The 2947-job
rescore that drained on 2026-09-21 stamped every lead current while keeping its old
angle, so the override matched nothing -- permanently, until `prose_version()` itself
changes. **One column answering two questions, and the unconditional write destroyed
the one the override needed.**

Nothing else could reach them either, which is the part worth remembering: the offer
wording that made those angles wrong moves **neither** hash. `offers` is outside
`ScoringConfig.fingerprint` on purpose ("it changes prose, never a number") and
`prose_version()` hashes prompt files and the token constants. Three mechanisms, none
of which moved -- the same shape as `single_source` inspecting only the top trigger,
where every part worked and nothing owned the question.

`leads.angle_version` (migration 0009) is stamped by the **same `CASE` as the angle
itself**, so it moves exactly when the angle does. `prompt_version` keeps its meaning
and the loop prevention built on it is untouched. NULL on every existing row is what
makes the backlog reachable on the first pass and what makes successive passes walk
forward: a rewritten angle carries the current version and drops out of the next
selection.

`test_the_stamp_that_reprose_reads_moves_only_with_the_angle` drives the real Scorer
twice -- once with a model, once without -- and reads the columns back. Against the old
code it fails with `assert '97545c08d79b25a1' == 'older'`, which is the production
defect exactly. **Its predecessor hand-wrote `prompt_version=prose_version()` into a
seeded row**, a pair the pipeline produces only by the bug, so it passed against broken
code. Sixth instance, after `discovered_by`, `enqueue_stale_extractions`, the HN mock,
the free-offer flag and `test_a_rescored_corpus_reports_current`.

**And the preview showed an unsendable card as though it were sendable.** `arxiv.org`
renders a complete Tier A card at 74 with `Compliance: VETO` in a field that looks like
every other field; the Dispatcher now refuses it and nothing told the reader that.
`preview_card.py` prints `### WOULD NOT BE DISPATCHED -- compliance veto` above the
card, because an instrument for deciding what to send has to answer the question it is
being asked.

**`queued 0` had a second way to mean nothing is wrong, and the message invited it.**
`--reprose` reports what a pass *newly* enqueued; run it again before the worker drains
and the same rows are selected, every dedupe key already exists, and it prints 0 --
which is exactly what the defect above printed for a week. "re-run to continue" was
advice to produce that second run.

`reprose_backlog` prints beside it: `42 queued ... 871 lead(s) still carry an angle
from an older build`. Same discipline as `no_job_lost` printing the unreachable count
beside the lost one and `throughput` printing the hours it was measured over -- **one
number cannot tell "already queued" from "nothing to do"**, and which it is decides
whether you wait or go looking for a bug. It is also the size of the job: ~18 s of
decode a row, so a corpus-wide backfill is hours on this box, and the operator should
get that number before the eighth pass rather than after it.

The count and the selection come from **one** `_stale_rows`, because a count that
drifted from the work the command queues would be a confident wrong number -- and a
confident wrong number is how `832 of 833` was read as a finding about the corpus.
`test_the_backlog_and_the_selection_cannot_disagree` asserts the predicate has exactly
two readers, by name, out of the source.

**The guard withheld the text the config was rewritten to produce, and it silenced
98% of the corpus.** Measured 2026-09-22 on the Pi:

| offer | leads | angle says "free" |
| --- | --- | --- |
| `ai_llm_assessment` | 242 | **238** |
| `watch` | 38 | **37** |
| `snapshot_free` | 119 | 47 (allowed) |

**275 of 280 paid-offer leads**, and not one of them a real defect. Tavus's stored
angle, read out of the database rather than guessed at:

> I'd like to run **a free first attack-surface Snapshot**, and an AI/LLM security
> assessment after it covering prompt injection, data leakage, agent tool abuse and
> the MCP tool surface **($2,000-8,000, 2-5 days)**

That is the config phrase reproduced faithfully, price intact, nothing given away.

**Two commits did this to each other and each was right alone.** `offers` deliberately
names the free first Snapshot *inside* the paid phrase, so the ask stays small without
giving the engagement away. The dispatch guard was deliberately narrowed to key on the
lead's own `recommended_offer`, because "is anything free anywhere" stopped
discriminating the moment one genuinely free offer existed. Together: the config puts
"free" in the text and the guard withholds the text for containing it. **98% incidence
is the shape of a constant, not a discriminator** -- the fifth time that tell appears
here, after `single_source` at 96%, "no escalation backend" at 100%, `832 of 833`, and
the trigger confidences on the face of a card.

**The price is the discriminator.** Allowing every "free" on a paid offer hands the
original defect back -- *"I'd like to run an AI/LLM assessment for you, free"* is
exactly what this guard exists to stop. What separates the two is not the word: it is
whether the price survived. A paid phrase always names one, the honest angle carries
it, the dangerous angle drops it. `_free_claim_is_backed` asks the config for the
phrase it actually handed the model and then requires a currency amount in the output;
it fails closed when the phrase promises nothing free, because a "free" there is
invented and an invented one has no excuse.
`test_every_paid_offer_names_a_price_the_guard_can_find` is the load-bearing check --
a paid phrase with no price would make every angle for that offer unpublishable,
silently, exactly the way the corpus just went quiet.

`test_the_real_angle_from_the_corpus_is_publishable` uses Tavus's stored text verbatim
and fails against the old guard with the production log line,
`card_prose_withheld_free_claim matched=['free']`.

**And the backlog was never real, so `--reprose` was the wrong prescription twice.**
The angles were already correct; re-prosing ~900 leads would have spent ~4.5 hours of
decode rewriting correct text into identical correct text and watching it be withheld
again. The observation that settled it was cheap and I nearly skipped it: the 42 jobs
ran, `done` climbed by 193, and the warning was byte-identical on all five cards. **A
repair that changes nothing visible is evidence about the diagnosis**, not a reason to
run it again -- and the previous note in this file, recommending exactly that, is the
mistake it should be read as.

**Known hardware gaps:** root is on microSD (no NVMe present), and sustained
inference reaches ~80 C with the fan at ~6000 RPM. Two unclean shutdowns have already
put 13k NUL bytes in the JSONL log; `PRAGMA integrity_check` on the database still
returns `ok`, which is luck rather than design. journald is volatile on this box, so
nothing survives the reboot you would want to investigate.
