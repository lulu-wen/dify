"""Unit tests for PR #11 cleanup abstractions.

Covers the new shapes introduced when collapsing PR #9 R2 + PR #10 R2
deferred P3 findings:

- TaskSupervisor + safe_shutdown_step (PR #11 #1)
- CancelSink dataclass (PR #11 #2 — covered by existing converter tests)
- graceful_upstream_stream (PR #11 #3)
- middleware RPM degenerate → MISCONFIGURED_RATE (PR #11 #4 + #5)
- HeadroomConfig __post_init__ (PR #11 #6 — covered in test_runtime_metrics)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from gateway.errors import DifyTimeoutError, DifyUpstreamError
from gateway.lifecycle import TaskSupervisor, safe_shutdown_step
from gateway.streaming.error_safety import graceful_upstream_stream

# --------------------------------------------------------------------------- #
# TaskSupervisor
# --------------------------------------------------------------------------- #


class TestTaskSupervisor:
    @pytest.mark.asyncio
    async def test_fire_and_forget_tracks_and_self_discards(self) -> None:
        sup = TaskSupervisor()
        done = asyncio.Event()

        async def work() -> None:
            await asyncio.sleep(0)
            done.set()

        sup.fire_and_forget(work(), name="t1")
        assert sup.pending_count == 1
        await done.wait()
        # Allow done_callback to fire (next loop iteration)
        await asyncio.sleep(0)
        assert sup.pending_count == 0

    @pytest.mark.asyncio
    async def test_spawn_long_running_returns_task_handle(self) -> None:
        sup = TaskSupervisor()

        async def loop() -> None:
            while True:
                await asyncio.sleep(0.01)

        task = sup.spawn_long_running(loop(), name="loop")
        assert sup.pending_count == 1
        assert task.get_name() == "loop"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_shutdown_cancels_pending_tasks(self) -> None:
        sup = TaskSupervisor()

        async def long_loop() -> None:
            while True:
                await asyncio.sleep(0.01)

        for i in range(3):
            sup.spawn_long_running(long_loop(), name=f"loop-{i}")
        assert sup.pending_count == 3
        await sup.shutdown(deadline_s=1.0)
        assert sup.pending_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_logs_non_cancelled_exceptions(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sup = TaskSupervisor()

        async def buggy() -> None:
            raise RuntimeError("escaped from polling loop")

        sup.spawn_long_running(buggy(), name="buggy")
        # buggy() finishes quickly, so done_callback may already have
        # removed it before shutdown runs — either way, no warning is
        # logged for completed-without-cancel runs.
        await asyncio.sleep(0)
        await sup.shutdown(deadline_s=1.0)
        # The supervisor swallows the RuntimeError into logger.exception;
        # we don't assert log content directly here (structlog routing
        # depends on config) — the smoke test is that shutdown completes
        # without raising.

    @pytest.mark.asyncio
    async def test_shutdown_no_op_when_empty(self) -> None:
        sup = TaskSupervisor()
        # Should not raise / not block
        await sup.shutdown(deadline_s=0.001)


# --------------------------------------------------------------------------- #
# safe_shutdown_step
# --------------------------------------------------------------------------- #


class TestSafeShutdownStep:
    @pytest.mark.asyncio
    async def test_swallows_cancelled_error(self) -> None:
        async def raises_cancelled() -> None:
            raise asyncio.CancelledError

        # Should not raise — CancelledError is the expected shutdown reason.
        await safe_shutdown_step("test", raises_cancelled())

    @pytest.mark.asyncio
    async def test_logs_unexpected_exception_but_does_not_raise(self) -> None:
        async def raises_runtime() -> None:
            raise RuntimeError("cleanup bug")

        # The bug surfaces through logger.exception but the helper
        # doesn't propagate — lifespan finally must finish all cleanup
        # steps even if one of them is buggy.
        await safe_shutdown_step("buggy_step", raises_runtime())


# --------------------------------------------------------------------------- #
# graceful_upstream_stream
# --------------------------------------------------------------------------- #


async def _wrap(gen: AsyncIterator[str]) -> list[str]:
    out: list[str] = []
    async for chunk in graceful_upstream_stream(gen, request_id="r"):
        out.append(chunk)
    return out


class TestGracefulUpstreamStream:
    @pytest.mark.asyncio
    async def test_passes_chunks_through_on_normal_completion(self) -> None:
        async def src() -> AsyncIterator[str]:
            yield "data: 1\n\n"
            yield "data: 2\n\n"

        out = await _wrap(src())
        # Inner generator completed naturally — no [DONE] injected; the
        # converter is responsible for emitting its own terminator on
        # normal exhaustion.
        assert out == ["data: 1\n\n", "data: 2\n\n"]

    @pytest.mark.asyncio
    async def test_catches_dify_upstream_error_and_emits_done(self) -> None:
        async def src() -> AsyncIterator[str]:
            yield "data: prefix\n\n"
            raise DifyUpstreamError("Dify 503")

        out = await _wrap(src())
        assert out == ["data: prefix\n\n", "data: [DONE]\n\n"]

    @pytest.mark.asyncio
    async def test_catches_dify_timeout_error(self) -> None:
        async def src() -> AsyncIterator[str]:
            # ``yield`` here forces Python to treat the function as an
            # async generator even though the raise fires before any
            # value is produced — without it the body becomes a plain
            # coroutine and ``async for`` over the result raises TypeError.
            if False:  # pragma: no cover
                yield ""
            raise DifyTimeoutError("upstream timed out")

        out = await _wrap(src())
        assert out == ["data: [DONE]\n\n"]

    @pytest.mark.asyncio
    async def test_catches_httpx_request_error(self) -> None:
        async def src() -> AsyncIterator[str]:
            yield "data: x\n\n"
            raise httpx.RequestError("network drop")

        out = await _wrap(src())
        assert out == ["data: x\n\n", "data: [DONE]\n\n"]

    @pytest.mark.asyncio
    async def test_programming_error_propagates(self) -> None:
        """TypeError / AttributeError from a converter bug MUST NOT be
        whitewashed — they should surface to the global exception handler
        so tests catch them and operators see them at ERROR level.
        PR #10 self-review-3 #2; PR #11 lifts to shared utility."""

        async def src() -> AsyncIterator[str]:
            yield "data: prefix\n\n"
            raise TypeError("inner generator bug")

        with pytest.raises(TypeError, match="inner generator bug"):
            await _wrap(src())

    def test_default_log_event_preserves_pr10_chat_dashboard_key(self) -> None:
        """PR #11 R2 #2 sibling-sweep observability: graceful_upstream_stream
        was extracted from chat.py's inline pattern; the log event key
        MUST remain ``chat.stream_upstream_error`` by default so any
        operator dashboard / Loki query / Grafana alert keyed on the
        PR #10 name keeps firing. Phase 3 routers pass their own key.
        """
        import inspect

        sig = inspect.signature(graceful_upstream_stream)
        log_event_param = sig.parameters["log_event"]
        assert log_event_param.default == "chat.stream_upstream_error", (
            f"log_event default changed to {log_event_param.default!r}; "
            "production dashboards keyed on the old name will go blind"
        )


