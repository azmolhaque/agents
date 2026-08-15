"""Network egress. Everything that leaves the Pi goes through this package.

One chokepoint is the whole design. The passive-only guarantee is auditable because
there is exactly one function that makes an outbound request, and it checks the source's
legality class before doing so — rather than that rule being scattered across ten stages
and re-argued each time someone adds a source.
"""

from cindraleads.sources.cache import CachedDocument, DocumentCache, cache_key_for
from cindraleads.sources.circuit import CircuitBreaker, CircuitOpen, SourceBreakers
from cindraleads.sources.http import EgressClient, FetchDenied, FetchResult
from cindraleads.sources.registry import (
    FetchDefaults,
    PublicWebPolicy,
    Source,
    SourceRegistry,
)

__all__ = [
    "CachedDocument",
    "CircuitBreaker",
    "CircuitOpen",
    "DocumentCache",
    "EgressClient",
    "FetchDefaults",
    "FetchDenied",
    "FetchResult",
    "PublicWebPolicy",
    "Source",
    "SourceBreakers",
    "SourceRegistry",
    "cache_key_for",
]
