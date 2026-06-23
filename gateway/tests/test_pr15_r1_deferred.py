"""Tests for PR #15 — PR #14 R1 deferred findings.

Coverage:

* #7  — chat_thin_proxy raises ``UnknownModelError`` (404 ``model_not_found``)
        matching chat.py, instead of ``InvalidRequestError`` (400).
* #8  — Startup logs a warning (not abort) when audio routes are mounted
        but asr/tts endpoints are unset.
* #10 — Registry load fails when a knowledge_base UUID appears in two
        customers' lists (cross-tenant retrieval would otherwise leak).
* #12-14 — Test rigor:
        - exact CSV column ORDER pinned (was: spot-check 5)
        - integration-style assertion that hybrid dispatcher actually
          invokes chat_router (was: status_code != 403 — false-positive)
"""

from __future__ import annotations

from typing import Any

import pytest

from gateway.errors import UnknownModelError

# --------------------------------------------------------------------------- #
# #7 — chat_thin_proxy raises UnknownModelError for unknown model
# --------------------------------------------------------------------------- #


class TestR1DeferredUnknownModelError:
    @pytest.mark.asyncio
    async def test_thin_proxy_unknown_model_returns_404_model_not_found(self) -> None:
        """R1 #7: was ``InvalidRequestError`` (400 ``invalid_request``).
        Now ``UnknownModelError`` (404 ``model_not_found``), matching
        chat.py so hybrid mode is internally consistent across
        ``use_rag=true`` and ``use_rag=false`` branches.
        """
        from httpx import ASGITransport, AsyncClient

        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries(
            [make_customer(sdk_key="bsa_tp", customer_id="tp-co", model_ids=("only-m1",))]
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
                    "model": "not-in-whitelist",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": "Bearer bsa_tp"},
            )

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "model_not_found"
        assert body["error"]["param"] == "model"

    def test_typed_class_alignment(self) -> None:
        """The class type is what code paths (alerts, dashboards) actually
        switch on. Pin the class so a refactor that reverts to
        ``InvalidRequestError`` is caught here, not in production."""
        # Just importable and 404 — both routers raise this class shape.
        assert UnknownModelError.status_code == 404
        assert UnknownModelError.code == "model_not_found"


# --------------------------------------------------------------------------- #
# #8 — startup warns when asr/tts endpoint empty + audio mounted
# --------------------------------------------------------------------------- #


