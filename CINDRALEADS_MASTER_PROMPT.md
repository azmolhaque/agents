# CINDRALEADS — Master Build Prompt for Claude Code

**Target repo:** `~/cindraleads`
**Runtime host:** Raspberry Pi 5, 16 GB RAM, NVMe, Debian arm64
**Owner:** Md. Azmol Haque Rony — Cindrasec (cindrasec.com)

> **How to use this file.** Open Claude Code in an empty `~/cindraleads` directory, then paste
> §0 as your first message and attach this whole file. Claude Code will read it, enter plan
> mode, and build the system phase by phase. Do **not** paste the whole thing as one giant
> instruction and expect one-shot success — the phase gates in §15 exist because they work.

---

## §0 — FIRST MESSAGE (paste this into Claude Code)

```
Read CINDRALEADS_MASTER_PROMPT.md in full before doing anything.

You are the lead architect for CindraLeads — an autonomous, self-hosted lead
intelligence system for Cindrasec, a boutique attack-surface + AI/LLM security
studio. It discovers companies that have a live, dated reason to need
attack-surface monitoring or LLM security testing, verifies and normalizes what
it finds, scores it against a written ICP, and delivers structured, actionable
lead cards to Discord webhooks.

Constraints that are not negotiable, ever:
  1. PASSIVE-ONLY. This system never touches a prospect's infrastructure in a way
     that is not a public-record lookup. See §12. This is a legal and brand
     requirement — Cindrasec's entire pitch is "no scan without a signed RoE."
  2. It must run 24/7 on a Raspberry Pi 5 (16 GB) with a local LLM doing the bulk
     of the inference. Cloud API calls are a rationed escalation path, not the
     default.
  3. Every lead that reaches Discord must be schema-valid, deduplicated,
     evidence-linked, and compliance-cleared. A fluent-sounding hallucinated lead
     is worse than no lead.

Do this now, in order:
  (a) Enter plan mode. Do not write code yet.
  (b) Restate the architecture in §4 in your own words and flag anything you
      think is wrong, over-engineered for a Pi, or missing.
  (c) Ask me at most 8 clarifying questions — only ones whose answers would
      change the architecture. Batch them in one message.
  (d) Then produce a written PLAN.md covering Phases 0-8 from §15, with a
      concrete file manifest and acceptance test per phase.
  (e) Stop. Wait for my approval before Phase 0.

Bias toward boring, debuggable, resource-cheap choices. If a dependency is not
arm64-native or pulls in more than ~200 MB, argue for it or drop it.
```

---

## §1 — MISSION & DEFINITION OF DONE

Build **CindraLeads**: a durable, resumable, observability-first pipeline that runs on one
Raspberry Pi and answers a single question every day —

> *Which specific organizations, right now, have a dated and evidenced reason to buy a
> Snapshot, a Watch subscription, or an AI/LLM security assessment from Cindrasec — and who
> exactly do I talk to?*

**Done means:**

| Criterion | Threshold |
| --- | --- |
| Precision @ Tier A (human-judged "I would actually contact this") | ≥ 70% |
| Leads delivered per day, Tier A+B | 15–40 (tunable) |
| Duplicate rate across a rolling 90 days | < 2% |
| Email deliverability of proposed contacts | ≥ 85% verified-or-role |
| Every lead card carries ≥ 1 clickable evidence URL | 100% |
| Cloud API spend | < $15/month at steady state |
| Median wall-clock per lead, harvest → Discord | < 90 s |
| Unattended uptime on the Pi | ≥ 7 days without manual intervention |
| Passive-only gate violations | 0, enforced in code + tested |

---

## §2 — BUSINESS CONTEXT (from cindrasec.com — treat as ground truth)

**What Cindrasec sells**

| Tier | Offer | Price (BDT / USD) | Delivery |
| --- | --- | --- | --- |
| Snapshot | One-time external attack-surface + exposed-secrets scan, verified report, walkthrough call, free re-check | ৳15k–25k / $250–600 | 3–5 working days |
| Watch | Continuous monitoring, monthly diff report, real-time alerts | ৳5k–12k/mo / $150–500/mo | Monthly, no lock-in |
| AI/LLM Security | Prompt injection, jailbreak, data-leak, agent tool-abuse, **MCP tool-surface review** | ৳40k–1.5L / $2k–8k | 2–5 days |
| Productized Gig | One tightly-scoped check, fixed price | ৳7k–15k | 2–4 hrs |

Founding cohort: **first Snapshot is free.** That is the wedge — the outreach ask is tiny.

**Differentiators to mirror in lead reasoning:** verification-first (no false-positive walls),
recurring not one-off, bilingual বাংলা + English delivery, Google VRP–credited researcher,
niche focus, days not weeks. Payment: bKash/bank (BD), Payoneer/Wise (global).

**Ideal Customer Profile**

```yaml
icp:
  primary:
    - Seed/Series-A SaaS, fintech, healthtech: 5-150 employees, no dedicated security hire
    - Any team that shipped an LLM chatbot, AI agent, or MCP server in the last 180 days
    - Companies with visible surface sprawl: many subdomains, admin panels, staging hosts
  secondary:
    - Bangladesh + South Asia SMEs with public web apps (local-trust wedge, BDT pricing)
    - Agencies/dev shops who could white-label Snapshot for their own clients
    - Marketplace buyers actively posting pentest/AppSec briefs (Upwork, Fiverr, LinkedIn)
  buyer_personas:
    - Technical co-founder / CTO      # highest intent, shortest chain
    - Head of Engineering / Platform Lead
    - Compliance or Ops lead chasing SOC 2 / ISO 27001 / EU AI Act
    - DevRel or AI Eng lead who shipped the agent and knows it is untested
  anti_icp:                            # HARD EXCLUDE — never emit
    - Enterprises >1000 employees with a named CISO and in-house red team
    - Existing security vendors and competitors
    - Government, defence, critical national infrastructure
    - Anyone on the suppression list, or previously marked "not interested"
    - Individuals with no business affiliation (this is B2B only)
```

**Trigger taxonomy — the actual product.** Fit alone is noise; a *dated trigger* is the lead.

