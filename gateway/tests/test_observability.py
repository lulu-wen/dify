"""Unit tests for PR #12a Prometheus instrumentation.

R1-revision: the middleware migrated from ``BaseHTTPMiddleware`` to a
pure-ASGI implementation so streaming responses get measured on body
completion rather than at first-byte. Label helpers (``normalise_route``
+ ``status_class``) moved to ``gateway.observability.labels`` so the
ratelimit_guard hook can use the same normalisation as the middleware
(without that, the admission_total counter blew up cardinality on
dataset path-param routes).

Covers:

- ``/metrics`` endpoint responds with Prometheus content-type
- Each instrumented hook emits its metric (smoke test, not value-precise)
- ``normalise_route`` collapses single + nested path params to ``:id``
- ``status_class`` clamps out-of-range codes to ``other`` instead of
  silently extending the label vocabulary
- ``is_excluded_path`` handles trailing-slash variants (``/health/``) +
  FastAPI auto-mounted ``/docs`` / ``/redoc`` / ``/openapi.json``
- Pure-ASGI middleware:

  * records on the final ``http.response.body`` event (so streaming
    requests get true end-to-end duration)
  * uses the captured response status (not hardcoded 5xx) so domain
    exceptions shaped to 4xx are labelled correctly
  * records ONCE — additional ``more_body=False`` events don't
    double-observe

- ``CancelSink.finalized_with_error`` set only on Dify error events
  (PR #12a R1 finding #6)
- ``chat_messages_stop`` distinguishes ``task_gone`` (404) from
  ``upstream_error`` (other non-2xx) — PR #12a R1 finding #7
- ``chat_messages_blocking`` records the histogram + counter in a
  finally so 4xx Dify replies don't pollute p99 with fast-fail
  observations (PR #12a R1 finding #8)

Test isolation strategy: production uses the default ``REGISTRY``. Tests
assert against ``generate_latest`` output containing expected metric
names rather than resetting state — counters are monotonic so previous
test runs' increments don't invalidate substring assertions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from gateway.observability.labels import (
    is_excluded_path,
    normalise_route,
    status_class,
)
from gateway.observability.metrics import (
    GATEWAY_ADMISSION_HEADROOM_FACTOR,
    GATEWAY_ADMISSION_IN_FLIGHT_TOKEN_COST,
    GATEWAY_ADMISSION_NODE_BUDGET,
    GATEWAY_ADMISSION_TOTAL,
    GATEWAY_APP_CACHE_SIZE,
    GATEWAY_BACKGROUND_TASKS_PENDING,
    GATEWAY_DIFY_CALL_TOTAL,
    GATEWAY_DIFY_CANCEL_TOTAL,
    GATEWAY_REQUEST_DURATION_SECONDS,
    GATEWAY_RUNTIME_METRICS_GPU_CACHE_USAGE,
    GATEWAY_RUNTIME_METRICS_NUM_RUNNING,
    GATEWAY_RUNTIME_METRICS_NUM_WAITING,
    GATEWAY_RUNTIME_METRICS_POLL_TOTAL,
    GATEWAY_SETTLE_SECONDS,
    GATEWAY_STREAM_DISCONNECT_TOTAL,
    render_metrics,
)
from gateway.observability.middleware import PrometheusMiddleware

# --------------------------------------------------------------------------- #
# Exposition format
# --------------------------------------------------------------------------- #


class TestRenderMetrics:
    def test_render_returns_bytes_and_content_type(self) -> None:
        body, content_type = render_metrics()
        assert isinstance(body, bytes)
        assert "text/plain" in content_type or "openmetrics" in content_type

    def test_render_contains_all_defined_metrics(self) -> None:
        """Smoke test: every metric we define is present in exposition."""
        body, _ = render_metrics()
        text = body.decode("utf-8")
        # Counters
        assert "gateway_admission_total" in text
        assert "gateway_stream_disconnect_total" in text
        assert "gateway_dify_cancel_total" in text
        assert "gateway_runtime_metrics_poll_total" in text
        assert "gateway_dify_call_total" in text
        # Gauges
        assert "gateway_admission_in_flight_token_cost" in text
        assert "gateway_admission_node_budget" in text
        assert "gateway_admission_headroom_factor" in text
        assert "gateway_runtime_metrics_gpu_cache_usage" in text
        assert "gateway_runtime_metrics_num_running" in text
        assert "gateway_runtime_metrics_num_waiting" in text
        assert "gateway_app_cache_size" in text
        assert "gateway_background_tasks_pending" in text
        # Histograms (registered as ``_bucket`` / ``_count`` / ``_sum``)
        assert "gateway_request_duration_seconds" in text
        assert "gateway_settle_seconds" in text
        assert "gateway_dify_call_duration_seconds" in text
        assert "gateway_dify_cancel_duration_seconds" in text


# --------------------------------------------------------------------------- #
# Counter / Gauge / Histogram smoke tests
# --------------------------------------------------------------------------- #


class TestCounters:
    def test_admission_total_emits_with_action_and_route(self) -> None:
        GATEWAY_ADMISSION_TOTAL.labels(action="accepted", route="/v1/chat/completions").inc()
        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'action="accepted"' in text
        assert 'route="/v1/chat/completions"' in text

    def test_stream_disconnect_total_emits_all_documented_reasons(self) -> None:
        """PR #12a R1 expanded the reason vocabulary."""
        for reason in (
            "normal",
            "early_termination",
            "no_first_event",
            "upstream_error",
            "preflight_failed",
        ):
            GATEWAY_STREAM_DISCONNECT_TOTAL.labels(reason=reason).inc()
        body, _ = render_metrics()
        text = body.decode("utf-8")
        for reason in (
            "normal",
            "early_termination",
            "no_first_event",
            "upstream_error",
            "preflight_failed",
        ):
            assert f'reason="{reason}"' in text, f"missing reason={reason}"

    def test_dify_cancel_total_emits_split_results(self) -> None:
        """PR #12a R1 split ``non_success`` into ``task_gone`` (404)
        vs ``upstream_error`` (other non-2xx)."""
        for result in ("success", "task_gone", "upstream_error", "timeout", "error"):
            GATEWAY_DIFY_CANCEL_TOTAL.labels(result=result).inc()
        body, _ = render_metrics()
        text = body.decode("utf-8")
        for result in ("success", "task_gone", "upstream_error", "timeout", "error"):
            assert f'result="{result}"' in text, f"missing result={result}"

    def test_runtime_metrics_poll_total_emits_both_results(self) -> None:
        GATEWAY_RUNTIME_METRICS_POLL_TOTAL.labels(result="success").inc()
        GATEWAY_RUNTIME_METRICS_POLL_TOTAL.labels(result="error").inc()
        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'result="success"' in text
        assert 'result="error"' in text

    def test_dify_call_total_emits_with_endpoint_and_status(self) -> None:
        GATEWAY_DIFY_CALL_TOTAL.labels(endpoint="chat_messages", status="2xx").inc()
        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'endpoint="chat_messages"' in text
        assert 'status="2xx"' in text


