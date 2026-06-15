"""Tests for PR #8 / Phase 1b — cost-based admission + TPM + pre-charge/refund.

Layers:
- Unit: cost estimator, InMemoryQuotaStore ledger, token-bucket cost>burst,
  jitter helper.
- Integration: chat router (TPM 429, admission 503, settle releases on
  blocking success / streaming completion / streaming disconnect) and
  embeddings router (TPM) via httpx ASGITransport with a fake Dify client.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import yaml
from fastapi import FastAPI

from gateway.config import Settings
from gateway.errors import RateLimitError
from gateway.main import create_app
from gateway.ratelimit import InMemoryQuotaStore, InMemoryTokenBucketLimiter, estimate_cost
from gateway.ratelimit.retry import jittered_retry_after
from gateway.ratelimit.types import ActionCode
from gateway.registry import CustomerEntry, CustomerRegistry, DifyConnection, ModelEntry
from tests.conftest import FakeDifyClient, make_customer

_AUTH = {"Authorization": "Bearer bsa_test_a"}


# --------------------------------------------------------------------------- #
# Unit: cost estimator
# --------------------------------------------------------------------------- #


class TestEstimateCost:
    def test_chars_over_four_plus_max_output(self) -> None:
        c = estimate_cost(input_chars=400, max_output_tokens=100, model_id="m1")
        assert c.input_tokens == 100  # 400 // 4
        assert c.max_output_tokens == 100
        assert c.token_cost == 200
        assert c.model_id == "m1"
        assert c.est_kv_bytes > 0

    def test_zero_max_output_is_input_only(self) -> None:
        c = estimate_cost(input_chars=40, max_output_tokens=0, model_id="emb")
        assert c.input_tokens == 10
        assert c.token_cost == 10  # no generation term

    def test_negatives_clamped(self) -> None:
        c = estimate_cost(input_chars=-5, max_output_tokens=-9, model_id="m")
        assert c.input_tokens == 0
        assert c.max_output_tokens == 0
        assert c.token_cost == 0


# --------------------------------------------------------------------------- #
# Unit: InMemoryQuotaStore
# --------------------------------------------------------------------------- #


def _cost(token_cost: int) -> object:
    # token_cost is the only field admission uses; build via estimate_cost so
    # the dataclass stays the single source of truth.
    return estimate_cost(input_chars=token_cost * 4, max_output_tokens=0, model_id="m")


class TestQuotaStore:
    def test_admit_until_budget_then_reject(self) -> None:
        q = InMemoryQuotaStore(node_token_budget=100)
        g1 = q.try_admit(tenant="t", cost=_cost(60))
        assert g1.admitted and g1.charge_id is not None
        assert q.in_flight == 60
        # 60 + 60 = 120 > 100 → reject, no reservation, action set.
        g2 = q.try_admit(tenant="t", cost=_cost(60))
        assert not g2.admitted
        assert g2.charge_id is None
        assert g2.action == ActionCode.REJECTED_OVERLOAD
        assert q.in_flight == 60  # unchanged by the rejected admit

    def test_admit_exactly_at_budget_boundary(self) -> None:
        q = InMemoryQuotaStore(node_token_budget=100)
        assert q.try_admit(tenant="t", cost=_cost(100)).admitted  # == budget, ok
        assert not q.try_admit(tenant="t", cost=_cost(1)).admitted  # 1 over

    def test_settle_releases_then_readmit(self) -> None:
        q = InMemoryQuotaStore(node_token_budget=100)
        g = q.try_admit(tenant="t", cost=_cost(100))
        assert q.in_flight == 100
        q.settle(g.charge_id, actual_output_tokens=5)  # type: ignore[arg-type]
        assert q.in_flight == 0
        # Budget freed → a fresh full request admits again.
        assert q.try_admit(tenant="t", cost=_cost(100)).admitted

    def test_settle_is_idempotent(self) -> None:
        q = InMemoryQuotaStore(node_token_budget=100)
        g = q.try_admit(tenant="t", cost=_cost(40))
        q.settle(g.charge_id, actual_output_tokens=0)  # type: ignore[arg-type]
        # Double settle + unknown id are both no-ops (keeps streaming +
        # pre-flight-failure paths safe to both call settle).
        q.settle(g.charge_id, actual_output_tokens=0)  # type: ignore[arg-type]
        q.settle("never-issued", actual_output_tokens=0)
        assert q.in_flight == 0


# --------------------------------------------------------------------------- #
# Unit: token bucket cost > burst (1a deferred P3, handled in 1b)
# --------------------------------------------------------------------------- #


class TestCostExceedsBurst:
    def test_unsatisfiable_cost_has_no_retry_after(self) -> None:
        limiter = InMemoryTokenBucketLimiter()
        # cost (50) > burst (10): even a full bucket can't admit it; waiting
        # never helps, so retry_after must be None (not a misleading finite).
        d = limiter.check("t:tpm", units_per_min=600, burst=10, cost=50.0)
        assert not d.allowed
        assert d.retry_after_s is None


# --------------------------------------------------------------------------- #
# Unit: jitter helper
# --------------------------------------------------------------------------- #


class TestJitter:
    def test_adds_base_plus_jitter(self) -> None:
        assert jittered_retry_after(5.0, rng=lambda a, b: 0.5) == 5.5

    def test_none_input_returns_none(self) -> None:
        # PR #10 self-review-3 #1: None is the limiter's "structurally
        # unsatisfiable" signal — must propagate unchanged so the
        # exception handler omits Retry-After. Callers wanting a default
        # backoff pass 0.0 explicitly.
        assert jittered_retry_after(None, rng=lambda a, b: 0.7) is None

    def test_zero_base_is_just_jitter(self) -> None:
        # Default-backoff use case: admit() passes 0.0 to get a small
        # jittered delay without confusing it with the "unsatisfiable"
        # None signal.
        assert jittered_retry_after(0.0, rng=lambda a, b: 0.7) == 0.7

    def test_nonpositive_base_floored(self) -> None:
        assert jittered_retry_after(-3.0, rng=lambda a, b: 0.2) == 0.2


# --------------------------------------------------------------------------- #
# Integration helpers
# --------------------------------------------------------------------------- #


def _build_app(fake_dify: FakeDifyClient, **settings_kwargs: object) -> FastAPI:
    settings = Settings(registry_path="unused.yaml", log_json=False, **settings_kwargs)  # type: ignore[arg-type]
    registry = CustomerRegistry.from_entries([make_customer(model_ids=("m1",))])
    app = create_app(settings=settings, registry=registry)

    def factory(_: object) -> object:
        return fake_dify

    app.state.dify_client_factory = factory
    app.state.app_manager._client_factory = factory
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# Integration: chat admission (node budget)
# --------------------------------------------------------------------------- #


class TestChatAdmission:
    @pytest.mark.asyncio
    async def test_request_over_node_budget_returns_503(self, fake_dify: FakeDifyClient) -> None:
        # Budget of 5 tokens; a request with no max_tokens estimates
        # token_cost = input//4 + default_max_output_tokens (1024) >> 5.
        app = _build_app(fake_dify, node_token_budget=5)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 503
        body = r.json()
        assert body["error"]["type"] == "overload_error"
        assert body["error"]["code"] == "overloaded"
        assert body["error"]["action"] == ActionCode.REJECTED_OVERLOAD
        assert int(r.headers["Retry-After"]) >= 1
        # Rejected admit holds no reservation.
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_blocking_success_releases_reservation(self, fake_dify: FakeDifyClient) -> None:
        fake_dify.blocking_response = {
            "id": "m",
            "answer": "ok",
            "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}},
        }
        app = _build_app(fake_dify, node_token_budget=200_000)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 50},
            )
        assert r.status_code == 200
        # settle ran → budget fully released after the blocking call.
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_disabled_bypasses_admission(self, fake_dify: FakeDifyClient) -> None:
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }
        # Tiny budget BUT rate limiting off → request still succeeds.
        app = _build_app(fake_dify, node_token_budget=1, rate_limit_enabled=False)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 200
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_local_rejection_after_admit_window_does_not_leak(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Regression: a body with no user message is rejected by
        ``_last_user_message`` (400). That validation runs BEFORE admit, so
        the reservation must never have been taken — in_flight stays 0.
        Guards the admit-ordering (admit must come after can-raise prep)."""
        app = _build_app(fake_dify, node_token_budget=200_000)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                # Only a system message → no user message → 400.
                json={"model": "m1", "messages": [{"role": "system", "content": "be brief"}]},
            )
        assert r.status_code == 400
        assert app.state.quota_store.in_flight == 0