| Code | Trigger | Signal source | Weight | Decay |
| --- | --- | --- | --- | --- |
| `T1_AI_SHIP` | Launched an LLM chatbot / AI agent / MCP server | ProductHunt, HN Show HN, GitHub, changelogs, press | 30 | 180 d |
| `T2_FUNDING` | Raised pre-seed → Series B in last 120 days | News, SerpAPI, funding RSS | 22 | 120 d |
| `T3_HIRING_SEC` | Hiring AppSec/SecOps/CISO — has budget, no coverage yet | Job boards via SerpAPI, careers pages | 20 | 90 d |
| `T4_HIRING_AI_ONLY` | Hiring AI/LLM engineers but **zero** security roles | Same | 18 | 90 d |
| `T5_COMPLIANCE` | Publicly pursuing SOC 2 / ISO 27001 / PCI DSS 4.0 / EU AI Act / DORA / NIS2 | Trust pages, blog, job posts | 18 | 180 d |
| `T6_INCIDENT` | Breach, leak, or outage disclosed — in company or its sector | News, SEC EDGAR Item 1.05, CERT advisories | 26 | 60 d |
| `T7_SURFACE_SPRAWL` | Rapid subdomain growth in Certificate Transparency logs | crt.sh | 15 | 90 d |
| `T8_HYGIENE_GAP` | Missing/weak DMARC, no SPF, no DNSSEC, no security.txt | Public DNS + well-known paths | 12 | 30 d |
| `T9_MARKETPLACE` | Actively posting a pentest / security-audit brief | Upwork/Fiverr/LinkedIn via SerpAPI | 28 | 30 d |
| `T10_VENDOR_PRESSURE` | Publicly mentions being asked for a pentest report by a customer | Forums, HN, Reddit, LinkedIn posts | 24 | 90 d |
| `T11_STACK_RISK` | Public repo/job post references LangChain, MCP, RAG, agent frameworks | GitHub API, job posts | 14 | 180 d |
| `T12_LOCAL` | Bangladesh / South Asia HQ with public web app | Registries, TLD, SerpAPI locale | 10 | none |

> `T8_HYGIENE_GAP` is a *public DNS record lookup only*. It never becomes a finding, never
> appears in outreach as "we scanned you," and never triggers an active probe. It is an
> internal prioritisation hint. See §12.

---

## §3 — HARDWARE & RUNTIME BUDGET (Raspberry Pi 5, 16 GB)

Design to these numbers. If a component cannot live inside them, it does not ship.

```yaml
host:
  soc: BCM2712, 4x Cortex-A76 @ 2.4 GHz, arm64
  ram: 16 GB LPDDR4X
  storage: NVMe over PCIe (REQUIRED — SQLite on microSD will corrupt under WAL load)
  cooling: active cooler REQUIRED; throttle begins ~80-85 C
  os: Debian 12/13 arm64, 64-bit kernel

memory_envelope:
  ollama_resident:        <= 6.5 GB   # one 4B chat model + one embedder
  python_workers:         <= 2.5 GB   # 4 async workers
  sqlite_page_cache:      <= 1.0 GB
  os_and_headroom:        >= 4.0 GB   # never let this go below 3 GB

os_tuning:
  - dtparam=pciex1_gen=3          # /boot/firmware/config.txt — NVMe at Gen 3
  - vm.swappiness=10, 8 GB swapfile on NVMe (NOT on SD)
  - zram enabled as first-tier swap
  - cpufreq governor: ondemand normally; 'performance' inside batch windows only
  - systemd services with MemoryMax= and CPUQuota= per unit

thermal_governor:            # implement as a real Python module, not a comment
  read: vcgencmd measure_temp + vcgencmd get_throttled
  policy:
    < 70 C : full concurrency (4 workers)
    70-78 C: halve concurrency, pause local-LLM batch jobs
    > 78 C : LLM inference paused, network-IO-only tasks continue
    throttled_flag_set: log CRITICAL to Discord ops channel, drop to 1 worker
```

**Model stack (Ollama, arm64).** Benchmark all of these on the actual Pi in Phase 1 and record
tokens/sec in `docs/BENCHMARKS.md` — do not trust these estimates.

| Role | Model | Quant | RAM | Notes |
| --- | --- | --- | --- | --- |
| Router / binary classifier | `qwen3:1.7b` | Q4_K_M | ~1.4 GB | Is this page a company? Is it in ICP? Fast yes/no. |
| **Workhorse** extractor & normalizer | `qwen3:4b-instruct` | Q4_K_M | ~2.8 GB | Entity extraction into strict JSON. 90% of all calls. |
| Multilingual embedder | `bge-m3` | — | ~1.2 GB | 1024-d, handles **Bengali + English** — matters for §2 locale. |
| Optional overnight reasoner | `qwen3:8b` or `gemma3:12b-qat` | Q4_K_M | 5–8 GB | Batch-only, off-peak, low tok/s. Benchmark before committing. |

```bash
# /etc/systemd/system/ollama.service.d/override.conf
OLLAMA_MAX_LOADED_MODELS=2
OLLAMA_NUM_PARALLEL=1
OLLAMA_KEEP_ALIVE=30m
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
```

**Structured output is mandatory.** Every local-LLM call passes a JSON Schema via Ollama's
`format` parameter and the result is parsed into a Pydantic model. A call that fails schema
validation retries once at `temperature=0`, then routes to the escalation tier, then dies into
the dead-letter queue. Never regex a model's prose into a field.

**Escalation policy to Claude API** — for the ≤ 5% of items the local model can't handle:

```yaml
escalate_when:
  - local model failed schema validation twice
  - router confidence < 0.55
  - candidate scored >= 70 (high-value: pay for a better read)
  - ambiguous entity resolution (two plausible companies for one signal)
  - drafting the final outreach angle for Tier A leads only
model_routing:
  bulk_reread:   claude-haiku-4-5      # cheap, high volume
  hard_reasoning: claude-sonnet-5      # scoring rationale, entity disambiguation, Tier A angles
budget_guard:
  daily_usd_cap: 0.50                  # hard stop, persisted in SQLite, survives restart
  on_exhaustion: degrade to local-only, log WARNING to ops channel, do not crash
```

