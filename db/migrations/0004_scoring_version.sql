-- A lead records which scoring rules produced it.
--
-- Changing a weight, a penalty or the arithmetic makes every stored score stale, and
-- nothing notices: `enqueue_stale_scores` reconciles on
-- `MAX(trigger.observed_at) > lead.last_updated_at`, and a config edit moves neither.
-- The `single_source` fix landed against 108 leads that all kept their old numbers.
--
-- `pipeline_version` cannot stand in for this. It tracks the shape of the pipeline,
-- not the calibration, and bumping it for a weight change would misreport every lead
-- as having come from a different pipeline.
--
-- NULL means "scored before this column existed", which reconciliation treats as
-- stale -- the correct reading, since those leads predate any recorded calibration.
ALTER TABLE leads ADD COLUMN scoring_version TEXT;
CREATE INDEX leads_scoring_version ON leads (scoring_version);
