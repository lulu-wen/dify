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
    UnknownModelError,
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


def _upstream_headers(api_key: str, request_id: str) -> dict[str, str]:
    """Build the standard upstream-call headers: bearer (when set) + request id.

    PR #13 R2 #6: operators can set ``GATEWAY_LLM_API_KEY`` /
    ``GATEWAY_ASR_API_KEY`` / ``GATEWAY_TTS_API_KEY`` when the EMS
    endpoint is hardened with LITELLM_MASTER_KEY-style bearer auth. An
    empty string means "no auth" (Tailscale-isolated default) and
    suppresses the Authorization header entirely so we don't send the
    literal ``Bearer `` to upstream.
    """
    headers: dict[str, str] = {"X-Request-ID": request_id}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


@router.post("/v1/chat/completions")
async def chat_completions_thin_proxy(
    request: Request, body: ChatCompletionRequest
) -> Any:
    """Thin-proxy chat completions — forward verbatim to the EMS LLM endpoint."""
    settings: Settings = request.app.state.settings
    customer: CustomerEntry = request.state.customer
    request_id: str = request.state.request_id

    # PR #14 R1 #5: in pure ``mode=thin_proxy`` deployments this router
    # is mounted directly (no hybrid dispatcher in front), so a client
    # request with ``use_rag=true`` lands here. Without an explicit
    # rejection it would silently thin-proxy → no RAG → confused
    # customer wondering why their toggle does nothing. The hybrid
    # dispatcher already filters use_rag before delegating, so this
    # path only fires under pure thin_proxy mounting.
    if body.use_rag:
        raise InvalidRequestError(
            "use_rag=true is not supported in thin-proxy mode "
            "(deploy mode='hybrid' or mode='dify' for RAG)",
            param="use_rag",
        )

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
    #
    # PR #15 R1 #7: previously raised ``InvalidRequestError`` (400) —
    # inconsistent with chat.py which raises ``UnknownModelError`` (404)
    # for the same condition. The 400 vs 404 mismatch broke SDK retry
    # heuristics that branched on status code in hybrid mode (same
    # request, different code depending on use_rag). Both routers now
    # raise the same typed error.
    if customer.find_model(selected_model) is None:
        raise UnknownModelError(
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
    # R2 fix #14: scale the per-completion cap by ``n`` so a client
    # sending ``n=5`` reserves 5x the output budget. The previous code
    # only saw ``max_output_tokens`` → admission gate's OOM guarantee
    # was silently n-times off whenever the client asked for multiple
    # choices.
    per_completion = body.effective_max_tokens
    n = body.n or 1
    scaled_max_output = per_completion * n if per_completion is not None else None
    cost = estimate_request_cost(
        request,
        input_chars=_messages_chars(body.messages),
        max_output_tokens=scaled_max_output,
        model_id=selected_model,
        has_knowledge_bases=False,  # thin-proxy has no RAG
    )
    grant = admit(request, customer, cost)
    try:
        # R2 fix: narrow to ``Exception`` so CancelledError / KeyboardInterrupt
        # propagate without us re-settling and delaying shutdown. ``settle``
        # is documented idempotent so the original double-call pattern was
        # already safe; the R2 narrowing is about exception semantics, not
        # the leak risk.
        enforce_tpm(request, customer, cost)
    except Exception:
        settle(request, grant, actual_output_tokens=0)
        raise

    # Body forwarded to the LLM endpoint. ``model_dump(exclude_none=True)``
    # drops fields the client didn't send so we don't poison vLLM's
    # parser with nulls. ``model`` is rewritten to the resolved id so
    # ``extra_body.llm_model`` (gateway-only escape hatch) doesn't leak.
    #
    # R2 fix: when a client sends BOTH ``max_tokens`` and
    # ``max_completion_tokens`` (or both ``user`` and ``safety_identifier``)
    # the previous R1 collapse logic let the legacy field win silently —
    # ``setdefault`` is a no-op when the legacy key already exists, and the
    # ``"user" not in forward_body`` guard fired False whenever the client
    # set ``user`` too. The fix uses the schema-level ``effective_*``
    # properties as the source of truth and unconditionally overwrites
    # ``max_tokens`` / ``user`` with them after stripping both aliases.
    forward_body = body.model_dump(exclude_none=True, by_alias=True)
    forward_body["model"] = selected_model
    for gateway_only in (
        "llm_model",              # gateway-only escape hatch
        "conversation_id",        # Dify-path stateful turn id
        "safety_identifier",      # OpenAI deprecation alias for ``user``
        "max_completion_tokens",  # OpenAI 2025 alias for ``max_tokens``
        # PR #14 R1 #1: hybrid-mode routing fields must NEVER reach vLLM.
        # Strict-validation vLLM rejects unknown body fields with 400;
        # lenient LiteLLM forwards them, polluting prompt-cache keys and
        # leaking gateway-internal vocabulary into upstream access logs.
        "use_rag",
        "dataset_ids",
    ):
        forward_body.pop(gateway_only, None)
    # Normalise OpenAI 2025 aliases by writing the schema-resolved value
    # back so vLLM/LiteLLM (which honour the legacy names) see exactly
    # what the schema's precedence rules say the request meant — not a
    # half-collapsed view that depends on which alias the client also sent.
    if body.effective_max_tokens is not None:
        forward_body["max_tokens"] = body.effective_max_tokens
    if body.effective_user is not None:
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
                # R2 fix: propagate request_id for log correlation and
                # forward the configured upstream bearer (R2 #6) when set.
                headers=_upstream_headers(settings.llm_api_key, request_id),
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
    #
    # R2 fix: guard ``int(...)`` against non-numeric usage values. A
    # misbehaving upstream / proxy could return
    # ``{"usage":{"completion_tokens":"NaN"}}`` and the bare ``int(...)``
    # would raise ValueError AFTER ``resp.json()`` succeeded but BEFORE
    # ``settle`` ran, leaking the grant until process restart.
    usage = payload.get("usage") or {}
    raw = usage.get("completion_tokens") or 0
    try:
        actual_completion = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "thin_proxy.chat.upstream_non_numeric_completion_tokens",
            request_id=request_id,
            raw=str(raw)[:64],
        )
        actual_completion = 0
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
    #
    # R2 fix: guard against ``stream_options`` arriving as a non-dict
    # (the schema is ``extra="allow"`` so a string like
    # ``"stream_options": "include_usage"`` would survive validation and
    # crash ``dict(str)`` with ValueError AFTER admit/enforce_tpm
    # committed the grant).
    existing_opts = forward_body.get("stream_options")
    if not isinstance(existing_opts, dict):
        existing_opts = {}
    forward_body["stream_options"] = {**existing_opts, "include_usage": True}

    timeout = httpx.Timeout(
        settings.ems_request_timeout_s, read=settings.ems_request_timeout_s
    )

    # R2 fix: pre-flight the upstream POST BEFORE returning StreamingResponse
    # so a vLLM 5xx / connect-refused surfaces as a synchronous GatewayError
    # (clean HTTP 502/504 envelope), matching the Dify-path chat router.
    # The prior structure returned 200 text/event-stream with the error
    # baked into the SSE body — SDK retry helpers that branch on
    # resp.status_code never retried; choices-only parsers silently saw an
    # empty completion. With pre-flight, the StreamingResponse only runs
    # when we already know the upstream is healthy; mid-stream errors
    # (after headers are flushed) still surface as inline SSE error
    # envelopes since there's no way to retroactively change the status.
    #
    # ``client`` and ``stream_cm`` are not closed here on the success path —
    # ownership transfers to ``event_source`` whose finally closes both.
    client = httpx.AsyncClient(timeout=timeout)
    stream_cm = client.stream(
        "POST",
        f"{endpoint}/v1/chat/completions",
        json=forward_body,
        headers={
            "Accept": "text/event-stream",
            **_upstream_headers(settings.llm_api_key, request_id),
        },
    )
    try:
        try:
            resp = await stream_cm.__aenter__()
        except httpx.TimeoutException as e:
            await client.aclose()
            settle(request, grant, actual_output_tokens=0)
            raise LLMTimeoutError("LLM upstream timed out") from e
        except httpx.RequestError as e:
            await client.aclose()
            settle(request, grant, actual_output_tokens=0)
            raise LLMUpstreamError(f"LLM upstream failed: {e}") from e

        if not resp.is_success:
            # R2 fix: bound the error-body read so a misbehaving upstream
            # returning a multi-MB HTML 502 page can't OOM the gateway.
            # We only need ~500 chars for the envelope preview anyway.
            body_buf = bytearray()
            try:
                async for chunk in resp.aiter_bytes():
                    body_buf.extend(chunk)
                    if len(body_buf) >= 2048:
                        break
            except httpx.HTTPError:
                pass
            preview = bytes(body_buf).decode(errors="replace")[:500]
            status = resp.status_code
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()
            settle(request, grant, actual_output_tokens=0)
            logger.warning(
                "thin_proxy.chat.stream_upstream_non_2xx",
                request_id=request_id,
                status=status,
                body=preview,
            )
            if 400 <= status < 500:
                raise UpstreamClientError(
                    f"LLM upstream rejected request (HTTP {status}): {preview}",
                    upstream_status=status,
                )
            raise LLMUpstreamError(
                f"LLM upstream returned HTTP {status}: {preview}"
            )
    except BaseException:
        # Pre-flight cleanup belt-and-braces — the typed except branches
        # above already close client + settle, but if a different exception
        # type (e.g. CancelledError during shutdown) lands here we still
        # need to release the connection. settle() is idempotent.
        raise

    async def event_source() -> AsyncIterator[bytes]:
        completion_tokens = 0  # best-effort count from the final usage chunk
        try:
            # Forward chunks 1:1. Inspect (cheaply) for the usage block
            # in the terminal chunk so settle can refund.
            try:
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
                    if "usage" not in tail:
                        continue
                    try:
                        obj = json.loads(tail)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    # Terminal usage chunk: choices is empty AND usage is present.
                    if obj.get("choices"):
                        continue
                    u = obj.get("usage") or {}
                    # R2 fix: guard int() so a non-numeric completion_tokens
                    # doesn't crash the generator mid-stream (which would
                    # leave the client without a [DONE] sentinel).
                    try:
                        ct = int(u.get("completion_tokens") or 0)
                    except (TypeError, ValueError):
                        logger.warning(
                            "thin_proxy.chat.stream_non_numeric_completion_tokens",
                            request_id=request_id,
                        )
                        ct = 0
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
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()
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