---

## §4 — ARCHITECTURE

Two things are being built and they must not be confused:

- **Claude Code (dev-time):** you, writing the system, with MCP servers wired into `.mcp.json`
  so you can exercise them interactively.
- **The runtime (prod-time):** a plain Python asyncio pipeline on the Pi, driven by systemd
  timers, calling local Ollama and the same MCP servers as in-process libraries. It does **not**
  need Claude Code running to work. Optionally, `claude-agent-sdk` (`pip install
  claude-agent-sdk`, Python ≥3.10) wraps the escalation tier — but the pipeline must degrade to
  local-only if it is unavailable.

```
                       ┌──────────────────────── systemd timers ────────────────────────┐
                       │  harvest.timer (hourly)   enrich.timer (15m)   digest.timer     │
                       └────────────────────────────────┬───────────────────────────────┘
                                                        ▼
  ┌──────────┐   ┌───────────┐   ┌───────────┐   ┌───────────┐   ┌──────────┐   ┌──────────┐
  │ 1 SCOUT  │──▶│2 HARVESTER│──▶│3 EXTRACTOR│──▶│ 4 RESOLVER│──▶│5 ENRICHER│──▶│6 VALIDATOR│
  │query plan│   │ fetch raw │   │ →entities │   │ dedupe/   │   │ passive  │   │ schema + │
  │from ICP  │   │ +cache    │   │ local LLM │   │ canonical │   │  OSINT   │   │  email   │
  └──────────┘   └───────────┘   └───────────┘   └───────────┘   └──────────┘   └────┬─────┘
        ▲                                                                             ▼
        │        ┌──────────┐   ┌────────────┐   ┌────────────┐   ┌──────────────────────┐
        │        │ 10 CRITIC│◀──│9 DISPATCHER│◀──│8 COMPLIANCE│◀──│      7 SCORER        │
        └────────│ precision│   │  Discord   │   │   GATE     │   │ CindraScore + tier   │
       feedback  │  sampling│   │  webhooks  │   │ hard rules │   │ + outreach angle     │
                 └──────────┘   └────────────┘   └────────────┘   └──────────────────────┘

  STATE: SQLite (WAL) + FTS5 + sqlite-vec   ·   QUEUE: SQLite-backed durable job table
  TELEMETRY: structlog JSON → /var/log/cindraleads → ops Discord webhook on ERROR+
```

**Why a durable SQLite job table and not Redis/Celery/Prefect:** power loss on a Pi is a
*when*, not an *if*. Every stage transition is a row update inside a transaction. On boot the
pipeline resumes mid-flight work by querying `WHERE status='in_flight' AND lease_expires_at <
now()`. One file, atomic, no broker to babysit. Add Redis only if you prove you need it.

---

## §5 — DATA MODEL

Define once in `src/cindraleads/models.py` as Pydantic v2, mirror in `db/schema.sql`. Every
agent boundary is typed; no dicts cross a stage.

```python
class Evidence(BaseModel):
    url: HttpUrl
    source_id: str              # 'serpapi_news' | 'crtsh' | 'github' | ...
    snippet: str = Field(max_length=500)
    observed_at: datetime
    content_sha256: str         # provenance: prove what we saw, when

class Trigger(BaseModel):
    code: Literal['T1_AI_SHIP', ...]     # from §2 taxonomy
    confidence: float = Field(ge=0, le=1)
    observed_at: datetime
    decays_at: datetime
    evidence: list[Evidence] = Field(min_length=1)   # never a bare assertion
    rationale: str = Field(max_length=280)

class Contact(BaseModel):
    full_name: str | None
    role_title: str | None
    persona: Literal['founder_cto','head_eng','compliance','ai_lead','generic'] | None
    email: EmailStr | None
    email_status: Literal['verified','role_account','catch_all','risky','unverified','none']
    linkedin_url: HttpUrl | None
    source: Evidence
    pii_basis: Literal['public_business_contact']    # §12

class Company(BaseModel):
    canonical_domain: str        # registrable domain, lowercase, punycode-normalized. THE KEY.
    legal_name: str | None
    display_name: str
    country: str | None          # ISO-3166-1 alpha-2
    hq_city: str | None
    employee_band: Literal['1-10','11-50','51-200','201-1000','1000+'] | None
    industry: str | None
    tech_signals: list[str]      # ['langchain','nextjs','supabase','mcp-server']
    ai_surface: list[str]        # ['public_chatbot','agent_with_tools','mcp_server']
    subdomain_count_ct: int | None       # from Certificate Transparency
    dns_hygiene: DnsHygiene | None       # passive lookups only
    funding: FundingInfo | None

class Lead(BaseModel):
    lead_id: str                 # sha256(canonical_domain)[:16] — stable forever
    company: Company
    contacts: list[Contact] = Field(max_length=3)
    triggers: list[Trigger] = Field(min_length=1)     # NO TRIGGER, NO LEAD.
    score: int = Field(ge=0, le=100)
    score_breakdown: dict[str, float]
    tier: Literal['A','B','C','REJECT']
    recommended_offer: Literal['snapshot_free','watch','ai_llm_assessment','gig']
    outreach_angle: str = Field(max_length=400)       # the specific, evidenced opener
    bengali_angle: str | None                          # only when country == 'BD'
    risk_notes: list[str]
    compliance: ComplianceVerdict
    first_seen_at: datetime
    last_updated_at: datetime
    pipeline_version: str
```

**SQLite tables:** `raw_documents` (content-addressed cache), `candidates`, `companies`,
`contacts`, `triggers`, `leads`, `evidence`, `jobs` (durable queue), `dispatch_log`,
`suppression_list`, `api_budget`, `metrics`, `dead_letter`, `feedback`.
Plus `companies_fts` (FTS5 over names/descriptions) and `company_vectors` (sqlite-vec,
1024-d bge-m3, for near-duplicate detection).

---

## §6 — SOURCE REGISTRY

