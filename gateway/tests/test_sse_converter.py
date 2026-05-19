"""Tests for Dify SSE → OpenAI chunk conversion."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from gateway.streaming.converter import dify_to_openai_chunks, parse_dify_sse_line


async def _alines(lines: list[str]) -> AsyncIterator[str]:
    for line in lines:
        yield line


def _data_payloads(chunks: list[str]) -> list[dict | str]:
    """Parse SSE chunks into JSON payloads (or '[DONE]')."""
    out: list[dict | str] = []
    for chunk in chunks:
        for line in chunk.splitlines():
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


class TestParseDifySseLine:
    def test_data_json_returns_dict(self) -> None:
        assert parse_dify_sse_line('data: {"event":"message"}') == {"event": "message"}

    def test_blank_returns_none(self) -> None:
        assert parse_dify_sse_line("") is None
        assert parse_dify_sse_line("\n") is None

    def test_done_returns_none(self) -> None:
        assert parse_dify_sse_line("data: [DONE]") is None

    def test_invalid_json_returns_none(self) -> None:
        assert parse_dify_sse_line("data: not json {") is None

    def test_non_data_line_returns_none(self) -> None:
        assert parse_dify_sse_line("event: ping") is None

    def test_array_json_returns_none(self) -> None:
        # Defensive: only dict events are forwarded.
        assert parse_dify_sse_line("data: [1,2]") is None


@pytest.mark.asyncio
async def test_message_chunks_translated_to_openai_chunks() -> None:
    dify_stream = [
        'data: {"event":"message","answer":"He"}',
        "",
        'data: {"event":"message","answer":"llo"}',
        "",
        'data: {"event":"message_end","metadata":{},"conversation_id":"c-1"}',
        "",
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)

    # First payload: role=assistant + content="He"
    first = payloads[0]
    assert first["object"] == "chat.completion.chunk"  # type: ignore[index]
    assert first["model"] == "m1"  # type: ignore[index]
    assert first["choices"][0]["delta"]["role"] == "assistant"  # type: ignore[index]
    assert first["choices"][0]["delta"]["content"] == "He"  # type: ignore[index]

    # Second payload: content="llo", no role
    second = payloads[1]
    assert "role" not in second["choices"][0]["delta"]  # type: ignore[index]
    assert second["choices"][0]["delta"]["content"] == "llo"  # type: ignore[index]

    # Final non-DONE: finish_reason=stop, conversation_id in metadata
    final = payloads[-2]
    assert final["choices"][0]["finish_reason"] == "stop"  # type: ignore[index]
    assert final["choices"][0]["delta"]["metadata"]["conversation_id"] == "c-1"  # type: ignore[index]

    # Last payload: [DONE]
    assert payloads[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_references_attached_to_final_chunk() -> None:
    dify_stream = [
        'data: {"event":"message","answer":"hi"}',
        'data: {"event":"message_end","conversation_id":"c-9","metadata":{"retriever_resources":['
        '{"content":"chunk one","score":0.81,"document_name":"d1","document_id":"d-1","segment_id":"s-1"}'
        "]}}",
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    final = payloads[-2]

    refs = final["choices"][0]["delta"]["metadata"]["references"]  # type: ignore[index]
    assert len(refs) == 1
    assert refs[0]["content"] == "chunk one"
    assert refs[0]["score"] == 0.81
    assert refs[0]["document_name"] == "d1"


@pytest.mark.asyncio
async def test_error_event_short_circuits_with_content_filter_finish() -> None:
    dify_stream = [
        'data: {"event":"message","answer":"part"}',
        'data: {"event":"error","code":"unknown","message":"boom"}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    final = payloads[-2]
    assert final["choices"][0]["finish_reason"] == "content_filter"  # type: ignore[index]
    assert payloads[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_ping_events_ignored() -> None:
    dify_stream = [
        'data: {"event":"ping"}',
        'data: {"event":"message","answer":"x"}',
        'data: {"event":"ping"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    # 1 message chunk + 1 final + DONE
    assert len(payloads) == 3
    assert payloads[0]["choices"][0]["delta"]["content"] == "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_empty_answer_chunks_skipped() -> None:
    dify_stream = [
        'data: {"event":"message","answer":""}',
        'data: {"event":"message","answer":"real"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    # Only the "real" chunk + final + DONE
    assert len(payloads) == 3
    assert payloads[0]["choices"][0]["delta"]["content"] == "real"  # type: ignore[index]


# ---------------------------------------------------------------------------
# R7: agent_thought → reasoning_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_thought_event_emits_reasoning_content_chunk() -> None:
    """A single ``agent_thought`` event with ``thought`` should produce one
    OpenAI chunk where ``delta.reasoning_content`` carries the thinking
    trace and ``delta.content`` is absent (matches OpenAI o1 streaming).
    """
    dify_stream = [
        'data: {"event":"agent_thought","id":"t1","position":1,"thought":"Let me think about this..."}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)

    # 1 reasoning chunk + 1 final + DONE
    assert len(payloads) == 3
    first = payloads[0]
    assert first["choices"][0]["delta"]["reasoning_content"] == "Let me think about this..."  # type: ignore[index]
    # First chunk announces role for the assistant message.
    assert first["choices"][0]["delta"]["role"] == "assistant"  # type: ignore[index]
    # OpenAI clients expect content absent (None / not present) during reasoning phase.
    assert "content" not in first["choices"][0]["delta"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_reasoning_then_message_phases_stream_in_order() -> None:
    """Reasoning models emit thinking first, then the actual answer. The
    converter must preserve order: reasoning_content chunks → content chunks
    → final chunk with finish_reason. This is the exact UX an OpenAI o1
    client renders as «AI is thinking...» followed by the streamed answer.

    Both agent_thought events here use different ``id`` so each carries
    fresh cumulative content (no suffix diffing kicks in for separate ids).
    """
    dify_stream = [
        'data: {"event":"agent_thought","id":"t1","thought":"User asks RSRP..."}',
        'data: {"event":"agent_thought","id":"t2","thought":" so I should explain..."}',
        'data: {"event":"message","answer":"基站告警 RSRP=-115"}',
        'data: {"event":"message","answer":" 通常是訊號弱"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)

    # 2 reasoning + 2 content + 1 final + DONE = 6
    assert len(payloads) == 6
    assert payloads[0]["choices"][0]["delta"]["reasoning_content"] == "User asks RSRP..."  # type: ignore[index]
    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"  # type: ignore[index]
    # Subsequent reasoning chunks do NOT repeat the role.
    assert "role" not in payloads[1]["choices"][0]["delta"]  # type: ignore[index]
    assert payloads[1]["choices"][0]["delta"]["reasoning_content"] == " so I should explain..."  # type: ignore[index]
    # Content phase begins; reasoning_content absent from content chunks.
    assert payloads[2]["choices"][0]["delta"]["content"] == "基站告警 RSRP=-115"  # type: ignore[index]
    assert "reasoning_content" not in payloads[2]["choices"][0]["delta"]  # type: ignore[index]
    assert payloads[3]["choices"][0]["delta"]["content"] == " 通常是訊號弱"  # type: ignore[index]
    # Final + DONE
    assert payloads[4]["choices"][0]["finish_reason"] == "stop"  # type: ignore[index]
    assert payloads[5] == "[DONE]"


@pytest.mark.asyncio
async def test_agent_thought_without_thought_field_skipped() -> None:
    """Tool-using agents emit ``agent_thought`` events where ``thought`` is
    null and other fields (``observation``, ``tool``) carry payloads. PR #3
    only surfaces pure reasoning content, so these events must not produce
    any chunk — would yield an empty ``reasoning_content`` chunk that
    confuses clients into rendering a blank thinking bubble.
    """
    dify_stream = [
        'data: {"event":"agent_thought","thought":null,"tool":"search","observation":"results"}',
        'data: {"event":"message","answer":"final"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    # No reasoning chunk; just 1 content + final + DONE
    assert len(payloads) == 3
    assert payloads[0]["choices"][0]["delta"]["content"] == "final"  # type: ignore[index]


@pytest.mark.asyncio
async def test_cumulative_thought_emits_only_suffix() -> None:
    """Codex review-1 P2: Dify's ``agent_thought`` event re-sends the
    **cumulative** ``thought`` for a given id each time it grows. OpenAI
    clients concatenate ``delta.reasoning_content``, so the converter must
    emit only the new suffix — otherwise two events with ``"foo"`` then
    ``"foobar"`` would render client-side as ``"foofoobar"``.

    This test pins the expected behaviour: each delta carries only the
    newly added text, keyed on the stable ``id`` Dify includes for every
    MessageAgentThought.
    """
    dify_stream = [
        'data: {"event":"agent_thought","id":"t1","position":1,"thought":"foo"}',
        'data: {"event":"agent_thought","id":"t1","position":1,"thought":"foobar"}',
        'data: {"event":"agent_thought","id":"t1","position":1,"thought":"foobar baz"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    # 3 reasoning chunks + final + DONE = 5
    assert len(payloads) == 5
    deltas = [p["choices"][0]["delta"]["reasoning_content"] for p in payloads[:3]]  # type: ignore[index]
    # If we concatenate the deltas as an OpenAI client would, we get back
    # the final cumulative thought (the property the bug violated).
    assert "".join(deltas) == "foobar baz"
    # Specifically: first chunk is "foo", second is " bar" suffix, third is " baz".
    assert deltas == ["foo", "bar", " baz"]


@pytest.mark.asyncio
async def test_redundant_thought_event_skipped() -> None:
    """If Dify emits the same cumulative thought twice (no new content),
    the converter must skip the redundant event — emitting an empty
    reasoning_content chunk would confuse clients."""
    dify_stream = [
        'data: {"event":"agent_thought","id":"t1","thought":"hello"}',
        'data: {"event":"agent_thought","id":"t1","thought":"hello"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    # 1 reasoning chunk only + final + DONE = 3
    assert len(payloads) == 3
    assert payloads[0]["choices"][0]["delta"]["reasoning_content"] == "hello"  # type: ignore[index]


@pytest.mark.asyncio
async def test_thought_rewrite_falls_back_to_full_emit() -> None:
    """Rare case: Dify rewrites a thought rather than appending (e.g. a
    tool result replaced an earlier draft). The converter cannot do a
    clean suffix diff, so emit the full new value — duplication beats
    silent content loss."""
    dify_stream = [
        'data: {"event":"agent_thought","id":"t1","thought":"draft text"}',
        'data: {"event":"agent_thought","id":"t1","thought":"completely different"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    deltas = [p["choices"][0]["delta"]["reasoning_content"] for p in payloads[:2]]  # type: ignore[index]
    assert deltas == ["draft text", "completely different"]


@pytest.mark.asyncio
async def test_thought_without_id_emits_full_each_time() -> None:
    """Defensive: malformed event missing ``id`` field → fall back to
    emitting the full thought each time (no dedup possible without an
    anchor). Loses the de-duplication benefit but preserves content."""
    dify_stream = [
        'data: {"event":"agent_thought","thought":"first"}',
        'data: {"event":"agent_thought","thought":"second"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    deltas = [p["choices"][0]["delta"]["reasoning_content"] for p in payloads[:2]]  # type: ignore[index]
    assert deltas == ["first", "second"]


@pytest.mark.asyncio
async def test_empty_thought_skipped_like_empty_answer() -> None:
    """Mirrors ``test_empty_answer_chunks_skipped``: an empty-string
    ``thought`` field should not produce a chunk."""
    dify_stream = [
        'data: {"event":"agent_thought","thought":""}',
        'data: {"event":"agent_thought","thought":"real thought"}',
        'data: {"event":"message_end","metadata":{}}',
    ]
    chunks = [c async for c in dify_to_openai_chunks(_alines(dify_stream), request_id="req-1", model_id="m1")]
    payloads = _data_payloads(chunks)
    # 1 reasoning + final + DONE
    assert len(payloads) == 3
    assert payloads[0]["choices"][0]["delta"]["reasoning_content"] == "real thought"  # type: ignore[index]
