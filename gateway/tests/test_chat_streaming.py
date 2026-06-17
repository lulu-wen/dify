"""Integration tests for ``POST /v1/chat/completions`` (streaming mode)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import FakeDifyClient


def _parse_data_lines(body_text: str) -> list[dict | str]:
    out: list[dict | str] = []
    for line in body_text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(payload))
    return out


@pytest.mark.asyncio
async def test_streaming_yields_openai_chunks(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.streaming_lines = [
        'data: {"event":"message","answer":"He"}',
        "",
        'data: {"event":"message","answer":"llo"}',
        "",
        'data: {"event":"message_end","conversation_id":"c-9","metadata":{'
        '"retriever_resources":[{"content":"a chunk","score":0.7,"document_name":"d1"}]}}',
        "",
    ]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        async with cli.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            text = await r.aread()
            body = text.decode("utf-8")

    payloads = _parse_data_lines(body)

    # First non-DONE: role=assistant, content="He"
    first = payloads[0]
    assert first["object"] == "chat.completion.chunk"  # type: ignore[index]
    assert first["choices"][0]["delta"]["role"] == "assistant"  # type: ignore[index]
    assert first["choices"][0]["delta"]["content"] == "He"  # type: ignore[index]

    # Second: content="llo", no role
    assert payloads[1]["choices"][0]["delta"]["content"] == "llo"  # type: ignore[index]

    # Final non-DONE: finish_reason=stop, references in metadata
    final = payloads[-2]
    assert final["choices"][0]["finish_reason"] == "stop"  # type: ignore[index]
    assert final["choices"][0]["delta"]["metadata"]["conversation_id"] == "c-9"  # type: ignore[index]
    assert len(final["choices"][0]["delta"]["metadata"]["references"]) == 1  # type: ignore[index]

    # Terminator
    assert payloads[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_streaming_unknown_model_returns_404_json(app: FastAPI) -> None:
    """Unknown model errors before streaming begins → standard JSON error."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={
                "model": "nope",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_streaming_passes_conversation_id_to_dify(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        async with cli.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
                "conversation_id": "conv-passed-in",
            },
        ) as r:
            await r.aread()

    sent = fake_dify.calls["streaming"][0]
    assert sent["conversation_id"] == "conv-passed-in"