Sources live in `config/sources.yaml`, each with a legality class the code enforces. Adding a
source means adding a row here, not editing pipeline code.

| Source | Access | Legality class | Gives us |
| --- | --- | --- | --- |
| **SerpAPI** — google, news, jobs, maps, scholar | API key | `licensed_api` | The spine: trigger discovery across every vertical below |
| Company website (homepage, /about, /careers, /security, /trust, /.well-known/security.txt) | HTTPS GET, robots-respecting, ≤2 req/domain/day | `public_web` | Offer, stack, AI surface, contact page |
| **Certificate Transparency (crt.sh)** | Public log | `public_record` | `T7_SURFACE_SPRAWL`, subdomain inventory |
| **Public DNS** (MX, SPF, DMARC, DKIM, DNSSEC, NS) | DNS query | `public_record` | `T8_HYGIENE_GAP`, mail validity |
| **RDAP / WHOIS** | Public registry | `public_record` | Domain age, registrar, org |
| **GitHub REST/GraphQL API** | Token | `licensed_api` | `T1_AI_SHIP`, `T11_STACK_RISK` — repos importing LLM/MCP SDKs |
| **SEC EDGAR** full-text + 8-K Item 1.05 | Public, UA required | `public_record` | `T6_INCIDENT` — mandatory cyber incident disclosures |
| **NVD / CISA KEV / GitHub Advisories** | Public API | `public_record` | Sector-level `T6_INCIDENT` context |
| Hacker News (Algolia API), Show HN | Public API | `licensed_api` | `T1_AI_SHIP`, `T10_VENDOR_PRESSURE` |
| ProductHunt | API | `licensed_api` | `T1_AI_SHIP` |
| Reddit (r/devops, r/startups, r/LocalLLaMA, r/cybersecurity) | Public API | `licensed_api` | `T10_VENDOR_PRESSURE` |
| Funding + tech RSS (TechCrunch, sector feeds, BD/SA tech press) | RSS | `public_web` | `T2_FUNDING` |
| Job boards via SerpAPI (`site:boards.greenhouse.io`, lever.co, ashbyhq.com, workable.com, bdjobs.com) | via SerpAPI | `licensed_api` | `T3`, `T4`, `T5`, `T11` |
| Marketplace briefs via SerpAPI (`site:upwork.com "penetration test"`) | via SerpAPI | `licensed_api` | `T9_MARKETPLACE` — highest intent |
| OpenCorporates / GLEIF / Companies House / RJSC (BD) | API | `public_record` | Legal entity, incorporation date |
| **Inbound: your own Web3Forms → contact@cindrasec.com** | IMAP/webhook | `first_party` | Highest-intent leads of all — wire this in Phase 6 |

**Rules the harvester enforces in code:**
- `public_web` sources: parse and obey `robots.txt`, honour `Crawl-delay`, send a real UA
  identifying Cindrasec with a contact URL, ≤ 2 requests per domain per day, exponential
  backoff on 429/503, never fetch anything behind a login or paywall.
- `licensed_api` sources: obey documented rate limits, use official APIs only. **Never scrape
  LinkedIn/Upwork/Fiverr directly** — reach them only as SerpAPI search results. Headless
  browsers are off by default and require an explicit per-source `allow_browser: true`.
- **SerpAPI budget guard:** read your plan's actual quota from the account/usage endpoint at
  startup — do not hardcode a number. Enforce a daily search cap in SQLite, cache every query
  by `sha256(engine|query|params)` for 24–72 h depending on volatility, deduplicate identical
  in-flight queries, and stop cleanly at 85% of quota with a Discord ops warning.

---

## §7 — SCORING: CindraScore

Deterministic arithmetic first; LLM only writes the *rationale* and the *angle*. A model must
never be allowed to invent the number.

```
CindraScore = clamp(0, 100,
      TriggerScore   * 0.45     # sum of decayed trigger weights, capped at 100
    + ICPFit         * 0.25     # size, sector, geo, no-security-hire signal
    + Reachability   * 0.15     # named contact + verified email + LinkedIn
    + SurfaceSignal  * 0.10     # AI surface present, subdomain sprawl, hygiene gaps
    + Freshness      * 0.05     # recency of the newest trigger
) - Penalties

decay(trigger) = weight * exp(-ln(2) * age_days / half_life_days)

Penalties:
  -100  anti-ICP match (enterprise, competitor, gov/CNI)   → hard REJECT
  -100  on suppression list
  -25   no named contact and no role email discoverable
  -20   all evidence older than 180 days
  -15   single-source evidence (one URL, one source_id) for the top trigger
  -10   country on the sanctions/embargo exclusion list

Tiers:  A >= 72   ·   B 55-71   ·   C 40-54   ·   REJECT < 40
Routing: A → #leads-hot (immediate) · B → #leads-warm (immediate)
         C → #leads-digest (daily 09:00 Asia/Dhaka roll-up only)
```

**Offer mapping**

| Condition | `recommended_offer` |
| --- | --- |
| `T1_AI_SHIP` or `T11_STACK_RISK` present and `ai_surface` non-empty | `ai_llm_assessment` |
| `T9_MARKETPLACE` present | `gig` |
| `T7_SURFACE_SPRAWL` or `T5_COMPLIANCE` present, no AI surface | `watch` |
| everything else | `snapshot_free` ← the founding-cohort wedge, use it liberally |

**Outreach angle rules.** ≤ 400 chars. Must name (1) the specific observed trigger, (2) the
concrete thing Cindrasec would look at, (3) a low-friction ask (usually the free Snapshot). It
must **never** claim or imply that anything has already been scanned, tested, or found. Write
in the register of a researcher, not a salesperson. For `country == 'BD'`, also produce a
natural Bengali variant — a real rewrite, not a machine gloss.

---

## §8 — MCP SERVER SPECIFICATIONS

Build each with **FastMCP (Python)**, stdio transport, in `mcp_servers/<name>/`. Each is
importable as a plain library too — the prod pipeline calls the library, Claude Code calls the
MCP wrapper. One implementation, two entry points. Every tool returns `{ok, data, error,
cost_units, cached}`.