# --------------------------------------------------------------------------- #
# Integration: chat TPM
# --------------------------------------------------------------------------- #


class TestChatTpm:
    @pytest.mark.asyncio
    async def test_over_tpm_cost_gt_burst_returns_misconfigured_rate(
        self, fake_dify: FakeDifyClient
    ) -> None:
        # tpm enabled, burst tiny (10) — a request costing ~1024 (default
        # max_output fallback) exceeds burst → structurally unsatisfiable
        # → 429 MISCONFIGURED_RATE (PR #11 R2 #1 sibling-sweep: middleware
        # already used this code; enforce_tpm now matches so SDKs stop
        # the halve-and-retry loop on this branch).
        app = _build_app(fake_dify, default_tpm=600, default_tpm_burst=10, node_token_budget=10_000_000)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 429
        body = r.json()
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["action"] == ActionCode.MISCONFIGURED_RATE
        # TPM rejected BEFORE admission → no reservation held.
        assert app.state.quota_store.in_flight == 0
        # The limiter signalled "unsatisfiable" (cost > burst) by returning
        # ``retry_after_s=None``. The 429 response must NOT carry a
        # ``Retry-After`` header — SDKs see MISCONFIGURED_RATE + no
        # backoff hint and surface the operator-misconfig to the user.
        assert "retry-after" not in {k.lower() for k in r.headers}, (
            f"Retry-After leaked on cost>burst path: {dict(r.headers)}"
        )
        assert body["error"].get("retry_after_s") in (None, 0, 0.0), (
            f"retry_after_s leaked: {body['error']}"
        )

    @pytest.mark.asyncio
    async def test_over_tpm_finite_retry_returns_reduce_max_tokens(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """PR #11 R2 #1 sibling-sweep correctness: the REDUCE_MAX_TOKENS
        branch survives for the genuine rate-exhaustion case (cost <=
        burst but bucket temporarily empty). Burn through the burst with
        a tight success, then send a second small request that's
        rejected with a FINITE retry estimate — the SDK can back off.
        """
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }
        # burst high enough for first request (default_max_output=1024
        # → cost ~1024 fits in burst=2000), tpm low so refill is slow.
        app = _build_app(
            fake_dify,
            default_tpm=60, default_tpm_burst=2000,
            node_token_budget=10_000_000,
        )
        async with _client(app) as cli:
            # First request burns ~1024 tokens of burst.
            r1 = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
            assert r1.status_code == 200
            # Second request: burst is now ~976; another ~1024 cost
            # doesn't fit but cost <= burst (1024 < 2000) → finite retry.
            r2 = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r2.status_code == 429
        body = r2.json()
        assert body["error"]["action"] == ActionCode.REDUCE_MAX_TOKENS
        # Finite retry case: Retry-After header IS present.
        assert "retry-after" in {k.lower() for k in r2.headers}

    @pytest.mark.asyncio
    async def test_tpm_unlimited_by_default(self, fake_dify: FakeDifyClient) -> None:
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }
        # default_tpm=0 → TPM check skipped entirely.
        app = _build_app(fake_dify, default_tpm=0)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Integration: streaming settle (completion + disconnect)