@pytest.mark.asyncio
async def test_streaming_dify_5xx_returns_502_json_not_broken_sse(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Regression for review-2 P2: a Dify 5xx during stream open must yield a
    structured 502 JSON envelope, not a 200 SSE that closes mid-way."""
    from gateway.errors import DifyUpstreamError

    fake_dify.streaming_pre_flight_error = DifyUpstreamError("Dify returned HTTP 503")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
    # A clean JSON error envelope, not a half-streamed 200.
    assert r.status_code == 502
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["error"]["code"] == "dify_upstream_error"


@pytest.mark.asyncio
async def test_streaming_dify_timeout_returns_504_json(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Companion to the 5xx regression: timeout during stream open → 504 JSON."""
    from gateway.errors import DifyTimeoutError

    fake_dify.streaming_pre_flight_error = DifyTimeoutError("upstream timed out")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "dify_timeout"


# --------------------------------------------------------------------------- #
# PR #10: cancel an outlived generation on disconnect / upstream error
# --------------------------------------------------------------------------- #


class TestPR10Cancellation:
    """When generation outlives its receiver — client disconnect OR mid-stream
    upstream error — the chat router fires a best-effort
    ``chat_messages_stop`` to free Dify/vLLM KV cache instead of letting the
    request grind to natural completion. Both cases reach the same streaming
    finally with ``cancel_sink["dify_finalized"]==False`` and a captured
    ``task_id`` — we exercise both via upstream error simulation because
    httpx ASGITransport buffers the response and does not propagate
    ``http.disconnect`` (the SSE generator runs to completion server-side
    even after the client breaks out of ``aiter_lines``).
    """

    @pytest.mark.asyncio
    async def test_natural_completion_does_not_cancel(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """Stream that runs through ``message_end`` already terminated on
        Dify's side. The converter sets ``cancel_sink["dify_finalized"]=True``,
        the router's finally skips the cancel, and we avoid burning a 404
        + log line on every healthy request.
        """
        fake_dify.streaming_lines = [
            'data: {"event":"message","task_id":"task-natural","answer":"hi"}',
            "",
            'data: {"event":"message_end","task_id":"task-natural","metadata":{},"conversation_id":"c"}',
            "",
        ]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_lines():
                    pass
        await asyncio.sleep(0)
        assert fake_dify.calls["stop"] == []
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_dify_error_event_does_not_cancel(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """A Dify ``error`` event also signals task termination on Dify's
        side — same skip-cancel logic as ``message_end``."""
        fake_dify.streaming_lines = [
            'data: {"event":"message","task_id":"task-err","answer":"hi"}',
            "",
            'data: {"event":"error","task_id":"task-err","code":"x","message":"boom"}',
            "",
        ]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_lines():
                    pass
        await asyncio.sleep(0)
        assert fake_dify.calls["stop"] == []

    @pytest.mark.asyncio
    async def test_upstream_drop_mid_stream_fires_cancel(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """Upstream connection drop after the first SSE event reaches the
        finally with task_id captured but ``dify_finalized=False`` — the
        cancel must fire, carrying the captured task_id and the gateway's
        user identifier. Same code path a real client disconnect hits
        in production (Starlette raises ``GeneratorExit`` on the body
        iterator on ``http.disconnect``).
        """
        from gateway.dify.client import DifyUpstreamError

        fake_dify.streaming_lines = [
            'data: {"event":"message","task_id":"task-abc","answer":"chunk"}',
        ]
        fake_dify.streaming_raise_after_n_lines = 1
        fake_dify.streaming_raise_exception = DifyUpstreamError("upstream dropped")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                # Status header was already 200 (preflight succeeded); drain.
                async for _ in resp.aiter_lines():
                    pass

        await asyncio.wait_for(fake_dify.chat_messages_stop_event.wait(), timeout=2.0)
        assert len(fake_dify.calls["stop"]) == 1
        stop_call = fake_dify.calls["stop"][0]
        assert stop_call["task_id"] == "task-abc"
        assert stop_call["user"], "Dify stop endpoint validates user — must be forwarded"
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_upstream_drop_before_task_id_skips_cancel(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """If the stream dies before any event carried a task_id (e.g. the
        very first byte was a connection reset), we have no cancel target —
        the ``if tid:`` guard prevents a stop call with an empty task_id
        (which would 404 or hit a different route)."""
        from gateway.dify.client import DifyUpstreamError

        # ``ping`` events without task_id then immediate drop.
        fake_dify.streaming_lines = ['data: {"event":"ping"}']
        fake_dify.streaming_raise_after_n_lines = 1
        fake_dify.streaming_raise_exception = DifyUpstreamError("upstream dropped")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                async for _ in resp.aiter_lines():
                    pass

        await asyncio.sleep(0.05)
        assert fake_dify.calls["stop"] == []
        assert app.state.quota_store.in_flight == 0

    @pytest.mark.asyncio
    async def test_cancel_does_not_block_settle(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """A slow stop endpoint must not delay releasing the node-budget
        reservation — fire-and-forget means settle runs in the same finally
        without awaiting the cancel POST. Asserted by giving stop a 500ms
        delay and checking the reservation is already released right after
        the request returns.
        """
        from gateway.dify.client import DifyUpstreamError

        fake_dify.streaming_lines = [
            'data: {"event":"message","task_id":"task-slow","answer":"chunk"}',
        ]
        fake_dify.streaming_raise_after_n_lines = 1
        fake_dify.streaming_raise_exception = DifyUpstreamError("upstream dropped")
        fake_dify.chat_messages_stop_delay_s = 0.5

        loop = asyncio.get_event_loop()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            async with cli.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "m1", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                async for _ in resp.aiter_lines():
                    pass
            t_finally = loop.time()
            # The cancel is in-flight (slow), but settle ran in the same
            # finally without awaiting it — quota gauge is already clean.
            assert app.state.quota_store.in_flight == 0
            assert (loop.time() - t_finally) < 0.1, (
                "settle blocked on the cancel POST — fire-and-forget broken"
            )

        await asyncio.wait_for(fake_dify.chat_messages_stop_event.wait(), timeout=2.0)


# --------------------------------------------------------------------------- #
# PR #10: converter populates cancel_sink correctly
# --------------------------------------------------------------------------- #


class TestPR10ConverterCancelSink:
    """The cancel decision (router finally) reads two fields the converter
    writes. These unit tests pin those writes so the router can trust them
    even when streaming SSE shape evolves.
    """

    @pytest.mark.asyncio
    async def test_first_event_captures_task_id(self) -> None:
        from gateway.streaming.converter import CancelSink, dify_to_openai_chunks

        async def lines() -> list:  # type: ignore[type-arg]
            for raw in [
                'data: {"event":"message","task_id":"t-1","answer":"hi"}',
                'data: {"event":"message","task_id":"t-1","answer":" there"}',
                'data: {"event":"message_end","task_id":"t-1","metadata":{},"conversation_id":"c"}',
            ]:
                yield raw

        sink = CancelSink()
        async for _ in dify_to_openai_chunks(
            lines(), request_id="r-1", model_id="m1", cancel_sink=sink
        ):
            pass
        assert sink.task_id == "t-1"
        assert sink.dify_finalized is True

    @pytest.mark.asyncio
    async def test_error_event_marks_finalized(self) -> None:
        from gateway.streaming.converter import CancelSink, dify_to_openai_chunks

        async def lines() -> list:  # type: ignore[type-arg]
            for raw in [
                'data: {"event":"message","task_id":"t-e","answer":"x"}',
                'data: {"event":"error","task_id":"t-e","code":"oops","message":"y"}',
            ]:
                yield raw

        sink = CancelSink()
        async for _ in dify_to_openai_chunks(
            lines(), request_id="r", model_id="m", cancel_sink=sink
        ):
            pass
        assert sink.dify_finalized is True

    @pytest.mark.asyncio
    async def test_pings_only_no_task_id(self) -> None:
        from gateway.streaming.converter import CancelSink, dify_to_openai_chunks

        async def lines() -> list:  # type: ignore[type-arg]
            for raw in ['data: {"event":"ping"}']:
                yield raw

        sink = CancelSink()
        async for _ in dify_to_openai_chunks(
            lines(), request_id="r", model_id="m", cancel_sink=sink
        ):
            pass
        assert sink.task_id is None
        assert sink.dify_finalized is False