| Server | Tools |
| --- | --- |
| `cindra-serp` | `search(engine, q, params)`, `search_jobs(company, keywords)`, `search_news(entity, since)`, `budget_status()` — with cache + budget guard |
| `cindra-osint` | `dns_hygiene(domain)`, `rdap(domain)`, `ct_subdomains(domain)`, `security_txt(domain)`, `tech_fingerprint(url)`, `fetch_public_page(url)` — **passive class enforced at the tool boundary** |
| `cindra-registry` | `edgar_search(q)`, `edgar_item_105(since)`, `opencorporates(name, jurisdiction)`, `gleif_lei(name)` |
| `cindra-signals` | `github_repo_search(q)`, `github_org_profile(org)`, `hn_search(q, since)`, `reddit_search(sub, q)`, `producthunt_recent()`, `rss_poll(feed_ids)`, `kev_recent()` |
| `cindra-verify` | `validate_email(addr)` (syntax → MX → disposable → role → catch-all detection; **no SMTP VRFY probing**), `normalize_domain(url)`, `normalize_phone(raw, cc)`, `liveness(domain)` |
| `cindra-store` | `upsert_company`, `upsert_lead`, `find_duplicates(company)`, `vector_search(text, k)`, `suppression_check(domain, email)`, `record_feedback` |
| `cindra-discord` | `send_lead_card(lead, channel)`, `send_digest(leads)`, `send_ops_alert(level, msg)` — embed builder + rate-limit handling |
| `cindra-thermal` | `host_status()` → temp, throttle flags, RAM, disk, ollama state |

Third-party MCP to wire into `.mcp.json` for dev convenience: `mcp-server-fetch`,
`mcp-server-sqlite`. Playwright MCP stays **disabled by default** and behind a per-source flag.

---

## §9 — AGENT ROSTER

Each is a `.claude/agents/*.md` subagent for dev-time work **and** a Python class implementing
`async def run(self, job: Job) -> StageResult` for runtime. Keep the prompt text in one place
(`prompts/`) and load it in both.

| # | Agent | Input → Output | Non-obvious responsibility |
| --- | --- | --- | --- |
| 1 | **Scout** | ICP + trigger taxonomy → `QueryPlan[]` | Generates *diverse* query batches, tracks which query shapes historically produced Tier A leads, retires dead ones. Never runs the same query twice in a cache window. |
| 2 | **Harvester** | `QueryPlan` → `RawDocument[]` | Owns all network egress. Budget guard, robots.txt, backoff, content-addressed cache. Nothing else in the system makes an outbound request. |
| 3 | **Extractor** | `RawDocument` → `Candidate` | Local LLM, JSON-Schema-constrained. Extracts company, contacts, trigger claims. **Every claim must carry the source URL and a ≤500-char snippet or it is dropped.** |
| 4 | **Resolver** | `Candidate` → canonical `Company` | Entity resolution: registrable-domain canonicalization (PSL-aware), then rapidfuzz on names, then bge-m3 cosine ≥ 0.88 for near-dupes. Merges rather than duplicates. |
| 5 | **Enricher** | `Company` → enriched `Company` | Passive OSINT fan-out, all in parallel, all failure-tolerant. Any single source failing must not fail the lead. |
| 6 | **Validator** | enriched → validated | Pydantic strict, email verification, domain liveness, evidence-URL reachability, trigger-decay recompute. Kills leads whose evidence 404s. |
| 7 | **Scorer** | validated → scored `Lead` | Runs the §7 arithmetic in pure Python. Calls the LLM *only* for `rationale`, `outreach_angle`, `bengali_angle`. |
| 8 | **ComplianceGate** | `Lead` → `ComplianceVerdict` | The §12 checklist as executable code. Can veto anything. Logs every veto with a reason code. |
| 9 | **Dispatcher** | cleared `Lead` → Discord | Embed construction, tier routing, rate limits, idempotency (never send the same `lead_id` twice unless score moved ≥ 10 points), retry with backoff, dead-letter on final failure. |
| 10 | **Critic** | sampled leads + human feedback → tuning | Weekly: samples 20 dispatched leads, recomputes precision from Discord reactions, writes `reports/precision_YYYY-WW.md`, proposes weight and query-plan adjustments. **Proposes only — a human applies them.** |

---

## §10 — DISCORD OUTPUT CONTRACT

Read limits from Discord's current docs at build time and encode them as constants in one
module. Do **not** hardcode a rate limit you guessed — read the `X-RateLimit-*` response
headers and obey `retry_after` on 429. Treat roughly 5 requests / 2 s per webhook as the
planning assumption, verify empirically, and back off adaptively.

Hard payload limits to enforce with tests: 2000 chars `content`; 10 embeds per message; 6000
chars total across all embeds; title 256; description 4096; 25 fields; field name 256; field
value 1024; footer 2048. Truncate at a word boundary with `…` and always keep the evidence
links.

**Lead card format**

```
┌─ Embed ─────────────────────────────────────────────────────┐
│ ● color: A=#FF5A36 (ember)  B=#FFC169  C=#33E0C8 (cyan)      │
│ author:  ▲ TIER A · CindraScore 84 · snapshot_free           │
│ title:   Acme Health  ·  acmehealth.io                       │
│ url:     https://acmehealth.io                               │
│ description:                                                  │
│   Seed-stage healthtech, ~35 staff, Dhaka + Singapore.       │
│   Shipped a patient-facing AI assistant 11 days ago and is   │
│   hiring 2 AI engineers with no security role open.          │
│ fields:                                                       │
│   🔥 Triggers   T1_AI_SHIP (0.91) · T4_HIRING_AI_ONLY (0.78) │
│                 T2_FUNDING (0.85, $3.2M seed, 41d ago)       │
│   🎯 Angle      "You shipped an AI assistant handling patient│
│                 data last month — I'd like to run a free     │
│                 prompt-injection + data-leak check on it     │
│                 under a signed RoE. 2 days, no cost."        │
│   🇧🇩 বাংলা      <natural Bengali rewrite, BD only>            │
│   👤 Contact    Nabila R. — CTO · nabila@acmehealth.io       │
│                 [verified] · linkedin.com/in/…               │
│   🛰 Surface    47 CT subdomains (+12 in 30d) · public chatbot│
│                 · DMARC p=none · no security.txt             │
│   📎 Evidence   [ProductHunt] [Greenhouse] [TechCrunch] [crt.sh]
│   ⚖️ Compliance PASS · basis: public business contact ·       │
│                 passive-only ✓ · suppression ✓               │
│ footer:  lead_id 9f2a…c41 · v1.4.2 · 2026-08-15 09:14 +06    │
└──────────────────────────────────────────────────────────────┘
```