# --------------------------------------------------------------------------- #


class TestStreamingSettle:
    @pytest.mark.asyncio
    async def test_stream_completion_releases_reservation(self, fake_dify: FakeDifyClient) -> None:
        app = _build_app(fake_dify, node_token_budget=200_000)
        async with _client(app) as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_lines():
                    pass
        await asyncio.sleep(0)  # let the generator finally run
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_stream_disconnect_releases_reservation(self, fake_dify: FakeDifyClient) -> None:
        # Many lines so the stream is still open when we bail out early —
        # exercises the GeneratorExit → finally → settle path (the edge's
        # biggest waste: a dropped long generation holding KV budget).
        fake_dify.streaming_lines = [
            'data: {"event":"message","answer":"chunk"}' for _ in range(50)
        ] + ['data: {"event":"message_end","metadata":{},"conversation_id":"c"}']
        app = _build_app(fake_dify, node_token_budget=200_000)
        async with _client(app) as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_lines():
                    break  # disconnect mid-stream
        await asyncio.sleep(0)
        assert app.state.quota_store.in_flight == 0


# --------------------------------------------------------------------------- #
# Integration: embeddings TPM (no admission)
# --------------------------------------------------------------------------- #


class TestEmbeddingsTpm:
    @pytest.mark.asyncio
    async def test_embeddings_over_tpm_returns_429(self, fake_dify: FakeDifyClient, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub the embeddings upstream so the request reaches TPM, not network.
        async def _fake_invoke(**_: object) -> dict[str, object]:
            return {"data": [], "model": "emb1", "usage": {"prompt_tokens": 0}}

        monkeypatch.setattr("gateway.routers.embeddings.invoke_embeddings", _fake_invoke)
        # burst tiny so a long input trips TPM.
        app = _build_app(fake_dify, default_tpm=600, default_tpm_burst=10)
        long_input = "x" * 1000  # ~250 input tokens > burst 10 → unsatisfiable
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/embeddings",
                headers=_AUTH,
                json={"model": "emb1", "input": long_input},
            )
        assert r.status_code == 429
        # cost > burst → MISCONFIGURED_RATE (PR #11 R2 #1 sibling-sweep)
        assert r.json()["error"]["action"] == ActionCode.MISCONFIGURED_RATE

    @pytest.mark.asyncio
    async def test_embeddings_unlimited_by_default(self, fake_dify: FakeDifyClient, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_invoke(**_: object) -> dict[str, object]:
            return {"data": [{"embedding": [0.1], "index": 0}], "model": "emb1", "usage": {"prompt_tokens": 1}}

        monkeypatch.setattr("gateway.routers.embeddings.invoke_embeddings", _fake_invoke)
        app = _build_app(fake_dify, default_tpm=0)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/embeddings",
                headers=_AUTH,
                json={"model": "emb1", "input": "hello"},
            )
        assert r.status_code == 200


