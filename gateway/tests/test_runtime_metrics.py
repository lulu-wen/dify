"""Unit + integration tests for PR #9 (Phase 2a) headroom-driven admission.

Three layers exercised:
- Pure math: EwmaHeadroomCalculator scaling + EWMA convergence.
- Parser: VLLMPrometheusMetrics against a captured Prometheus text fixture.
- QuotaStore.set_budget: atomic update under concurrent writers (defensive
  asyncio.Lock — current pure-asyncio semantics already guarantee atomicity
  for the single-attribute write, but the lock future-proofs against multi-field
  extensions and matches user feedback 2026-06-09).
- End-to-end via create_app + MockRuntimeMetrics: live cache_usage=0.9
  shrinks the effective budget enough to 503 a previously-admittable
  request; fetch failure leaves the previous budget unchanged (fail-open).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from gateway.config import Settings
from gateway.ratelimit.headroom import EwmaHeadroomCalculator, HeadroomConfig
from gateway.ratelimit.quota import InMemoryQuotaStore
from gateway.ratelimit.runtime_metrics import (
    RuntimeSnapshot,
    VLLMPrometheusMetrics,
    _parse_prometheus,
    run_metrics_poll_loop,
)
from gateway.ratelimit.types import RequestCost
from gateway.registry import CustomerRegistry
from tests.conftest import FakeDifyClient, MockRuntimeMetrics, make_customer

# --------------------------------------------------------------------------- #
# Headroom math
# --------------------------------------------------------------------------- #


def _calc(soft: float = 0.80, hard: float = 0.95, alpha: float = 1.0) -> EwmaHeadroomCalculator:
    """Calculator with alpha=1.0 by default so update() returns the raw mapping."""
    return EwmaHeadroomCalculator(
        HeadroomConfig(soft_threshold=soft, hard_threshold=hard, ewma_alpha=alpha)
    )


class TestHeadroomScaling:
    def test_below_soft_returns_full_budget(self) -> None:
        assert _calc().update(0.50) == 1.0
        assert _calc().update(0.79) == 1.0
        # Boundary INCLUSIVE on soft — usage exactly at soft = full budget.
        assert _calc().update(0.80) == 1.0

    def test_above_hard_returns_zero(self) -> None:
        assert _calc().update(0.95) == 0.0
        assert _calc().update(0.99) == 0.0
        # Clamped to 1.0 — usage > 1.0 input still produces factor 0.
        assert _calc().update(1.5) == 0.0

    def test_linear_ramp_between_thresholds(self) -> None:
        # midpoint of 0.80-0.95 is 0.875 → factor exactly 0.5
        assert _calc().update(0.875) == pytest.approx(0.5)
        # Quarter of the way through → 0.75 factor
        assert _calc().update(0.8375) == pytest.approx(0.75)

    def test_negative_usage_clamped(self) -> None:
        assert _calc().update(-0.1) == 1.0


class TestEwmaSmoothing:
    def test_first_update_seeds_directly(self) -> None:
        # alpha doesn't matter on first call; the seed is the raw value.
        c = EwmaHeadroomCalculator(
            HeadroomConfig(soft_threshold=0.80, hard_threshold=0.95, ewma_alpha=0.3)
        )
        c.update(0.90)
        assert c.smoothed_usage == pytest.approx(0.90)

    def test_ewma_converges_toward_step_input(self) -> None:
        # alpha=0.3, seed 0.0, then 0.9 every step. After 4 steps:
        # step 0 → 0.0 (seed)
        # step 1 → 0.7 * 0.0 + 0.3 * 0.9 = 0.27
        # step 2 → 0.7 * 0.27 + 0.3 * 0.9 = 0.459
        # step 3 → 0.7 * 0.459 + 0.3 * 0.9 = 0.5913
        # By step ~10 we're within 0.05 of the step target.
        c = EwmaHeadroomCalculator(
            HeadroomConfig(soft_threshold=0.80, hard_threshold=0.95, ewma_alpha=0.3)
        )
        c.update(0.0)
        for _ in range(15):
            c.update(0.9)
        assert c.smoothed_usage == pytest.approx(0.9, abs=0.01)


class TestHeadroomConfigValidation:
    def test_soft_must_be_below_hard(self) -> None:
        with pytest.raises(ValueError, match="soft < hard"):
            EwmaHeadroomCalculator(
                HeadroomConfig(soft_threshold=0.95, hard_threshold=0.80, ewma_alpha=0.3)
            )

    def test_alpha_must_be_in_open_unit_interval(self) -> None:
        with pytest.raises(ValueError, match="ewma_alpha"):
            EwmaHeadroomCalculator(
                HeadroomConfig(soft_threshold=0.8, hard_threshold=0.95, ewma_alpha=0.0)
            )
        with pytest.raises(ValueError, match="ewma_alpha"):
            EwmaHeadroomCalculator(
                HeadroomConfig(soft_threshold=0.8, hard_threshold=0.95, ewma_alpha=1.5)
            )

    def test_headroom_config_post_init_catches_swapped_thresholds(self) -> None:
        """PR #11 #6: cross-field validation moved from Settings.@model_validator
        to HeadroomConfig.__post_init__. Now EVERY construction path
        (env-loaded Settings, test fixture, Phase 4 hot-reload) is covered
        by one source of truth — the dataclass itself. The error fires at
        the construction site rather than relying on Pydantic to catch it.
        """
        with pytest.raises(ValueError, match="soft < hard"):
            HeadroomConfig(soft_threshold=0.95, hard_threshold=0.90, ewma_alpha=0.3)

    def test_create_app_validates_headroom_at_startup_even_if_disabled(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """PR #11 R2 #2: HeadroomConfig is constructed unconditionally at
        ``create_app`` time, so a misconfigured env raises before the
        gateway accepts traffic — even when ``runtime_metrics_enabled`` is
        False (the default). The previous Settings.@model_validator did
        catch this path too, but the calculator-internal validation
        DIDN'T fire unless metrics were enabled. PR #11 unifies on
        HeadroomConfig.__post_init__ + unconditional construction.
        """
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry

        registry = CustomerRegistry.from_entries([make_customer()])
        with pytest.raises(ValueError, match="soft < hard"):
            create_app(
                settings=Settings(
                    registry_path="unused.yaml",
                    log_json=False,
                    runtime_metrics_enabled=False,   # feature off — still validates
                    headroom_soft_threshold=0.95,
                    headroom_hard_threshold=0.90,
                ),
                registry=registry,
            )

    def test_headroom_config_is_frozen(self) -> None:
        """PR #11 R2 #1: ``frozen=True`` preserved. Runtime cannot mutate
        a HeadroomConfig instance after construction — guards against the
        EWMA / scaling math going off-spec between polls."""
        from dataclasses import FrozenInstanceError

        cfg = HeadroomConfig(soft_threshold=0.8, hard_threshold=0.95, ewma_alpha=0.3)
        with pytest.raises(FrozenInstanceError):
            cfg.soft_threshold = 0.5  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Prometheus parser
# --------------------------------------------------------------------------- #


_VLLM_SAMPLE = """\
# HELP vllm:gpu_cache_usage_perc GPU KV-cache usage percentage.
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc{model_name="gemma-3n-e4b"} 0.4321
# HELP vllm:num_requests_running Currently running requests.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="gemma-3n-e4b"} 3
vllm:num_requests_waiting{model_name="gemma-3n-e4b"} 0
# HELP vllm:time_to_first_token_seconds TTFT histogram.
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{le="0.1"} 12
vllm:time_to_first_token_seconds_bucket{le="0.5"} 47
vllm:time_to_first_token_seconds_sum 5.23
vllm:time_to_first_token_seconds_count 50
"""


class TestPrometheusParser:
    def test_extracts_scalar_gauges(self) -> None:
        parsed = _parse_prometheus(_VLLM_SAMPLE)
        assert parsed["vllm:gpu_cache_usage_perc"] == pytest.approx(0.4321)
        assert parsed["vllm:num_requests_running"] == 3.0
        assert parsed["vllm:num_requests_waiting"] == 0.0

    def test_skips_comment_and_blank_lines(self) -> None:
        parsed = _parse_prometheus("\n# a comment\n# HELP foo bar\n\nfoo 42\n")
        assert parsed == {"foo": 42.0}

    def test_malformed_line_ignored_not_raised(self) -> None:
        parsed = _parse_prometheus("garbage_no_number\nvalid 1.5")
        assert parsed == {"valid": 1.5}

    def test_negative_exponent_matches(self) -> None:
        # PR #9 review-1 #1: Prometheus value regex had [0-9.eE+] which
        # silently dropped 1.0e-4 (the minus after `e` wasn't in the
        # class). Pin the fix so a future refactor doesn't regress.
        parsed = _parse_prometheus("vllm:foo 1.0e-4\nvllm:bar -2.5e+3")
        assert parsed["vllm:foo"] == pytest.approx(1.0e-4)
        assert parsed["vllm:bar"] == pytest.approx(-2500.0)

    def test_allowlist_skips_unwanted(self) -> None:
        # PR #9 review-1 #4: vLLM /metrics emits hundreds of histogram-
        # bucket lines per scrape. The allowlist must drop them BEFORE
        # the regex runs. Use a payload where an unwanted metric uses a
        # syntax that WOULD match if the regex ran (so we know the
        # allowlist actually short-circuited it).
        payload = (
            "vllm:gpu_cache_usage_perc 0.5\n"
            "vllm:other_metric 99.9\n"
            "vllm:time_to_first_token_seconds_bucket{le=\"0.1\"} 12\n"
        )
        parsed = _parse_prometheus(payload, names=frozenset({"vllm:gpu_cache_usage_perc"}))
        assert parsed == {"vllm:gpu_cache_usage_perc": 0.5}
        # Sanity check the negative case: no allowlist → all valid lines parsed.
        parsed_all = _parse_prometheus(payload)
        assert "vllm:other_metric" in parsed_all


class TestVLLMPrometheusMetricsTransport:
    @pytest.mark.asyncio
    async def test_snapshot_round_trip(self) -> None:
        # Inject a transport-mocked httpx client returning the sample payload.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_VLLM_SAMPLE)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        metrics = VLLMPrometheusMetrics(url="http://test/metrics", http=client)
        snap = await metrics.snapshot()
        assert snap.gpu_cache_usage_perc == pytest.approx(0.4321)
        assert snap.num_requests_running == 3
        await metrics.aclose()  # owns_http=False; close is no-op
        await client.aclose()

    @pytest.mark.asyncio
    async def test_snapshot_raises_on_5xx(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="oh no")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        metrics = VLLMPrometheusMetrics(url="http://test/metrics", http=client)
        with pytest.raises(httpx.HTTPStatusError):
            await metrics.snapshot()
        await client.aclose()


# --------------------------------------------------------------------------- #
# QuotaStore.set_budget — atomic, concurrent
# --------------------------------------------------------------------------- #


class TestQuotaStoreSetBudget:
    @pytest.mark.asyncio
    async def test_admission_uses_new_budget(self) -> None:
        store = InMemoryQuotaStore(node_token_budget=10_000)
        cost = RequestCost(
            input_tokens=100,
            max_output_tokens=900,
            model_id="m",
            token_cost=1000,
            est_kv_bytes=0,
        )
        # Before resize: admits.
        g1 = store.try_admit(tenant="t", cost=cost)
        assert g1.admitted
        store.settle(g1.charge_id, actual_output_tokens=0) if g1.charge_id else None

        # After shrink: same request rejected.
        await store.set_budget(500)
        g2 = store.try_admit(tenant="t", cost=cost)
        assert not g2.admitted

    @pytest.mark.asyncio
    async def test_concurrent_set_budget_calls_settle_to_one_value(self) -> None:
        # Pure asyncio single-loop: writes are already atomic for our
        # single-attribute store, but the lock guarantees that future
        # multi-field extensions stay consistent. This test pins that
        # parallel set_budget calls converge to one of the values, never
        # a torn/mixed state.
        store = InMemoryQuotaStore(node_token_budget=10_000)

        async def setter(value: int) -> None:
            await store.set_budget(value)

        await asyncio.gather(*(setter(v) for v in (100, 200, 300, 400, 500)))
        # The store's budget must be one of the values written — never -1,
        # never a half-applied state.
        assert store.budget in {100, 200, 300, 400, 500}

    @pytest.mark.asyncio
    async def test_set_budget_floors_at_zero(self) -> None:
        store = InMemoryQuotaStore(node_token_budget=1000)
        await store.set_budget(-100)
        assert store.budget == 0


# --------------------------------------------------------------------------- #
# Polling loop fail-open
# --------------------------------------------------------------------------- #


class TestPollLoopEagerFetch:
    @pytest.mark.asyncio
    async def test_first_fetch_is_eager_not_sleep_first(self) -> None:
        """PR #9 review-1 #7: previously the loop slept poll_s BEFORE the
        first fetch — for a 30s poll interval the gateway ran on the
        static budget for the entire first 30s window. Now the first
        snapshot lands immediately so headroom is authoritative from t=0.
        We pin this by using a poll interval much longer than the test
        sleep; if the loop still slept first, call_count would be 0.
        """
        store = InMemoryQuotaStore(node_token_budget=1000)
        calc = EwmaHeadroomCalculator(
            HeadroomConfig(soft_threshold=0.8, hard_threshold=0.95, ewma_alpha=1.0)
        )
        metrics = MockRuntimeMetrics(
            initial=RuntimeSnapshot(
                gpu_cache_usage_perc=0.875,  # midpoint → factor 0.5
                num_requests_running=0,
                num_requests_waiting=0,
            )
        )

        task = asyncio.create_task(
            run_metrics_poll_loop(
                metrics=metrics,
                calculator=calc,
                quota_store=store,
                static_budget=1000,
                poll_interval_s=10.0,  # long: would lock budget at 1000 if sleep-first
            )
        )
        # Tiny wait — far less than poll_interval_s. With eager-fetch,
        # the first snapshot must already have applied.
        await asyncio.sleep(0.05)
        assert metrics.call_count == 1
        assert store.budget == 500  # 1000 * 0.5 factor at midpoint usage

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestPollLoopFailOpen:
    @pytest.mark.asyncio
    async def test_metrics_failure_leaves_budget_untouched(self) -> None:
        store = InMemoryQuotaStore(node_token_budget=1000)
        calc = _calc(alpha=1.0)  # no smoothing — see scaling directly
        metrics = MockRuntimeMetrics(
            initial=RuntimeSnapshot(
                gpu_cache_usage_perc=0.0,
                num_requests_running=0,
                num_requests_waiting=0,
            )
        )

        task = asyncio.create_task(
            run_metrics_poll_loop(
                metrics=metrics,
                calculator=calc,
                quota_store=store,
                static_budget=1000,
                poll_interval_s=0.01,
            )
        )

        # Let one normal poll land — budget = 1000 (factor 1.0).
        await asyncio.sleep(0.03)
        assert store.budget == 1000

        # Inject a transient failure. Loop logs warn, continues, budget
        # stays at last good value.
        metrics.fail_with = httpx.RequestError("transient")
        await asyncio.sleep(0.05)
        assert store.budget == 1000  # unchanged — fail-open
        assert metrics.call_count >= 2  # we polled at least twice

        # Recovery: usage spike triggers ramp.
        metrics.snapshot_value = RuntimeSnapshot(
            gpu_cache_usage_perc=0.875,  # midpoint of 0.8-0.95 → factor 0.5
            num_requests_running=0,
            num_requests_waiting=0,
        )
        await asyncio.sleep(0.05)
        assert store.budget == 500

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- #
# End-to-end via create_app + MockRuntimeMetrics
# --------------------------------------------------------------------------- #


from gateway.main import create_app  # noqa: E402

_AUTH = {"Authorization": "Bearer bsa_test_a"}


def _build_metrics_app(
    fake_dify: FakeDifyClient,
    metrics: MockRuntimeMetrics,
    *,
    node_token_budget: int = 10_000,
) -> FastAPI:
    customer = make_customer(model_ids=("m1",))
    registry = CustomerRegistry.from_entries([customer])
    settings = Settings(
        registry_path="unused.yaml",
        log_json=False,
        node_token_budget=node_token_budget,
        runtime_metrics_enabled=True,
        runtime_metrics_poll_s=0.01,  # very fast in tests
        headroom_soft_threshold=0.80,
        headroom_hard_threshold=0.95,
        headroom_ewma_alpha=1.0,  # no smoothing → snapshot value applied directly
    )
    app = create_app(
        settings=settings,
        registry=registry,
        runtime_metrics=metrics,
    )
    app.state.dify_client_factory = lambda _: fake_dify  # type: ignore[assignment]
    app.state.app_manager._client_factory = lambda _: fake_dify
    fake_dify.blocking_response = {
        "id": "m",
        "answer": "ok",
        "conversation_id": "c",
        "metadata": {
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        },
    }
    return app


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        yield cli


class TestHeadroomDrivenAdmission:
    @pytest.mark.asyncio
    async def test_high_cache_usage_shrinks_budget_to_503(
        self, fake_dify: FakeDifyClient
    ) -> None:
        # gpu_cache_usage = 0.875 → factor 0.5 → effective_budget = 1000.
        # The chat request (1024 default_max_output) doesn't fit → 503.
        metrics = MockRuntimeMetrics(
            initial=RuntimeSnapshot(
                gpu_cache_usage_perc=0.875,
                num_requests_running=0,
                num_requests_waiting=0,
            )
        )
        app = _build_metrics_app(fake_dify, metrics, node_token_budget=2000)
        async with app.router.lifespan_context(app):
            # Let the polling loop land at least once.
            await asyncio.sleep(0.05)
            assert app.state.quota_store.budget == 1000

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as cli:
                r = await cli.post(
                    "/v1/chat/completions",
                    headers=_AUTH,
                    json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
                )
            assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_low_cache_usage_keeps_full_budget(
        self, fake_dify: FakeDifyClient
    ) -> None:
        metrics = MockRuntimeMetrics(
            initial=RuntimeSnapshot(
                gpu_cache_usage_perc=0.20,
                num_requests_running=0,
                num_requests_waiting=0,
            )
        )
        app = _build_metrics_app(fake_dify, metrics, node_token_budget=200_000)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
            assert app.state.quota_store.budget == 200_000

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as cli:
                r = await cli.post(
                    "/v1/chat/completions",
                    headers=_AUTH,
                    json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
                )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_metrics_fetch_failure_is_fail_open(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Edge node first-rule: monitoring outage must NOT take generation
        offline. The polling loop catches the exception, leaves the budget
        at its previous value (or the static startup value if no successful
        poll has landed), and requests continue to admit normally.
        """
        metrics = MockRuntimeMetrics(
            initial=RuntimeSnapshot(
                gpu_cache_usage_perc=0.0,
                num_requests_running=0,
                num_requests_waiting=0,
            )
        )
        metrics.fail_with = httpx.RequestError("vLLM /metrics unreachable")
        app = _build_metrics_app(fake_dify, metrics, node_token_budget=200_000)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.03)
            # Budget stays at the static startup value — fail-open.
            assert app.state.quota_store.budget == 200_000

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as cli:
                r = await cli.post(
                    "/v1/chat/completions",
                    headers=_AUTH,
                    json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
                )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_disabled_by_default_leaves_static_budget(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """``runtime_metrics_enabled=False`` (default) skips the loop. The
        existing static-budget admission path is unchanged — proves Phase 2a
        is opt-in and pre-existing 1b behavior is preserved.
        """
        customer = make_customer(model_ids=("m1",))
        registry = CustomerRegistry.from_entries([customer])
        settings = Settings(
            registry_path="unused.yaml",
            log_json=False,
            node_token_budget=200_000,
            runtime_metrics_enabled=False,
        )
        app = create_app(settings=settings, registry=registry)
        app.state.dify_client_factory = lambda _: fake_dify  # type: ignore[assignment]
        app.state.app_manager._client_factory = lambda _: fake_dify

        async with app.router.lifespan_context(app):
            assert app.state.runtime_metrics is None
            assert app.state.quota_store.budget == 200_000
