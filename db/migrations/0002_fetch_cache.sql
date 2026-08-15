-- 0002_fetch_cache.sql — the harvest cache index.
--
-- Two different questions need two different tables, and conflating them is a bug
-- waiting to happen:
--
--   fetch_cache     "have I already ASKED this?"   keyed by sha256(engine|query|params)
--   raw_documents   "what did I SEE?"              keyed by sha256(body)
--
-- They are not the same key. Two distinct queries can return byte-identical bodies
-- (an empty result set, a 404 page), and one query re-run tomorrow returns a
-- different body under the same cache key. raw_documents alone cannot express
-- "this query was answered at 09:00 and is good until 11:00".
--
-- Bodies are NOT stored here. They live gzip-compressed under var/cache/, and only
-- the index lives in SQLite, so the database stays small enough that the nightly
-- .backup is not itself a thermal event (PLAN.md 2.7).

CREATE TABLE fetch_cache (
    cache_key       TEXT PRIMARY KEY,
    content_sha256  TEXT NOT NULL,
    url             TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    legality_class  TEXT NOT NULL,
    content_type    TEXT,
    status_code     INTEGER,
    byte_size       INTEGER NOT NULL DEFAULT 0,
    stored_bytes    INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    last_hit_at     TEXT
);

CREATE INDEX fetch_cache_expiry ON fetch_cache (expires_at);
CREATE INDEX fetch_cache_source ON fetch_cache (source_id);
CREATE INDEX fetch_cache_sha ON fetch_cache (content_sha256);

-- Per-domain politeness for public_web, persisted so a restart cannot reset it.
-- The approved policy is 6 fetches per domain per rolling 24 h, >= 3 s apart
-- (PLAN.md 2.5); this table is what makes both halves survive a power cut.
CREATE TABLE domain_fetch_log (
    fetch_id   TEXT PRIMARY KEY,
    host       TEXT NOT NULL,
    url        TEXT NOT NULL,
    status     INTEGER,
    fetched_at TEXT NOT NULL
);

CREATE INDEX domain_fetch_host_time ON domain_fetch_log (host, fetched_at);
