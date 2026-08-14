-- 0001_init.sql — CindraLeads initial schema.
--
-- Migrations are the source of truth. db/schema.sql is a generated reference
-- dump (`make schema`) and is never applied directly.
--
-- Conventions:
--   * All timestamps are ISO-8601 UTC strings ('2026-08-14T09:14:02Z'). They sort
--     lexicographically, which is what the queue's lease comparisons rely on.
--   * All JSON blobs are TEXT holding a JSON document, validated by Pydantic on
--     the way in and out. SQLite's JSON1 is available for ad-hoc querying.
--   * canonical_domain is THE key. lead_id = sha256(canonical_domain)[:16].

-- ---------------------------------------------------------------- provenance

-- Content-addressed cache index. PLAN.md 2.7: the document BODY lives on disk at
-- var/cache/<sha256[:2]>/<sha256>.zst, not in here, so the DB stays small enough
-- that the nightly .backup is not itself a thermal event.
CREATE TABLE raw_documents (
    content_sha256  TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    legality_class  TEXT NOT NULL,
    content_type    TEXT,
    byte_size       INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT,
    injection_flag  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX raw_documents_url ON raw_documents (url);
CREATE INDEX raw_documents_expiry ON raw_documents (expires_at);

CREATE TABLE evidence (
    evidence_id     TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    snippet         TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    last_checked_at TEXT,
    reachable       INTEGER
);
CREATE INDEX evidence_sha ON evidence (content_sha256);

-- ---------------------------------------------------------------- entities

CREATE TABLE companies (
    canonical_domain    TEXT PRIMARY KEY,
    legal_name          TEXT,
    display_name        TEXT NOT NULL,
    country             TEXT,
    hq_city             TEXT,
    employee_band       TEXT,
    industry            TEXT,
    description         TEXT,
    tech_signals        TEXT NOT NULL DEFAULT '[]',
    ai_surface          TEXT NOT NULL DEFAULT '[]',
    subdomain_count_ct  INTEGER,
    dns_hygiene         TEXT,
    funding             TEXT,
    first_seen_at       TEXT NOT NULL,
    last_updated_at     TEXT NOT NULL
);
CREATE INDEX companies_country ON companies (country);

CREATE TABLE contacts (
    contact_id       TEXT PRIMARY KEY,
    canonical_domain TEXT NOT NULL REFERENCES companies (canonical_domain) ON DELETE CASCADE,
    full_name        TEXT,
    role_title       TEXT,
    persona          TEXT,
    email            TEXT,
    email_status     TEXT NOT NULL DEFAULT 'none',
    linkedin_url     TEXT,
    evidence_id      TEXT REFERENCES evidence (evidence_id),
    pii_basis        TEXT NOT NULL DEFAULT 'public_business_contact',
    first_seen_at    TEXT NOT NULL
);
CREATE INDEX contacts_domain ON contacts (canonical_domain);
CREATE INDEX contacts_email ON contacts (email);

CREATE TABLE triggers (
    trigger_id       TEXT PRIMARY KEY,
    canonical_domain TEXT NOT NULL REFERENCES companies (canonical_domain) ON DELETE CASCADE,
    code             TEXT NOT NULL,
    confidence       REAL NOT NULL,
    observed_at      TEXT NOT NULL,
    decays_at        TEXT NOT NULL,
    rationale        TEXT NOT NULL DEFAULT '',
    active           INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX triggers_domain ON triggers (canonical_domain);
CREATE INDEX triggers_decay ON triggers (active, decays_at);

-- A trigger is never a bare assertion: it must join to >= 1 evidence row.
CREATE TABLE trigger_evidence (
    trigger_id  TEXT NOT NULL REFERENCES triggers (trigger_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence (evidence_id) ON DELETE CASCADE,
    PRIMARY KEY (trigger_id, evidence_id)
);

CREATE TABLE candidates (
    candidate_id     TEXT PRIMARY KEY,
    content_sha256   TEXT NOT NULL,
    raw_payload      TEXT NOT NULL,
    resolved_domain  TEXT,
    status           TEXT NOT NULL DEFAULT 'new',
    created_at       TEXT NOT NULL
);
CREATE INDEX candidates_status ON candidates (status);

CREATE TABLE leads (
    lead_id            TEXT PRIMARY KEY,
    canonical_domain   TEXT NOT NULL REFERENCES companies (canonical_domain) ON DELETE CASCADE,
    score              INTEGER NOT NULL,
    score_breakdown    TEXT NOT NULL DEFAULT '{}',
    tier               TEXT NOT NULL,
    recommended_offer  TEXT NOT NULL,
    outreach_angle     TEXT NOT NULL DEFAULT '',
    bengali_angle      TEXT,
    risk_notes         TEXT NOT NULL DEFAULT '[]',
    compliance         TEXT NOT NULL DEFAULT '{}',
    first_seen_at      TEXT NOT NULL,
    last_updated_at    TEXT NOT NULL,
    pipeline_version   TEXT NOT NULL,
    prompt_version     TEXT NOT NULL DEFAULT '',
    archived           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX leads_tier ON leads (tier, archived);
CREATE UNIQUE INDEX leads_domain ON leads (canonical_domain);

-- ---------------------------------------------------------------- durable queue

-- PLAN.md: power loss on a Pi is a *when*. Every stage transition is a row update
-- inside a transaction; on boot the worker reclaims orphans by expired lease.
CREATE TABLE jobs (
    job_id           TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    payload          TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'pending',
    priority         INTEGER NOT NULL DEFAULT 100,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    dedupe_key       TEXT,
    worker_id        TEXT,
    lease_expires_at TEXT,
    available_at     TEXT NOT NULL,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
-- Enqueueing the same logical work twice is a no-op, not a duplicate job.
CREATE UNIQUE INDEX jobs_dedupe ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX jobs_claim ON jobs (status, available_at, priority, created_at);
CREATE INDEX jobs_lease ON jobs (status, lease_expires_at);

CREATE TABLE dead_letter (
    job_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    attempts     INTEGER NOT NULL,
    last_error   TEXT,
    died_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------- dispatch & feedback

CREATE TABLE dispatch_log (
    dispatch_id        TEXT PRIMARY KEY,
    lead_id            TEXT NOT NULL,
    channel            TEXT NOT NULL,
    tier               TEXT NOT NULL,
    score              INTEGER NOT NULL,
    idempotency_key    TEXT NOT NULL,
    -- PLAN.md 2.1: webhooks are write-only, so the Dispatcher POSTs with ?wait=true
    -- and stores the message id. Without this column the Phase 8 Critic has no way
    -- to join a reaction back to a lead.
    discord_message_id TEXT,
    dispatched_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX dispatch_idem ON dispatch_log (idempotency_key);
CREATE INDEX dispatch_lead ON dispatch_log (lead_id);
CREATE INDEX dispatch_message ON dispatch_log (discord_message_id);

CREATE TABLE feedback (
    feedback_id  TEXT PRIMARY KEY,
    lead_id      TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    source       TEXT NOT NULL,
    actor        TEXT,
    note         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX feedback_lead ON feedback (lead_id);

-- ---------------------------------------------------------------- compliance & ops

CREATE TABLE suppression_list (
    entry_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX suppression_unique ON suppression_list (kind, value);

CREATE TABLE quarantine (
    quarantine_id  TEXT PRIMARY KEY,
    subject_kind   TEXT NOT NULL,
    subject_id     TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    detail         TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE api_budget (
    budget_id   TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    day         TEXT NOT NULL,
    units_used  REAL NOT NULL DEFAULT 0,
    units_cap   REAL NOT NULL,
    usd_spent   REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX api_budget_day ON api_budget (provider, day);

CREATE TABLE metrics (
    metric_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    value      REAL NOT NULL,
    labels     TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);
CREATE INDEX metrics_name_time ON metrics (name, recorded_at);

-- ---------------------------------------------------------------- search & vectors

CREATE VIRTUAL TABLE companies_fts USING fts5 (
    canonical_domain UNINDEXED,
    display_name,
    legal_name,
    description,
    tokenize = 'unicode61'
);

-- PLAN.md 2.3: dedupe rung 3 ships gated off, but the table exists from day one so
-- enabling it is a config flip and never a migration. Rows are written only when
-- dedupe.vector_rung_enabled is true.
CREATE TABLE company_vectors (
    canonical_domain TEXT PRIMARY KEY REFERENCES companies (canonical_domain) ON DELETE CASCADE,
    dim              INTEGER NOT NULL,
    model            TEXT NOT NULL,
    embedding        BLOB NOT NULL,
    embedded_at      TEXT NOT NULL
);
