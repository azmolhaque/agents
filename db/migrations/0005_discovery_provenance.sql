-- Which discovery template first found this company.
--
-- Query quality was unmeasurable without it. The first real corpus reached 148
-- companies at 82% T1_AI_SHIP, full of side projects -- a tic-tac-toe game, a world
-- clock, a personal blog -- and there was no way to say which template produced them,
-- so any rewrite of `icp.yaml` would have been a guess with no way to check it.
--
-- Set once, on first resolution, and never overwritten: a company found by the HN
-- hiring thread and seen again on Product Hunt was *found* by the hiring thread, and
-- that is the credit the yield report has to attribute.
ALTER TABLE companies ADD COLUMN discovered_by TEXT;
CREATE INDEX companies_discovered_by ON companies (discovered_by);
