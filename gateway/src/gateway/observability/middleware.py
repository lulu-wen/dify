"""Request-duration instrumentation middleware (PR #12a).

Wraps every request to record ``gateway_request_duration_seconds``.
Labels: normalised ``route`` (path) + ``status`` class (2xx/3xx/4xx/5xx)
to keep cardinality bounded — never raw URL with path parameters or
status codes individually.

Order in the middleware chain: this should be the OUTERMOST middleware
(added LAST in Starlette's reverse-add semantics), so the recorded
duration covers all of: auth, rate-limit, routing, body parse, the
request handler, and response serialisation. ``Logging`` is even more
outer — it owns request_id assignment — but its overhead is tiny.

Excludes ``/metrics`` and ``/health`` endpoints from instrumentation
to avoid (a) self-recording the scraper, and (b) polluting the
histogram with health-check noise that distorts p99 estimates.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from gateway.observability.metrics import GATEWAY_REQUEST_DURATION_SECONDS

_EXCLUDED_PATHS = frozenset({"/metrics", "/health", "/"})


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Records ``gateway_request_duration_seconds`` per route + status class."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in _EXCLUDED_PATHS:
            return await call_next(request)

        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # Don't swallow — record as 5xx and re-raise so the global
            # exception handler still shapes the response.
            elapsed = time.monotonic() - start
            GATEWAY_REQUEST_DURATION_SECONDS.labels(
                route=_normalize_route(path),
                status="5xx",
            ).observe(elapsed)
            raise

        elapsed = time.monotonic() - start
        GATEWAY_REQUEST_DURATION_SECONDS.labels(
            route=_normalize_route(path),
            status=_status_class(response.status_code),
        ).observe(elapsed)
        return response


def _normalize_route(path: str) -> str:
    """Collapse path parameters so labels stay low-cardinality.

    Current routes are mostly static (``/v1/chat/completions`` etc.).
    For routes with path params (``/v1/datasets/{dataset_id}``), the
    raw path would explode the label set by dataset count; collapse
    by recognising known prefixes.

    Keep simple: walk known prefixes and stop at the first match. Add
    new patterns here when new path-param routes land.
    """
    # Datasets-related: collapse /v1/datasets/{id}/... -> /v1/datasets/:id/...
    if path.startswith("/v1/datasets/"):
        parts = path.split("/")
        # /v1/datasets/{id} or /v1/datasets/{id}/files etc.
        if len(parts) >= 4 and parts[3]:
            parts[3] = ":id"
        return "/".join(parts)
    if path.startswith("/v1/files/"):
        parts = path.split("/")
        if len(parts) >= 4 and parts[3]:
            parts[3] = ":id"
        return "/".join(parts)
    return path


def _status_class(status_code: int) -> str:
    """Bucket status codes into ``Nxx`` form."""
    return f"{status_code // 100}xx"
