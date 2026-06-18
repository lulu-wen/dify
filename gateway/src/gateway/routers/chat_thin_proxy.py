"""``/v1/chat/completions`` thin-proxy router (PR #13).

When ``settings.thin_proxy_mode`` is True, the gateway bypasses Dify
entirely and forwards chat completions directly to the EMS-provided
LLM endpoint (vLLM via LiteLLM at ``http://100.88.9.9/llm`` in the
PoC). The LLM endpoint is OpenAI-compatible so the body is forwarded
as-is; streaming responses are piped through SSE without translation.

What this DOES still do:

* Auth + RPM middleware (PR #1 + #7)
* Cost-based admission gate (PR #8) + TPM (PR #8). Same accounting as
  the Dify path — the node-budget OOM guard still applies whether the
  inference happens via Dify or direct.
* Settle on success / error / disconnect (PR #11 cleanup + PR #10
  cancel-on-disconnect concept — though we don't forward cancel
  because vLLM/LiteLLM doesn't expose a stop endpoint).

What this does NOT do (vs Dify path):

* No App lazy-build, no AppManager — there is no Dify here.
* No DSL injection (system prompt → ``inputs.system_prompt``); the
  client's ``messages`` array is forwarded verbatim. If a customer
  needs prompt injection / RAG, they go through the Dify path.
* No conversation_id forwarding — vLLM is stateless OpenAI.

When this router is mounted, the Dify-based chat router is NOT — they
share the same path so FastAPI would 500 on duplicate route.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.config import Settings
from gateway.errors import (
    InvalidRequestError,
    LLMTimeoutError,
    LLMUpstreamError,
    ServiceUnavailableError,
    UpstreamClientError,
)
from gateway.registry import CustomerEntry
from gateway.routers.ratelimit_guard import (
    admit,
    enforce_tpm,
    estimate_request_cost,
    settle,
)
from gateway.schemas import ChatCompletionRequest, ChatMessage

logger = structlog.get_logger(__name__)

router = APIRouter()


def _messages_chars(messages: list[ChatMessage]) -> int:
    """Sum of all message content lengths (input to chars/4 cost heuristic)."""
    return sum(len(m.content) for m in messages if m.content)


@router.post("/v1/chat/completions")
async def chat_completions_thin_proxy(
    request: Request, body: ChatCompletionRequest
) -> Any:
    """Thin-proxy chat completions — forward verbatim to the EMS LLM endpoint."""
    settings: Settings = request.app.state.settings
    customer: CustomerEntry = request.state.customer
    request_id: str = request.state.request_id

    endpoint = settings.llm_endpoint.rstrip("/")
    if not endpoint:
        raise ServiceUnavailableError(
            "LLM endpoint not configured (set GATEWAY_LLM_ENDPOINT)."
        )

    # Cheap sync validation: client must request a model the customer
    # is registered for. ``llm_model`` (extra_body trick) takes precedence
    # over the top-level ``model`` field, matching the Dify path's
    # semantics so client code stays portable across modes.
    selected_model = body.llm_model or body.model
    if not selected_model:
        raise InvalidRequestError(
            "missing 'model' field", param="model"
        )

    # ``CustomerEntry.find_model`` is the registry-level whitelist; if the
    # customer has no matching entry, reject as 404 to match the Dify
    # path's behaviour.
    if customer.find_model(selected_model) is None:
        raise InvalidRequestError(
            f"model '{selected_model}' is not enabled for this customer",
            param="model",
        )

    # Cost estimation + admission. Same accounting as Dify path so the
    # node-budget OOM guard works regardless of which inference path
    # served the request.
    #
    # R1 fix: use ``body.effective_max_tokens`` (the schema property that
    # picks the newer ``max_completion_tokens`` over the deprecated
    # ``max_tokens``). The previous version read ``body.max_tokens``
    # directly so a modern client sending only ``max_completion_tokens``
    # got the ``default_max_output_tokens`` fallback — wildly
    # under-budgeting then forwarding a field name vLLM ignores → KV
    # cache OOM the admission gate exists to prevent.
    cost = estimate_request_cost(
        request,
        input_chars=_messages_chars(body.messages),
        max_output_tokens=body.effective_max_tokens,
        model_id=selected_model,
        has_knowledge_bases=False,  # thin-proxy has no RAG
    )
    grant = admit(request, customer, cost)
    try:
        enforce_tpm(request, customer, cost)
    except BaseException:
        settle(request, grant, actual_output_tokens=0)
        raise

    # Body forwarded to the LLM endpoint. ``model_dump(exclude_none=True)``
    # drops fields the client didn't send so we don't poison vLLM's
    # parser with nulls. ``model`` is rewritten to the resolved id so
    # ``extra_body.llm_model`` (gateway-only escape hatch) doesn't leak.
    #
    # R1 fix: also strip ``conversation_id`` (Dify-path-only state) and
    # the legacy/new alias pair (``safety_identifier``,
    # ``max_completion_tokens``). vLLM/LiteLLM in strict-schema mode
    # would 422 on the unknown fields, lenient mode silently ignores
    # but still logs them upstream — either way the gateway-internal
    # vocabulary must not leak. After normalisation the forwarded body
    # is a clean OpenAI chat-completions shape vLLM accepts verbatim.
    forward_body = body.model_dump(exclude_none=True, by_alias=True)
    forward_body["model"] = selected_model
    for gateway_only in (
        "llm_model",          # gateway-only escape hatch
        "conversation_id",    # Dify-path stateful turn id
        "safety_identifier",  # OpenAI deprecation alias for ``user``
    ):
        forward_body.pop(gateway_only, None)
    # Normalise OpenAI 2025 token-limit alias: ``max_completion_tokens`` is
    # the new field, ``max_tokens`` is the legacy one. vLLM honours
    # ``max_tokens``, so collapse to that and drop the alias.
    if "max_completion_tokens" in forward_body:
        forward_body.setdefault("max_tokens", forward_body["max_completion_tokens"])
        forward_body.pop("max_completion_tokens", None)
    # Likewise for end-user identity: collapse safety_identifier (popped
    # above) into ``user`` if it was set and ``user`` wasn't.
    if "user" not in forward_body and body.effective_user is not None:
        forward_body["user"] = body.effective_user

    if body.stream:
        return await _stream(
            request,
            endpoint=endpoint,
            forward_body=forward_body,
            grant=grant,
            request_id=request_id,
            settings=settings,
        )
    return await _blocking(
        request,
        endpoint=endpoint,
        forward_body=forward_body,
        grant=grant,
        request_id=request_id,
        settings=settings,
    )


async def _blocking(
    request: Request,
    *,
    endpoint: str,
    forward_body: dict[str, Any],
    grant: Any,
    request_id: str,
    settings: Settings,
) -> JSONResponse:
    """Blocking forward — POST, parse, return JSON.

    R1 fixes:

    * 4xx upstream → ``UpstreamClientError`` (preserves the status code)
      not ``DifyUpstreamError`` (would mask client-input mistakes as
      gateway outage 502s). 5xx → ``LLMUpstreamError`` (502, new
      typed class so dashboards/alerts keyed on ``code`` don't lie
      about Dify).
    * ``resp.json()`` raising ``JSONDecodeError`` on a malformed 2xx
      body is now caught and converted to ``LLMUpstreamError``; before,
      it propagated past the GatewayError handler and clients saw
      Starlette's default ``{"detail":"Internal Server Error"}`` envelope
      instead of the OpenAI shape.
    * Single settle path: success path settles inline; any error settles
      via the outer ``except`` then re-raises. The previous double-try
      structure could double-settle if ``JSONResponse(...)`` itself
      raised.
    """
    timeout = httpx.Timeout(
        settings.ems_request_timeout_s, read=settings.ems_request_timeout_s
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{endpoint}/v1/chat/completions",
                json=forward_body,
            )
    except httpx.TimeoutException as e:
        settle(request, grant, actual_output_tokens=0)
        raise LLMTimeoutError("LLM upstream timed out") from e
    except httpx.RequestError as e:
        settle(request, grant, actual_output_tokens=0)
        raise LLMUpstreamError(f"LLM upstream failed: {e}") from e

    if not resp.is_success:
        preview = resp.text[:500]
        logger.warning(
            "thin_proxy.chat.upstream_non_2xx",
            request_id=request_id,
            status=resp.status_code,
            body=preview,
        )
        settle(request, grant, actual_output_tokens=0)
        if 400 <= resp.status_code < 500:
            # Client input issue — preserve the upstream status.
            raise UpstreamClientError(
                f"LLM upstream rejected request (HTTP {resp.status_code}): {preview}",
                upstream_status=resp.status_code,
            )
        raise LLMUpstreamError(
            f"LLM upstream returned HTTP {resp.status_code}: {preview}"
        )

    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        # Malformed 2xx body (rare but happens behind some proxies).
        # Surface as an upstream error rather than letting Starlette
        # emit its non-OpenAI {"detail": ...} envelope.
        preview = resp.text[:500]
        logger.warning(
            "thin_proxy.chat.upstream_malformed_json",
            request_id=request_id,
            body=preview,
        )
        settle(request, grant, actual_output_tokens=0)
        raise LLMUpstreamError(
            "LLM upstream returned malformed JSON body"
        ) from e

    # Pull completion tokens out of the upstream's ``usage`` so settle
    # can refund the over-reservation. vLLM/LiteLLM both emit this in
    # OpenAI's standard shape.
    usage = payload.get("usage") or {}
    actual_completion = int(usage.get("completion_tokens") or 0)
    settle(request, grant, actual_output_tokens=actual_completion)
    return JSONResponse(content=payload)


async def _stream(
    request: Request,
    *,
    endpoint: str,
    forward_body: dict[str, Any],
    grant: Any,
    request_id: str,
    settings: Settings,
) -> StreamingResponse:
    """Streaming forward — open SSE stream, pipe chunks 1:1.

    R1 fixes:

    * Inject ``stream_options.include_usage=True`` into the forwarded
      body so vLLM/LiteLLM emit a terminal usage chunk regardless of
      whether the client set the option. Without this, the SDK default
      omits the option, vLLM omits the usage block, and settle refunds
      the full reservation — TPM accounting silently zeros for every
      streaming request.
    * Status check uses ``resp.is_success`` (any 2xx) instead of the
      stricter ``status_code != 200`` to match the blocking path.
    * Error envelopes emitted into the stream now carry the right
      ``code`` (``llm_upstream_error`` / ``llm_timeout``) rather than
      Dify-shaped codes.
    * Disconnect-and-retry caveat: the docstring notes vLLM has no
      cancel handle in this version, so we still settle as soon as the
      generator exits. Future PR can call ``/v1/completions/{id}/abort``
      when vLLM exposes it.
    """
    # R1: force-emit usage so settle has real token counts. We merge
    # rather than overwrite so a client that explicitly set
    # ``include_usage=False`` still gets it (gateway accounting > client
    # convenience).
    stream_opts = dict(forward_body.get("stream_options") or {})
    stream_opts["include_usage"] = True
    forward_body["stream_options"] = stream_opts

    timeout = httpx.Timeout(
        settings.ems_request_timeout_s, read=settings.ems_request_timeout_s
    )

    async def event_source() -> AsyncIterator[bytes]:
        completion_tokens = 0  # best-effort count from the final usage chunk
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{endpoint}/v1/chat/completions",
                    json=forward_body,
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    if not resp.is_success:
                        # Surface as a clean SSE error event then [DONE].
                        body_bytes = await resp.aread()
                        envelope = {
                            "error": {
                                "message": body_bytes.decode(errors="replace")[:500],
                                "type": "upstream_error",
                                "code": "llm_upstream_error",
                                "upstream_status": resp.status_code,
                            }
                        }
                        yield f"data: {json.dumps(envelope)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return

                    # Forward chunks 1:1. Inspect (cheaply) for the usage
                    # block in the terminal chunk so settle can refund.
                    # R1: parse only when the chunk is the terminal one
                    # (empty choices + usage block), avoiding false-
                    # positive json.loads on content text that contains
                    # the literal word "usage".
                    async for raw in resp.aiter_lines():
                        if not raw:
                            yield b"\n"
                            continue
                        line = raw if raw.endswith("\n") else raw + "\n"
                        yield line.encode()
                        if not raw.startswith("data: "):
                            continue
                        tail = raw[len("data: "):].strip()
                        if not tail or tail == "[DONE]":
                            continue
                        # Cheap pre-filter: only parse when 'usage' is
                        # actually present. Avoids decoding every chunk.
                        if "usage" not in tail:
                            continue
                        try:
                            obj = json.loads(tail)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if not isinstance(obj, dict):
                            continue
                        # Terminal usage chunk: choices is empty AND
                        # usage is present. include_usage spec from
                        # OpenAI. Content chunks with the word "usage"
                        # in them have non-empty choices.
                        if obj.get("choices"):
                            continue
                        u = obj.get("usage") or {}
                        ct = int(u.get("completion_tokens") or 0)
                        if ct:
                            completion_tokens = ct
                    yield b"\n"
        except httpx.TimeoutException as e:
            logger.warning(
                "thin_proxy.chat.stream_timeout",
                request_id=request_id,
                error=str(e),
            )
            envelope = {
                "error": {
                    "message": "LLM upstream timed out",
                    "type": "upstream_error",
                    "code": "llm_timeout",
                }
            }
            yield f"data: {json.dumps(envelope)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except httpx.RequestError as e:
            logger.warning(
                "thin_proxy.chat.stream_transport_error",
                request_id=request_id,
                error=str(e),
            )
            envelope = {
                "error": {
                    "message": "LLM upstream transport error",
                    "type": "upstream_error",
                    "code": "llm_upstream_error",
                }
            }
            yield f"data: {json.dumps(envelope)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            settle(request, grant, actual_output_tokens=completion_tokens)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