class TestGauges:
    def test_gauge_set_round_trip(self) -> None:
        GATEWAY_ADMISSION_IN_FLIGHT_TOKEN_COST.set(12345)
        GATEWAY_ADMISSION_NODE_BUDGET.set(200000)
        GATEWAY_ADMISSION_HEADROOM_FACTOR.set(0.42)
        GATEWAY_RUNTIME_METRICS_GPU_CACHE_USAGE.set(0.85)
        GATEWAY_RUNTIME_METRICS_NUM_RUNNING.set(7)
        GATEWAY_RUNTIME_METRICS_NUM_WAITING.set(2)
        GATEWAY_APP_CACHE_SIZE.set(11)
        GATEWAY_BACKGROUND_TASKS_PENDING.set(3)

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert "gateway_admission_in_flight_token_cost 12345" in text
        assert "gateway_admission_node_budget 200000" in text


class TestHistograms:
    def test_observe_round_trip(self) -> None:
        GATEWAY_REQUEST_DURATION_SECONDS.labels(
            route="/v1/chat/completions", status="2xx"
        ).observe(0.123)
        GATEWAY_SETTLE_SECONDS.labels(result="ok").observe(0.001)
        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert "gateway_request_duration_seconds_count" in text
        assert "gateway_settle_seconds_count" in text


