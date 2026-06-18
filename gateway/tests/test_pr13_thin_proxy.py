"""Tests for PR #13 thin-proxy mode (audio router + chat passthrough + WhisperX translator).

Coverage:

* WhisperX request builder (OpenAI Whisper schema → WhisperX form fields)
* WhisperX response translator (segments → ``{"text":...}`` or verbose_json)
* /v1/audio/transcriptions router (multipart upload → WhisperX upstream)
* /v1/audio/speech router (passthrough to Kokoro)
* /v1/chat/completions thin-proxy router (forward to LLM endpoint)
* Settings flag wiring in create_app (Dify-mode vs thin-proxy-mode mount)
* Endpoint-not-configured → ServiceUnavailableError (503)
"""

from __future__ import annotations

import json
from typing import ClassVar

import httpx
import pytest

from gateway.errors import InvalidRequestError
from gateway.translators.whisperx import (
    build_whisperx_request,
    translate_whisperx_response,
)

# --------------------------------------------------------------------------- #
# WhisperX translator — pure data, no HTTP
# --------------------------------------------------------------------------- #


class TestBuildWhisperxRequest:
    def test_minimal_defaults(self) -> None:
        req, shape = build_whisperx_request(
            language=None,
            response_format="json",
            prompt=None,
            temperature=0.0,
            extra_body=None,
        )
        assert req.language == "auto"
        assert req.num_speakers is None
        assert req.min_speakers is None
        assert req.max_speakers is None
        assert shape == "json"
        assert req.to_form() == {"language": "auto"}

    def test_language_passthrough(self) -> None:
        req, _ = build_whisperx_request(
            language="zh", response_format="json",
            prompt=None, temperature=0.0, extra_body=None,
        )
        assert req.language == "zh"

    def test_speaker_hints_from_extra_body(self) -> None:
        req, _ = build_whisperx_request(
            language="en",
            response_format="verbose_json",
            prompt=None,
            temperature=0.0,
            extra_body='{"num_speakers": 3, "min_speakers": 2, "max_speakers": 4}',
        )
        assert req.num_speakers == 3
        assert req.min_speakers == 2
        assert req.max_speakers == 4
        form = req.to_form()
        assert form == {
            "language": "en",
            "num_speakers": "3",
            "min_speakers": "2",
            "max_speakers": "4",
        }

    def test_invalid_response_format_raises(self) -> None:
        for bad in ("text", "srt", "vtt", "foo"):
            with pytest.raises(InvalidRequestError) as excinfo:
                build_whisperx_request(
                    language=None, response_format=bad,
                    prompt=None, temperature=0.0, extra_body=None,
                )
            assert bad in str(excinfo.value)
            assert excinfo.value.param == "response_format"

    def test_prompt_and_temperature_are_dropped_silently(self) -> None:
        # Should not raise; just log warnings. We don't assert log content
        # to keep the test robust to structlog config drift.
        req, _ = build_whisperx_request(
            language=None,
            response_format="json",
            prompt="please pronounce names carefully",
            temperature=0.7,
            extra_body=None,
        )
        # Prompt and temperature are NOT in the WhisperX form.
        assert "prompt" not in req.to_form()
        assert "temperature" not in req.to_form()

    def test_extra_body_invalid_json_falls_back_to_no_hints(self) -> None:
        req, _ = build_whisperx_request(
            language=None, response_format="json",
            prompt=None, temperature=0.0,
            extra_body="not valid json {",
        )
        assert req.num_speakers is None
        assert req.min_speakers is None
        assert req.max_speakers is None

    def test_extra_body_negative_speaker_count_is_dropped(self) -> None:
        # WhisperX rejects 0 / negative; drop silently rather than 422.
        req, _ = build_whisperx_request(
            language=None, response_format="json",
            prompt=None, temperature=0.0,
            extra_body='{"num_speakers": 0, "min_speakers": -1}',
        )
        assert req.num_speakers is None
        assert req.min_speakers is None

    def test_extra_body_boolean_speaker_count_is_dropped(self) -> None:
        # PR #13 R1: ``isinstance(True, int)`` is True (bool is an int
        # subclass). Without the explicit bool guard a JSON ``true`` would
        # have serialised as the literal string "True" → WhisperX 422.
        req, _ = build_whisperx_request(
            language=None, response_format="json",
            prompt=None, temperature=0.0,
            extra_body='{"num_speakers": true, "min_speakers": false}',
        )
        assert req.num_speakers is None
        assert req.min_speakers is None
        # And the form payload reflects the drop — no stringified bool.
        assert "num_speakers" not in req.to_form()
        assert "min_speakers" not in req.to_form()


