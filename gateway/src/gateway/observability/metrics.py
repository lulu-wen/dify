"""Prometheus metric definitions for Gateway observability (PR #12a).

All metrics are module-level singletons registered against the default
``REGISTRY``. Hook points (``ratelimit_guard``, ``InMemoryQuotaStore``,
``chat`` router, ``DifyClient``, headroom poll loop, ``TaskSupervisor``)
import the symbols and call ``.inc()`` / ``.set()`` / ``.observe()``.

Naming convention:
    - ``gateway_*`` prefix scopes all metrics to this service.
    - Counter names end in ``_total`` (Prometheus convention).
    - Histograms measuring time use ``_seconds`` suffix.
    - Gauges have no suffix.

Cardinality discipline (review-1 prep):
    - Labels stay minimal — never use ``customer_id`` or other
      high-cardinality values; Prometheus query perf collapses past
      ~1000 active label combinations per metric.
    - ``route`` is normalised by the middleware to a small enum (path
      template), not raw URL.

Test isolation: production uses the default ``REGISTRY``. Tests that
need a clean registry should construct a fresh ``CollectorRegistry``
and pass it to the metric constructors — but the simpler pattern (and
what test_observability.py does) is to assert against ``generate_latest``
output without resetting, since assertions are scoped to specific
metric NAMES and counter behavior is monotonically meaningful.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #

GATEWAY_ADMISSION_TOTAL = Counter(
    "gateway_admission_total",
    "Admission Control invocations grouped by outcome and route",
    labelnames=("action", "route"),
)

GATEWAY_STREAM_DISCONNECT_TOTAL = Counter(
    "gateway_stream_disconnect_total",
    "資料串流回應過程中連線中斷的累計次數",
    labelnames=("reason",),  # normal | client_disconnect | upstream_error
)

GATEWAY_DIFY_CANCEL_TOTAL = Counter(
    "gateway_dify_cancel_total",
    "Gateway 向 Dify 發送 chat_messages_stop 的累計次數",
    labelnames=("result",),  # success | timeout | error | non_success
)

GATEWAY_RUNTIME_METRICS_POLL_TOTAL = Counter(
    "gateway_runtime_metrics_poll_total",
    "vLLM /metrics polling loop 的成功與失敗計數",
    labelnames=("result",),  # success | error
)

GATEWAY_DIFY_CALL_TOTAL = Counter(
    "gateway_dify_call_total",
    "Gateway 向 Dify 發送 HTTP 請求的累計總次數",
    labelnames=("endpoint", "status"),  # endpoint=chat_messages|app_login|... status=2xx|4xx|5xx
)

# --------------------------------------------------------------------------- #
# Gauges
# --------------------------------------------------------------------------- #

GATEWAY_ADMISSION_IN_FLIGHT_TOKEN_COST = Gauge(
    "gateway_admission_in_flight_token_cost",
    "當前處理中請求消耗的 Token 成本總量",
)

GATEWAY_ADMISSION_NODE_BUDGET = Gauge(
    "gateway_admission_node_budget",
    "Current node token budget cap (PR #9 headroom scales this dynamically)",
)

GATEWAY_ADMISSION_HEADROOM_FACTOR = Gauge(
    "gateway_admission_headroom_factor",
    "EWMA-smoothed admission headroom factor (0.0-1.0)",
)

GATEWAY_RUNTIME_METRICS_GPU_CACHE_USAGE = Gauge(
    "gateway_runtime_metrics_gpu_cache_usage",
    "vLLM KV cache usage ratio (raw, unsmoothed)",
)

GATEWAY_RUNTIME_METRICS_NUM_RUNNING = Gauge(
    "gateway_runtime_metrics_num_running",
    "vLLM 內正在執行中的推論任務數",
)

GATEWAY_RUNTIME_METRICS_NUM_WAITING = Gauge(
    "gateway_runtime_metrics_num_waiting",
    "vLLM 內排隊等待的推論任務數",
)

GATEWAY_APP_CACHE_SIZE = Gauge(
    "gateway_app_cache_size",
    "AppManager cache 內 Dify app 條目數",
)

GATEWAY_BACKGROUND_TASKS_PENDING = Gauge(
    "gateway_background_tasks_pending",
    "TaskSupervisor 內未完成的背景任務數",
)

# --------------------------------------------------------------------------- #
# Histograms
# --------------------------------------------------------------------------- #

GATEWAY_REQUEST_DURATION_SECONDS = Histogram(
    "gateway_request_duration_seconds",
    "客戶請求從發出到 Gateway 完全回應結束的總耗時",
    labelnames=("route", "status"),  # status = 2xx | 3xx | 4xx | 5xx
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

GATEWAY_SETTLE_SECONDS = Histogram(
    "gateway_settle_seconds",
    "流量准入後的費用結算與退款流程耗時",
    labelnames=("result",),  # ok | error
    buckets=(0.001, 0.01, 0.1, 0.5, 1.0, 5.0),
)

GATEWAY_DIFY_CALL_DURATION_SECONDS = Histogram(
    "gateway_dify_call_duration_seconds",
    "Gateway 向 Dify HTTP 呼叫的耗時",
    labelnames=("endpoint",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

GATEWAY_DIFY_CANCEL_DURATION_SECONDS = Histogram(
    "gateway_dify_cancel_duration_seconds",
    "Dify cancel POST duration (fire-and-forget pattern verification)",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0),
)

# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #


def render_metrics() -> tuple[bytes, str]:
    """Return current metric state as Prometheus exposition format.

    Body is UTF-8 encoded bytes; second value is the Content-Type
    string that scrapers expect (e.g.
    ``application/openmetrics-text; version=1.0.0; charset=utf-8``).
    """
    return generate_latest(), CONTENT_TYPE_LATEST
