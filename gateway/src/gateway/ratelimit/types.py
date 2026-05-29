"""Value types shared across the rate-limiting layer.

Pure data — no logic, no I/O. Kept in their own module so the protocols,
the in-memory implementations, the middleware, and the tests all import
the same definitions without cycles.

Only the Phase 1a subset lives here for now: ``ActionCode`` and
``RateDecision``. The admission / cost / runtime-metrics types
(``RequestCost``, ``AdmissionGrant``, ``RuntimeSnapshot``) arrive with
Phase 1b — see the Edge AI Rate Limiting design doc.
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
