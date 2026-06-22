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

from gateway.errors import NotEntitledError, UnknownDatasetError

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
    @pytest.mark.asyncio
    async def test_dataset_ids_subset_of_customer_kbs_allowed(self) -> None:
        """Subset of owned dataset_ids passes the scope check."""
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(
            rag_enabled=True,
            knowledge_bases=["kb-A", "kb-B", "kb-C"],
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
                    "dataset_ids": ["kb-A", "kb-B"],
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        # Past the scope check — should NOT be UnknownDatasetError.
        assert resp.json()["error"]["code"] != "dataset_not_found"

    @pytest.mark.asyncio
    async def test_dataset_ids_foreign_uuid_returns_404(self) -> None:
        """Shared-mode safety: a UUID belonging to another customer must
        return ``dataset_not_found`` with the same envelope shape as
        'doesn't exist'. Per PR #4 R7 hotfix pattern, the gateway never
        distinguishes the two so cross-tenant probes can't tell the
        difference."""
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_hybrid_app(
            rag_enabled=True,
            knowledge_bases=["kb-A"],
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
                    "dataset_ids": ["kb-A", "kb-foreign"],
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "dataset_not_found"

    @pytest.mark.asyncio
    async def test_dataset_ids_empty_list_allowed(self) -> None:
        """Empty ``dataset_ids`` is a legitimate 'no retrieval but still use
        the Dify DSL' opt-in (e.g. A/B testing RAG impact). Must pass the
        scope check since {} ⊆ any."""
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

        # Past scope check (empty subset is fine) — shouldn't be 404.
        assert resp.json()["error"]["code"] != "dataset_not_found"

    @pytest.mark.asyncio
    async def test_dataset_ids_with_use_rag_false_logged_but_not_rejected(
        self,
    ) -> None:
        """Internally inconsistent (dataset_ids set but use_rag false): the
        dispatcher honours use_rag=false and routes thin-proxy. dataset_ids
        is silently ignored. Behaviour-wise the request must succeed (or fail
        for other reasons), NOT bounce on the inconsistency."""
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
                        "dataset_ids": ["kb-A"],
                    },
                    headers={"Authorization": "Bearer bsa_hybrid_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original  # type: ignore[misc]

        # Thin-proxy succeeded; dataset_ids was silently ignored.
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Schema-level (no HTTP) — direct dispatcher tests via the entitlement helper
# --------------------------------------------------------------------------- #


class TestEntitlementHelper:
    """Direct unit tests of ``_check_rag_entitlement`` — avoids spinning up
    a full FastAPI app for each scenario."""

    def _customer(
        self,
        *,
        rag_enabled: bool = True,
        with_dify: bool = True,
        knowledge_bases: list[str] | None = None,
    ):
        from tests.conftest import make_customer

        c = make_customer(knowledge_bases=knowledge_bases or ["kb-A"])
        update: dict[str, Any] = {"rag_enabled": rag_enabled}
        if not with_dify:
            update["dify"] = None  # type: ignore[assignment]
        return c.model_copy(update=update)

    def test_passes_with_full_entitlement(self) -> None:
        from gateway.routers.chat_hybrid import _check_rag_entitlement
        from gateway.schemas import ChatCompletionRequest, ChatMessage

        body = ChatCompletionRequest(
            model="m1",
            messages=[ChatMessage(role="user", content="hi")],
            use_rag=True,
        )
        _check_rag_entitlement(
            self._customer(), body, request_id="req-1"
        )  # no raise

    def test_raises_not_entitled_when_rag_disabled(self) -> None:
        from gateway.routers.chat_hybrid import _check_rag_entitlement
        from gateway.schemas import ChatCompletionRequest, ChatMessage

        body = ChatCompletionRequest(
            model="m1",
            messages=[ChatMessage(role="user", content="hi")],
            use_rag=True,
        )
        with pytest.raises(NotEntitledError):
            _check_rag_entitlement(
                self._customer(rag_enabled=False),
                body,
                request_id="req-2",
            )

    def test_raises_unknown_dataset_for_foreign_uuid(self) -> None:
        from gateway.routers.chat_hybrid import _check_rag_entitlement
        from gateway.schemas import ChatCompletionRequest, ChatMessage

        body = ChatCompletionRequest(
            model="m1",
            messages=[ChatMessage(role="user", content="hi")],
            use_rag=True,
            dataset_ids=["kb-A", "kb-foreign"],
        )
        with pytest.raises(UnknownDatasetError) as ei:
            _check_rag_entitlement(
                self._customer(knowledge_bases=["kb-A"]),
                body,
                request_id="req-3",
            )
        # The error message names the unknown ids (legitimate-operator
        # debugging) but the HTTP code stays identical to "not found".
        assert "kb-foreign" in str(ei.value)

    def test_passes_with_empty_dataset_ids(self) -> None:
        """Empty list is a valid 'RAG-off, DSL-on' opt-in."""
        from gateway.routers.chat_hybrid import _check_rag_entitlement
        from gateway.schemas import ChatCompletionRequest, ChatMessage

        body = ChatCompletionRequest(
            model="m1",
            messages=[ChatMessage(role="user", content="hi")],
            use_rag=True,
            dataset_ids=[],
        )
        _check_rag_entitlement(
            self._customer(knowledge_bases=["kb-A"]),
            body,
            request_id="req-4",
        )  # no raise
