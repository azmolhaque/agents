"""Pipeline stages.

Only three of the ten ever touch an LLM (PLAN.md 2.9): the Extractor, the Scorer's
prose fields, and — on paper — the Scout. The rest are deterministic Python, and the
ones that do not need a model are not given one, structurally rather than by
convention.
"""

from cindraleads.agents.dispatcher import DISPATCH_KIND, Dispatcher
from cindraleads.agents.enricher import ENRICH_KIND, Enricher, enqueue_unenriched
from cindraleads.agents.extractor import (
    Extractor,
    enqueue_stale_extractions,
    enqueue_unextracted,
)
from cindraleads.agents.harvester import EXTRACT_KIND, HARVEST_KIND, Harvester
from cindraleads.agents.resolver import RESOLVE_KIND, Resolver
from cindraleads.agents.scorer import SCORE_KIND, Scorer, enqueue_stale_scores
from cindraleads.agents.scout import QueryTemplate, Scout, ScoutConfig

__all__ = [
    "DISPATCH_KIND",
    "ENRICH_KIND",
    "EXTRACT_KIND",
    "HARVEST_KIND",
    "RESOLVE_KIND",
    "SCORE_KIND",
    "Dispatcher",
    "Enricher",
    "Extractor",
    "Harvester",
    "QueryTemplate",
    "Resolver",
    "Scorer",
    "Scout",
    "ScoutConfig",
    "enqueue_stale_extractions",
    "enqueue_stale_scores",
    "enqueue_unenriched",
    "enqueue_unextracted",
]
