"""In-memory node-budget admission store (Phase 1b).

Tracks total in-flight token cost across all active requests on this node
and rejects new requests that would push it over ``node_token_budget``.
This is the OOM guard — see ``QuotaStore`` in protocols.py for the contract
and the design doc appendix B.3 for the rationale.

Per-process / in-memory, same multi-worker caveat as the token bucket: N
workers each enforce their own budget. A Redis-backed implementation can
replace this behind the ``QuotaStore`` protocol when that matters.
"""

from __future__ import annotations

import uuid

from gateway.ratelimit.types import ActionCode, AdmissionGrant, RequestCost


class InMemoryQuotaStore:
    """Concurrent node-budget gauge with a pre-charge / refund ledger.

    ``try_admit`` reserves ``cost.token_cost`` if it fits under the budget
    and records the open charge; ``settle`` releases the full reservation.
    The reservation is the worst case (``input + max_output``), so an
    in-flight request can never use more KV than we reserved — releasing the
    whole thing on settle is correct because the request is then done.

    All mutation is synchronous (no ``await``), so it's atomic across
    coroutines on a single asyncio event loop without an explicit lock.
    """

    def __init__(self, *, node_token_budget: int) -> None:
        self._budget = node_token_budget
        self._in_flight = 0
        self._open: dict[str, RequestCost] = {}

    def try_admit(self, *, tenant: str, cost: RequestCost) -> AdmissionGrant:
        # ``tenant`` is accepted for telemetry / a future per-tenant gauge;
        # Phase 1b enforces the node-level budget only (the OOM guard).
        if self._in_flight + cost.token_cost > self._budget:
            return AdmissionGrant(
                admitted=False,
                charge_id=None,
                action=ActionCode.REJECTED_OVERLOAD,
            )
        charge_id = uuid.uuid4().hex
        self._open[charge_id] = cost
        self._in_flight += cost.token_cost
        return AdmissionGrant(admitted=True, charge_id=charge_id, action=None)

    def settle(self, charge_id: str, *, actual_output_tokens: int) -> None:
        # Idempotent: a double-settle (e.g. pre-flight-failure path AND the
        # streaming finally) or an unknown id is a harmless no-op. This is
        # what makes the streaming + error paths safe to both call settle.
        cost = self._open.pop(charge_id, None)
        if cost is None:
            return
        # Release the full pre-charged reservation. ``actual_output_tokens``
        # is telemetry only — the request is finished, so its entire
        # reservation frees up regardless of how much it actually generated.
        self._in_flight -= cost.token_cost
        if self._in_flight < 0:
            self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """Current reserved token cost (for tests / telemetry)."""
        return self._in_flight
