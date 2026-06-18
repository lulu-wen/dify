"""Request-duration instrumentation middleware (PR #12a, R1 rewrite).

Pure ASGI middleware that records ``gateway_request_duration_seconds`` —
labels: normalised ``route`` (path with ``:id`` placeholders) + ``status``
class (2xx/3xx/4xx/5xx/other) — bounding cardinality.

Why pure ASGI instead of ``BaseHTTPMiddleware`` (R1 fix):

* ``BaseHTTPMiddleware.call_next`` returns as soon as response HEADERS are
  ready. For ``StreamingResponse`` (every ``/v1/chat/completions`` SSE
  stream) the body keeps flowing for tens of seconds afterwards — but the
  histogram observation would land at first-byte, reporting ~50 ms for a
  60 s stream and making the p99 panel useless for the dominant route.
  Pure ASGI lets us hook the ``send`` callable and observe at the final
  ``http.response.body`` event (``more_body=False``), capturing the true
  end-to-end duration.

* ``BaseHTTPMiddleware`` sits OUTSIDE Starlette's ``ExceptionMiddleware``,
  so domain exceptions (``GatewayError`` etc.) propagate THROUGH the
  middleware before the global handler shapes them into the actual 4xx
  response. The original code hardcoded ``status="5xx"`` in its
  ``except`` branch, so 429 ``RateLimitError`` etc. were mis-recorded as
  5xx — false-positive SRE alerts. Adding our middleware via
  ``app.add_middleware`` still keeps it INSIDE the FastAPI exception
  handlers (Starlette runs user-added middleware between
  ``ExceptionMiddleware`` and the router), so the status_code we see on
  ``http.response.start`` is already the real client-facing one.

Excludes ``/metrics``, ``/health``, ``/`` and FastAPI's auto-mounted
``/docs``, ``/openapi.json``, ``/redoc``. Trailing-slash variants
(``/health/``) are also excluded — see :func:`is_excluded_path`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from gateway.observability.labels import (
    is_excluded_path,
    normalise_route,
    status_class,
)
from gateway.observability.metrics import GATEWAY_REQUEST_DURATION_SECONDS

# ASGI type aliases. We don't import from `asgiref` to avoid a runtime
# dep; the runtime contract is documented in PEP 3333 / asgiref.
_Scope = dict[str, Any]
_Message = dict[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]


class PrometheusMiddleware:
    """Pure-ASGI middleware: observes request_duration_seconds on body completion.

    Lifecycle:

    1. On ``http.response.start`` — capture ``status_code`` from the message
       envelope; this is the final, post-exception-handler status.
    2. On ``http.response.body`` with ``more_body=False`` — observe the
       elapsed time. Streaming responses naturally complete only here.
    3. If the inner app raises uncaught — observe with the captured
       status (or 5xx if start never fired) and re-raise.

    The middleware does NOT touch the message contents — it only listens.
    """

    def __init__(self, app: _ASGIApp) -> None:
        self._app = app

    async def __call__(
        self, scope: _Scope, receive: _Receive, send: _Send
    ) -> None:
        if scope["type"] != "http":
            # Lifespan, websocket, etc. — pass through with no instrumentation.
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if is_excluded_path(path):
            await self._app(scope, receive, send)
            return

        route = normalise_route(path)
        start = time.monotonic()
        # Default 5xx — if the inner app never sends response.start (e.g.
        # exception before any output) we record under 5xx.
        observed_status: int = 500
        observed: bool = False

        async def instrumented_send(message: _Message) -> None:
            nonlocal observed_status, observed
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                observed_status = int(message.get("status", 500))
            elif msg_type == "http.response.body":
                # ``more_body`` defaults False per ASGI spec — observe here
                # only when the body is final, covering streaming + regular
                # responses uniformly.
                if not message.get("more_body", False) and not observed:
                    elapsed = time.monotonic() - start
                    GATEWAY_REQUEST_DURATION_SECONDS.labels(
                        route=route,
                        status=status_class(observed_status),
                    ).observe(elapsed)
                    observed = True
            await send(message)

        try:
            await self._app(scope, receive, instrumented_send)
        except Exception:
            # Inner app raised AFTER (or instead of) sending the response.
            # If we never observed (no body event fired with more_body=False),
            # record one under the captured status (or 5xx default).
            if not observed:
                elapsed = time.monotonic() - start
                GATEWAY_REQUEST_DURATION_SECONDS.labels(
                    route=route,
                    status=status_class(observed_status),
                ).observe(elapsed)
            raise