class TestTranslateWhisperxResponse:
    _WHISPERX_TYPICAL: ClassVar[dict] = {
        "segments": [
            {
                "start": 0.0,
                "end": 1.5,
                "text": "Hello world",
                "speaker": "SPEAKER_00",
                "words": [
                    {"word": "Hello", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_00"},
                    {"word": "world", "start": 0.5, "end": 1.5, "speaker": "SPEAKER_00"},
                ],
            },
            {
                "start": 1.5,
                "end": 2.8,
                "text": "How are you",
                "speaker": "SPEAKER_01",
                "words": [],
            },
        ],
        "language": "en",
    }

    def test_simple_json_returns_concatenated_text(self) -> None:
        out = translate_whisperx_response(
            self._WHISPERX_TYPICAL,
            response_shape="json",
            requested_language="en",
        )
        assert out == {"text": "Hello world How are you"}

    def test_verbose_json_returns_full_envelope(self) -> None:
        out = translate_whisperx_response(
            self._WHISPERX_TYPICAL,
            response_shape="verbose_json",
            requested_language="en",
        )
        assert out["task"] == "transcribe"
        assert out["language"] == "en"
        assert out["duration"] == 2.8
        assert out["text"] == "Hello world How are you"
        assert len(out["segments"]) == 2
        # Speaker info passed through as an extra field (OpenAI clients
        # tolerate unknown fields).
        assert out["segments"][0]["speaker"] == "SPEAKER_00"
        assert out["segments"][1]["speaker"] == "SPEAKER_01"
        # Words flattened across segments.
        assert len(out["words"]) == 2
        assert out["words"][0]["word"] == "Hello"

    def test_empty_segments_returns_empty_text(self) -> None:
        out = translate_whisperx_response(
            {"segments": [], "language": "en"},
            response_shape="json",
            requested_language="en",
        )
        assert out == {"text": ""}

    def test_missing_language_falls_back_to_requested(self) -> None:
        out = translate_whisperx_response(
            {"segments": [{"text": "hi"}]},
            response_shape="verbose_json",
            requested_language="zh",
        )
        assert out["language"] == "zh"

    def test_malformed_segments_does_not_crash(self) -> None:
        # WhisperX returned an unexpected shape — we should fall back to
        # empty text rather than KeyError or crash.
        out = translate_whisperx_response(
            {"segments": "not a list"},  # type: ignore[dict-item]
            response_shape="json",
            requested_language="en",
        )
        assert out == {"text": ""}


# --------------------------------------------------------------------------- #
# create_app routing: thin-proxy vs Dify-mode wiring
# --------------------------------------------------------------------------- #


class TestCreateAppRouting:
    def test_thin_proxy_mode_mounts_audio_router(self) -> None:
        """In thin-proxy mode, /v1/audio/transcriptions exists in the
        registered routes. In Dify mode it does not."""
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        # main.py uses ``registry or load_from_yaml`` and an empty
        # CustomerRegistry is falsy (len==0), so we must inject a
        # non-empty one to bypass disk loading in tests.
        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            thin_proxy_mode=True,
            llm_endpoint="http://test/llm",
            asr_endpoint="http://test/asr",
            tts_endpoint="http://test/tts",
        )
        app = create_app(settings=s, registry=registry)
        # OpenAPI schema enumerates ALL registered paths (including those
        # mounted via include_router, which app.routes wraps in opaque
        # _IncludedRouter objects without a .path attribute).
        paths = set(app.openapi()["paths"].keys())
        assert "/v1/audio/transcriptions" in paths
        assert "/v1/audio/speech" in paths
        assert "/v1/chat/completions" in paths

    def test_dify_mode_does_not_mount_audio_router(self) -> None:
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            thin_proxy_mode=False,
        )
        app = create_app(settings=s, registry=registry)
        # OpenAPI schema enumerates ALL registered paths (including those
        # mounted via include_router, which app.routes wraps in opaque
        # _IncludedRouter objects without a .path attribute).
        paths = set(app.openapi()["paths"].keys())
        # Audio routes are NOT mounted in Dify mode.
        assert "/v1/audio/transcriptions" not in paths
        assert "/v1/audio/speech" not in paths
        # Chat is the Dify-flavoured version, still on the same path.
        assert "/v1/chat/completions" in paths


# --------------------------------------------------------------------------- #
# /v1/audio/transcriptions — end-to-end (with mocked WhisperX upstream)
# --------------------------------------------------------------------------- #


def _build_thin_proxy_app(
    *,
    asr_endpoint: str = "http://test/asr",
    tts_endpoint: str = "http://test/tts",
    llm_endpoint: str = "http://test/llm",
):
    """Build a thin-proxy app with a single test customer wired in."""
    from gateway.config import Settings
    from gateway.main import create_app
    from gateway.registry import CustomerRegistry
    from tests.conftest import make_customer

    customer = make_customer(sdk_key="bsa_test_key", customer_id="test-co")
    registry = CustomerRegistry.from_entries([customer])
    settings = Settings(
        registry_path="unused.yaml",
        log_json=False,
        rate_limit_enabled=False,
        thin_proxy_mode=True,
        llm_endpoint=llm_endpoint,
        asr_endpoint=asr_endpoint,
        tts_endpoint=tts_endpoint,
        strict_startup=False,
    )
    app = create_app(settings=settings, registry=registry)
    return app, customer


