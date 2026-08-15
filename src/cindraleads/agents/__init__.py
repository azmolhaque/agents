"""Pipeline stages.

Only three of the ten ever touch an LLM (PLAN.md 2.9): the Extractor, the Scorer's
prose fields, and — on paper — the Scout. The rest are deterministic Python, and the
ones that do not need a model are not given one, structurally rather than by
convention.
"""

from cindraleads.agents.extractor import Extractor
from cindraleads.agents.harvester import EXTRACT_KIND, HARVEST_KIND, Harvester
from cindraleads.agents.resolver import RESOLVE_KIND, Resolver
from cindraleads.agents.scout import QueryTemplate, Scout, ScoutConfig

__all__ = [
    "EXTRACT_KIND",
    "HARVEST_KIND",
    "RESOLVE_KIND",
    "Extractor",
    "Harvester",
    "QueryTemplate",
    "Resolver",
    "Scout",
    "ScoutConfig",
]
