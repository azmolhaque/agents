# CindraLeads — Architecture Review & Build Plan (Phases 0–8)

## Context

Cindrasec needs a self-hosted pipeline that answers one question daily: *which specific
organizations have a dated, evidenced reason to buy a Snapshot, Watch, or AI/LLM assessment —
and who do I talk to?* It must run unattended on a Raspberry Pi 5 (16 GB), do the bulk of
inference locally, never touch a prospect's infrastructure beyond public-record lookups, and
emit only schema-valid, deduplicated, evidence-linked lead cards to Discord.

This document is the plan-mode deliverable required by §0(b)–(d) of
`CINDRALEADS_MASTER_PROMPT.md`: a restatement and critique of §4, plus a phased build plan
with a file manifest and acceptance test per phase. Nothing is built until this is approved.

**Repo note.** The master prompt targets `~/cindraleads`. We are in `/home/user/agents`
(repo `azmolhaque/agents`, branch `claude/cindraleads-architecture-plan-ywog5j`), which is
empty — no commits. I will build the project at the repo root and commit `PLAN.md` (this
document) as the first artifact.

## Decisions locked after Phase 1 measurement (2026-08-15)

Three more deviations, forced by measurement rather than taste: two from the 24-page
benchmark on the Pi (`docs/BENCHMARKS.md`), one from the actual SerpAPI quota. These
override the master prompt where they conflict.

| # | Decision | Rationale |
| --- | --- | --- |
| 5 | **Definition of Done drops "median wall-clock per lead < 90 s"** and replaces it with a throughput criterion: *sustains 40 leads/day end to end, with the queue draining faster than it fills.* | One extraction alone is **64 s** measured (39 s on `llama3.2:3b`), and a lead needs several LLM calls. The 90 s target is off by 2-3x and no tuning closes it — only different hardware would. Throughput is the thing that actually matters and it is comfortably reachable: 200 docs/day is 3.6 h of inference on the 4B, 2.2 h on the 3B. |
| 6 | **Thermal governor pauses inference at 84 C, not 78 C.** Only *under-voltage* forces the critical single-worker state; thermal capping does not. | The Pi climbs for ~5 pages then **plateaus at 80-82.3 C** and holds, with 100% success, zero timeouts and no upward latency trend (page 5: 69.5 s, page 20: 62.5 s). It is an equilibrium below the 85 C hard limit. Pausing at 78 C would have stopped a machine that was working; stopping is far worse than the ~8% clock reduction the Pi applies itself. Under-voltage stays critical because it is a power fault, risks corruption, and does not resolve by waiting. |
| 7 | **SerpAPI is not the spine.** Free sources do discovery and enrichment; SerpAPI is rationed to ~7 queries/day under the budget guard, like the cloud LLM tier. | The account's free plan is **250 searches/month (~8/day)**. With hourly harvests that is 0.3 queries per harvest — SerpAPI cannot carry the pipeline. Upgrading does not resolve it either: the paid tier is ~**$75/month against a $15/month** Definition-of-Done budget, 5x over. The fix is the discovery/enrichment split: *discovery* (finding unknown companies) is genuinely hard and worth a credit; *enrichment* (deepening a known company) is almost entirely free. A company's Greenhouse, Lever or Ashby board is public JSON with no key, and those carry T3/T4/T5/T11 — the high-weight hiring triggers SerpAPI was meant to find. Add GitHub, HN Algolia, crt.sh, RDAP, RSS, EDGAR and CISA KEV and every trigger in the taxonomy is reachable for $0. |

Phase 2's acceptance test changes to match #7. "50 real SerpAPI queries produce >= 200
raw docs" would spend a fifth of the monthly quota on a test that could then only be run
once, which makes it useless as a regression. It becomes: **50 queries across the free
sources produce >= 200 raw docs; an identical second run makes 0 network calls; the
budget guard halts at its cap** — plus a 2-3 query SerpAPI smoke test to prove that
integration works.

Phase 7's acceptance criterion changes to match #6: **"no thermal throttle event"** is not
achievable for sustained inference on this hardware and was never the right question.
It becomes *stays below 85 C, no under-voltage, throughput within 20% of the Phase 1
baseline over 72 h.*

## Decisions locked (answered 2026-08-14)

These four deviate from the master prompt and are now settled. Rationale in Part 2.