class TestAudioTranscriptionsRouter:
    @pytest.mark.asyncio
    async def test_forwards_to_whisperx_and_translates_response(self) -> None:
        """Default response_format=json → concat WhisperX segments."""
        from httpx import ASGITransport, AsyncClient

        captured: dict[str, object] = {}

        def _whisperx_handler(req: httpx.Request) -> httpx.Response:
            captured["path"] = req.url.path
            captured["method"] = req.method
            # Form fields land in the multipart body; httpx exposes the
            # raw bytes — we don't fully parse, just record that the call
            # happened and return a typical WhisperX shape.
            return httpx.Response(
                200,
                json={
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "hello world"}
                    ],
                    "language": "en",
                },
            )

        upstream_transport = httpx.MockTransport(_whisperx_handler)

        # Patch httpx.AsyncClient so every audio router-side call lands
        # in our mock. AsyncClient is instantiated inside the route; the
        # cleanest hook is via the global default transport.
        import gateway.routers.audio as audio_mod

        original_client = audio_mod.httpx.AsyncClient

        def _patched_async_client(*args, **kwargs):
            kwargs["transport"] = upstream_transport
            return original_client(*args, **kwargs)

        audio_mod.httpx.AsyncClient = _patched_async_client  # type: ignore[misc]
        try:
            app, _customer = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/audio/transcriptions",
                    files={"file": ("audio.wav", b"RIFF....fake", "audio/wav")},
                    data={"model": "whisper-1", "language": "en"},
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
        finally:
            audio_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert resp.status_code == 200
        assert resp.json() == {"text": "hello world"}
        assert captured["path"] == "/asr/transcribe"
        assert captured["method"] == "POST"

    @pytest.mark.asyncio
    async def test_missing_asr_endpoint_returns_503(self) -> None:
        """No asr_endpoint configured → ServiceUnavailableError."""
        from httpx import ASGITransport, AsyncClient

        app, _ = _build_thin_proxy_app(asr_endpoint="")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            resp = await client.post(
                "/v1/audio/transcriptions",
                files={"file": ("audio.wav", b"x", "audio/wav")},
                data={"model": "whisper-1"},
                headers={"Authorization": "Bearer bsa_test_key"},
            )
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "service_unavailable"
        assert "ASR endpoint not configured" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_asr_upstream_4xx_passes_through_status(self) -> None:
        """WhisperX 422 → UpstreamClientError preserves 422 to the client.

        PR #13 R1 fix: surfacing a client-shaped error (bad audio mime,
        unsupported language code) as a 502 misled SDKs into retrying
        their own bad input.
        """
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422, json={"detail": "unsupported audio format"}
            )

        import gateway.routers.audio as audio_mod

        original_client = audio_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original_client(*args, **kwargs)

        audio_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/audio/transcriptions",
                    files={"file": ("a.wav", b"x", "audio/wav")},
                    data={"model": "whisper-1"},
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
        finally:
            audio_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "upstream_invalid_request"


class TestAudioSpeechRouter:
    @pytest.mark.asyncio
    async def test_tts_upstream_4xx_passes_through_status(self) -> None:
        """Kokoro 422 (e.g. unknown voice) → UpstreamClientError, not 502."""
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"error": "unknown voice"})

        import gateway.routers.audio as audio_mod

        original_client = audio_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original_client(*args, **kwargs)

        audio_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/audio/speech",
                    json={
                        "model": "kokoro",
                        "voice": "bogus",
                        "input": "hello world",
                    },
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
        finally:
            audio_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "upstream_invalid_request"

    @pytest.mark.asyncio
    async def test_tts_upstream_5xx_returns_tts_error_envelope(self) -> None:
        """Kokoro 500 → TTSUpstreamError envelope (not raw bytes labelled audio/mpeg).

        PR #13 R1 fix: the previous implementation forwarded the upstream
        body verbatim with ``media_type=audio/mpeg``, which crashed audio
        decoders on the client side when the body was actually a JSON
        error envelope.
        """
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "kokoro crashed"})

        import gateway.routers.audio as audio_mod

        original_client = audio_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original_client(*args, **kwargs)

        audio_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/audio/speech",
                    json={
                        "model": "kokoro",
                        "voice": "alloy",
                        "input": "hello world",
                    },
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
        finally:
            audio_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert resp.status_code == 502
        body = resp.json()
        # The new typed error class drives the code; dashboards keyed on
        # ``tts_upstream_error`` get the truth instead of a Dify alarm.
        assert body["error"]["code"] == "tts_upstream_error"