# --------------------------------------------------------------------------- #
# middleware unified MISCONFIGURED_RATE
# --------------------------------------------------------------------------- #


class TestMiddlewareUnifiedMisconfiguredRate:
    """PR #11 #4 + #5: middleware's two response branches collapsed into
    one path. When the limiter returns ``retry_after_s=None`` (structurally
    unsatisfiable RPM, e.g. ``refill_per_s <= 0`` or ``cost > burst``), the
    middleware emits ``MISCONFIGURED_RATE`` + NO ``Retry-After`` header so
    SDKs stop retrying. When finite, standard ``RETRY_AFTER`` + header.

    The Settings constraints (``ge=1`` on ``default_rpm`` and
    ``default_rpm_burst``) prevent the middleware-RPM degenerate path from
    being reached via real env config in production, but it IS reachable
    if a test or future feature injects a custom limiter. Unit-test the
    middleware with a fake limiter so the response path is pinned
    regardless of which limiter sits underneath.
    """

    @pytest.mark.asyncio
    async def test_degenerate_decision_emits_misconfigured_rate(self) -> None:

        from fastapi import FastAPI, Request, Response
        from fastapi.responses import JSONResponse

        from gateway.config import Settings
        from gateway.middleware.rate_limit import RateLimitMiddleware
        from gateway.ratelimit.protocols import RateLimiter
        from gateway.ratelimit.types import RateDecision
        from tests.conftest import make_customer

        class _NoneLimiter:
            """Always-rejects limiter with retry_after_s=None — the
            structurally-unsatisfiable signal (PR #10 self-review-2)."""

            def check(
                self, key: str, *, units_per_min: int, burst: int, cost: float = 1.0
            ) -> RateDecision:
                return RateDecision(
                    allowed=False, retry_after_s=None, limit=units_per_min, remaining=0.0
                )

        # Drive the middleware directly with a stub call_next + Request.
        settings = Settings(
            registry_path="unused.yaml", log_json=False, rate_limit_enabled=True
        )
        limiter: RateLimiter = _NoneLimiter()  # type: ignore[assignment]
        mw = RateLimitMiddleware(
            app=FastAPI(), limiter=limiter, settings=settings, rng=lambda _a, _b: 0.0
        )

        async def _call_next(_: Request) -> Response:
            return JSONResponse({"ok": True})

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"",
        }

        async def _receive() -> dict[str, object]:
            return {"type": "http.request"}

        request = Request(scope, _receive)   # type: ignore[arg-type]
        request.state.customer = make_customer()

        resp = await mw.dispatch(request, _call_next)
        assert resp.status_code == 429
        body = resp.body.decode()
        assert '"action":"MISCONFIGURED_RATE"' in body
        # No Retry-After header — backoff would be pointless
        assert "retry-after" not in {k.lower() for k in resp.headers}
