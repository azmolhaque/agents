-- An interruption is not a failure, and charging them to one counter buries work.
--
-- `attempts` was incremented at *claim* time, so a job whose worker was killed
-- mid-flight looked identical to one whose stage ran and raised. Three worker
-- restarts during a slow LLM call therefore dead-lettered a `score.company` job that
-- had never once failed -- observed 2026-08-19, twice, during a day of deploys.
--
-- The claim-time increment existed for a real reason: a SIGKILL'd worker never gets
-- to call `fail()`, so without it a poison job that reliably wedges the worker would
-- retry forever. That protection now lives in `reclaims`, which `reclaim_expired`
-- charges instead -- so every claim still ends in exactly one of done, attempts+1, or
-- reclaims+1, and nothing is uncounted.
--
-- The ceiling is higher because the evidence is weaker. Three stage failures say the
-- job is broken. Three interruptions say we deployed three times.
ALTER TABLE jobs ADD COLUMN reclaims INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN max_reclaims INTEGER NOT NULL DEFAULT 10;
