"""``/v1/chat/completions`` router (blocking + streaming).

Translates OpenAI Chat Completions requests into Dify ``chat-messages`` calls
via the resolved customer's lazy-built App. References returned by Dify's
retriever are surfaced under ``choices[0].message.metadata.references`` (R6).

Conversation handling:
    OpenAI is stateless (full ``messages`` array each request) while Dify
    tracks conversations server-side keyed by ``conversation_id``. We take the
    *last user message* as the Dify ``query`` and forward
    ``extra_body.conversation_id`` (if any) so clients can opt into Dify-side
    history.

    System messages and prior turns are bundled into a single
    ``inputs.system_prompt`` payload that the Dify App expands via the
    ``{{system_prompt}}`` template variable declared in ``dsl.py``. Dify's
    chat App then wraps that text as the LLM's system role message. Without
    this assembly OpenAI ``system`` messages would never reach the LLM —
    Dify silently drops ``inputs`` keys not referenced by ``pre_prompt``.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.dify.app_manager import AppManager
from gateway.dify.client import DifyClient
from gateway.errors import InvalidRequestError
from gateway.registry import CustomerEntry
from gateway.schemas import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatResponseMessage,
    Reference,
    Usage,
    make_metadata,
)
from gateway.streaming.converter import dify_to_openai_chunks

logger = structlog.get_logger(__name__)

router = APIRouter()


# ---------- helpers ----------


def _last_user_message(messages: list[ChatMessage]) -> str:
    """Return the content of the most recent ``user`` message, or raise."""
    for msg in reversed(messages):
        if msg.role == "user" and msg.content:
            return msg.content
    raise InvalidRequestError("messages must contain at least one user message", param="messages")


def _build_system_prompt(messages: list[ChatMessage]) -> str:
    """Assemble OpenAI ``system`` messages + prior turns into one block.

    The returned string is sent as ``inputs.system_prompt`` and reaches the
    LLM as its system-role prompt (see module docstring + ``dsl.py``). Empty
    return is fine — Dify substitutes it as an empty system message, which
    is the same behaviour as the legacy empty ``pre_prompt``.
    """
    if not messages:
        return ""

    system_text = "\n".join(
        m.content for m in messages if m.role == "system" and m.content
    )

    # Prior conversation = everything before the final user turn, excluding
    # the system messages already captured above (so we don't duplicate them).
    if messages[-1].role == "user":
        prior_turns = messages[:-1]
    else:
        prior_turns = list(messages)
    history_lines = [
        f"{m.role}: {m.content}"
        for m in prior_turns
        if m.content and m.role != "system"
    ]

    sections: list[str] = []
    if system_text:
        sections.append(system_text)
    if history_lines:
        sections.append("Previous conversation:\n" + "\n".join(history_lines))
    return "\n\n".join(sections)


def _user_id(req: ChatCompletionRequest, customer: CustomerEntry, request_id: str) -> str:
    """Stable end-user identifier sent to Dify.

    Honors OpenAI's deprecation precedence via ``effective_user``
    (``safety_identifier`` > ``user``). Falls back to a deterministic
    per-customer identifier when both are omitted, since Dify requires this field.
    """
    resolved = req.effective_user
    if resolved:
        return resolved
    # Use ``customer_id:request_id`` as fallback; not ideal for cross-call
    # personalisation but unambiguous for tracing.
    return f"{customer.customer_id}:{request_id}"


def _extract_references(metadata: dict[str, Any] | None) -> list[Reference]:
    if not metadata:
        return []
    out: list[Reference] = []
    for r in metadata.get("retriever_resources", []) or []:
        out.append(
            Reference(
                content=r.get("content", ""),
                score=r.get("score"),
                document_name=r.get("document_name"),
                document_id=r.get("document_id"),
                segment_id=r.get("segment_id"),
            )
        )
    return out


def _extract_usage(metadata: dict[str, Any] | None) -> Usage:
    if not metadata:
        return Usage()
    u = metadata.get("usage") or {}
    return Usage(
        prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
        completion_tokens=int(u.get("completion_tokens", 0) or 0),
        total_tokens=int(u.get("total_tokens", 0) or 0),
    )


# ---------- endpoint ----------


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest) -> Any:
    """OpenAI-compatible chat completions.

    Honors ``stream`` flag; non-streaming returns JSON, streaming returns SSE.
    """
    customer: CustomerEntry = request.state.customer
    request_id: str = request.state.request_id
    app_manager: AppManager = request.app.state.app_manager
    dify_factory = request.app.state.dify_client_factory

    # Resolve which model to use for App selection. Per spec R3, clients may
    # override the OpenAI ``model`` via ``extra_body={"llm_model": "..."}``,
    # which the OpenAI SDK flattens to the top-level ``llm_model`` field.
    # Fall back to ``body.model`` when not provided.
    selected_model = body.llm_model or body.model

    # Validate model + obtain App key (lazy-build).
    app_key = await app_manager.get_app_key(customer, selected_model)
    dify_client: DifyClient = dify_factory(customer)

    query = _last_user_message(body.messages)
    inputs: dict[str, Any] = {"system_prompt": _build_system_prompt(body.messages)}

    user = _user_id(body, customer, request_id)

    # ---- streaming branch ----
    #
    # Pre-flight pattern: open the upstream stream **before** returning a
    # ``StreamingResponse``. If Dify replies non-2xx or times out at this
    # stage, the GatewayError propagates to the global exception handler and
    # becomes a clean 502/504 JSON envelope. Without this, errors raised
    # inside ``StreamingResponse`` after headers are flushed would yield a
    # broken SSE stream and hide the real status from clients.
    if body.stream:
        stream_cm = dify_client.open_chat_stream(
            app_key=app_key,
            query=query,
            user=user,
            inputs=inputs,
            conversation_id=body.conversation_id,
        )
        # Enter the context here; raises DifyUpstreamError / DifyTimeoutError
        # synchronously which is exactly what we want before sending headers.
        dify_lines = await stream_cm.__aenter__()

        async def event_source():  # type: ignore[no-untyped-def]
            try:
                async for chunk in dify_to_openai_chunks(
                    dify_lines, request_id=request_id, model_id=selected_model
                ):
                    yield chunk
            finally:
                # Best-effort close; errors inside cleanup are swallowed because
                # the response has already started streaming.
                try:
                    await stream_cm.__aexit__(None, None, None)
                except Exception:
                    logger.exception("chat.stream_close_failed")

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable proxy buffering for true streaming
            },
        )

    # ---- blocking branch ----
    dify_resp = await dify_client.chat_messages_blocking(
        app_key=app_key,
        query=query,
        user=user,
        inputs=inputs,
        conversation_id=body.conversation_id,
    )

    answer: str = dify_resp.get("answer") or ""
    metadata = dify_resp.get("metadata") or {}
    references = _extract_references(metadata)
    usage = _extract_usage(metadata)
    conversation_id = dify_resp.get("conversation_id")

    response = ChatCompletionResponse(
        id=f"chatcmpl-{request_id}",
        model=selected_model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatResponseMessage(
                    role="assistant",
                    content=answer,
                    metadata=make_metadata(
                        references=[r.model_dump(exclude_none=True) for r in references],
                        conversation_id=conversation_id,
                        request_id=request_id,
                    ),
                ),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )

    logger.info(
        "chat.blocking.completed",
        model=selected_model,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        references=len(references),
    )

    # ``model_dump`` to honour ``extra='allow'`` fields (metadata).
    return JSONResponse(content=json.loads(response.model_dump_json(exclude_none=True)))