def test_ratelimit_error_carries_action_and_retry() -> None:
    """RateLimitError now accepts action + retry_after_s (used by router TPM)."""
    e = RateLimitError("x", action=ActionCode.REDUCE_MAX_TOKENS, retry_after_s=2.5)
    env = e.to_openai_envelope()
    assert env["error"]["action"] == ActionCode.REDUCE_MAX_TOKENS
    assert e.retry_after_s == 2.5


# --------------------------------------------------------------------------- #
# Codex 1b review-2 fixes
# --------------------------------------------------------------------------- #


class TestReviewFixes:
    @pytest.mark.asyncio
    async def test_invalid_model_404_not_429(self, fake_dify: FakeDifyClient) -> None:
        """Codex 1b review-2 P2-1: model validation runs BEFORE TPM metering,
        so an invalid model 404s without draining the TPM bucket. With burst=1
        and TPM enabled, if TPM ran first this would be 429 — asserting 404
        proves the order."""
        app = _build_app(fake_dify, default_tpm=600, default_tpm_burst=1)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "does-not-exist", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "model_not_found"

    @pytest.mark.asyncio
    async def test_reservation_uses_model_configured_max_tokens(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Codex 1b review-2 P2-2: the node reservation uses the MODEL's
        configured generation cap (completion_params.max_tokens), not the
        client's max_tokens (which Dify ignores). Model caps at 2048; budget
        is 1500. cost = 0 input + 2048 > 1500 → 503. (Old behaviour used the
        1024 default → would have admitted.)"""
        customer = CustomerEntry(
            sdk_key="bsa_test_a",
            customer_id="test-a",
            dify=DifyConnection(
                base_url="http://dify.test",
                console_email="a@x",
                console_password="pw",
                dataset_api_key="ds-x",
            ),
            models=[ModelEntry(id="m1", provider="prov", name="n", completion_params={"max_tokens": 2048})],
        )
        registry = CustomerRegistry.from_entries([customer])
        settings = Settings(registry_path="unused.yaml", log_json=False, node_token_budget=1500)
        app = create_app(settings=settings, registry=registry)
        app.state.dify_client_factory = lambda _: fake_dify
        app.state.app_manager._client_factory = lambda _: fake_dify

        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                # No max_tokens in the request — reservation still uses the
                # model's 2048 cap, not the client's omission.
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 503
        assert r.json()["error"]["action"] == ActionCode.REJECTED_OVERLOAD

    @pytest.mark.asyncio
    async def test_autobuilt_app_gets_injected_output_cap(self, fake_dify: FakeDifyClient) -> None:
        """Codex 1b review-2 P2-2: when a model has no configured max_tokens,
        AppManager injects default_max_output_tokens into the auto-built App's
        DSL — so generation is genuinely bounded and the reservation is a true
        upper bound (not just a hopeful estimate)."""
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }
        # m1 (make_customer) has completion_params={} → injection applies.
        app = _build_app(fake_dify, default_max_output_tokens=512)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 200
        # The App import DSL must carry the injected cap.
        assert fake_dify.calls["import"], "App was not built"
        _, yaml_content = fake_dify.calls["import"][-1]
        dsl = yaml.safe_load(yaml_content)
        assert dsl["model_config"]["model"]["completion_params"]["max_tokens"] == 512


class TestReview3Fixes:
    @pytest.mark.asyncio
    async def test_node_full_503_does_not_debit_tpm(self, fake_dify: FakeDifyClient) -> None:
        """Codex 1b review-3 P2-1: when the node budget is full, a chat
        request must 503 WITHOUT consuming the tenant's (non-refundable) TPM
        bucket. We assert the limiter was never asked to consume TPM —
        admit() runs first and raises before enforce_tpm()."""
        seen_tpm = {"called": False}

        class _SpyLimiter:
            def __init__(self) -> None:
                self._inner = InMemoryTokenBucketLimiter()

            def check(self, key: str, **kw: object):  # type: ignore[no-untyped-def]
                if key.endswith(":tpm"):
                    seen_tpm["called"] = True
                return self._inner.check(key, **kw)  # type: ignore[arg-type]

        registry = CustomerRegistry.from_entries([make_customer(model_ids=("m1",))])
        # Budget 1 → any chat request's cost (>= input + cap) exceeds it → 503.
        settings = Settings(
            registry_path="unused.yaml", log_json=False,
            node_token_budget=1, default_tpm=600, default_tpm_burst=100000,
        )
        app = create_app(settings=settings, registry=registry, rate_limiter=_SpyLimiter())  # type: ignore[arg-type]
        app.state.dify_client_factory = lambda _: fake_dify
        app.state.app_manager._client_factory = lambda _: fake_dify

        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 503
        assert seen_tpm["called"] is False, "TPM was consumed before admission rejected the request"
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_tpm_rejection_after_admit_releases_reservation(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Admit succeeds, TPM then rejects → the reservation must be
        released (settle on TPM-rejection path), not leaked."""
        # Big node budget (admit passes) + tiny TPM burst (TPM rejects: cost
        # >> burst → unsatisfiable).
        app = _build_app(
            fake_dify, node_token_budget=10_000_000, default_tpm=600, default_tpm_burst=10
        )
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 429
        assert app.state.quota_store.in_flight == 0  # reservation released

    @pytest.mark.asyncio
    async def test_null_max_tokens_injects_default_cap(self, fake_dify: FakeDifyClient) -> None:
        """Codex 1b review-3 P2-2: completion_params={'max_tokens': None}
        must be treated as uncapped — the App gets the injected default cap
        (not left unbounded), matching the reservation's fallback."""
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }
        customer = CustomerEntry(
            sdk_key="bsa_test_a",
            customer_id="test-a",
            dify=DifyConnection(
                base_url="http://dify.test", console_email="a@x",
                console_password="pw", dataset_api_key="ds-x",
            ),
            models=[ModelEntry(id="m1", provider="prov", name="n", completion_params={"max_tokens": None})],
        )
        registry = CustomerRegistry.from_entries([customer])
        settings = Settings(registry_path="unused.yaml", log_json=False, default_max_output_tokens=512)
        app = create_app(settings=settings, registry=registry)
        app.state.dify_client_factory = lambda _: fake_dify
        app.state.app_manager._client_factory = lambda _: fake_dify

        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 200
        _, yaml_content = fake_dify.calls["import"][-1]
        dsl = yaml.safe_load(yaml_content)
        # null max_tokens → default injected, NOT left as null/unbounded.
        assert dsl["model_config"]["model"]["completion_params"]["max_tokens"] == 512


