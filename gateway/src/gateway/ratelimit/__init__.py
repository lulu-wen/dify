"""Rate-limiting layer for the gateway (PR #7 onward).

Phase 1a exposes a per-tenant requests-per-minute token bucket enforced
in middleware. Cost-based admission, token-per-minute metering, and
runtime-metrics-driven backpressure (Phase 1b) build on the same
protocols. See the Edge AI Rate Limiting design doc.
"""

from __future__ import annotations

from gateway.ratelimit.protocols import RateLimiter
from gateway.ratelimit.token_bucket import InMemoryTokenBucketLimiter
from gateway.ratelimit.types import ActionCode, RateDecision

__all__ = [
    "ActionCode",
    "InMemoryTokenBucketLimiter",
    "RateDecision",
    "RateLimiter",
]
