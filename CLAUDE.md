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
`git pull && cindra db migrate && sudo systemctl restart cindraleads-worker cindraleads-health`.
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

**Known hardware gaps:** root is on microSD (no NVMe present), and sustained
inference reaches ~80 C with the fan at ~6000 RPM. Two unclean shutdowns have already
put 13k NUL bytes in the JSONL log; `PRAGMA integrity_check` on the database still
returns `ok`, which is luck rather than design. journald is volatile on this box, so
nothing survives the reboot you would want to investigate.
