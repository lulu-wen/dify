"""Tests for PR #7 / Phase 1a — per-tenant RPM rate limiting.

Two layers:
- Unit: ``InMemoryTokenBucketLimiter`` math with an injected clock (no sleep).
- Integration: real middleware chain via TestClient against ``/v1/models``
  (auth-required but Dify-free, so it isolates rate limiting from upstream).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.main import create_app
from gateway.ratelimit import InMemoryTokenBucketLimiter
from gateway.ratelimit.types import ActionCode, RateDecision
from gateway.registry import CustomerRegistry

from .conftest import make_customer

# --------------------------------------------------------------------------- #
# Unit: token bucket math
# --------------------------------------------------------------------------- #


class _FakeClock:
    """Controllable monotonic clock — advance time without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTokenBucketMath:
    def test_new_key_starts_full(self) -> None:
        """A customer's first requests get the full burst immediately —
        an empty initial bucket would throttle a brand-new tenant."""
        clock = _FakeClock()
        limiter = InMemoryTokenBucketLimiter(clock=clock)
        # burst=5: first 5 allowed back-to-back at t=0.
        for i in range(5):
            d = limiter.check("c:rpm", units_per_min=60, burst=5, cost=1.0)
            assert d.allowed, f"request {i} should be allowed from a full bucket"
        # 6th exhausts the bucket.
        d = limiter.check("c:rpm", units_per_min=60, burst=5, cost=1.0)
        assert not d.allowed
        assert d.retry_after_s is not None and d.retry_after_s > 0

    def test_refill_over_time(self) -> None:
        """Bucket refills at units_per_min/60 per second."""
        clock = _FakeClock()
        limiter = InMemoryTokenBucketLimiter(clock=clock)
        # Drain a burst=2 bucket.
        assert limiter.check("c:rpm", units_per_min=60, burst=2, cost=1.0).allowed
        assert limiter.check("c:rpm", units_per_min=60, burst=2, cost=1.0).allowed
        assert not limiter.check("c:rpm", units_per_min=60, burst=2, cost=1.0).allowed
        # 60 rpm = 1 token/s. After 1s, exactly one token back.
        clock.advance(1.0)
        assert limiter.check("c:rpm", units_per_min=60, burst=2, cost=1.0).allowed
        assert not limiter.check("c:rpm", units_per_min=60, burst=2, cost=1.0).allowed

    def test_refill_caps_at_burst(self) -> None:
        """Idle time can't accumulate more than ``burst`` tokens."""
        clock = _FakeClock()
        limiter = InMemoryTokenBucketLimiter(clock=clock)
        limiter.check("c:rpm", units_per_min=60, burst=3, cost=1.0)  # 2 left
        clock.advance(3600.0)  # idle an hour
        # Only burst (3) available, not 3600.
        for _ in range(3):
            assert limiter.check("c:rpm", units_per_min=60, burst=3, cost=1.0).allowed
        assert not limiter.check("c:rpm", units_per_min=60, burst=3, cost=1.0).allowed

    def test_rejection_does_not_consume(self) -> None:
        """A rejected check leaves the bucket untouched (no partial drain),
        so ``retry_after_s`` stays accurate across repeated polls."""
        clock = _FakeClock()
        limiter = InMemoryTokenBucketLimiter(clock=clock)
        limiter.check("c:rpm", units_per_min=60, burst=1, cost=1.0)  # drain
        d1 = limiter.check("c:rpm", units_per_min=60, burst=1, cost=1.0)
        d2 = limiter.check("c:rpm", units_per_min=60, burst=1, cost=1.0)
        assert not d1.allowed and not d2.allowed
        # No time passed, no consumption → identical retry estimate.
        assert d1.retry_after_s == d2.retry_after_s

    def test_per_key_isolation(self) -> None:
        """Buckets are independent per key — one tenant draining theirs
        must not affect another."""
        clock = _FakeClock()
        limiter = InMemoryTokenBucketLimiter(clock=clock)
        assert limiter.check("a:rpm", units_per_min=60, burst=1, cost=1.0).allowed
        assert not limiter.check("a:rpm", units_per_min=60, burst=1, cost=1.0).allowed
        # b untouched.
        assert limiter.check("b:rpm", units_per_min=60, burst=1, cost=1.0).allowed

    def test_cost_parameter_meters_in_units(self) -> None:
        """``cost`` lets the same limiter meter tokens (Phase 1b) not just
        requests: a cost=5 consume drains 5 of the burst."""
        clock = _FakeClock()
        limiter = InMemoryTokenBucketLimiter(clock=clock)
        assert limiter.check("c:tpm", units_per_min=600, burst=10, cost=5.0).allowed
        d = limiter.check("c:tpm", units_per_min=600, burst=10, cost=10.0)
        assert not d.allowed  # only 5 left, asked for 10

    def test_decision_carries_header_fields(self) -> None:
        clock = _FakeClock()
        limiter = InMemoryTokenBucketLimiter(clock=clock)
        d = limiter.check("c:rpm", units_per_min=120, burst=5, cost=1.0)
        assert isinstance(d, RateDecision)
        assert d.limit == 120
        assert d.remaining == pytest.approx(4.0)