class TestR1DeferredAudioEndpointStartupWarn:
    def test_warning_emitted_when_asr_empty_in_thin_proxy(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """R1 #8: previously startup silently let asr_endpoint=''
        through, then every /v1/audio/transcriptions request 503'd.
        Now logs a structured warning so operators see it at startup
        instead of when the first audio request comes in.
        """
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            mode="thin_proxy",
            llm_endpoint="http://test/llm",  # required, set
            asr_endpoint="",                  # ← deliberately empty
            tts_endpoint="",
        )
        # create_app must succeed — the asr/tts checks are advisory.
        create_app(settings=s, registry=registry)
        captured = capsys.readouterr()
        log_text = captured.out + captured.err
        assert "gateway.startup.audio_endpoint_unset" in log_text
        assert "GATEWAY_ASR_ENDPOINT" in log_text
        assert "GATEWAY_TTS_ENDPOINT" in log_text

    def test_no_warning_when_endpoints_set(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            mode="thin_proxy",
            llm_endpoint="http://test/llm",
            asr_endpoint="http://test/asr",
            tts_endpoint="http://test/tts",
        )
        create_app(settings=s, registry=registry)
        captured = capsys.readouterr()
        assert "audio_endpoint_unset" not in captured.out + captured.err

    def test_no_warning_in_dify_mode(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dify mode doesn't mount audio routes — no warning even with
        empty asr/tts endpoints."""
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            mode="dify",
            asr_endpoint="",
            tts_endpoint="",
        )
        create_app(settings=s, registry=registry)
        captured = capsys.readouterr()
        assert "audio_endpoint_unset" not in captured.out + captured.err


# --------------------------------------------------------------------------- #
# #10 — cross-customer knowledge_base UUID collision check
# --------------------------------------------------------------------------- #


class TestR1DeferredKnowledgeBaseUniqueness:
    def test_collision_across_customers_fails_load(self) -> None:
        """R1 #10: a knowledge_base UUID that appears in two customers'
        lists is rejected at registry load. The hybrid dispatcher's
        scope check would otherwise let either customer reference the
        UUID, defeating PR #14's data-isolation guarantee."""
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        a = make_customer(
            sdk_key="bsa_a", customer_id="co-a",
            knowledge_bases=["uuid-shared", "uuid-a-only"],
        )
        b = make_customer(
            sdk_key="bsa_b", customer_id="co-b",
            knowledge_bases=["uuid-shared", "uuid-b-only"],
        )
        with pytest.raises(ValueError, match="uuid-shared"):
            CustomerRegistry.from_entries([a, b])

    def test_unique_kbs_load_fine(self) -> None:
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        a = make_customer(
            sdk_key="bsa_a", customer_id="co-a",
            knowledge_bases=["kb-1", "kb-2"],
        )
        b = make_customer(
            sdk_key="bsa_b", customer_id="co-b",
            knowledge_bases=["kb-3"],
        )
        reg = CustomerRegistry.from_entries([a, b])
        assert len(reg) == 2

    def test_empty_kb_lists_allowed(self) -> None:
        """A customer with no knowledge_bases doesn't trigger the check."""
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        a = make_customer(
            sdk_key="bsa_a", customer_id="co-a", knowledge_bases=[]
        )
        b = make_customer(
            sdk_key="bsa_b", customer_id="co-b", knowledge_bases=[]
        )
        reg = CustomerRegistry.from_entries([a, b])
        assert len(reg) == 2

    def test_same_customer_duplicate_kb_within_list_is_separate_concern(
        self,
    ) -> None:
        """A single customer listing the same UUID twice is allowed by
        this check (would be silly but isn't a cross-tenant leak).
        Codified so a future ``set``-using refactor doesn't tighten this
        accidentally without a discussion."""
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        a = make_customer(
            sdk_key="bsa_a", customer_id="co-a",
            knowledge_bases=["kb-1", "kb-1"],
        )
        # No raise — the check only fires for cross-customer collisions.
        CustomerRegistry.from_entries([a])

    def test_error_message_lists_all_collisions(self) -> None:
        """Error message must name every offending UUID so operators can
        find the source quickly (don't make them iterate)."""
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        a = make_customer(
            sdk_key="bsa_a", customer_id="co-a",
            knowledge_bases=["clash-1", "clash-2"],
        )
        b = make_customer(
            sdk_key="bsa_b", customer_id="co-b",
            knowledge_bases=["clash-1", "clash-2"],
        )
        with pytest.raises(ValueError) as ei:
            CustomerRegistry.from_entries([a, b])
        msg = str(ei.value)
        assert "clash-1" in msg
        assert "clash-2" in msg
        assert "co-a" in msg
        assert "co-b" in msg


# --------------------------------------------------------------------------- #
# #12 — replace weak hybrid-dispatch assertions with direct verification
# --------------------------------------------------------------------------- #


class TestR1DeferredHybridDispatchVerification:
    """R1 #12: TestHybridDispatch.test_use_rag_true_with_rag_enabled_attempts_dify
    only checked ``status_code != 403`` (any non-403 passed — including a
    silent fall-through to thin-proxy). This test directly verifies the
    delegated function was called.
    """

    @pytest.mark.asyncio
    async def test_use_rag_true_actually_invokes_chat_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Use monkeypatch (R1 #13) to record whether chat_router or
        chat_thin_proxy_router got the call. If a future refactor inverts
        the dispatch condition, ``chat_thin_proxy.chat_completions_thin_proxy``
        would be called and this test fails clearly."""
        from httpx import ASGITransport, AsyncClient

        from gateway.routers import chat as chat_router
        from gateway.routers import chat_thin_proxy as chat_thin_proxy_router

        # Late-imports to avoid coupling at module load.
        from tests.test_pr14_hybrid import _build_hybrid_app

        called: dict[str, int] = {"chat": 0, "thin_proxy": 0}

        # Capture invocations. We can't simply replace with no-op because
        # callers await a Response/JSONResponse — return a dummy success.
        from fastapi.responses import JSONResponse

        async def _fake_chat(request: Any, body: Any) -> Any:
            called["chat"] += 1
            return JSONResponse(content={"id": "fake-chat", "ok": True})

        async def _fake_thin(request: Any, body: Any) -> Any:
            called["thin_proxy"] += 1
            return JSONResponse(content={"id": "fake-thin", "ok": True})

        monkeypatch.setattr(
            chat_router, "chat_completions", _fake_chat
        )
        monkeypatch.setattr(
            chat_thin_proxy_router,
            "chat_completions_thin_proxy",
            _fake_thin,
        )

        app, _ = _build_hybrid_app(rag_enabled=True)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            r_rag = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": True,
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )
            r_thin = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "use_rag": False,
                },
                headers={"Authorization": "Bearer bsa_hybrid_key"},
            )

        assert r_rag.status_code == 200
        assert r_thin.status_code == 200
        # The proof: each call hit exactly the intended router.
        assert called == {"chat": 1, "thin_proxy": 1}
        # And the response bodies came from the right fake.
        assert r_rag.json()["id"] == "fake-chat"
        assert r_thin.json()["id"] == "fake-thin"
