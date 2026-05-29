"""Swappable seams for the rate-limiting layer.

Defining these as :class:`typing.Protocol` (structural) rather than ABCs
lets the middleware / routers depend on behaviour, not on a concrete
class — so the in-memory implementation can be swapped for a Redis-backed
one later (multi-replica / K8s) without touching any caller.

Phase 1a ships only :class:`RateLimiter`. ``QuotaStore`` and
``RuntimeMetrics`` (Phase 1b) will be added here when cost-based
admission lands.
"""

from __future__ import annotations

from typing import Protocol

from gateway.ratelimit.types import RateDecision


class RateLimiter(Protocol):
    """A rate-over-time limiter, metered in arbitrary cost units.

    Intentionally **synchronous**: a correct in-memory implementation does
    pure arithmetic (refill + consume) with no ``await`` in the critical
    section, so on a single asyncio event loop the check-and-decrement is
    effectively atomic across coroutines without an explicit lock. A future
    Redis implementation would run the same logic in a server-side Lua
    script (atomic there) and expose the same sync signature to callers.

    The limiter is generic over *what* is being metered — the caller picks
    the unit via ``cost``:

    - requests-per-minute: ``check(key, units_per_min=rpm, burst=..., cost=1.0)``
    - tokens-per-minute (Phase 1b): ``check(key, units_per_min=tpm, burst=..., cost=token_count)``

    ``key`` namespaces independent buckets (e.g. ``f"{customer_id}:rpm"``).
    """

    def check(
        self,
        key: str,
        *,
        units_per_min: int,
        burst: int,
        cost: float = 1.0,
    ) -> RateDecision:
        """Attempt to consume ``cost`` units from ``key``'s bucket.

        Returns a :class:`RateDecision`. When ``allowed`` is False the
        bucket is left unchanged (no partial consumption) and
        ``retry_after_s`` estimates when ``cost`` units will be available.
        """
        ...
