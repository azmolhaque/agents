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
cindra pipeline                          # harvest -> extract -> resolve, one pass
cindra work --kinds harvest.query,extract.candidate,resolve.company [--drain-inflight]
cindra status                            # candidates, companies, live triggers
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

**Phases 0-3 code complete; Phase 3's accuracy gate still needs the Pi.**

The pipeline runs end to end: `Scout -> Harvester -> Extractor -> Resolver`, three
durable job kinds driven by one async worker loop. Every stage is two-phase --
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

**Known hardware gaps for Phase 7:** root is on microSD (no NVMe present), and sustained
inference reaches ~80 C with the fan at ~6000 RPM. Neither blocks Phases 2-6.
