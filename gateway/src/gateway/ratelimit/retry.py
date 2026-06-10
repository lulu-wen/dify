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
) -> float | None:
    """Return ``base_seconds`` plus 0-1s of jitter, or None when input is None.

    ``base_seconds`` is the limiter's estimate. ``None`` is the limiter's
    signal that the request is structurally unsatisfiable (``cost > burst``,
    or ``refill_per_s <= 0``) — no amount of waiting can satisfy it, so we
    must NOT emit a finite ``Retry-After`` (lures standard SDKs into
    retrying the same request forever — codex PR #10 self-review-2 P2).
    Pass None through unchanged; callers raise an error with
    ``retry_after_s=None`` and the exception handler omits the header.

    Callers wanting a "default backoff with jitter" semantics (e.g.,
    admission overload where retrying after a small delay is sensible)
    must pass ``0.0`` explicitly — relying on None to mean "small jittered
    delay" buries the unsatisfiable signal.

    Negative or zero ``base_seconds`` is floored to 0 (jitter only).
    ``rng`` is injectable so tests can pin it.
    """
    if base_seconds is None:
        return None
    base = base_seconds if base_seconds > 0 else 0.0
    return base + rng(0.0, 1.0)
