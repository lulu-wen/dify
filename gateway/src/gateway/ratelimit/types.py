"""Value types shared across the rate-limiting layer.

Pure data — no logic, no I/O. Kept in their own module so the protocols,
the in-memory implementations, the middleware, and the tests all import
the same definitions without cycles.

Phase 1a: ``ActionCode``, ``RateDecision``.
Phase 1b adds ``RequestCost`` + ``AdmissionGrant`` (cost-based admission).
The runtime-metrics type (``RuntimeSnapshot``) is deferred to Phase 2 —
see the Edge AI Rate Limiting design doc, appendix B.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActionCode(StrEnum):
    """Advisory hint returned to the client alongside a 429 / 503.

    The client MAY act on these to self-heal (back off, ask for fewer
    tokens, downgrade model) — but the server enforces independently of
    whether the client honours the hint. Surfaced in the error body's
    ``action`` field; ``RETRY_AFTER`` additionally maps to the
    ``Retry-After`` header.
    """

    RETRY_AFTER = "RETRY_AFTER"
    REDUCE_MAX_TOKENS = "REDUCE_MAX_TOKENS"
    USE_SMALLER_MODEL = "USE_SMALLER_MODEL"
    REJECTED_OVER_QUOTA = "REJECTED_OVER_QUOTA"
    REJECTED_OVERLOAD = "REJECTED_OVERLOAD"


@dataclass(frozen=True)
class RateDecision:
    """Result of a single token-bucket check.

    ``allowed`` is the only field the caller must branch on. The rest feed
    response headers so clients can self-pace:

    - ``retry_after_s``: seconds until the bucket has room again (only
      meaningful when ``allowed`` is False). The exception handler rounds
      this up into the ``Retry-After`` header.
    - ``limit``: the configured per-minute rate (``X-RateLimit-Limit``).
    - ``remaining``: whole tokens left in the bucket right now
      (``X-RateLimit-Remaining``); float internally, clients see the floor.
    """

    allowed: bool
    retry_after_s: float | None
    limit: int
    remaining: float


@dataclass(frozen=True)
class RequestCost:
    """Estimated resource cost of a single inference request (Phase 1b).

    ``token_cost`` (= ``input_tokens + max_output_tokens``) is the budget
    unit used by both the TPM token bucket and the node admission gauge.
    It's a deliberate **upper bound** — we charge the worst case
    (``max_output_tokens``) up front so a request can never exceed what we
    reserved; the actual output is usually smaller. ``est_kv_bytes`` is
    informational (telemetry); admission compares ``token_cost``, not bytes.

    ``input_tokens`` is a cheap chars/4 heuristic, not a real tokenizer —
    exactness isn't needed to prevent OOM, and a tokenizer dependency isn't
    worth the cold-path cost (design decision, appendix B.2).
    """

    input_tokens: int
    max_output_tokens: int
    model_id: str
    token_cost: int
    est_kv_bytes: int


@dataclass(frozen=True)
class AdmissionGrant:
    """Result of a node-budget admission check (Phase 1b).

    On ``admitted=True`` the caller holds a reservation identified by
    ``charge_id`` and MUST release it via ``QuotaStore.settle`` (in a
    ``finally``) — on normal completion AND on client disconnect.
    On ``admitted=False`` no reservation was made; ``action`` carries the
    advisory code for the client (e.g. ``REJECTED_OVERLOAD``).
    """

    admitted: bool
    charge_id: str | None
    action: ActionCode | None
