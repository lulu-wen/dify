"""Live vLLM Prometheus metrics for headroom-driven admission (PR #9 / Phase 2a).

Phase 1b's admission gauge tracked **reservations** (input + max_output) against
a static ``node_token_budget``. That guards against unbounded sum-of-reservations
but doesn't see real KV pressure: actual output is usually shorter than
max_output, so our gauge over-counts and the static budget has to be
conservative. Phase 2a closes the gap by polling vLLM's own metrics and
scaling the effective budget against the live ``gpu_cache_usage_perc`` reading.

Design (from /Notion Edge AI Rate Limiting appendix C.1):

- Polls vLLM's ``/metrics`` (Prometheus text format) every
  ``runtime_metrics_poll_s`` seconds on a background asyncio task. The task is
  spun up at FastAPI lifespan startup and cancelled on shutdown.
- Each poll yields a :class:`RuntimeSnapshot`; the chat-router admission path
  never blocks on the network — it reads the latest snapshot from memory
  (atomic on a single event loop, see :class:`InMemoryQuotaStore.set_budget`).
- Fail-open: a fetch / parse failure logs at warning and leaves the previous
  effective budget in place (Edge node 1st-rule is availability — a monitoring
  outage MUST NOT take generation offline). The cache_usage threshold and
  EWMA tuning live in :mod:`gateway.ratelimit.headroom`.

vLLM exposes (among many other metrics):

* ``vllm:gpu_cache_usage_perc`` — fraction of KV blocks in use (0.0-1.0)
* ``vllm:num_requests_running`` — currently generating
* ``vllm:num_requests_waiting`` — queued / pending
* ``vllm:time_to_first_token_seconds`` (histogram, not consumed in Phase 2a)

We parse only the four scalar gauges; histograms are deferred to Phase 3
when bounded priority queues need TTFT percentiles.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from gateway.ratelimit.headroom import EwmaHeadroomCalculator
from gateway.ratelimit.protocols import QuotaStore

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RuntimeSnapshot:
    """One poll's worth of vLLM live metrics.

    ``gpu_cache_usage_perc`` is the headroom-driven admission's primary
    input — it's what KV cache the model is actually using right now.
    The other fields are surfaced for telemetry / future Phase 3 use.
    """

    gpu_cache_usage_perc: float
    num_requests_running: int
    num_requests_waiting: int


class RuntimeMetrics(Protocol):
    """Pull-only metrics source. The polling loop calls ``snapshot``."""

    async def snapshot(self) -> RuntimeSnapshot: ...


# ---------------------------------------------------------------------- #
# vLLM Prometheus parser                                                  #
# ---------------------------------------------------------------------- #

# Match a Prometheus metric line:
#   metric_name 0.42
#   metric_name{label="x"} 0.42
#   metric_name 1.0e-4         (PR #9 review-1 #1: scientific notation with
#                                negative exponent must match; the previous
#                                ``[0-9.eE+]`` class dropped the minus)
# Leading whitespace and trailing comments are tolerated. We ignore HELP /
# TYPE / EOF lines naturally because they don't match.
_METRIC_LINE = re.compile(
    r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([-+]?[0-9.eE+\-]+)\s*$"
)


def _parse_prometheus(text: str, names: frozenset[str] | None = None) -> dict[str, float]:
    """Scrape scalar metrics from a Prometheus text payload.

    Returns the LAST seen value per metric name. vLLM emits one line per
    label-set; for the scalar gauges we care about there's only one line,
    so "last wins" is safe. Histogram buckets share their base name with a
    ``_bucket`` suffix and are NOT collected here (parser stays cheap).

    ``names`` is an optional allowlist of metric names; when given, lines
    whose name (the prefix before ``{`` or whitespace) is not in the set
    are skipped before the regex runs. vLLM /metrics emits hundreds of
    histogram-bucket lines per scrape and we only consume 3 scalars, so
    the allowlist drops ~99% of regex work (PR #9 review-1 #4).
    """
    out: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if names is not None:
            # Cheap name prefix check before regex: split on first '{' or
            # whitespace, compare to the allowlist. ``startswith`` over a
            # ``frozenset`` is O(1) per name, but allows mid-string false
            # positives (vllm:foo_bucket startswith vllm:foo); explicit
            # name parse avoids those.
            i = 0
            while i < len(line) and line[i] not in " \t{":
                i += 1
            if line[:i] not in names:
                continue
        m = _METRIC_LINE.match(line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


# The three scalar gauges PR #9 admission uses. Passed to ``_parse_prometheus``
# in production so we skip the histogram-bucket lines that dominate a vLLM
# payload (PR #9 review-1 #4).
_VLLM_WANTED: frozenset[str] = frozenset(
    {
        "vllm:gpu_cache_usage_perc",
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
    }
)


class VLLMPrometheusMetrics:
    """Polls vLLM's ``/metrics`` endpoint and parses the payload."""

    def __init__(
        self,
        *,
        url: str,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        # Own the http client when one isn't injected — tests inject a
        # transport-mocked client; production passes the URL only.
        self._url = url
        self._owns_http = http is None
        # Cache the Timeout object — value is immutable, no reason to
        # rebuild ~43k objects/day per node (PR #9 review-2 #2).
        self._timeout = httpx.Timeout(timeout_s)
        self._http = http or httpx.AsyncClient(timeout=self._timeout)

    async def snapshot(self) -> RuntimeSnapshot:
        """Fetch + parse the latest snapshot.

        Raises ``httpx.RequestError`` / ``httpx.HTTPStatusError`` on transport
        / status problems. The polling loop catches these and triggers
        fail-open behavior (last good budget stays in effect).
        """
        resp = await self._http.get(self._url, timeout=self._timeout)
        resp.raise_for_status()
        parsed = _parse_prometheus(resp.text, names=_VLLM_WANTED)
        # Default missing fields to 0 — better than rejecting the snapshot
        # if vLLM transiently omits a series during model swap.
        return RuntimeSnapshot(
            gpu_cache_usage_perc=parsed.get("vllm:gpu_cache_usage_perc", 0.0),
            num_requests_running=int(parsed.get("vllm:num_requests_running", 0)),
            num_requests_waiting=int(parsed.get("vllm:num_requests_waiting", 0)),
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()


# ---------------------------------------------------------------------- #
# Polling loop                                                             #
# ---------------------------------------------------------------------- #


async def run_metrics_poll_loop(
    *,
    metrics: RuntimeMetrics,
    calculator: EwmaHeadroomCalculator,
    quota_store: QuotaStore,
    static_budget: int,
    poll_interval_s: float,
) -> None:
    """Background poll loop. Cancelled by lifespan on shutdown.

    Each iteration:
    1. ``metrics.snapshot()`` — short-timeout HTTP GET.
    2. ``calculator.update(snapshot.gpu_cache_usage_perc)`` — EWMA + factor.
    3. ``quota_store.set_budget(int(static_budget * factor))`` — atomic.
    4. ``await asyncio.sleep(poll_interval_s)`` — sleep AFTER the fetch so
       the very first poll lands immediately at startup (PR #9 review-1
       #7). With sleep-first the operator opting into headroom-driven
       admission would still get the static budget for the full first
       poll interval — fine at 2s, but actively harmful when an operator
       picks a longer interval to cut vLLM scrape load. Fetch-then-sleep
       keeps the static budget only for the eager-fetch's own duration.

    Fail-open: any exception inside the loop body (other than
    ``asyncio.CancelledError`` from shutdown) is caught, logged at
    warning, and the loop continues. The previous budget value stays in
    effect — an unhealthy vLLM at startup just means we keep the static
    budget until /metrics comes online, never crashes the gateway.
    """
    logger.info(
        "runtime_metrics.poll_start",
        poll_interval_s=poll_interval_s,
        static_budget=static_budget,
    )
    try:
        while True:
            try:
                snap = await metrics.snapshot()
                factor = calculator.update(snap.gpu_cache_usage_perc)
                new_budget = int(static_budget * factor)
                await quota_store.set_budget(new_budget)
                logger.debug(
                    "runtime_metrics.tick",
                    gpu_cache_usage=snap.gpu_cache_usage_perc,
                    running=snap.num_requests_running,
                    waiting=snap.num_requests_waiting,
                    factor=factor,
                    effective_budget=new_budget,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Fail-open: leave the last good budget in place. An edge
                # node MUST stay available even when metrics are flapping.
                logger.warning(
                    "runtime_metrics.poll_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        logger.info("runtime_metrics.poll_stop")
        raise