def test_effective_max_tokens_normalizes_invalid_values() -> None:
    """Unit: only a positive int counts as a usable cap."""
    from gateway.ratelimit import effective_max_tokens

    assert effective_max_tokens({"max_tokens": 2048}) == 2048
    assert effective_max_tokens({}) is None
    assert effective_max_tokens({"max_tokens": None}) is None
    assert effective_max_tokens({"max_tokens": 0}) is None
    assert effective_max_tokens({"max_tokens": -5}) is None
    assert effective_max_tokens({"max_tokens": "1024"}) is None
    assert effective_max_tokens({"max_tokens": True}) is None  # bool excluded


class TestReview4Fixes:
    @pytest.mark.asyncio
    async def test_streaming_settles_after_upstream_close(
        self, fake_dify: FakeDifyClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex 1b review-4 P2: settle MUST run after the upstream Dify
        stream is closed. If we release the reservation before __aexit__,
        Dify/vLLM may still hold KV cache during the close window and
        another admit could exceed the real budget. Spy on both the
        upstream close and settle and assert order."""
        events: list[str] = []

        original_open = fake_dify.open_chat_stream

        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def open_with_exit_spy(**kwargs: object) -> AsyncIterator[object]:
            async with original_open(**kwargs) as inner:
                try:
                    yield inner
                finally:
                    events.append("close")

        fake_dify.open_chat_stream = open_with_exit_spy  # type: ignore[method-assign]

        import gateway.routers.chat as chat_mod
        original_settle = chat_mod.settle

        def settle_spy(*args: object, **kwargs: object) -> None:
            events.append("settle")
            return original_settle(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(chat_mod, "settle", settle_spy)

        app = _build_app(fake_dify, node_token_budget=200_000)
        async with _client(app) as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_lines():
                    pass
        await asyncio.sleep(0)

        # The upstream MUST be closed before the budget is released — else
        # a concurrent admit could over-commit while vLLM still has KV.
        assert events == ["close", "settle"], (
            f"settle ran before upstream close (order: {events}) — node budget "
            f"was released while Dify/vLLM may still have been holding KV cache"
        )
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_overload_503_does_not_call_get_app_key(
        self, fake_dify: FakeDifyClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex 1b review-4 P3: when the node budget is full, admission
        503s BEFORE the (potentially network-heavy) get_app_key lazy-build.
        Spy on app_manager.get_app_key and assert call_count == 0 — wasted
        Dify console_login + DSL import + api-key creation against a request
        we're about to reject would defeat the point of fast-rejection."""
        app = _build_app(fake_dify, node_token_budget=1)

        calls = {"count": 0}
        original = app.state.app_manager.get_app_key

        async def get_app_key_spy(customer: object, model_id: str) -> str:
            calls["count"] += 1
            return await original(customer, model_id)  # type: ignore[no-any-return]

        monkeypatch.setattr(app.state.app_manager, "get_app_key", get_app_key_spy)

        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )

        assert r.status_code == 503
        assert calls["count"] == 0, (
            "get_app_key was called for an over-budget request — admission must "
            "run before any potentially-network-heavy lazy-build"
        )
        # Invalid model still 404s synchronously (find_model before admit).
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "nope", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 404


