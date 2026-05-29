"""In-memory token-bucket rate limiter.

The Phase 1a backend: per-process, in-memory, zero dependencies. Good for
a single edge node / single uvicorn worker. Multi-worker or multi-replica
deployments get one bucket *per process*, so the effective limit is
``workers x units_per_min`` — documented here and in the design doc;
swap in a Redis-backed :class:`~gateway.ratelimit.protocols.RateLimiter`
when that matters (the middleware depends only on the protocol).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from gateway.ratelimit.types import RateDecision


@dataclass
class _Bucket:
    tokens: float
    last_refill: float  # monotonic seconds


class InMemoryTokenBucketLimiter:
    """Classic token bucket, one bucket per ``key``.

    A bucket holds up to ``burst`` tokens and refills at
    ``units_per_min / 60`` tokens per second. A request consuming ``cost``
    tokens is allowed iff the (refilled) bucket holds at least ``cost``;
    otherwise it's rejected and the bucket is left untouched.

    Why token bucket over a fixed/sliding window: O(1) state per key,
    configurable burst, and smooth (no window-boundary 2x spikes). The
    refill+consume critical section is pure synchronous arithmetic, so on
    one asyncio event loop it's atomic across coroutines without a lock.

    Uses a **monotonic** clock — never wall-clock — so an NTP step or DST
    change can't corrupt refill timing. The clock is injectable for tests
    (drive time forward without ``sleep``).
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._clock = clock

    def check(
        self,
        key: str,
        *,
        units_per_min: int,
        burst: int,
        cost: float = 1.0,
    ) -> RateDecision:
        now = self._clock()
        # Callers enforce units_per_min >= 1 (config ``ge=1`` /
        # ``rpm_limit gt=0``), but this is a reusable component — guard the
        # division so a future caller passing 0 degrades to "never refills"
        # instead of crashing the request with ZeroDivisionError.
        refill_per_s = units_per_min / 60.0 if units_per_min > 0 else 0.0

        bucket = self._buckets.get(key)
        if bucket is None:
            # New key starts full — a customer's first request shouldn't be
            # throttled by an empty bucket. Full = burst capacity.
            bucket = _Bucket(tokens=float(burst), last_refill=now)
            self._buckets[key] = bucket
        else:
            # Refill for the elapsed time, capped at burst. ``max(0.0, ...)``
            # guards against a non-monotonic clock injected in a test.
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(float(burst), bucket.tokens + elapsed * refill_per_s)
            bucket.last_refill = now

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return RateDecision(
                allowed=True,
                retry_after_s=None,
                limit=units_per_min,
                remaining=bucket.tokens,
            )

        # Rejected: leave the bucket untouched (no partial consume) and
        # estimate when ``cost`` tokens will have refilled. When
        # ``refill_per_s`` is 0 (degenerate units_per_min<=0 config) the
        # bucket never refills, so there's no meaningful retry time → None.
        deficit = cost - bucket.tokens
        retry_after_s = deficit / refill_per_s if refill_per_s > 0 else None
        return RateDecision(
            allowed=False,
            retry_after_s=retry_after_s,
            limit=units_per_min,
            remaining=bucket.tokens,
        )
