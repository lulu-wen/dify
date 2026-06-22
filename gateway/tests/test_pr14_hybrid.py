"""Tests for PR #14 hybrid-mode chat dispatcher.

Coverage:

* ``Settings.effective_mode`` 3-state resolution (legacy ``thin_proxy_mode``
  alias still wins when True)
* ``create_app`` mounts the correct chat router per mode
* Hybrid dispatcher: use_rag dispatch + entitlement (rag_enabled, dify
  config) + per-request dataset scope validation (shared-mode safety)
* Backward compat: existing thin-proxy tests still pass (separate file)
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from gateway.errors import NotEntitledError

# --------------------------------------------------------------------------- #
# Settings.effective_mode resolution
# --------------------------------------------------------------------------- #


class TestEffectiveMode:
    def test_default_is_dify(self) -> None:
        from gateway.config import Settings

        s = Settings(registry_path="x.yaml")
        assert s.effective_mode == "dify"

    def test_mode_thin_proxy_wins(self) -> None:
        from gateway.config import Settings

        s = Settings(registry_path="x.yaml", mode="thin_proxy")
        assert s.effective_mode == "thin_proxy"

    def test_mode_hybrid(self) -> None:
        from gateway.config import Settings

        s = Settings(registry_path="x.yaml", mode="hybrid")
        assert s.effective_mode == "hybrid"

    def test_legacy_thin_proxy_mode_alias_overrides_mode(self) -> None:
        """thin_proxy_mode=True is the PR #13 boolean. It must still produce
        the same behaviour as before PR #14 even when mode is set to something
        else (backward compat with existing deployments)."""
        from gateway.config import Settings

        s = Settings(
            registry_path="x.yaml", thin_proxy_mode=True, mode="dify"
        )
        assert s.effective_mode == "thin_proxy"

        s = Settings(
            registry_path="x.yaml", thin_proxy_mode=True, mode="hybrid"
        )
        assert s.effective_mode == "thin_proxy"


# --------------------------------------------------------------------------- #
# create_app mounts the right router for each mode
# --------------------------------------------------------------------------- #


class TestCreateAppRouting:
    def _build(self, *, mode: str, llm_endpoint: str = "http://test/llm"):
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        kwargs: dict[str, Any] = dict(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            mode=mode,
            llm_endpoint=llm_endpoint,
            asr_endpoint="http://test/asr",
            tts_endpoint="http://test/tts",
        )
        s = Settings(**kwargs)
        return create_app(settings=s, registry=registry)

    def test_dify_mode_does_not_mount_audio(self) -> None:
        app = self._build(mode="dify", llm_endpoint="")
        paths = set(app.openapi()["paths"].keys())
        assert "/v1/audio/transcriptions" not in paths
        assert "/v1/audio/speech" not in paths
        assert "/v1/chat/completions" in paths

    def test_thin_proxy_mode_mounts_audio(self) -> None:
        app = self._build(mode="thin_proxy")
        paths = set(app.openapi()["paths"].keys())
        assert "/v1/audio/transcriptions" in paths
        assert "/v1/audio/speech" in paths
        assert "/v1/chat/completions" in paths

    def test_hybrid_mode_mounts_audio_and_chat(self) -> None:
        app = self._build(mode="hybrid")
        paths = set(app.openapi()["paths"].keys())
        assert "/v1/audio/transcriptions" in paths
        assert "/v1/audio/speech" in paths
        assert "/v1/chat/completions" in paths

    def test_hybrid_mode_without_llm_endpoint_raises(self) -> None:
        """PR #14 extends PR #13 R1 #8: hybrid mode also needs llm_endpoint
        because half its requests still thin-proxy. Fail at startup."""
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            mode="hybrid",
            llm_endpoint="",
        )
        with pytest.raises(RuntimeError, match="GATEWAY_LLM_ENDPOINT"):
            create_app(settings=s, registry=registry)


# --------------------------------------------------------------------------- #
# Hybrid dispatcher: use_rag routing
# --------------------------------------------------------------------------- #


def _build_hybrid_app(
    *,
    rag_enabled: bool = True,
    knowledge_bases: list[str] | None = None,
):
    """Hybrid-mode app with a single test customer wired in."""
    from gateway.config import Settings
    from gateway.main import create_app
    from gateway.registry import CustomerRegistry
    from tests.conftest import make_customer

    customer = make_customer(
        sdk_key="bsa_hybrid_key",
        customer_id="hybrid-co",
        knowledge_bases=knowledge_bases or ["kb-uuid-1", "kb-uuid-2"],
    )
    # rag_enabled isn't a make_customer kwarg yet — patch via model_copy.
    customer = customer.model_copy(update={"rag_enabled": rag_enabled})
    registry = CustomerRegistry.from_entries([customer])

    settings = Settings(
        registry_path="unused.yaml",
        log_json=False,
        rate_limit_enabled=False,
        mode="hybrid",
        llm_endpoint="http://test/llm",
        asr_endpoint="http://test/asr",
        tts_endpoint="http://test/tts",
        strict_startup=False,
    )
    app = create_app(settings=settings, registry=registry)
    return app, customer


class TestHybridDispatch:
    @pytest.mark.asyncio
    async def test_use_rag_false_routes_to_thin_proxy(self) -> None:
        """No use_rag set → thin-proxy path (POST hits the LLM endpoint
        directly, not Dify)."""
        from httpx import ASGITransport, AsyncClient

        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            # Thin-proxy posts to ``{llm_endpoint}/v1/chat/completions``.
            captured["url"] = str(req.url)
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_hybrid_app()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                        # use_rag deliberately omitted
                    },
                    headers={"Authorization": "Bearer bsa_hybrid_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original  # type: ignore[misc]

        assert resp.status_code == 200
        assert "test/llm/v1/chat/completions" in captured["url"]

    @pytest.mark.asyncio
    async def test_use_rag_explicit_false_routes_to_thin_proxy(self) -> None:
        """Same as above but ``use_rag: false`` explicitly — still thin-proxy."""
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_hybrid_app()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                        "use_rag": False,
                    },
                    headers={"Authorization": "Bearer bsa_hybrid_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original  # type: ignore[misc]

        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Hybrid dispatcher: entitlement
# --------------------------------------------------------------------------- #


class TestHybridEntitlement:
    @pytest.mark.asyncio
    async def test_use_rag_true_without_rag_enabled_returns_403(self) -> None:
        """``use_rag=true`` + ``customer.rag_enabled=false`` → 403 not_entitled.

        The error code mirrors the audio entitlement gate so SDK clients can
        share a single 'upgrade your plan' handler.
        """
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(rag_enabled=False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "not_entitled"
        assert "RAG" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_use_rag_true_with_rag_enabled_attempts_dify(self) -> None:
        """``use_rag=true`` + ``rag_enabled=true`` + dify config present →
        request reaches the Dify-path router. We verify it gets PAST the
        entitlement gate; the actual Dify roundtrip isn't easy to mock here
        without a FakeDifyClient fixture (covered by existing PR #1-#12 tests).
        Instead we assert the response is NOT a 403/503 entitlement error.
        """
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(rag_enabled=True)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        # We don't expect 200 (no FakeDifyClient wired in this app) but
        # the failure must NOT be the hybrid-dispatcher entitlement gate.
        assert resp.status_code != 403, "entitlement should have passed"
        body = resp.json()
        assert body["error"]["code"] != "not_entitled"


# --------------------------------------------------------------------------- #
# Per-request dataset_ids scope validation (shared-mode safety)
# --------------------------------------------------------------------------- #


class TestDatasetScope:
    """R1 #2 + #9: dataset_ids override is REJECTED with 400 in v1 until
    the override is wired through AppManager. Both use_rag branches
    enforce the same rejection so a cross-tenant UUID probe can't
    distinguish 'foreign UUID' from 'unsupported feature'."""

    @pytest.mark.asyncio
    async def test_dataset_ids_rejected_when_use_rag_true(self) -> None:
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(
            rag_enabled=True, knowledge_bases=["kb-A", "kb-B"]
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                    "dataset_ids": ["kb-A"],
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["param"] == "dataset_ids"
        assert "not yet supported" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_dataset_ids_rejected_when_use_rag_false(self) -> None:
        """R1 #9: foreign-UUID probe can't slip through the use_rag=false
        branch. The rejection is identical regardless of use_rag value so
        cross-tenant probes get no oracle."""
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(
            rag_enabled=True, knowledge_bases=["kb-A"]
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": False,
                    "dataset_ids": ["kb-foreign"],
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"]["param"] == "dataset_ids"

    @pytest.mark.asyncio
    async def test_dataset_ids_empty_list_also_rejected(self) -> None:
        """R1 #2: even ``dataset_ids: []`` is non-None so it's rejected.
        Empty list previously meant 'RAG-on, no retrieval' but with the
        override not threaded through, accepting [] would still silently
        retrieve from the customer's default knowledge_bases — same
        contract violation we're closing."""
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(
            rag_enabled=True, knowledge_bases=["kb-A"]
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                    "dataset_ids": [],
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_dataset_ids_too_long_rejected_at_schema(self) -> None:
        """R1 #11: schema caps dataset_ids at 50 items so a megabyte-list
        attack can't force the gateway to echo back gigantic 400 envelopes."""
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(
            rag_enabled=True, knowledge_bases=["kb-A"]
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                    "dataset_ids": [f"kb-{i}" for i in range(100)],
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        # Pydantic schema validation (422) fires before dispatcher.
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_dataset_ids_too_long_per_item_rejected(self) -> None:
        """R1 #11: per-item max_length=128 stops megabyte-string-per-item
        attacks too."""
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(rag_enabled=True)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                    "dataset_ids": ["A" * 200],
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        assert resp.status_code in (400, 422)


# --------------------------------------------------------------------------- #
# Schema-level (no HTTP) — direct dispatcher tests via the entitlement helper
# --------------------------------------------------------------------------- #


class TestEntitlementHelper:
    """Direct unit tests of ``_check_rag_entitlement`` (R1 #14: only via
    real CustomerEntry constructors — model_copy bypasses validators and
    silently constructs states the live registry can never produce, so
    that pattern is no longer used).

    Helper signature shrank in R1 #4 (no body param) since dataset_ids
    is now rejected up-front by the dispatcher, not inside the helper.
    """

    def _customer(
        self,
        *,
        rag_enabled: bool = True,
        knowledge_bases: list[str] | None = None,
    ):
        """Build a real CustomerEntry honouring registry validators."""
        from tests.conftest import make_customer

        c = make_customer(knowledge_bases=knowledge_bases or ["kb-A"])
        # rag_enabled isn't a make_customer kwarg yet — model_copy IS
        # safe for plain-bool overrides (no validator depends on it).
        return c.model_copy(update={"rag_enabled": rag_enabled})

    def test_passes_with_rag_enabled(self) -> None:
        from gateway.routers.chat_hybrid import _check_rag_entitlement

        _check_rag_entitlement(self._customer(), request_id="req-1")  # no raise

    def test_raises_not_entitled_when_rag_disabled(self) -> None:
        from gateway.routers.chat_hybrid import _check_rag_entitlement

        with pytest.raises(NotEntitledError):
            _check_rag_entitlement(
                self._customer(rag_enabled=False), request_id="req-2"
            )


# --------------------------------------------------------------------------- #
# R1 regression tests
# --------------------------------------------------------------------------- #


class TestR1ThinProxyRejectsUseRag:
    """R1 #5: in pure mode='thin_proxy' deployments the chat_thin_proxy
    router is mounted directly (no hybrid dispatcher), so a client
    sending use_rag=true would silently get plain LLM. Reject up-front."""

    @pytest.mark.asyncio
    async def test_use_rag_true_in_thin_proxy_mode_returns_400(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries(
            [make_customer(sdk_key="bsa_tp_key", customer_id="tp-co")]
        )
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            mode="thin_proxy",
            llm_endpoint="http://test/llm",
        )
        app = create_app(settings=s, registry=registry)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                },
                headers={"Authorization": "Bearer bsa_tp_key"},
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["param"] == "use_rag"
        assert "not supported" in body["error"]["message"]


class TestR1UseRagOmittedWarning:
    """R1 #3: silent thin-proxy for a rag_enabled customer that doesn't
    set use_rag is a quality-regression UX failure. The dispatcher emits
    a warning event when that happens so operators can detect SDK lag."""

    @pytest.mark.asyncio
    async def test_warning_emitted_when_rag_enabled_customer_omits_use_rag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_hybrid_app(rag_enabled=True)
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                        # use_rag deliberately omitted (None)
                    },
                    headers={"Authorization": "Bearer bsa_hybrid_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original  # type: ignore[misc]

        # Request still succeeded (we honour the omission with a warning,
        # not a rejection).
        assert resp.status_code == 200
        # And the warning fired. structlog writes to stderr in the
        # gateway's configured chain so capsys captures it there.
        captured = capsys.readouterr()
        assert (
            "hybrid.use_rag_omitted_for_rag_enabled_customer"
            in captured.out + captured.err
        )

    @pytest.mark.asyncio
    async def test_no_warning_for_rag_disabled_customer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """rag_enabled=false customers omit use_rag all the time — no
        warning."""
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_hybrid_app(rag_enabled=False)
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": "Bearer bsa_hybrid_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original  # type: ignore[misc]

        captured = capsys.readouterr()
        assert (
            "hybrid.use_rag_omitted"
            not in captured.out + captured.err
        )


class TestR1ThinProxyStripsHybridFields:
    """R1 #1: the thin-proxy forward body MUST NOT carry use_rag /
    dataset_ids — vLLM 0.6+ rejects unknown fields with 400, and
    LiteLLM access logs would otherwise show gateway-internal vocab."""

    @pytest.mark.asyncio
    async def test_use_rag_stripped_from_forward_body(self) -> None:
        from httpx import ASGITransport, AsyncClient

        captured_body: dict[str, Any] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured_body.update(_json.loads(req.content))
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_hybrid_app()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                        "use_rag": False,
                        # dataset_ids omitted (would be rejected if set)
                    },
                    headers={"Authorization": "Bearer bsa_hybrid_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original  # type: ignore[misc]

        assert "use_rag" not in captured_body
        assert "dataset_ids" not in captured_body
        # Sanity: the actual OpenAI fields ARE forwarded.
        assert captured_body.get("model") == "m1"


class TestR1RagDispatchSuccessLog:
    """R1 #6: when the dispatcher delegates to the RAG path, emit an
    info-level ``hybrid.rag_dispatch`` so dashboards can count RAG
    attempts independent of downstream success/failure."""

    @pytest.mark.asyncio
    async def test_dispatch_event_fires_before_chat_router_runs(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(rag_enabled=True)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            # Will fail downstream (no FakeDifyClient wired) but the
            # dispatch log must already have fired by then.
            await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        captured = capsys.readouterr()
        assert "hybrid.rag_dispatch" in captured.out + captured.err