| # | Decision | Effect |
| --- | --- | --- |
| 1 | **Discord gateway bot** for feedback ingress | New `discord.py` dep, new `cindraleads-feedback.service`, bot token env var, `dispatch_log.discord_message_id` column. CLI built in Phase 0 anyway as the test seam. |
| 2 | **Two models, no router** — `qwen3:4b-instruct` + `bge-m3` | ~4.0 GB, fits `OLLAMA_MAX_LOADED_MODELS=2` exactly. `qwen3:1.7b` dropped from §3. |
| 3 | **Dedupe rungs 1/2/4 ship; rung 3 gated** | `sqlite-vec` + `company_vectors` in schema day one, `bge-m3` not pinned. Enable rung 3 only if the <2% dupe target is missed. |
| 4 | **`anthropic` SDK in prod, not `claude-agent-sdk`** | −100 MB, no Node on the Pi. `claude-agent-sdk` stays in the `[dev]` extra only. |

---

## Part 1 — §4 restated in my own words

Two separate things exist and must never be conflated:

- **Dev-time.** Claude Code, with the eight `cindra-*` MCP servers wired into `.mcp.json` so I
  can poke each tool by hand while building.
- **Prod-time.** A single Python asyncio worker on the Pi, woken by systemd timers, importing
  those same eight tool modules *as ordinary libraries*. No Claude Code, no MCP transport, no
  Node. One implementation, two entry points — the MCP server is a thin `FastMCP` wrapper
  around a plain Python module, and the pipeline imports the module directly.

The pipeline is ten stages in a fixed line. Each consumes a typed object and produces a typed
object; nothing but a validated Pydantic model crosses a boundary:

**Scout** turns the ICP and trigger taxonomy into a batch of diverse search queries, remembering
which query shapes historically yielded Tier A and retiring the dead ones. **Harvester** is the
only component in the entire system allowed to make an outbound network request — it owns the
budget guard, robots.txt, backoff, and the content-addressed cache. **Extractor** runs the local
LLM under a JSON Schema to turn a raw document into a `Candidate`, dropping any claim that
doesn't carry a source URL and a snippet. **Resolver** collapses candidates onto a canonical
company keyed by registrable domain, merging rather than duplicating. **Enricher** fans out
passive OSINT in parallel, tolerating any individual source failing. **Validator** re-checks
everything against strict Pydantic, verifies emails without probing, HEAD-checks evidence URLs,
and recomputes trigger decay — killing leads whose evidence has 404'd. **Scorer** computes
CindraScore as pure arithmetic and calls the LLM only to write prose. **ComplianceGate** is §12
as executable code and can veto anything, logging a reason. **Dispatcher** builds Discord embeds,
routes by tier, enforces idempotency, and dead-letters on final failure. **Critic** runs weekly,
samples dispatched leads, recomputes precision from human feedback, and *proposes* tuning
changes that a human applies.

State is one SQLite file in WAL mode carrying FTS5 and sqlite-vec. The job queue is a table in
that same file, not Redis or Celery — because power loss on a Pi is a *when*. Every stage
transition is a row update inside a transaction, and on boot the worker reclaims orphaned work
with `WHERE status='in_flight' AND lease_expires_at < now()`. Telemetry is structlog JSON on
disk, with ERROR-and-above mirrored to an ops Discord webhook.

**I agree with the core of this.** The durable-SQLite-queue argument is correct and I would not
change it. The library-plus-MCP-wrapper split is the right call. The insistence that only one
component does network egress is the single best decision in the document — it makes the
passive-only guarantee auditable at one chokepoint instead of scattered across ten stages.

---

## Part 2 — What I think is wrong, over-engineered, or missing

Ordered by how much it would cost to discover late.

### 2.1 — MISSING · The feedback loop has no ingress. Phase 8 cannot pass as written.

This is the real hole. §9 says the Critic "recomputes precision from Discord reactions" and
Phase 8's acceptance test requires a "Week-1 report generated from real reactions." **Webhooks
are write-only.** A `discord.com/api/webhooks/...` URL cannot read a reaction, cannot read a
message, cannot receive an event. There is no architecture in §4 by which reaction data ever
reaches the `feedback` table.

Three ways out:

| Option | Cost | Verdict |
| --- | --- | --- |
| Gateway bot (`discord.py`) as a second long-lived systemd service listening for `reaction_add` | ~90 MB RSS, one bot token, bot must be invited to the guild | **Recommended.** Reactions are the only feedback mechanism with a low enough friction that you'll actually use it daily. |
| `cindra feedback <lead_id> good\|bad` CLI | Zero infra | Works, but you won't do it. Manual feedback loops die in week two. |
| Message components / buttons | Requires a public HTTPS interactions endpoint on the Pi | No. Don't expose the Pi. |