Webhook routing via env, never committed:
`DISCORD_WEBHOOK_HOT`, `_WARM`, `_DIGEST`, `_OPS`. Redact webhook URLs in every log line.

Digest message: one embed per lead, max 10 embeds, paginate beyond that, plus a summary line —
harvested / candidates / deduped / dispatched / rejected-with-reasons / SerpAPI credits used /
API spend / peak temp.

---

## §11 — VALIDATION, DEDUPLICATION, DATA QUALITY

```yaml
canonical_key: registrable domain via Public Suffix List, lowercased, punycode-normalized,
               www/tracking params stripped.  lead_id = sha256(canonical_domain)[:16]

dedupe_ladder:
  1. exact canonical_domain match                          → merge
  2. rapidfuzz token_set_ratio(legal_name) >= 92 AND same country → merge, flag for review
  3. bge-m3 cosine(description) >= 0.88                    → flag as probable dupe, hold
  4. shared MX + shared registrant org                     → probable same entity, hold

email_verification:  syntax → domain MX → disposable-domain blocklist → role-account
                     detection → catch-all detection.  NEVER SMTP VRFY / RCPT probing.
                     status ∈ {verified, role_account, catch_all, risky, unverified, none}

hallucination_guards:
  - every Trigger requires >= 1 Evidence with a reachable URL (HEAD-checked at validation)
  - every factual field traces to an evidence_id; unsourced fields are set to None, not guessed
  - numeric claims (funding amount, headcount, subdomain count) must appear verbatim in a
    snippet or be dropped
  - Validator re-fetches a 10% random sample of evidence URLs daily; systematic drift → alert

freshness: nightly job recomputes trigger decay; a Lead whose score falls below its tier
           threshold is demoted and NOT re-dispatched. Leads with all triggers expired are
           archived, not deleted.
```

---

## §12 — COMPLIANCE GATE (hard rules, enforced in code and tested)

This is the section that protects the business. Implement as `ComplianceGate` with one test per
rule; CI fails if any test is missing.

**PASSIVE-ONLY — the absolute boundary.** Cindrasec's public promise is *"No scan ever starts
without a signed Rules of Engagement."* A prospecting bot that probes prospects would destroy
that promise and expose you legally. So:

```
ALLOWED (public-record / self-published lookups, no privileged interaction):
  ✓ DNS queries to public resolvers (A, MX, TXT/SPF, DMARC, DKIM, NS, DNSSEC)
  ✓ Certificate Transparency log queries (crt.sh)
  ✓ RDAP/WHOIS registry lookups
  ✓ HTTPS GET of the public homepage and standard well-known paths, robots-respecting,
    rate-limited, ≤2 requests per domain per day
  ✓ Licensed API results (SerpAPI, GitHub, HN, Reddit, EDGAR)

FORBIDDEN — must be impossible to express through any tool in this codebase:
  ✗ port scanning, service/version detection, nmap/masscan/naabu of prospects
  ✗ vulnerability scanning of prospects (nuclei, nikto, ZAP, sqlmap, anything)
  ✗ directory/subdomain brute-forcing, fuzzing, or content discovery
  ✗ any authenticated request, credential test, or session against a prospect
  ✗ scraping anything behind a login, paywall, or explicit ToS prohibition
  ✗ SMTP VRFY/RCPT probing against a prospect's mail server
  ✗ any request rate that could degrade a prospect's service
Enforcement: source registry legality class checked at the tool boundary; a denylist of
binaries the runtime refuses to shell out to; a PreToolUse hook in dev that blocks these
commands; an integration test that asserts each forbidden action raises PassiveOnlyViolation.
```

**Data protection.** GDPR/UK-GDPR lawful basis: legitimate interest for B2B prospecting, with a
documented LIA in `docs/LIA.md`. Business contacts only — never personal emails, never
non-business individuals. Data minimization: store only what the outreach needs. Retention: 180
days for a lead with no engagement, then purge; align with your existing 30-day client-data
policy where they overlap. Honour erasure requests within 30 days via a
`scripts/erase_subject.py` that purges across every table plus the vector index. Maintain
`suppression_list` (domain + email + free-text org name), consulted before every dispatch,
appendable by a Discord slash command or a one-line CLI.

**Outreach law** (applies when *you* contact them, and it constrains what the angle may say):
CAN-SPAM (US) — accurate headers, physical address, working opt-out. GDPR/PECR (EU/UK) — B2B
legitimate interest, easy objection. Bangladesh — comply with local data-protection and cyber
law; keep RoE-first framing everywhere. **The Dispatcher never sends outbound email. It writes
lead cards to your Discord. A human decides who to contact.** That human-in-the-loop is a
feature; do not automate past it.

**Prompt-injection defense.** You are feeding untrusted scraped web content into an LLM. Treat
every `RawDocument` as hostile: wrap it in explicit delimiters, instruct the model that content
inside is data and never instruction, strip/flag `ignore previous instructions`-class strings,
never let extracted content reach a tool-call argument without schema validation, and run the
extractor with no network-capable tools. Log and quarantine any document that trips the
injection heuristics — as a security studio, these are also interesting artifacts.

**Verdict object:** `ComplianceVerdict{passed: bool, checks: dict[str,bool], basis: str,
vetoes: list[str], reviewed_at: datetime}`. Attached to every lead. Vetoed leads go to a
`quarantine` table with the reason, never silently dropped.

---

## §13 — OBSERVABILITY, RESILIENCE, OPS

