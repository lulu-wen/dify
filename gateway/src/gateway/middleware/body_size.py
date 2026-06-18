"""Max-body-size enforcement (PR #13 R2 #9).

Some routes (audio_speech, audio_transcriptions, eventually embeddings
file uploads) read the full request body into memory before the
admission gate can reject. Without an upper bound an attacker can post
a multi-GB body and OOM the worker before any per-request cost check
runs. RPM caps don't help — they count requests, not bytes.

This middleware enforces a Content-Length cap at the OUTER edge of the
ASGI stack so abusive uploads are rejected with 413 before any
downstream code allocates. We intentionally only check the declared
Content-Length header rather than streaming-count bytes:

* Browsers and SDKs always send Content-Length for non-streamed
  bodies — the 99% case is covered.
* Chunked / streaming uploads (no Content-Length) are rare in this
  gateway's traffic profile and would require a wrapped receive() that
  raises 413 mid-body. Adding that complexity is deferred until we see
  a real chunked-upload code path.

The cap value comes from ``settings.max_body_bytes`` and applies to all
routes uniformly. Routes that legitimately need larger bodies can be
added to ``EXEMPT_PATHS`` later (none today).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from gateway.errors import InvalidRequestError


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared Content-Length exceeds ``max_bytes``.

    Returns the OpenAI-shaped 413 envelope directly (matching the auth
    middleware pattern) since we run outside Starlette's exception
    middleware and a raise here would 500 instead of 413.
    """

    def __init__(self, app: object, *, max_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max_bytes = max_bytes

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._max_bytes <= 0:
            # Cap disabled — useful for tests that need to exercise large
            # bodies without bumping a env var.
            return await call_next(request)
        content_length_header = request.headers.get("content-length")
        if content_length_header is not None:
            try:
                length = int(content_length_header)
            except ValueError:
                # Malformed Content-Length — treat as suspicious and reject.
                err = InvalidRequestError(
                    "malformed Content-Length header", param="Content-Length"
                )
                return JSONResponse(
                    status_code=400, content=err.to_openai_envelope()
                )
            if length > self._max_bytes:
                # 413 Payload Too Large. We use a fresh envelope rather
                # than a dedicated GatewayError subclass — this is a
                # single producer site and a new class for a single
                # status code is over-engineering for now.
                envelope = {
                    "error": {
                        "message": (
                            f"request body of {length} bytes exceeds the "
                            f"configured limit of {self._max_bytes} bytes"
                        ),
                        "type": "invalid_request_error",
                        "code": "payload_too_large",
                        "param": "Content-Length",
                    }
                }
                return JSONResponse(status_code=413, content=envelope)
        return await call_next(request)
