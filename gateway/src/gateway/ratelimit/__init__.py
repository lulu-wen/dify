"""Rate-limiting layer for the gateway (PR #7 onward).

Phase 1a: per-tenant requests-per-minute token bucket in middleware.
Phase 1b: cost-based node admission + tokens-per-minute metering +
pre-charge/refund. Runtime-metrics-driven backpressure is Phase 2.
See the Edge AI Rate Limiting design doc.
"""

from __future__ import annotations

from gateway.ratelimit.cost import estimate_cost
from gateway.ratelimit.protocols import QuotaStore, RateLimiter
from gateway.ratelimit.quota import InMemoryQuotaStore
from gateway.ratelimit.retry import jittered_retry_after
from gateway.ratelimit.token_bucket import InMemoryTokenBucketLimiter
from gateway.ratelimit.types import (
    ActionCode,
    AdmissionGrant,
    RateDecision,
    RequestCost,
)

__all__ = [
    "ActionCode",
    "AdmissionGrant",
    "InMemoryQuotaStore",
    "InMemoryTokenBucketLimiter",
    "QuotaStore",
    "RateDecision",
    "RateLimiter",
    "RequestCost",
    "estimate_cost",
    "jittered_retry_after",
]
