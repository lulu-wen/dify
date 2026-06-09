"""Per-tenant requests-per-minute rate limiting (PR #7 / Phase 1a).

Enforces a token-bucket RPM limit per customer. Wired **inner to**
:class:`~gateway.middleware.auth.AuthMiddleware` so it runs *after* auth
has resolved ``request.state.customer`` (see ``main.py`` for the ordering
rationale: Logging -> Auth -> RateLimit -> route).

Like ``AuthMiddleware``, this runs *outside* Starlette's
``ExceptionMiddleware``, so a rejection cannot ``raise`` — it would become
a 500. We render the 429 JSON envelope directly instead.

Only the request-rate dimension lives here. Token-per-minute metering and
cost-based admission (Phase 1b) need the parsed request body and so live
in the routers, not in this pre-body middleware.
"""

from __future__ import annotations

import math
import random
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from gateway.config import Settings
from gateway.errors import RateLimitError
from gateway.middleware.auth import EXEMPT_PATHS
from gateway.ratelimit.protocols import RateLimiter
from gateway.ratelimit.retry import jittered_retry_after
from gateway.ratelimit.types import ActionCode, RateDecision


def _rate_limit_headers(decision: RateDecision) -> dict[str, str]:
    """Standard informational headers so clients can self-pace."""
    return {
        "X-RateLimit-Limit": str(decision.limit),
        # Floor so a client never over-reads its remaining allowance.
        "X-RateLimit-Remaining": str(max(0, int(decision.remaining))),
    }


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket RPM limit, keyed per ``customer_id``.

    The per-customer limit comes from ``CustomerEntry.rpm_limit`` when set,
    else the gateway-wide ``settings.default_rpm``. ``settings.default_rpm_burst``
    sets the bucket capacity. When ``settings.rate_limit_enabled`` is False
    the middleware is a pass-through.

    ``Retry-After`` gets 0-1s of jitter (via the shared
    :func:`~gateway.ratelimit.retry.jittered_retry_after`) to avoid a
    synchronized retry storm. ``rng`` is injectable so tests can pin it.
    """

    def __init__(  # type: ignore[no-untyped-def]
        self,
        app,
        *,
        limiter: RateLimiter,
        settings: Settings,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._settings = settings
        self._rng = rng

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._settings.rate_limit_enabled:
            return await call_next(request)

        # Exempt paths (/health etc.) never carry a resolved customer —
        # AuthMiddleware skips them too, so there's nothing to key on.
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # AuthMiddleware runs first and either rejected the request or set
        # request.state.customer. Guard defensively rather than assume.
        customer = getattr(request.state, "customer", None)
        if customer is None:
            return await call_next(request)

        rpm = customer.rpm_limit if customer.rpm_limit is not None else self._settings.default_rpm
        decision = self._limiter.check(
            f"{customer.customer_id}:rpm",
            units_per_min=rpm,
            burst=self._settings.default_rpm_burst,
            cost=1.0,
        )

        if not decision.allowed:
            retry_after = jittered_retry_after(decision.retry_after_s, rng=self._rng)
            exc = RateLimitError(
                f"rate limit exceeded: {rpm} requests/min for customer "
                f"'{customer.customer_id}'",
                action=ActionCode.RETRY_AFTER,
                retry_after_s=retry_after,
            )
            headers = _rate_limit_headers(decision)
            # Retry-After mirrors the exception handler's rounding (ceil,
            # min 1) so middleware-rendered and handler-rendered 429s look
            # identical to a client.
            headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.to_openai_envelope(),
                headers=headers,
            )

        response = await call_next(request)
        for name, value in _rate_limit_headers(decision).items():
            response.headers[name] = value
        return response
