"""Prometheus instrumentation (PR #12a).

Module-level metric singletons + /metrics endpoint. Hooks in
``ratelimit``, ``routers``, ``dify``, ``lifecycle`` import the symbols
from ``metrics`` and call ``.inc()`` / ``.set()`` / ``.observe()``.

Off-path by design: the polling task that already feeds headroom
(PR #9) doubles as the source for vLLM-derived gauges; the
``/metrics`` endpoint is pull-based and does NOT trigger any work
beyond reading in-memory counters.
"""

from gateway.observability.metrics import render_metrics
from gateway.observability.middleware import PrometheusMiddleware
from gateway.observability.router import router

__all__ = ["PrometheusMiddleware", "render_metrics", "router"]