class TestChatThinProxyRouter:
    """PR #13 R1: chat router forwarding, field stripping, error mapping."""

    @pytest.mark.asyncio
    async def test_blocking_forward_normalises_max_completion_tokens(self) -> None:
        """``max_completion_tokens`` (OpenAI 2025 alias) is collapsed to
        ``max_tokens`` before forwarding so vLLM honours the cap. The
        gateway-only ``conversation_id`` and ``safety_identifier`` aliases
        are stripped — vLLM/LiteLLM would 422 in strict mode."""
        from httpx import ASGITransport, AsyncClient

        captured_body: dict[str, object] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(req.content))
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                    },
                },
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original_client = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original_client(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_completion_tokens": 64,
                        "safety_identifier": "user-9",
                        "conversation_id": "conv-x",
                    },
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert resp.status_code == 200
        # Normalisation: max_completion_tokens collapsed to max_tokens.
        assert captured_body.get("max_tokens") == 64
        assert "max_completion_tokens" not in captured_body
        # Gateway-only fields stripped before forwarding.
        assert "conversation_id" not in captured_body
        assert "safety_identifier" not in captured_body
        # safety_identifier collapsed into 'user' (OpenAI alias).
        assert captured_body.get("user") == "user-9"

    @pytest.mark.asyncio
    async def test_blocking_4xx_upstream_passes_through_status(self) -> None:
        """vLLM 422 (invalid request) → 422 to client via UpstreamClientError.

        PR #13 R1 fix: rejecting client-shaped errors as 502 misled SDKs
        into retrying their own bad input."""
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422, json={"error": "invalid model parameter"}
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original_client = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original_client(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "upstream_invalid_request"

    @pytest.mark.asyncio
    async def test_blocking_malformed_json_returns_llm_upstream_error(self) -> None:
        """vLLM 200 with non-JSON body → LLMUpstreamError envelope.

        PR #13 R1 fix: ``resp.json()`` would propagate JSONDecodeError
        past the GatewayError handler so clients saw Starlette's default
        ``{"detail":"Internal Server Error"}`` instead of OpenAI shape.
        """
        from httpx import ASGITransport, AsyncClient

        def _handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"not json at all", headers={"content-type": "text/plain"}
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original_client = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original_client(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
        finally:
            chat_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["code"] == "llm_upstream_error"

    @pytest.mark.asyncio
    async def test_stream_injects_include_usage(self) -> None:
        """The forwarded stream request always carries
        ``stream_options.include_usage=True`` so vLLM emits the terminal
        usage chunk and TPM accounting stops zeroing out streaming
        requests.
        """
        from httpx import ASGITransport, AsyncClient

        captured_body: dict[str, object] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(req.content))
            # A minimal SSE stream — one content chunk + terminal usage chunk + [DONE].
            sse = (
                'data: {"id":"x","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
                'data: {"id":"x","choices":[],"usage":{"prompt_tokens":3,'
                '"completion_tokens":2,"total_tokens":5}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(
                200,
                content=sse.encode(),
                headers={"content-type": "text/event-stream"},
            )

        import gateway.routers.chat_thin_proxy as chat_mod

        original_client = chat_mod.httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original_client(*args, **kwargs)

        chat_mod.httpx.AsyncClient = _patched  # type: ignore[misc]
        try:
            app, _ = _build_thin_proxy_app()
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "m1",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer bsa_test_key"},
                )
                # Drain the stream so the request fully completes.
                async for _ in resp.aiter_bytes():
                    pass
        finally:
            chat_mod.httpx.AsyncClient = original_client  # type: ignore[misc]

        assert captured_body.get("stream_options") == {"include_usage": True}


class TestCreateAppStartupValidation:
    """PR #13 R1 #8: thin-proxy without an LLM endpoint must fail fast."""

    def test_thin_proxy_mode_without_llm_endpoint_raises(self) -> None:
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            thin_proxy_mode=True,
            llm_endpoint="",
        )
        with pytest.raises(RuntimeError, match="GATEWAY_LLM_ENDPOINT"):
            create_app(settings=s, registry=registry)

    def test_dify_mode_without_llm_endpoint_is_fine(self) -> None:
        # The validation only kicks in under thin_proxy_mode — Dify mode
        # never consults llm_endpoint so an empty value is a non-event.
        from gateway.config import Settings
        from gateway.main import create_app
        from gateway.registry import CustomerRegistry
        from tests.conftest import make_customer

        registry = CustomerRegistry.from_entries([make_customer()])
        s = Settings(
            registry_path="unused.yaml",
            log_json=False,
            rate_limit_enabled=False,
            thin_proxy_mode=False,
            llm_endpoint="",
        )
        app = create_app(settings=s, registry=registry)
        assert app is not None
