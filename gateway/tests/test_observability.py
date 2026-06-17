"""Unit tests for PR #12a Prometheus instrumentation.

Covers:
- /metrics endpoint responds 200 with Prometheus text content-type
- Each instrumented hook emits its metric (smoke test, not value-precise)
- PrometheusMiddleware normalises path params to keep cardinality bounded
- Excluded paths (/metrics, /health) don't recurse into request duration

Test isolation strategy: production uses the default ``REGISTRY``. Tests
assert against ``generate_latest`` output containing expected metric
names rather than resetting state — counters are monotonic so previous
test runs' increments don't invalidate assertions.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
from gateway.observability.middleware import (
    PrometheusMiddleware,
    _normalize_route,
    _status_class,
)

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

    def test_stream_disconnect_total_emits_with_reason(self) -> None:
        GATEWAY_STREAM_DISCONNECT_TOTAL.labels(reason="normal").inc()
        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'reason="normal"' in text

    def test_dify_cancel_total_emits_with_result(self) -> None:
        GATEWAY_DIFY_CANCEL_TOTAL.labels(result="success").inc()
        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'result="success"' in text

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
        # Gauges expose ``name value`` lines without trailing labels (these
        # are unlabelled). Spot-check a couple to confirm the set landed.
        assert "gateway_admission_in_flight_token_cost 12345" in text
        assert "gateway_admission_node_budget 200000" in text


class TestHistograms:
    def test_observe_round_trip(self) -> None:
        GATEWAY_REQUEST_DURATION_SECONDS.labels(route="/v1/chat/completions", status="2xx").observe(0.123)
        GATEWAY_SETTLE_SECONDS.labels(result="ok").observe(0.001)
        body, _ = render_metrics()
        text = body.decode("utf-8")
        # Histograms add ``_count`` / ``_sum`` / ``_bucket`` series — assert
        # the count line appeared for both.
        assert "gateway_request_duration_seconds_count" in text
        assert "gateway_settle_seconds_count" in text


# --------------------------------------------------------------------------- #
# PrometheusMiddleware
# --------------------------------------------------------------------------- #


class TestNormalizeRoute:
    def test_static_path_passes_through(self) -> None:
        assert _normalize_route("/v1/chat/completions") == "/v1/chat/completions"

    def test_dataset_id_collapsed_to_placeholder(self) -> None:
        assert (
            _normalize_route("/v1/datasets/abc-123-uuid")
            == "/v1/datasets/:id"
        )
        assert (
            _normalize_route("/v1/datasets/abc-123-uuid/retrieve")
            == "/v1/datasets/:id/retrieve"
        )

    def test_file_id_collapsed_to_placeholder(self) -> None:
        assert _normalize_route("/v1/files/file-xyz789") == "/v1/files/:id"

    def test_root_unchanged(self) -> None:
        # ``/`` is handled by the EXCLUDED_PATHS set in middleware; the
        # normaliser doesn't have a path for it.
        assert _normalize_route("/") == "/"


class TestStatusClass:
    def test_2xx_3xx_4xx_5xx(self) -> None:
        assert _status_class(200) == "2xx"
        assert _status_class(301) == "3xx"
        assert _status_class(404) == "4xx"
        assert _status_class(500) == "5xx"
        assert _status_class(429) == "4xx"


class TestPrometheusMiddleware:
    @pytest.mark.asyncio
    async def test_excluded_paths_skip_instrumentation(self) -> None:
        """``/metrics`` and ``/health`` must not be recorded."""
        from starlette.requests import Request

        mw = PrometheusMiddleware(app=FastAPI())

        async def _call_next(_: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        for excluded in ("/metrics", "/health"):
            scope = {
                "type": "http",
                "method": "GET",
                "path": excluded,
                "headers": [],
                "query_string": b"",
            }

            async def _receive() -> dict[str, object]:
                return {"type": "http.request"}

            req = Request(scope, _receive)  # type: ignore[arg-type]
            resp = await mw.dispatch(req, _call_next)
            assert resp.status_code == 200

        # Smoke check: no excluded labels showed up.
        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'route="/metrics"' not in text
        assert 'route="/health"' not in text

    @pytest.mark.asyncio
    async def test_records_duration_for_normal_request(self) -> None:
        from starlette.requests import Request

        mw = PrometheusMiddleware(app=FastAPI())

        async def _call_next(_: Request) -> JSONResponse:
            await asyncio.sleep(0)
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

        req = Request(scope, _receive)  # type: ignore[arg-type]
        resp = await mw.dispatch(req, _call_next)
        assert resp.status_code == 200

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'route="/v1/chat/completions"' in text
        assert 'status="2xx"' in text

    @pytest.mark.asyncio
    async def test_records_5xx_when_handler_raises(self) -> None:
        from starlette.requests import Request

        mw = PrometheusMiddleware(app=FastAPI())

        class _Boom(Exception):
            pass

        async def _call_next(_: Request) -> JSONResponse:
            raise _Boom("synthetic")

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"",
        }

        async def _receive() -> dict[str, object]:
            return {"type": "http.request"}

        req = Request(scope, _receive)  # type: ignore[arg-type]
        with pytest.raises(_Boom):
            await mw.dispatch(req, _call_next)

        body, _ = render_metrics()
        text = body.decode("utf-8")
        assert 'status="5xx"' in text