- **Logging:** `structlog` JSON to `/var/log/cindraleads/*.jsonl`, rotated by size. Every line
  carries `job_id`, `lead_id`, `stage`, `duration_ms`, `cost_units`. Secrets redacted by a
  processor, tested.
- **Metrics:** `metrics` table + `/metrics` Prometheus endpoint on localhost. Track per stage:
  throughput, p50/p95 latency, error rate, LLM tok/s, cache hit rate, SerpAPI credits, API USD
  spend, SoC temp, throttle events, queue depth, dead-letter count.
- **Health:** `/healthz` returns degraded/healthy per subsystem (ollama, sqlite, network,
  budget, thermal). systemd `Restart=on-failure`, `WatchdogSec`.
- **Failure semantics:** every stage idempotent and retry-safe; jobs leased with expiry so a
  crash mid-stage self-heals; exponential backoff with jitter on all network calls; circuit
  breaker per source (3 consecutive failures → 15 min open); dead-letter queue with a replay
  CLI. **The pipeline must never crash-loop because one source is down.**
- **Backups:** nightly `sqlite3 .backup` to a second NVMe path + weekly encrypted off-device
  copy. Test the restore in Phase 8 — an untested backup is not a backup.
- **CLI (`cindra`):** `harvest --source X`, `replay <job_id>`, `lead show <id>`,
  `suppress <domain>`, `budget`, `dispatch-test`, `benchmark-models`, `precision-report`,
  `erase-subject <email>`.

---

## §14 — REPO LAYOUT

```
cindraleads/
├── CLAUDE.md                     # §18
├── PLAN.md                       # you write this in plan mode
├── .mcp.json  .env.example  pyproject.toml  Makefile
├── .claude/
│   ├── agents/{scout,harvester,extractor,resolver,enricher,validator,
│   │           scorer,compliance-gate,dispatcher,critic}.md
│   ├── skills/{pi-ops,passive-osint,discord-embeds,scoring-calibration}/SKILL.md
│   ├── commands/{harvest,precision-report,add-source,ship}.md
│   └── settings.json             # hooks: PreToolUse passive-only guard, PostToolUse ruff+pytest
├── config/{icp.yaml,sources.yaml,scoring.yaml,discord.yaml,models.yaml}
├── prompts/                      # shared by subagents and runtime
├── db/{schema.sql,migrations/}
├── mcp_servers/{cindra_serp,cindra_osint,cindra_registry,cindra_signals,
│                cindra_verify,cindra_store,cindra_discord,cindra_thermal}/
├── src/cindraleads/
│   ├── models.py  pipeline.py  queue.py  store.py  llm.py  budget.py
│   ├── thermal.py  compliance.py  scoring.py  dedupe.py  injection.py
│   ├── agents/  sources/  cli.py
├── tests/{unit,integration,fixtures,golden}/
├── deploy/{systemd/*.service,*.timer, install_pi.sh, ollama-override.conf}
├── docs/{ARCHITECTURE.md,RUNBOOK.md,BENCHMARKS.md,LIA.md,DATA_RETENTION.md,SOURCES.md}
└── reports/                      # weekly precision reports from the Critic
```

---

## §15 — BUILD PHASES (each ends in a demo I can see; do not skip a gate)

| Phase | Deliverable | Acceptance test |
| --- | --- | --- |
| **0 · Foundation** | Repo, pyproject, SQLite schema + migrations, Pydantic models, durable job queue, structlog, CLI skeleton, CI (ruff + mypy + pytest) | `make test` green; enqueue 100 jobs, `kill -9` the worker mid-run, restart, all 100 complete exactly once |
| **1 · Pi baseline** | Ollama installed, all §3 models pulled, `thermal.py`, `llm.py` with JSON-Schema-constrained calls, `docs/BENCHMARKS.md` with real tok/s | Extract a `Company` from 20 fixture HTML pages using local model only; ≥ 95% schema-valid; record p95 latency and peak temp |
| **2 · Harvest** | `cindra-serp` + `cindra-signals` MCP servers, cache, budget guard, robots.txt, circuit breakers | 50 real SerpAPI queries produce ≥ 200 raw docs; second identical run costs 0 credits (cache); budget guard halts at cap |
| **3 · Extract & Resolve** | Extractor + Resolver, FTS5 + sqlite-vec, dedupe ladder, injection guards | On a 300-doc corpus: ≥ 90% extraction accuracy vs. a 50-item hand-labelled golden set; < 2% dupes; 10 planted injection payloads all quarantined |
| **4 · Enrich & Validate** | `cindra-osint` + `cindra-verify`, passive enrichment fan-out, evidence URL checks | 100 domains enriched; every forbidden action in §12 raises `PassiveOnlyViolation`; email verification agrees with a known-good sample ≥ 90% |
| **5 · Score & Comply** | `scoring.py`, `compliance.py`, LLM rationale + bilingual angles | Score 50 leads; you hand-rank 20 and Spearman ρ ≥ 0.7; every §12 rule has a passing test; Bengali angles reviewed by you |
| **6 · Dispatch** | `cindra-discord`, embed builder, tier routing, idempotency, digest, ops alerts, **inbound Web3Forms/IMAP ingestion** | 30 leads land in the right channels, correctly formatted, zero duplicates on re-run, 429 handled with proper backoff, oversized payloads truncate gracefully |
| **7 · Autonomy** | systemd services + timers, watchdog, backups, `/healthz`, `/metrics`, install script | 72 h unattended: ≥ 15 Tier A+B leads/day, zero manual interventions, no OOM, no thermal throttle event |
| **8 · Self-improvement** | Critic agent, Discord reaction feedback loop, weekly precision report, calibration proposals | Week-1 report generated from real reactions; Critic proposes ≥ 3 concrete weight/query changes with evidence; restore-from-backup drill passes |

---

## §16 — TESTING

- **Golden fixtures:** 50 hand-labelled real pages → expected `Candidate` JSON. This is the
  regression suite for every prompt change. Never change a prompt without re-running it.
- **Property tests (hypothesis):** domain canonicalization is idempotent; scoring is monotonic
  in trigger weight; dedupe is symmetric and transitive; embed builder never exceeds any
  Discord limit for arbitrary input.
