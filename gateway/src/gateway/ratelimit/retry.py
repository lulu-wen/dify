"""Shared Retry-After jitter (Phase 1b — closes a 1a self-review P3).

A synchronized retry storm happens when every rejected client retries at
the same instant the server told them to — re-creating the load spike that
caused the rejection. Adding a random spread to ``Retry-After`` de-syncs
them.

In 1a this lived inside ``RateLimitMiddleware``. 1b adds router-raised
rate-limit / overload errors (TPM, admission) that flow through the global
exception handler, which would otherwise emit a jitter-free Retry-After.
Centralising the jitter here lets both the middleware and the routers apply
it identically at the point they construct the response/error.
"""

from __future__ import annotations

import random
from collections.abc import Callable


def jittered_retry_after(
    base_seconds: float | None,
    *,
    rng: Callable[[float, float], float] = random.uniform,
) -> float:
    """Return ``base_seconds`` plus 0-1s of jitter (min 0).

    ``base_seconds`` is the limiter's estimate (may be None when the wait is
    indeterminate — treated as 0, so the caller still gets a small jittered
    floor rather than nothing). ``rng`` is injectable so tests can pin it.
    """
    base = base_seconds if base_seconds is not None and base_seconds > 0 else 0.0
    return base + rng(0.0, 1.0)
