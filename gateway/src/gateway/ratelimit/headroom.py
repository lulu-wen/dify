"""Headroom-driven admission scaling (PR #9 / Phase 2a).

Converts a live ``gpu_cache_usage_perc`` reading (0.0-1.0) into an effective
budget multiplier (0.0-1.0). The :class:`InMemoryQuotaStore`'s budget is
recomputed each poll as ``static_node_token_budget * factor`` so admission
naturally tightens when vLLM's KV cache pressure rises and loosens when
generation completes.

Math (user-approved 2026-06-09):

- usage <= soft_threshold (default 0.80) → factor = 1.0 (full budget)
- soft_threshold < usage < hard_threshold (default 0.95) → linear降階
- usage >= hard_threshold (0.95) → factor = 0.0 (reject all new admits)

The 5% gap between soft and hard preserves headroom for in-flight
generations that need to extend KV as their context grows — if we admitted
new requests at 95% the next token would push vLLM into OOM or paged-attention
swap, both of which cause TTFT spikes that defeat the point.

EWMA (alpha = 0.3) smooths the raw usage so a one-step spike doesn't slam the
budget to 0. alpha = 0.3 reaches ~70% of a step change in 3-4 polls (6-8s at
2s poll interval) — fast enough for edge bursts, slow enough to ignore
single-sample noise.

Fail-open: when a poll fails, the calculator's last EWMA value is left
untouched. The polling loop logs a warning and skips the budget update,
so the previous good budget stays in effect. See
:mod:`gateway.ratelimit.runtime_metrics`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeadroomConfig:
    """Thresholds + EWMA smoothing. Sourced from :class:`Settings` at startup."""

    soft_threshold: float  # default 0.80 — start降階
    hard_threshold: float  # default 0.95 — full reject
    ewma_alpha: float  # default 0.3 — smoothing factor (higher = more reactive)


class EwmaHeadroomCalculator:
    """Stateful smoother → linear scaler.

    Not thread-safe by design — there's exactly one polling task writing
    here, and the read happens in the same asyncio loop. If the
    architecture ever changes (multi-thread metrics, parallel pollers),
    add a lock then.
    """

    def __init__(self, config: HeadroomConfig) -> None:
        if not 0.0 <= config.soft_threshold < config.hard_threshold <= 1.0:
            raise ValueError(
                "headroom thresholds must satisfy "
                "0.0 <= soft < hard <= 1.0; "
                f"got soft={config.soft_threshold}, hard={config.hard_threshold}"
            )
        if not 0.0 < config.ewma_alpha <= 1.0:
            raise ValueError(
                f"ewma_alpha must be in (0.0, 1.0]; got {config.ewma_alpha}"
            )
        self._config = config
        # ``None`` until the first observation — first poll seeds the EWMA
        # directly (no smoothing into a 0.0 default; that would let a busy
        # edge node start with an artificially-low usage estimate and
        # over-admit for the first 6-8 seconds after restart).
        self._ewma: float | None = None

    def update(self, raw_gpu_cache_usage: float) -> float:
        """Smooth the new reading and return the effective budget factor (0-1)."""
        clamped = max(0.0, min(1.0, raw_gpu_cache_usage))
        if self._ewma is None:
            self._ewma = clamped
        else:
            alpha = self._config.ewma_alpha
            self._ewma = alpha * clamped + (1.0 - alpha) * self._ewma
        return self._factor_for(self._ewma)

    def _factor_for(self, usage: float) -> float:
        soft = self._config.soft_threshold
        hard = self._config.hard_threshold
        if usage <= soft:
            return 1.0
        if usage >= hard:
            return 0.0
        # Linear ramp: at soft → 1.0, at hard → 0.0
        return (hard - usage) / (hard - soft)

    @property
    def smoothed_usage(self) -> float | None:
        """Current EWMA value (for telemetry / tests). ``None`` before first update."""
        return self._ewma