- **Adversarial suite:** 20 prompt-injection payloads embedded in scraped content, all must be
  quarantined and none may influence a tool call.
- **Compliance suite:** one test per §12 rule, plus a test asserting the forbidden-binary
  denylist is non-empty and enforced. CI fails on a missing compliance test.
- **Chaos:** kill workers mid-stage; return 500s/429s/timeouts from every source; fill the
  disk; simulate 85 °C; exhaust the SerpAPI budget; take Ollama down. In all cases: degrade,
  alert, resume — never crash-loop, never lose a job.
- **Cost regression:** a test that fails if median cloud-API spend per dispatched lead exceeds
  its budget.

---

## §17 — CLAUDE CODE PROJECT CONFIG

**`CLAUDE.md`** (keep under ~150 lines; it loads every session):

```markdown
# CindraLeads
Autonomous lead intelligence for Cindrasec. Runs on a Raspberry Pi 5 (16 GB).

## Absolute rules
1. PASSIVE-ONLY. Never write code that scans, probes, brute-forces, or authenticates
   against a prospect. See docs/COMPLIANCE.md §12. This is a legal boundary.
2. Every lead needs >= 1 Trigger with >= 1 reachable Evidence URL. No evidence, no lead.
3. Local model first. Cloud API is a rationed escalation path with a hard daily cap.
4. Nothing crosses an agent boundary except a validated Pydantic model.
5. Secrets live in .env, are redacted in logs, and are never committed.
6. The Dispatcher writes to Discord. It never emails a prospect. A human decides.

## Commands
make test | make lint | make typecheck | cindra harvest | cindra dispatch-test
make bench   # re-run model benchmarks on the Pi

## Conventions
Python 3.11+, asyncio, Pydantic v2, ruff + mypy strict, pytest.
Every network call: timeout, retry with jitter, circuit breaker.
Every LLM call: JSON Schema via Ollama `format`, parsed into Pydantic, retried once at temp 0.
Prefer stdlib and small arm64-native deps. Justify anything over ~200 MB.

## Where things are
config/*.yaml = behaviour (edit these, not code) · prompts/ = all LLM prompts
mcp_servers/ = tools (library + MCP wrapper, one implementation)
docs/RUNBOOK.md = what to do when it breaks at 3am
```

**`.mcp.json`** — register all eight `cindra-*` stdio servers plus `mcp-server-fetch` and
`mcp-server-sqlite`, with API keys via `${ENV_VAR}` expansion, never literals.

**Subagents** (`.claude/agents/*.md`): one per §9 agent. Give each a tight `description` (that
is what triggers delegation), a minimal `tools` allowlist, and `model: haiku` for the
mechanical ones (Harvester, Validator, Dispatcher) to keep dev cost down.

**Skills** (`.claude/skills/*/SKILL.md`): `pi-ops` (thermal, systemd, memory limits, NVMe),
`passive-osint` (the §12 allowlist as procedural guidance), `discord-embeds` (limits + builder
patterns), `scoring-calibration` (how to change weights safely without breaking golden tests).

**Hooks** (`.claude/settings.json`): a `PreToolUse` hook on `Bash` that hard-blocks
`nmap|masscan|nuclei|nikto|sqlmap|gobuster|ffuf|dirb|hydra` and any `curl`/`wget` loop
targeting a prospect domain; a `PostToolUse` hook on `Edit|Write` running `ruff format` and the
affected tests; a `Stop` hook that runs the compliance test suite before the session ends.

---

## §18 — PI BOOTSTRAP (`deploy/install_pi.sh` should automate all of this)

```bash
# Kernel / firmware
sudo sed -i 's/^dtparam=pciex1_gen=.*/dtparam=pciex1_gen=3/' /boot/firmware/config.txt || \
  echo 'dtparam=pciex1_gen=3' | sudo tee -a /boot/firmware/config.txt

sudo apt update && sudo apt install -y python3.11 python3.11-venv sqlite3 git \
     build-essential libsqlite3-dev jq

# Ollama (arm64) + models
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:1.7b && ollama pull qwen3:4b-instruct && ollama pull bge-m3

# Project
git clone <your-repo> ~/cindraleads && cd ~/cindraleads
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # includes claude-agent-sdk for the escalation tier
cp .env.example .env             # fill SERPAPI_KEY, ANTHROPIC_API_KEY, GITHUB_TOKEN,
                                 # DISCORD_WEBHOOK_{HOT,WARM,DIGEST,OPS}
cindra db migrate && cindra benchmark-models && cindra dispatch-test

sudo cp deploy/systemd/* /etc/systemd/system/
sudo systemctl enable --now cindraleads-worker.service cindraleads-harvest.timer \
                            cindraleads-enrich.timer cindraleads-digest.timer
```

**Secrets:** `.env` at `0600`, owned by the service user, `EnvironmentFile=` in the unit,
never in the repo, redacted in logs. Consider `systemd-creds` for the Discord webhooks.

---

## §19 — WHAT I WILL PUSH BACK ON

Tell me directly if you think any of this is wrong. Specifically, I want you to argue with me
if: a 4B model can't hit 90% extraction accuracy on real pages (then propose a different split
between local and cloud); sqlite-vec is the wrong choice at our expected volume; the durable
SQLite queue will bottleneck before 10k leads; or the Pi cannot sustain Phase 7's throughput
without thermal throttling. Bring benchmark numbers, not opinions.

---

## §20 — OPEN QUESTIONS FOR ME (answer before Phase 0)

1. Target geography split — what fraction Bangladesh/South Asia vs. global?
2. Daily lead volume you'd actually action: 10? 30? 50?
3. Do you want separate Discord servers per tier, or channels in one?
4. Do you already have a suppression list / past-client list to seed?
5. SerpAPI plan tier and monthly search quota?
6. Should the system also ingest your `contact@cindrasec.com` inbox for inbound (Phase 6)?
7. Do you want a lightweight local web dashboard, or is Discord the only UI?
8. Any verticals to hard-exclude beyond the anti-ICP list (gambling, adult, crypto, etc.)?
