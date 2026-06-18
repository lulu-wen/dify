"""Convert Dify SSE event stream into OpenAI-compatible ``chat.completion.chunk`` SSE.

Dify's ``POST /v1/chat-messages`` (response_mode=streaming) emits events:

* ``message`` — a chunk of the assistant's answer (``answer`` field is delta).
* ``agent_thought`` — reasoning model (Qwen3/DeepSeek-R1/o1-family) thinking
  trace; the ``thought`` field is the incremental content. Routed to OpenAI's
  ``delta.reasoning_content`` so standard clients can render a "thinking" UI.
* ``message_end`` — terminal event with ``metadata`` (usage, retriever_resources).
* ``error`` — Dify reports an error mid-stream.
* ``ping`` — keep-alive.

OpenAI SSE chunks look like:

.. code-block:: text

    data: {"id":"...","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hi"},"index":0,"finish_reason":null}],...}\n\n
    ...
    data: {"id":"...","choices":[{"delta":{},"index":0,"finish_reason":"stop"}],...}\n\n
    data: [DONE]\n\n

The first chunk carries ``role: "assistant"`` in delta; subsequent chunks carry
``content`` deltas; the final chunk carries ``finish_reason``. We attach
``references`` and ``conversation_id`` to the **final chunk's**
``choices[0].delta.metadata`` so streaming clients receive the same metadata
they would in blocking mode (R6).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog

from gateway.schemas import ChatChunkChoice, ChatCompletionChunk, DeltaMessage, Reference

logger = structlog.get_logger(__name__)


@dataclass
class CancelSink:
    """Side-channel state from the SSE converter to the chat router's
    streaming ``finally`` (PR #10, typed by PR #11, refined PR #12a R1).

    Fields populated as the converter consumes Dify events:

    - ``task_id``: first non-empty Dify ``task_id`` seen — the cancel
      target. ``None`` if no event carried one (no upstream activity).
    - ``dify_finalized``: True after ``message_end`` OR ``error`` event;
      means Dify already tore the task down so a stop call would be a
      redundant 404. The router skips the cancel POST when True.
    - ``finalized_with_error``: True only when Dify emitted an ``error``
      event (NOT on normal ``message_end``). Lets the chat router's
      ``GATEWAY_STREAM_DISCONNECT_TOTAL`` instrumentation distinguish
      genuine upstream errors from natural completion — both used to
      land under ``reason="normal"`` because ``dify_finalized`` flipped
      True in both cases (PR #12a R1 finding #6).

    All attributes are mutated in-place by the converter — this is a
    side-channel by design so the converter stays a plain async iterator
    (existing callers that don't care about cancellation pass nothing).
    Replaces the loose ``dict[str, Any]`` shape PR #10 originally used
    (PR #11 R2 #7: typo-safe via mypy).
    """

    task_id: str | None = None
    dify_finalized: bool = False
    finalized_with_error: bool = False


def parse_dify_sse_line(line: str) -> dict[str, Any] | None:
    """Parse a single SSE line; return the JSON event dict or None for non-data."""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("dify.sse.invalid_json", line=payload[:200])
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _format_chunk(chunk: ChatCompletionChunk) -> str:
    return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"


async def dify_to_openai_chunks(
    dify_lines: AsyncIterable[str],
    *,
    request_id: str,
    model_id: str,
    cancel_sink: CancelSink | None = None,
) -> AsyncIterator[str]:
    """Yield OpenAI SSE strings derived from Dify SSE lines.

    Args:
        dify_lines: async iterator of raw lines from
            :meth:`DifyClient.chat_messages_streaming`.
        request_id: gateway-issued request id (used as ``id`` of every chunk).
        model_id: client-facing model id (echoed in every chunk).
        cancel_sink: when given, populated with two side-channel fields
            consumed by the chat router's streaming finally (PR #10, typed
            by PR #11) to decide whether to fire ``chat_messages_stop``:
              * ``cancel_sink.task_id`` — the first non-empty Dify
                ``task_id`` we saw, used as the cancel target.
              * ``cancel_sink.dify_finalized`` — True if Dify has emitted
                ``message_end`` OR ``error`` (Dify-side termination, so
                cancelling would only produce a 404). Without this signal,
                the router cannot tell apart "natural completion" from
                "client disconnect" — the ``async for chunk in ...`` loop
                exits cleanly in either case because GeneratorExit on the
                outer generator aclose's the inner generator instead of
                propagating.
            Side-channel rather than a tuple return so this stays a plain
            async iterator and existing callers (tests, non-cancelling
            paths) don't need to change shape.

    Yields:
        Strings already framed as ``data: {...}\\n\\n`` ready to push down the
        wire. The terminator ``data: [DONE]\\n\\n`` is emitted once at the end.
    """
    started = False
    last_event_metadata: dict[str, Any] | None = None
    finish_reason: str = "stop"
    conversation_id: str | None = None
    # PR #10 review-2 #5: track first task_id capture with a local flag so
    # the per-event `cancel_sink.get("task_id") is None` lookup short-
    # circuits after capture instead of running on every SSE event for the
    # stream's lifetime (10k+ events for long responses).
    task_id_captured = cancel_sink is None

    # Codex review-1 P2: Dify's ``agent_thought`` payload carries the
    # **cumulative** ``thought`` text — re-emitted in full when the agent
    # appends new reasoning. OpenAI clients concatenate ``delta.reasoning_content``,
    # so forwarding the whole thought each time would produce ``"foofoobar"``
    # for two events with thoughts ``"foo"`` then ``"foobar"``. Track the last
    # seen thought per id (Dify gives a stable ``id`` per MessageAgentThought)
    # and emit only the new suffix.
    last_thought_by_id: dict[str, str] = {}

    async for raw in dify_lines:
        event = parse_dify_sse_line(raw)
        if event is None:
            continue

        # Capture Dify ``task_id`` (carried on every event) the first time
        # we see one — used by the chat router to cancel generation on
        # client disconnect (PR #10). Write-once; ``ping`` events also
        # carry task_id so we typically get this before any answer chunk.
        # ``task_id_captured`` flag short-circuits this branch after the
        # first capture (review-2 #5).
        if not task_id_captured:
            tid = event.get("task_id")
            if isinstance(tid, str) and tid:
                assert cancel_sink is not None  # narrow for mypy
                cancel_sink.task_id = tid
                task_id_captured = True

        event_type = event.get("event")

        if event_type == "message":
            answer = event.get("answer", "")
            if not answer:
                continue
            delta = DeltaMessage(content=answer)
            if not started:
                # First chunk announces the role per OpenAI convention.
                delta.role = "assistant"
                started = True
            yield _format_chunk(
                ChatCompletionChunk(
                    id=request_id,
                    model=model_id,
                    choices=[ChatChunkChoice(index=0, delta=delta, finish_reason=None)],
                )
            )

        elif event_type == "agent_thought":
            # Reasoning model thinking trace → OpenAI-style ``reasoning_content``.
            # Dify emits ``agent_thought`` with several extra fields (observation,
            # tool, tool_input, ...). For pure reasoning models only ``thought``
            # is populated; tool-using agents will use the other fields and we
            # do not surface them in PR #3 (chat-only reasoning is the target).
            thought = event.get("thought") or ""
            if not thought:
                continue
            # Dify re-sends the cumulative thought on every update; emit only
            # the new suffix relative to the last value we saw for this id.
            # Missing id (older Dify, malformed event) → treat as standalone
            # and skip dedup so we don't drop content.
            thought_id = event.get("id")
            if isinstance(thought_id, str) and thought_id:
                prev = last_thought_by_id.get(thought_id, "")
                if thought == prev:
                    # No new content; ignore the redundant event.
                    continue
                if thought.startswith(prev):
                    delta_text = thought[len(prev):]
                else:
                    # Upstream rewrote the thought (rare; e.g. tool result
                    # replaced earlier draft). Fall back to emitting the full
                    # new value — duplication beats silent loss.
                    delta_text = thought
                last_thought_by_id[thought_id] = thought
            else:
                delta_text = thought

            if not delta_text:
                continue

            delta = DeltaMessage(reasoning_content=delta_text)
            if not started:
                # First chunk announces the role even when it's a reasoning
                # chunk — clients building UIs need ``role`` to bind the message.
                delta.role = "assistant"
                started = True
            yield _format_chunk(
                ChatCompletionChunk(
                    id=request_id,
                    model=model_id,
                    choices=[ChatChunkChoice(index=0, delta=delta, finish_reason=None)],
                )
            )

        elif event_type == "message_end":
            last_event_metadata = event.get("metadata") or {}
            conversation_id = event.get("conversation_id")
            # PR #10: Dify-side termination signal — clearing this flag
            # tells the chat router NOT to fire a redundant stop call.
            if cancel_sink is not None:
                cancel_sink.dify_finalized = True
            # Loop continues; we emit the final chunk after exhausting the stream.

        elif event_type == "error":
            # Surface the error as a final chunk with finish_reason='content_filter'
            # to keep the stream parseable; clients that need the raw error can
            # inspect logs / non-streaming retry.
            logger.warning(
                "dify.sse.error_event",
                code=event.get("code"),
                message=event.get("message"),
            )
            # PR #10: Dify already reported the error and tore down the
            # task — no point in firing a stop call.
            # PR #12a R1: also flag ``finalized_with_error`` so the chat
            # router's stream_disconnect metric distinguishes upstream
            # errors from natural completion (both used to land under
            # ``reason="normal"``).
            if cancel_sink is not None:
                cancel_sink.dify_finalized = True
                cancel_sink.finalized_with_error = True
            finish_reason = "content_filter"
            break

        # ``ping`` and unknown events are ignored.

    # Emit terminal chunk with metadata (references + conversation_id).
    references_payload: list[dict[str, Any]] = []
    if last_event_metadata:
        for r in last_event_metadata.get("retriever_resources", []) or []:
            references_payload.append(
                Reference(
                    content=r.get("content", ""),
                    score=r.get("score"),
                    document_name=r.get("document_name"),
                    document_id=r.get("document_id"),
                    segment_id=r.get("segment_id"),
                ).model_dump(exclude_none=True)
            )

    final_delta_metadata: dict[str, Any] = {}
    if references_payload:
        final_delta_metadata["references"] = references_payload
    if conversation_id:
        final_delta_metadata["conversation_id"] = conversation_id

    final_delta = DeltaMessage()
    final_chunk_data: dict[str, Any] = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "delta": final_delta.model_dump(exclude_none=True),
                "finish_reason": finish_reason,
            }
        ],
    }
    if final_delta_metadata:
        # Attach metadata to the delta (extra='allow' on schemas permits this).
        final_chunk_data["choices"][0]["delta"]["metadata"] = final_delta_metadata

    yield f"data: {json.dumps(final_chunk_data, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
