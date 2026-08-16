-- Phase 4: enrichment state.
--
-- `enriched_at` is load-bearing for scoring, not just bookkeeping. The `no_contact`
-- penalty means "we looked for a contact and found none", which is a fact about the
-- prospect. Before enrichment has run nobody has looked, and charging the penalty
-- anyway put every lead below the REJECT threshold regardless of quality -- measured
-- 2026-08-15, a fresh well-evidenced lead scored 0.
--
-- A nullable column rather than a boolean: NULL means never enriched, a timestamp says
-- when, and the maintenance job re-enriches whatever is stale.

ALTER TABLE companies ADD COLUMN enriched_at TEXT;

CREATE INDEX companies_enriched ON companies (enriched_at);