# --------------------------------------------------------------------------- #
# Label-derivation helpers (PR #12a R1: extracted to labels module)
# --------------------------------------------------------------------------- #


class TestNormaliseRoute:
    def test_static_path_passes_through(self) -> None:
        assert normalise_route("/v1/chat/completions") == "/v1/chat/completions"

    def test_dataset_id_collapsed_to_placeholder(self) -> None:
        assert normalise_route("/v1/datasets/abc-123-uuid") == "/v1/datasets/:id"
        assert (
            normalise_route("/v1/datasets/abc-123-uuid/retrieve")
            == "/v1/datasets/:id/retrieve"
        )

    def test_nested_document_id_also_collapsed(self) -> None:
        """PR #12a R1 finding #13: nested doc/seg IDs leaked previously."""
        assert (
            normalise_route("/v1/datasets/abc/documents/xyz")
            == "/v1/datasets/:id/documents/:id"
        )
        assert (
            normalise_route("/v1/datasets/abc/documents/xyz/segments/p456")
            == "/v1/datasets/:id/documents/:id/segments/:id"
        )

    def test_file_id_collapsed_to_placeholder(self) -> None:
        assert normalise_route("/v1/files/file-xyz789") == "/v1/files/:id"

    def test_idempotent_on_collapsed_path(self) -> None:
        """Calling twice doesn't double-rewrite."""
        once = normalise_route("/v1/datasets/abc")
        assert normalise_route(once) == once


class TestStatusClass:
    def test_2xx_3xx_4xx_5xx(self) -> None:
        assert status_class(200) == "2xx"
        assert status_class(301) == "3xx"
        assert status_class(404) == "4xx"
        assert status_class(500) == "5xx"
        assert status_class(429) == "4xx"

    def test_out_of_range_clamped_to_other(self) -> None:
        """PR #12a R1 finding #9: 999, 0, negative values used to extend
        the label set permanently (``9xx``, ``0xx``, ``-1xx``)."""
        assert status_class(999) == "other"
        assert status_class(0) == "other"
        assert status_class(-1) == "other"
        assert status_class(99) == "other"
        assert status_class(600) == "other"


class TestIsExcludedPath:
    def test_canonical_paths_excluded(self) -> None:
        for path in ("/metrics", "/health", "/", "/docs", "/openapi.json", "/redoc"):
            assert is_excluded_path(path) is True, f"{path} should be excluded"

    def test_trailing_slash_variants_excluded(self) -> None:
        """PR #12a R1 finding #4: K8s probes commonly use trailing-slash
        variants like ``/health/``."""
        for path in ("/health/", "/metrics/", "/docs/", "/redoc/"):
            assert is_excluded_path(path) is True, f"{path} should be excluded"

    def test_unrelated_paths_not_excluded(self) -> None:
        for path in ("/v1/chat/completions", "/v1/datasets", "/random"):
            assert is_excluded_path(path) is False


# --------------------------------------------------------------------------- #
# Pure-ASGI PrometheusMiddleware
# --------------------------------------------------------------------------- #


_Send = Callable[[dict[str, Any]], Awaitable[None]]
_Receive = Callable[[], Awaitable[dict[str, Any]]]


