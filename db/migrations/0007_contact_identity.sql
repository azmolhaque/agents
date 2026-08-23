-- One contact per (company, address), enforced rather than intended.
--
-- The Enricher has always written contacts with `INSERT OR IGNORE`, which reads as
-- dedupe protection and provides none: the primary key is a fresh `uuid4()` on every
-- call, so it can never collide, and nothing else constrained the pair. Every
-- re-enrichment therefore inserted another copy of the same address.
--
-- `cindra reconcile --force` re-enriches deliberately, and after a few of those the
-- call list was unusable: GAIA appeared 12 times, xalgorix 9, Weavori 6. The scores
-- were unaffected -- reachability reads the best contact by status, and a duplicate is
-- not better than its original -- so this was invisible to every report and only showed
-- up the first time a human tried to read the list and act on it.
--
-- Grouped by `COALESCE(email, contact_id)`, not by `email`. A NULL email means a
-- name-only contact, and grouping those together would merge genuinely different people
-- into one row -- a far worse outcome than the duplication being fixed. Keeping MIN
-- (rowid) preserves the earliest sighting, which carries the original `first_seen_at`.

DELETE FROM contacts
WHERE rowid NOT IN (
    SELECT MIN(rowid) FROM contacts
    GROUP BY canonical_domain, COALESCE(email, contact_id)
);

-- SQLite treats NULLs as distinct in a UNIQUE index, so several name-only contacts per
-- company remain legal. Only a repeated address is refused, which is what makes the
-- Enricher's `INSERT OR IGNORE` mean what it says.
CREATE UNIQUE INDEX contacts_domain_email ON contacts (canonical_domain, email);