I recommend the bot, with the CLI built anyway in Phase 0 as the fallback and the test seam.
Note this also changes §10: the Dispatcher must persist the returned Discord `message_id` per
lead (requires `?wait=true` on the webhook POST) so reactions can be joined back to `lead_id`.
That column does not exist in the §5 schema and needs adding to `dispatch_log`.

### 2.2 — OVER-ENGINEERED · The router model buys nothing and costs a load slot.

§3 specifies three resident models (`qwen3:1.7b` router ~1.4 GB, `qwen3:4b-instruct` workhorse
~2.8 GB, `bge-m3` embedder ~1.2 GB = 5.4 GB) against `OLLAMA_MAX_LOADED_MODELS=2`. Three models
competing for two slots means Ollama evicts and reloads on every alternation. A 2.8 GB reload
from NVMe is seconds of dead time, repeatedly, forever.

The router's job is a schema-constrained boolean. The 4B can answer that in ~5 output tokens for
a few hundred ms. A separate 1.7 B model to save ~200 ms on a task that runs behind a network
fetch taking 800 ms is a false economy that costs a permanent load slot.

**Recommendation:** drop `qwen3:1.7b`. Two models resident — workhorse + embedder — fits two
slots exactly and lands at ~4.0 GB, comfortably under the 6.5 GB envelope. Re-add the router only
if Phase 1 benchmarks show the 4B's classification latency is actually the bottleneck.

### 2.3 — WRONG PREMISE · `bge-m3` is the expensive part of dedupe, not `sqlite-vec`.

§19 asks whether sqlite-vec is wrong at our volume. I checked: `sqlite-vec` ships a prebuilt
`manylinux_2_17_aarch64` wheel at **0.3 MB**. It is not the problem — it's one of the cheapest
dependencies in the stack and I'd keep it.

The cost is `bge-m3`: 1.2 GB resident and an embedding call per company, to serve dedupe ladder
rung 3 on a corpus that grows to roughly 10k companies a year. Rungs 1 (exact canonical domain)
and 2 (rapidfuzz ≥92 on name + same country) will catch the overwhelming majority of duplicates
at zero model cost. Rung 3 earns its keep only for the genuinely hard case: same company,
different domain, differently-spelled name.

**Recommendation:** build rungs 1, 2, and 4 in Phase 3. Keep the `company_vectors` table and the
sqlite-vec extension in the schema from day one so it is never a migration. Gate rung 3 behind
`dedupe.vector_rung_enabled` in `scoring.yaml`, and when enabled, only invoke it when rungs 1–2
are ambiguous — not on every company. Measure the dupe rate at the end of Phase 3 against the
<2% target; turn the rung on only if rungs 1–2 miss it. `bge-m3` stays justified regardless for
the Bengali-language matching §2 needs, but should be loaded on demand, not pinned.

### 2.4 — WRONG · `claude-agent-sdk` on the Pi drags in a Node runtime for no benefit.

§4 and §18 propose `claude-agent-sdk` for the escalation tier. Its PyPI artifact is **100 MB**
(vs the plain `anthropic` SDK at **1.0 MB**, pure Python) because it bundles the Claude Code CLI
— it is a harness for *driving an agent loop*, and it wants Node present.

Our escalation tier is not an agent loop. It is: send one prompt, get one JSON object back,
validate it against a Pydantic model. That is four lines of `anthropic`.

**Recommendation:** `anthropic` in prod, full stop. This drops a 100 MB dependency and a Node
runtime off the Pi and makes the "degrade to local-only" path trivially testable. `claude-agent-sdk`
can stay in the `[dev]` extra if you want it for dev-time experiments.

### 2.5 — INTERNALLY CONTRADICTORY · §6's per-domain rate limit vs. its own path list.

§6 lists five paths to fetch per company (`/`, `/about`, `/careers`, `/security` or `/trust`,
`/.well-known/security.txt`) and in the next bullet caps fetches at **≤2 requests per domain per
day**. Both cannot hold. As written the Enricher can never complete a company's profile in fewer
than three days.