class TestReview5Fixes:
    @pytest.mark.asyncio
    async def test_rag_customer_reservation_includes_kb_allowance(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Codex 1b review-5 P2: a customer with attached KBs has Dify inject
        retrieval chunks into the prompt before vLLM sees it. The reservation
        must add a RAG allowance for those chunks, else a short query over a
        large KB can under-reserve and exceed the node budget the OOM guard
        is meant to enforce.

        With default_kb_top_k=3 and default_kb_chunk_tokens=1000, a RAG
        customer's reservation = input + max_out + 3000 RAG. A non-RAG
        customer's reservation = input + max_out. We pick a node_token_budget
        that admits the non-RAG cost but rejects the RAG cost — proves the
        allowance is actually applied.
        """
        # Non-RAG customer: m1, no KBs. cost = 0 input + 1024 max_out = 1024.
        non_rag = make_customer(
            sdk_key="bsa_norag", customer_id="norag", model_ids=("m1",)
        )
        # RAG customer: same model but with KBs attached → cost adds
        # 3 * 1000 = 3000 RAG allowance → total 4024.
        rag = make_customer(
            sdk_key="bsa_rag", customer_id="rag", model_ids=("m1",),
            knowledge_bases=["ds-uuid-1"],
        )
        registry = CustomerRegistry.from_entries([non_rag, rag])

        # Budget that fits non-RAG (1024) but not RAG (4024).
        settings = Settings(
            registry_path="unused.yaml", log_json=False,
            node_token_budget=2000,
            default_kb_top_k=3,
            default_kb_chunk_tokens=1000,
        )
        app = create_app(settings=settings, registry=registry)
        app.state.dify_client_factory = lambda _: fake_dify
        app.state.app_manager._client_factory = lambda _: fake_dify
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }

        async with _client(app) as cli:
            # Non-RAG customer → admitted (1024 <= 2000).
            r1 = await cli.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_norag"},
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
            # RAG customer → rejected (4024 > 2000).
            r2 = await cli.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_rag"},
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )

        assert r1.status_code == 200
        assert r2.status_code == 503, (
            "RAG customer admitted under a budget that should have rejected "
            "them once the kb allowance is included (codex 1b review-5 P2)"
        )
        assert r2.json()["error"]["action"] == ActionCode.REJECTED_OVERLOAD

    @pytest.mark.asyncio
    async def test_autobuilt_app_caps_dify_retrieval_top_k(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """The reservation's RAG allowance is only a real bound if Dify
        actually retrieves at most ``default_kb_top_k`` chunks. AppManager
        must inject ``dataset_configs.top_k`` into the DSL when KBs are
        attached."""
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }
        customer = make_customer(model_ids=("m1",), knowledge_bases=["ds-uuid-1"])
        registry = CustomerRegistry.from_entries([customer])
        settings = Settings(
            registry_path="unused.yaml", log_json=False,
            default_kb_top_k=2,   # custom value to assert it's plumbed through
        )
        app = create_app(settings=settings, registry=registry)
        app.state.dify_client_factory = lambda _: fake_dify
        app.state.app_manager._client_factory = lambda _: fake_dify

        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 200
        _, yaml_content = fake_dify.calls["import"][-1]
        dsl = yaml.safe_load(yaml_content)
        cfg = dsl["model_config"]["dataset_configs"]
        assert cfg["top_k"] == 2, f"Dify retrieval not capped — got {cfg}"

    @pytest.mark.asyncio
    async def test_no_kb_omits_top_k_in_dsl(self, fake_dify: FakeDifyClient) -> None:
        """A customer without KBs should NOT carry a top_k in the DSL —
        no retrieval means no cap to enforce, and emitting one would just be
        noise. The reservation also skips the allowance for non-RAG."""
        fake_dify.blocking_response = {
            "id": "m", "answer": "ok", "conversation_id": "c",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        }
        app = _build_app(fake_dify, default_kb_top_k=3)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 200
        _, yaml_content = fake_dify.calls["import"][-1]
        dsl = yaml.safe_load(yaml_content)
        cfg = dsl["model_config"]["dataset_configs"]
        assert "top_k" not in cfg, f"top_k emitted with no KBs — got {cfg}"
        # Reservation also unchanged: in_flight back to 0 after blocking call.
        assert app.state.quota_store.in_flight == 0

    def test_estimate_cost_adds_rag_allowance(self) -> None:
        """Unit: rag_allowance_tokens flows into token_cost."""
        c_no_rag = estimate_cost(input_chars=40, max_output_tokens=100, model_id="m")
        c_rag = estimate_cost(
            input_chars=40, max_output_tokens=100, model_id="m",
            rag_allowance_tokens=3000,
        )
        assert c_rag.token_cost == c_no_rag.token_cost + 3000
        # Negative clamped (defensive).
        c_neg = estimate_cost(
            input_chars=40, max_output_tokens=100, model_id="m",
            rag_allowance_tokens=-5,
        )
        assert c_neg.token_cost == c_no_rag.token_cost


class TestReview2PinAdmitRetryAfter:
    """PR #10 review-2 #4: pin that admit() emits OverloadError with a
    non-None retry_after_s. The admit path calls jittered_retry_after(0.0)
    (not None) precisely BECAUSE jittered_retry_after now treats None as
    'structurally unsatisfiable, omit Retry-After header'. If a future
    refactor reverts the helper's None semantics, admit() would silently
    start emitting retry_after_s=None and clients would lose their
    Retry-After hint without any test catching it.
    """

    @pytest.mark.asyncio
    async def test_overload_503_carries_finite_retry_after(
        self, fake_dify: FakeDifyClient
    ) -> None:
        # node_token_budget=1 forces 503 on the first non-trivial request.
        app = _build_app(fake_dify, node_token_budget=1)
        async with _client(app) as cli:
            r = await cli.post(
                "/v1/chat/completions",
                headers=_AUTH,
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}]},
            )
        assert r.status_code == 503
        # The Retry-After header MUST be present (non-None retry_after_s).
        # If the helper's None handling regresses to "small jittered delay",
        # this stays True (harmless). If admit() ever starts passing None,
        # this test fails — exactly the regression we want to catch.
        assert "retry-after" in {k.lower() for k in r.headers}, (
            "OverloadError must carry a finite Retry-After — admit() relies "
            "on jittered_retry_after(0.0) for default backoff. If a refactor "
            "passes None instead, this regression would silently drop the "
            "header (PR #10 review-2 #4)."
        )