# --------------------------------------------------------------------------- #
# Integration: middleware chain via TestClient
# --------------------------------------------------------------------------- #


_AUTH = {"Authorization": "Bearer bsa_test_a"}


def _build_app(
    settings: Settings,
    registry: CustomerRegistry,
    fake_dify: Any,
    rate_limiter: Any | None = None,
) -> FastAPI:
    application = create_app(settings=settings, registry=registry, rate_limiter=rate_limiter)

    def factory(_: Any) -> Any:
        return fake_dify

    application.state.dify_client_factory = factory
    application.state.app_manager._client_factory = factory
    return application


class TestRateLimitMiddleware:
    def test_under_limit_passes_with_headers(
        self, registry: CustomerRegistry, fake_dify: Any
    ) -> None:
        settings = Settings(registry_path="unused.yaml", log_json=False, default_rpm=120, default_rpm_burst=5)
        app = _build_app(settings, registry, fake_dify)
        with TestClient(app) as client:
            r = client.get("/v1/models", headers=_AUTH)
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Limit"] == "120"
        assert int(r.headers["X-RateLimit-Remaining"]) >= 0

    def test_over_limit_returns_429_with_retry_after_and_action(
        self, registry: CustomerRegistry, fake_dify: Any
    ) -> None:
        # burst=2: 3rd rapid request (within the same sub-second window,
        # negligible refill) trips the limit.
        settings = Settings(registry_path="unused.yaml", log_json=False, default_rpm=60, default_rpm_burst=2)
        app = _build_app(settings, registry, fake_dify)
        with TestClient(app) as client:
            codes = [client.get("/v1/models", headers=_AUTH).status_code for _ in range(5)]
            blocked = client.get("/v1/models", headers=_AUTH)

        assert codes[:2] == [200, 200]
        assert 429 in codes[2:]
        assert blocked.status_code == 429
        # Retry-After present + integer >= 1.
        assert int(blocked.headers["Retry-After"]) >= 1
        body = blocked.json()
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["code"] == "rate_limited"
        assert body["error"]["action"] == ActionCode.RETRY_AFTER

    def test_disabled_bypasses_entirely(
        self, registry: CustomerRegistry, fake_dify: Any
    ) -> None:
        settings = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            default_rpm=1,
            default_rpm_burst=1,
        )
        app = _build_app(settings, registry, fake_dify)
        with TestClient(app) as client:
            codes = [client.get("/v1/models", headers=_AUTH).status_code for _ in range(10)]
        # Even with rpm/burst of 1, all pass because the feature is off.
        assert codes == [200] * 10
        # No rate-limit headers when disabled.
        with TestClient(app) as client:
            r = client.get("/v1/models", headers=_AUTH)
        assert "X-RateLimit-Limit" not in r.headers

    def test_exempt_path_not_limited(
        self, registry: CustomerRegistry, fake_dify: Any
    ) -> None:
        settings = Settings(registry_path="unused.yaml", log_json=False, default_rpm=1, default_rpm_burst=1)
        app = _build_app(settings, registry, fake_dify)
        with TestClient(app) as client:
            codes = [client.get("/health").status_code for _ in range(10)]
        assert codes == [200] * 10

    def test_per_customer_override_beats_default(
        self, fake_dify: Any
    ) -> None:
        """A customer whose registry entry sets rpm_limit is metered at that
        rate, not the gateway default. Verified by recording what
        units_per_min the limiter is asked for."""
        seen_units: list[int] = []

        class _RecordingLimiter:
            def __init__(self) -> None:
                self._inner = InMemoryTokenBucketLimiter()

            def check(self, key: str, *, units_per_min: int, burst: int, cost: float = 1.0) -> RateDecision:
                seen_units.append(units_per_min)
                return self._inner.check(key, units_per_min=units_per_min, burst=burst, cost=cost)

        customer = make_customer(sdk_key="bsa_capped", customer_id="capped", model_ids=("m1",))
        customer = customer.model_copy(update={"rpm_limit": 30})
        registry = CustomerRegistry.from_entries([customer])
        settings = Settings(registry_path="unused.yaml", log_json=False, default_rpm=120, default_rpm_burst=5)

        app = _build_app(settings, registry, fake_dify, rate_limiter=_RecordingLimiter())
        with TestClient(app) as client:
            r = client.get("/v1/models", headers={"Authorization": "Bearer bsa_capped"})
        assert r.status_code == 200
        # The customer override (30), not the default (120), was used.
        assert seen_units == [30]