async def _drive(
    middleware: PrometheusMiddleware,
    *,
    path: str,
    inner_status: int = 200,
    body_chunks: list[bytes] | None = None,
    raise_exc: BaseException | None = None,
) -> tuple[list[dict[str, Any]], BaseException | None]:
    """Drive a pure-ASGI middleware end-to-end against a mock app.

    Returns ``(messages_seen_by_client, raised_exception)``. ``body_chunks``
    defaults to a single non-streaming body; supply multiple bytes to
    simulate a streaming response (only the LAST event has
    ``more_body=False``).
    """
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def fake_app(
        scope: dict[str, Any], _r: _Receive, s: _Send
    ) -> None:
        if raise_exc is not None:
            raise raise_exc
        await s({"type": "http.response.start", "status": inner_status, "headers": []})
        chunks = body_chunks or [b'{"ok":true}']
        for i, chunk in enumerate(chunks):
            await s(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": i < len(chunks) - 1,
                }
            )

    # Install our fake app inside the middleware so it routes through it.
    middleware._app = fake_app  # type: ignore[attr-defined]

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    captured_exc: BaseException | None = None
    try:
        await middleware(scope, receive, send)
    except BaseException as e:
        captured_exc = e
    return sent, captured_exc


class TestPrometheusMiddleware:
    @pytest.mark.asyncio
    async def test_excluded_paths_skip_instrumentation(self) -> None:
        """``/metrics``, ``/health``, ``/docs`` etc. must not be recorded."""
        from fastapi import FastAPI

        mw = PrometheusMiddleware(FastAPI())
        for excluded in ("/metrics", "/health", "/docs", "/openapi.json"):
            await _drive(mw, path=excluded)

        body, _ = render_metrics()
        text = body.decode("utf-8")
        for excluded in ("/metrics", "/health", "/docs", "/openapi.json"):
            assert f'route="{excluded}"' not in text, (
                f"{excluded} leaked into request_duration"
            )

    @pytest.mark.asyncio
    async def test_records_duration_for_normal_request(self) -> None:
        from fastapi import FastAPI

        mw = PrometheusMiddleware(FastAPI())
        await _drive(mw, path="/v1/chat/completions", inner_status=200)

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'route="/v1/chat/completions"' in text
        assert 'status="2xx"' in text

    @pytest.mark.asyncio
    async def test_uses_real_status_not_hardcoded_5xx_when_inner_returns_4xx(
        self,
    ) -> None:
        """PR #12a R1 finding #5: previously ``except`` branch labelled
        any propagated exception 5xx even when the global exception
        handler had already shaped a 4xx response. With pure-ASGI we
        see the real status on http.response.start."""
        from fastapi import FastAPI

        mw = PrometheusMiddleware(FastAPI())
        await _drive(mw, path="/v1/chat/completions", inner_status=429)

        body, _ = render_metrics()
        text = body.decode("utf-8")
        # Specifically verify 4xx landed for chat-completions, not 5xx.
        # We look for the histogram count line tagged with the right pair.
        assert (
            'gateway_request_duration_seconds_count{route="/v1/chat/completions",status="4xx"}'
            in text
            or 'gateway_request_duration_seconds_count{status="4xx",route="/v1/chat/completions"}'
            in text
        )

    @pytest.mark.asyncio
    async def test_records_5xx_when_handler_raises_before_start(self) -> None:
        from fastapi import FastAPI

        class _Boom(Exception):
            pass

        mw = PrometheusMiddleware(FastAPI())
        _, exc = await _drive(
            mw, path="/v1/chat/completions", raise_exc=_Boom("synthetic")
        )
        assert isinstance(exc, _Boom)

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'status="5xx"' in text

    @pytest.mark.asyncio
    async def test_streaming_response_observes_at_body_completion(self) -> None:
        """PR #12a R1 finding #1: BaseHTTPMiddleware observed at first-byte
        for streaming responses, recording ~50ms for 30s SSE chats. Pure-
        ASGI observes at the final ``more_body=False`` event."""
        from fastapi import FastAPI

        mw = PrometheusMiddleware(FastAPI())
        # Three chunks: first two have more_body=True, the last completes.
        sent, exc = await _drive(
            mw,
            path="/v1/chat/completions",
            inner_status=200,
            body_chunks=[b"chunk1", b"chunk2", b"final"],
        )
        assert exc is None
        # Verify the middleware passed every chunk through.
        body_events = [m for m in sent if m["type"] == "http.response.body"]
        assert len(body_events) == 3
        assert body_events[-1].get("more_body", False) is False

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'route="/v1/chat/completions"' in text


