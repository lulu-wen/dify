"""Reusable streaming error-safety wrapper (PR #11).

Extracts a pattern that originated as a PR #10 inline fix in
:func:`gateway.routers.chat.chat_completions`: once HTTP headers are
flushed for a ``StreamingResponse``, any exception propagating out of the
generator triggers Starlette's ``response already started`` crash. The
remediation is to catch the EXPECTED upstream-failure classes mid-stream,
log them, and emit a clean ``[DONE]`` terminator so the client sees a
well-formed stream end.

Lifted here so the next streaming endpoint (embeddings streaming /
future Phase 3 routers) doesn't have to re-copy the eight lines AND can
benefit from any later refinement (e.g. structured error chunks before
``[DONE]``).

Deliberately narrow: only the three upstream-shape exceptions
(``DifyUpstreamError``, ``DifyTimeoutError``, ``httpx.RequestError``) are
caught. Programming errors (``TypeError``, ``AttributeError``,
``KeyError`` from a bug in the inner generator) still propagate so they
surface to the global exception handler at ERROR level + show up in
tests, rather than being silently whitewashed (PR #10 self-review-3 #2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import structlog

from gateway.errors import DifyTimeoutError, DifyUpstreamError

logger = structlog.get_logger(__name__)


async def graceful_upstream_stream(
    inner: AsyncIterator[str],
    *,
    request_id: str,
    log_event: str = "chat.stream_upstream_error",
) -> AsyncIterator[str]:
    """Wrap an SSE generator to catch upstream errors mid-stream + emit ``[DONE]``.

    Usage in a router::

        async def event_source() -> AsyncIterator[str]:
            try:
                async for chunk in graceful_upstream_stream(
                    dify_to_openai_chunks(...),
                    request_id=request_id,
                ):
                    yield chunk
            finally:
                # cancel + close + settle (router-specific cleanup)
                ...

    The wrapped ``inner`` is consumed via plain ``async for``. When it
    raises one of the upstream-shape exceptions, we log at warning
    (operator visibility without alarming since these are recoverable)
    and yield the terminator. Other exceptions propagate; if the
    response was already flushed they'll crash Starlette's
    ``response already started`` guard — which is the correct signal
    for a real bug.

    ``log_event`` is parameterised with the chat-router event key as the
    default so PR #11's extraction-into-utility doesn't silently break
    operator dashboards / alerts keyed on ``chat.stream_upstream_error``
    (PR #11 R2 #2). Phase 3 routers should pass their own key, e.g.
    ``embeddings.stream_upstream_error``.
    """
    try:
        async for chunk in inner:
            yield chunk
    except (DifyUpstreamError, DifyTimeoutError, httpx.RequestError) as exc:
        logger.warning(
            log_event,
            request_id=request_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        yield "data: [DONE]\n\n"