**Recommendation:** 6 requests per domain per rolling 24 h, minimum 3 s apart, robots-respecting.
That is still far politer than any commercial crawler and satisfies the spirit of the rule
(never degrade a prospect's service). Make it `sources.yaml: fetch_budget_per_domain_24h: 6` so
it's a config value you can tighten without touching code. **Flagging rather than deciding —
this is a stated constraint and it's your call, but the spec cannot ship self-contradictory.**

### 2.6 — WILL FAIL A TEST · The digest cannot hold 10 lead embeds.

§10 says the digest is "one embed per lead, max 10 embeds" and separately enforces **6000 chars
total across all embeds**. The lead card as drawn in §10 is roughly 1,100–1,400 chars. Ten of
them is ~12,000 chars — a guaranteed 400 from Discord.

**Recommendation:** two embed builders sharing one limits module — `build_lead_card()` (full, for
hot/warm, one per message) and `build_digest_row()` (compact: title, score, top trigger, one
evidence link, ~450 chars, 8 per message with headroom). Paginate the digest at 8. The property
test from §16 ("embed builder never exceeds any Discord limit for arbitrary input") must run
against both builders and against the *sum*, not just per-embed.

### 2.7 — MISSING · No maintenance timer, and `raw_documents` will eat the disk.

§4 names three timers (harvest, enrich, digest). But §11 requires nightly trigger-decay
recompute, tier demotion, a 10% evidence-URL resample, and §12 requires 180-day retention purge.
Nothing runs these. Add **`maintenance.timer`** (nightly, off-peak): decay → demote → resample →
purge → `sqlite3 .backup` → cache sweep.

Separately: §5 puts `raw_documents` in SQLite as a content-addressed cache. At 200+ docs per
harvest batch, hourly, storing raw HTML inline will grow the DB to multiple GB — which bloats the
WAL, slows every `.backup`, and makes the nightly backup itself a thermal event.

**Recommendation:** store the document *body* zstd-compressed on the filesystem at
`var/cache/<sha256[:2]>/<sha256>.zst`, and keep only `sha256`, URL, source_id, fetched_at,
content_type, and size in SQLite. The DB stays in the low hundreds of MB and stays fast to back
up. A 30-day TTL sweeper in the maintenance job reclaims space. Evidence snippets are already
capped at 500 chars and stay in the DB, so provenance survives cache eviction.

### 2.8 — MISSING · Inbound (Web3Forms / IMAP) has no node in the diagram.

§6 calls `contact@cindrasec.com` "the highest-intent leads of all" and Phase 6 requires ingesting
it, but §4's diagram has no ingress for it. It bypasses Scout and Harvester entirely and should
enter at **Resolver** with a synthetic `T0_INBOUND` trigger (weight ~35, decay 30 d, evidence =
the message itself, `first_party` legality class). Treating it as just another `Candidate` means
it inherits dedupe, compliance, and scoring for free. It needs a taxonomy row that §2 doesn't have.

### 2.9 — NAMING RISK · Seven of the ten "agents" must not be agents.

Only three stages should ever touch an LLM: **Extractor** (schema-constrained extraction),
**Scout** (query synthesis), and **Scorer** (prose only — rationale, angle, Bengali variant).
The other seven are deterministic Python. §7 already says "a model must never be allowed to
invent the number," and §9's Scorer row says the same — but calling all ten "agents" invites
exactly that drift six weeks from now.

**Recommendation:** in `src/`, they are `Stage` subclasses, and only three of them are given an
`llm` handle at construction. The other four have no way to call a model. Keep `.claude/agents/*.md`
for dev-time delegation — that's a separate concern and the naming there is fine.

### 2.10 — Smaller things, batched

- **Suppression is checked too late.** §12 consults it "before every dispatch." Check it at
  **Scout** time too — no reason to spend SerpAPI credits and 4B tokens on a company you will
  veto at stage 8.
- **Idempotency key is too coarse.** §9 says re-send only if score moved ≥10. But a company that
  picks up a *new trigger* at the same score is genuinely new news. Key on
  `(lead_id, sorted(trigger_codes), score // 10)`.
- **Hourly harvest will burn the SerpAPI daily cap by mid-morning.** Make the guard a token
  bucket refilling across 24 h, not a naive daily counter.
- **Feed the model text, not HTML.** Strip boilerplate before extraction — `selectolax` is
  4.9 MB with aarch64 wheels and is very fast. This cuts prompt tokens roughly 5× and is worth
  real accuracy points on a 4B. `trafilatura` extracts better but drags in `lxml`; start with
  `selectolax` and only escalate if Phase 3 accuracy demands it.
- **Prompts need their own version hash.** `Lead.pipeline_version` won't catch a prompt edit.
  Hash the prompt files and store it, so golden-fixture invalidation is automatic.
- **`vcgencmd` needs the `video` group** for the service user, or `thermal.py` silently returns
  nothing and the governor never engages. Easy to miss until the Pi cooks.

### 2.11 — On §19: can a 4B hit 90% extraction accuracy?

Probably not as a single number, and I think the metric is the problem rather than the model.
A Q4 4B under a strict JSON Schema will hit **schema validity** in the high 90s — that part is
easy and mostly a function of Ollama's grammar constraint, not model quality. **Field accuracy**
on real messy pages is a different distribution per field: `canonical_domain` and `display_name`
are near-perfect; `employee_band`, `industry`, and funding amounts are guesses dressed as facts.

**Recommendation — split the Phase 3 gate into three:**

| Metric | Target | Rationale |
| --- | --- | --- |
| Schema validity | ≥ 98% | Grammar-constrained; anything less is a prompt bug |
| Critical fields (`canonical_domain`, `display_name`, trigger code present, evidence URL present) | ≥ 90% | These decide whether the lead exists at all |
| Soft fields (`employee_band`, `industry`, funding amount, headcount) | ≥ 70%, **unsourced ⇒ `None`** | §11 already says numeric claims must appear verbatim in a snippet or be dropped — enforce it and let recall suffer |

If critical-field accuracy lands under 90% after prompt iteration, the escalation path is already
built: route the failures to `claude-haiku-4-5` at ~$1/MTok in. At 40 leads/day with a 10% escalation
rate and ~3k tokens each, that's roughly $0.01/day — nowhere near the $0.50 cap. **The cloud tier
is the answer to a weak local model, and the budget already covers it.** I'd rather ship with a
measured 85% local + escalation than spend two weeks fighting a 4B.

I can't validate the model stack numbers from here — `registry.ollama.ai` is unreachable through
this session's proxy, and your Pi currently has only `llama3.2:3b`. Phase 1 pulls the models and
records real tok/s; if a `qwen3:4b-instruct` tag doesn't resolve, `llama3.2:3b` is the fallback
and the benchmark table says so honestly.

### 2.12 — Dependency weight audit (§0 asked for this)

Verified against PyPI. Everything below has an aarch64 wheel or is pure Python.

| Dep | Size | Verdict |
| --- | --- | --- |
| `sqlite-vec` 0.1.9 | 0.3 MB, aarch64 wheel | Keep — cheap, see §2.3 |
| `rapidfuzz` 3.14 | 58 MB, aarch64 wheels | Keep — carries dedupe rung 2 |
| `selectolax` 0.4.11 | 4.9 MB, aarch64 wheels | Add — see §2.10 |
| `pydantic` 2.13, `structlog`, `dnspython`, `tldextract`, `prometheus-client`, `httpx`, `pyyaml`, `typer` | all ≤ 1.5 MB | Keep |
| `anthropic` 0.122 | 1.0 MB, pure Python | Keep for escalation |
| **`claude-agent-sdk` 0.2.138** | **100 MB** | **Drop from prod** — see §2.4 |
| **Playwright** | ~400 MB + Chromium | **Do not install at all.** Not in any extra. §8 disables it by default; the stronger move is to make it absent, so `allow_browser: true` fails loudly rather than quietly working. |
| `discord.py` | ~1.5 MB pure Python | **Add** — feedback bot approved (§2.1) |
| `trafilatura` | pure Python but pulls `lxml` (~60 MB) | Defer — only if `selectolax` underperforms |

Total prod install lands around 150 MB, dominated by `rapidfuzz`.

---

## Part 3 — Build plan, Phases 0–8

Conventions throughout: Python 3.11+, asyncio, Pydantic v2, ruff + mypy strict, pytest.
Every network call has a timeout, jittered retry, and circuit breaker. Every LLM call passes a
JSON Schema via Ollama `format`, parses into Pydantic, retries once at `temperature=0`, then
escalates, then dead-letters. Config in `config/*.yaml`; prompts in `prompts/`; behaviour changes
edit those, not code.

### Phase 0 · Foundation

**Files**

```
PLAN.md  CLAUDE.md  README.md  pyproject.toml  Makefile  .env.example  .gitignore
.github/workflows/ci.yml
db/schema.sql  db/migrations/0001_init.sql
src/cindraleads/{__init__,models,store,queue,logging,config,errors}.py
src/cindraleads/cli.py
tests/unit/{test_models,test_queue,test_store,test_redaction}.py
tests/conftest.py
```

`models.py` is the whole §5 object graph plus `Job`, `StageResult`, `QueryPlan`, `RawDocument`,
`Candidate`, `ComplianceVerdict`, `DnsHygiene`, `FundingInfo`. `queue.py` is lease-based:
`claim(worker_id, lease_secs)`, `complete`, `fail`, `reclaim_expired`. Schema includes
`company_vectors` and the `dispatch_log.discord_message_id` column from §2.1 even though nothing
writes them yet — so neither is a migration later.

**Acceptance:** `make test` green (ruff + mypy strict + pytest). Enqueue 100 jobs, `kill -9` the
worker mid-run, restart — all 100 complete **exactly once**, asserted by a uniqueness check on a
side-effect table. A log-redaction test asserts a webhook URL and an API key never appear in
`*.jsonl` output.

### Phase 1 · Pi baseline

**Files**

```
src/cindraleads/{llm,thermal}.py
config/models.yaml
deploy/ollama-override.conf
scripts/benchmark_models.py
docs/BENCHMARKS.md
tests/unit/{test_llm_schema,test_thermal_policy}.py
tests/fixtures/html/*.html          # 20 real saved pages
```

`llm.py` exposes `generate_structured(prompt, schema, model_role) -> BaseModel` with the
retry/escalate/dead-letter ladder and per-call token+latency accounting. `thermal.py` implements
the §3 governor as a real state machine over `vcgencmd measure_temp` / `get_throttled`, with the
readings injectable so the policy is unit-testable without a Pi.

**Acceptance:** extract a `Company` from all 20 fixture pages using the local model only.
Schema validity ≥ 95%. `docs/BENCHMARKS.md` records measured tok/s, p50/p95 latency, peak SoC
temp, and RSS per model — **on the actual Pi**, and it records which model tags actually resolved.
Thermal policy unit tests pass at simulated 65/75/85 °C and with the throttle flag set.

### Phase 2 · Harvest

**Files**

```
mcp_servers/cindra_serp/{__init__,lib,server}.py
mcp_servers/cindra_signals/{__init__,lib,server}.py
src/cindraleads/sources/{registry,http,cache,robots,circuit}.py
src/cindraleads/budget.py
src/cindraleads/agents/{scout,harvester}.py
config/sources.yaml
.mcp.json
tests/unit/{test_cache_key,test_robots,test_budget_bucket,test_circuit}.py
tests/integration/test_harvest_live.py
```

`http.py` is the sole egress chokepoint — every outbound request in the system goes through one
function that enforces legality class, robots, per-domain budget, UA, and backoff. Budget guard
reads real SerpAPI quota from the account endpoint at startup (§6) and spends it through a 24 h
token bucket (§2.10).

**Acceptance:** 50 real SerpAPI queries yield ≥ 200 raw docs. An identical second run costs
**0 credits** (cache hit, asserted against the budget table). The guard halts cleanly at 85% of
quota and emits one ops-channel warning. Killing a source with forced 500s trips its breaker in
3 failures and leaves the other sources running.

### Phase 3 · Extract & Resolve

**Files**

```
src/cindraleads/agents/{extractor,resolver}.py
src/cindraleads/{injection,dedupe,textextract}.py
prompts/{extract_candidate,classify_icp,scout_queries}.md
tests/golden/{corpus/*.html,labels.json}     # 50 hand-labelled
tests/unit/{test_dedupe_ladder,test_canonical_domain,test_injection}.py
tests/adversarial/{payloads.json,test_injection_suite.py}
tests/property/test_domain_canonicalization.py
```

`textextract.py` is the `selectolax` boilerplate stripper from §2.10. `injection.py` wraps every
`RawDocument` in explicit data delimiters, runs the heuristics, and quarantines trips. The
extractor is constructed with no network-capable tools — structurally, not by instruction.

**Acceptance:** on a 300-doc corpus against the 50-item golden set — schema validity ≥ 98%,
critical-field accuracy ≥ 90%, soft-field accuracy ≥ 70% with unsourced fields `None`
(the §2.11 split). Duplicate rate < 2% using rungs 1/2/4 only. All 20 planted injection payloads
quarantined, **and** a test asserts none of them reached a tool-call argument. Property test:
domain canonicalization is idempotent; dedupe is symmetric.

### Phase 4 · Enrich & Validate

**Files**

```
mcp_servers/cindra_osint/{__init__,lib,server}.py
mcp_servers/cindra_verify/{__init__,lib,server}.py
mcp_servers/cindra_registry/{__init__,lib,server}.py
src/cindraleads/agents/{enricher,validator}.py
src/cindraleads/passive.py                   # PassiveOnlyViolation + binary denylist
docs/SOURCES.md
tests/compliance/test_passive_only.py
tests/unit/{test_email_verify,test_evidence_check,test_decay}.py
```

`passive.py` holds the legality-class enforcement, the forbidden-binary denylist, and the
`PassiveOnlyViolation` exception. Enrichment fans out with `asyncio.gather(return_exceptions=True)`
— no single source failure fails a lead.

**Acceptance:** 100 domains enriched end to end. **Every forbidden action in §12 raises
`PassiveOnlyViolation`** — one test per forbidden category, and a test that the denylist is
non-empty and actually consulted. Email verification agrees with a known-good sample ≥ 90%, and
a test asserts no SMTP `VRFY`/`RCPT` is ever issued. Validator kills a lead whose evidence URL
404s.

### Phase 5 · Score & Comply

**Files**

```
src/cindraleads/{scoring,compliance}.py
config/{scoring,icp}.yaml
prompts/{rationale,outreach_angle,bengali_angle}.md
docs/{LIA,DATA_RETENTION,COMPLIANCE}.md
scripts/erase_subject.py
tests/unit/test_scoring_arithmetic.py
tests/property/test_scoring_monotonic.py
tests/compliance/test_compliance_gate.py
tests/integration/test_rank_correlation.py
```

`scoring.py` is pure arithmetic with **no LLM handle in scope** — the model is called separately,
afterwards, only for the three prose fields, and its output can never write a number.

**Acceptance:** score 50 leads; you hand-rank 20 and Spearman ρ ≥ 0.7. Every §12 rule has a
passing test and CI fails if a rule lacks one. Property test: score is monotonic in trigger
weight. You personally review the Bengali angles for register — a machine gloss fails this gate.
`erase_subject.py` purges across every table plus the vector index, verified by a test.

### Phase 6 · Dispatch

**Files**

```
mcp_servers/cindra_discord/{__init__,lib,server,limits,embeds}.py
src/cindraleads/agents/dispatcher.py
src/cindraleads/inbound/{web3forms,imap}.py
config/discord.yaml
tests/unit/{test_embed_limits,test_tier_routing,test_idempotency}.py
tests/property/test_embed_never_exceeds.py
tests/integration/test_dispatch_429.py
```

`limits.py` is the single home for every Discord constant. Two builders per §2.6. Dispatcher
posts with `?wait=true` and persists `discord_message_id` for the Phase 8 feedback join.
Inbound enters at Resolver with the `T0_INBOUND` trigger from §2.8.

**Acceptance:** 30 leads land in the correct channels, correctly formatted. Re-run produces
**zero** duplicates. A simulated 429 with `retry_after` is obeyed, not guessed. Oversized input
truncates at a word boundary with `…` while **keeping every evidence link** — property-tested
against arbitrary input for per-embed *and* 6000-char total limits. An inbound Web3Forms message
produces a Tier A card.

### Phase 7 · Autonomy

**Files**

```
deploy/systemd/{cindraleads-worker.service,harvest.timer,enrich.timer,
                digest.timer,maintenance.timer,*.service}
deploy/install_pi.sh
src/cindraleads/{health,metrics}.py
scripts/backup.sh
docs/RUNBOOK.md
tests/chaos/{test_ollama_down,test_disk_full,test_budget_exhausted,test_thermal_85c}.py
```

Four timers, including the `maintenance.timer` from §2.7. Units carry `MemoryMax=`, `CPUQuota=`,
`Restart=on-failure`, `WatchdogSec=`. `/healthz` and `/metrics` bind **localhost only**.

**Acceptance:** 72 h unattended, measured by `cindra acceptance` — ≥ 15 Tier A+B leads/day,
no job lost (zero dead letters, no gap in the worker's own heartbeat record), no unit silent,
one build throughout, and the thermal governor **recovered** if it engaged. Chaos suite: with
Ollama stopped, the disk full, the budget exhausted, and a simulated 85 °C, the pipeline
**degrades, alerts, and resumes** — it never crash-loops and never loses a job.

> **Amended 2026-08-19.** This originally read "no thermal throttle event (`get_throttled`
> stays `0x0`)". That is an assertion about a heatsink rather than about this system, and on
> the Pi we have it was already false twenty minutes after a cold boot — sticky bits 17/18/19
> set, bits 0/16 clear, so purely thermal with no under-voltage. More to the point, §3
> specifies a thermal governor whose entire job is to pause inference when the SoC is hot and
> resume when it is not; `0x0` requires that the governor never once does the thing it was
> built to do. Observed behaviour under real load: it engaged, `scorer_prose_failed` logged
> `will_retry: true`, and the jobs completed later. That is the design working, and the old
> gate would have failed the run for it.
>
> Heat is now **reported and never graded** — peak temperature, minutes per governor state,
> throttled samples. Those numbers decide whether to buy a cooler. They do not decide whether
> the software works. A criterion that cannot be evaluated from the window reports `n/a` and
> **does not pass**: an unmeasured run must never read as a clean one.

### Phase 8 · Self-improvement

**Files**

```
src/cindraleads/agents/critic.py
src/cindraleads/feedback/{bot,cli,store}.py
deploy/systemd/cindraleads-feedback.service
reports/.gitkeep
scripts/restore_drill.sh
tests/unit/{test_precision_calc,test_critic_proposals}.py
tests/integration/test_reaction_join.py
```

`bot.py` is a `discord.py` gateway client — the *only* component besides the Dispatcher that
touches Discord, and it is read-only: it listens for `reaction_add`, joins the message ID back
to `lead_id` via `dispatch_log`, and writes to `feedback`. It runs as its own systemd service so
a gateway disconnect can never stall the pipeline worker. New env var `DISCORD_BOT_TOKEN`
(`.env`, `0600`, redacted in logs like the webhooks).

**Acceptance:** week-1 precision report generated from **real reactions**. React to a dispatched
card in Discord and assert the row lands in `feedback` joined to the right `lead_id`. Killing the
bot service leaves the pipeline fully operational (degraded feedback only). Critic proposes ≥ 3
concrete weight or query-plan changes, each citing the leads that justify it, and **applies
none** — a test asserts the Critic has no write access to `scoring.yaml`. Restore-from-backup
drill passes: wipe the DB, restore, and the pipeline resumes with no duplicate dispatches.

---

## Verification (how you'll see it working)

Per phase: `make test` (ruff + mypy strict + pytest) must be green before the next phase starts —
that's the gate, and I won't cross it silently. Beyond the unit suites, each phase ends in
something you can look at:

- **P1** `make bench` → read `docs/BENCHMARKS.md`, real numbers off your Pi.
- **P2** `cindra harvest --source serpapi --dry-run` then re-run → watch credits stay flat.
- **P3** `cindra extract --golden` → prints the three-way accuracy table from §2.11.
- **P4** `pytest tests/compliance/ -v` → every §12 rule, one line each.
- **P5** `cindra score --sample 20` → the ranking you hand-correlate.
- **P6** `cindra dispatch-test` → real cards in your real Discord.
- **P7** `systemctl status` + `curl localhost:9109/healthz` after 72 h.
- **P8** `cindra precision-report` → `reports/precision_YYYY-WW.md`.

## Open items carried into Phase 0

The four architecture-changing questions are answered and locked at the top of this document.
One item still needs your call, and it is a stated constraint so I won't decide it unilaterally:

- **§2.5 — per-domain fetch budget.** §6 says ≤2 requests/domain/day but lists 5 paths to fetch.
  I've planned for `fetch_budget_per_domain_24h: 6` at ≥3 s apart. Say the word if you want it
  held at 2 and I'll spread each company's paths across multiple days instead.

The remaining §20 questions are config-level, not architectural. I've assumed these defaults,
recorded in `config/*.yaml` and trivial to override without touching code:

| §20 | Assumed default |
| --- | --- |
| Geography split | 60% global / 40% BD+South Asia, as `icp.yaml` weights |
| Daily actionable volume | 25/day Tier A+B; tier thresholds tunable |
| Discord layout | One server, four channels (hot/warm/digest/ops) |
| Suppression seed | Empty; `cindra suppress <domain>` from day one |
| SerpAPI quota | Auto-discovered from the account endpoint at startup, per §6 |
| Inbound inbox | Yes, Phase 6, behind a feature flag |
| Dashboard | No separate app — a read-only HTML view on the existing localhost `/metrics` server |
| Extra excluded verticals | None beyond anti-ICP; add to `icp.yaml: hard_exclude_sectors` |