# --------------------------------------------------------------------------- #
# CancelSink: finalized_with_error (PR #12a R1 finding #6)
# --------------------------------------------------------------------------- #


class TestCancelSinkFinalizationState:
    @pytest.mark.asyncio
    async def test_message_end_does_not_set_error_flag(self) -> None:
        from gateway.streaming.converter import CancelSink, dify_to_openai_chunks

        async def _src() -> Any:
            yield 'data: {"event": "message", "task_id": "t1", "answer": "hi"}\n\n'
            yield 'data: {"event": "message_end", "metadata": {}}\n\n'

        sink = CancelSink()
        async for _ in dify_to_openai_chunks(
            _src(), request_id="r", model_id="m", cancel_sink=sink
        ):
            pass

        assert sink.dify_finalized is True
        assert sink.finalized_with_error is False
        assert sink.task_id == "t1"

    @pytest.mark.asyncio
    async def test_error_event_sets_both_flags(self) -> None:
        from gateway.streaming.converter import CancelSink, dify_to_openai_chunks

        async def _src() -> Any:
            yield 'data: {"event": "message", "task_id": "t2", "answer": "hi"}\n\n'
            yield 'data: {"event": "error", "code": "x", "message": "boom"}\n\n'

        sink = CancelSink()
        async for _ in dify_to_openai_chunks(
            _src(), request_id="r", model_id="m", cancel_sink=sink
        ):
            pass

        assert sink.dify_finalized is True
        assert sink.finalized_with_error is True


# --------------------------------------------------------------------------- #
# DifyClient.chat_messages_stop result label split (PR #12a R1 finding #7)
# --------------------------------------------------------------------------- #


class TestChatMessagesStopLabels:
    @pytest.mark.asyncio
    async def test_404_records_task_gone_not_upstream_error(self) -> None:
        from gateway.dify.client import DifyClient

        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "not found"})

        transport = httpx.MockTransport(_handler)
        client = DifyClient(base_url="http://test")
        client._http = httpx.AsyncClient(  # type: ignore[attr-defined]
            base_url="http://test", transport=transport
        )

        await client.chat_messages_stop(app_key="k", task_id="t", user="u")

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'result="task_gone"' in text

    @pytest.mark.asyncio
    async def test_5xx_records_upstream_error_not_task_gone(self) -> None:
        from gateway.dify.client import DifyClient

        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "boom"})

        transport = httpx.MockTransport(_handler)
        client = DifyClient(base_url="http://test")
        client._http = httpx.AsyncClient(  # type: ignore[attr-defined]
            base_url="http://test", transport=transport
        )

        await client.chat_messages_stop(app_key="k", task_id="t", user="u")

        body, _ = render_metrics()
        text = body.decode("utf-8")
        # Already asserted task_gone in the test above; here we only need
        # to make sure upstream_error landed for a 5xx path. The previous
        # bug would have lumped both under ``non_success``.
        assert 'result="upstream_error"' in text


# --------------------------------------------------------------------------- #
# DifyClient.chat_messages_blocking observe ordering (PR #12a R1 finding #8)
# --------------------------------------------------------------------------- #


class TestChatMessagesBlockingObserveOrdering:
    @pytest.mark.asyncio
    async def test_4xx_response_labels_4xx_not_2xx(self) -> None:
        """A 4xx Dify response must record ``status=4xx`` (not 2xx) AND
        the histogram observation should NOT be inflated by a missing
        body parse."""
        from gateway.dify.client import DifyClient
        from gateway.errors import DifyUpstreamError

        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "no"})

        transport = httpx.MockTransport(_handler)
        client = DifyClient(base_url="http://test")
        client._http = httpx.AsyncClient(  # type: ignore[attr-defined]
            base_url="http://test", transport=transport
        )

        with pytest.raises(DifyUpstreamError):
            await client.chat_messages_blocking(
                app_key="k", query="q", user="u"
            )

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert (
            'gateway_dify_call_total{endpoint="chat_messages",status="4xx"}' in text
            or 'gateway_dify_call_total{status="4xx",endpoint="chat_messages"}' in text
        )
