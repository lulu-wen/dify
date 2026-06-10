"""Swappable seams for the rate-limiting layer.

Defining these as :class:`typing.Protocol` (structural) rather than ABCs
lets the middleware / routers depend on behaviour, not on a concrete
class — so the in-memory implementation can be swapped for a Redis-backed
one later (multi-replica / K8s) without touching any caller.

Phase 1a ships :class:`RateLimiter`; Phase 1b adds :class:`QuotaStore`
(cost-based node admission). ``RuntimeMetrics`` (live vLLM polling) is
deferred to Phase 2 — see the design doc, appendix B.0.
"""

from __future__ import annotations

from typing import Protocol

from gateway.ratelimit.types import AdmissionGrant, RateDecision, RequestCost


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


class QuotaStore(Protocol):
    """Concurrent node-budget admission with pre-charge / refund (Phase 1b).

    Unlike :class:`RateLimiter` (rate over time), this tracks the total
    *in-flight* cost reserved across all active requests on the node and
    rejects new ones that would exceed the budget. It's the OOM guard:
    edge nodes share one finite vLLM whose ceiling is KV-cache memory, not
    request rate.

    Lifecycle: ``try_admit`` reserves ``cost.token_cost`` and returns a
    ``charge_id``; the caller MUST later ``settle`` that charge — in a
    ``finally`` so it fires on normal completion AND client disconnect — to
    release the reservation. ``settle`` is idempotent (a double-settle or
    unknown id is a no-op), which keeps the streaming + pre-flight-failure
    paths safe.

    The admit/settle path is synchronous for the same reason as
    :class:`RateLimiter`: pure arithmetic, atomic across coroutines on one
    event loop without a lock. ``set_budget`` is async because it's
    written by a different actor (the headroom polling task) and the
    in-memory impl wraps the write in an ``asyncio.Lock`` to future-proof
    multi-field extensions.
    """

    def try_admit(self, *, tenant: str, cost: RequestCost) -> AdmissionGrant:
        """Reserve ``cost.token_cost`` against the node budget, or reject."""
        ...

    def settle(self, charge_id: str, *, actual_output_tokens: int) -> None:
        """Release the reservation for ``charge_id``. Idempotent.

        ``actual_output_tokens`` is recorded for telemetry; the released
        amount is the full pre-charged ``token_cost`` (the request is done,
        so its entire reservation frees up regardless of how much it
        actually generated).
        """
        ...

    @property
    def budget(self) -> int:
        """Current effective budget. Used by the headroom polling task."""
        ...

    async def set_budget(self, new_budget: int) -> None:
        """Update the effective budget atomically (PR #9 / Phase 2a).

        Called by the background headroom task on each poll. Negative
        values must be floored to 0. Existing in-flight reservations are
        unaffected — only future ``try_admit`` calls see the new value.
        """
        ...
