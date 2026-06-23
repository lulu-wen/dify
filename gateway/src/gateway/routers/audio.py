"""``/v1/audio/*`` routers (PR #13 — thin-proxy mode).

Two endpoints surface EMS-managed audio equipment to OpenAI SDK clients:

* ``POST /v1/audio/transcriptions`` — WhisperX (ASR).
  Schema-translated via :mod:`gateway.translators.whisperx`.
* ``POST /v1/audio/speech`` — Kokoro (TTS).
  Pure passthrough; Kokoro already speaks the OpenAI shape.

Both routes are only mounted when ``settings.thin_proxy_mode`` is True
(otherwise the gateway has no upstream to forward to — Dify does not
provide ASR/TTS). When the corresponding endpoint env var is empty
``ServiceUnavailableError`` is raised so the client sees a 503 with a
clear ``"asr endpoint not configured"`` message.
"""

from __future__ import annotations

import json
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from gateway.errors import (
    ASRTimeoutError,
    ASRUpstreamError,
    NotEntitledError,
    ServiceUnavailableError,
    TTSTimeoutError,
    TTSUpstreamError,
    UpstreamClientError,
)
from gateway.routers.ratelimit_guard import (
    admit,
    enforce_tpm,
    estimate_request_cost,
    settle,
)
from gateway.translators.whisperx import (
    build_whisperx_request,
    translate_whisperx_response,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


def _upstream_headers(api_key: str, request_id: str) -> dict[str, str]:
    """Build standard upstream headers: bearer (when set) + request id.

    Duplicated from chat_thin_proxy.py rather than imported to keep
    router modules independent — a future shared upstream-utils module
    can absorb both call sites (R2 reuse finding).
    """
    headers: dict[str, str] = {"X-Request-ID": request_id}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# --------------------------------------------------------------------------- #
# /v1/audio/transcriptions — WhisperX (schema-translated)
# --------------------------------------------------------------------------- #
@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    file: Annotated[UploadFile, File()],
    model: Annotated[str, Form()] = "whisper-1",
    language: Annotated[str | None, Form()] = None,
    prompt: Annotated[str | None, Form()] = None,
    response_format: Annotated[str, Form()] = "json",
    temperature: Annotated[float, Form()] = 0.0,
    extra_body: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """OpenAI Whisper → WhisperX translator + forwarder.

    OpenAI exposed fields are honoured where WhisperX supports them; the
    rest are logged and dropped (prompt, temperature). The WhisperX-only
    speaker hints (``num_speakers``, ``min_speakers``, ``max_speakers``)
    are accepted via the OpenAI SDK's ``extra_body`` mechanism — clients
    pass ``extra_body={"num_speakers": 2}`` and it arrives here as a
    JSON-encoded form field.
    """
    settings = request.app.state.settings
    request_id = request.state.request_id
    customer = request.state.customer
    # R2 fix #3: per-customer entitlement gate. Chat-only tenants don't
    # silently get GPU-hungry transcription access just because the
    # gateway is in thin-proxy mode.
    if not customer.audio_enabled:
        raise NotEntitledError(
            "customer is not entitled to /v1/audio/transcriptions"
        )
    endpoint = settings.asr_endpoint.rstrip("/")
    if not endpoint:
        raise ServiceUnavailableError(
            "ASR endpoint not configured (set GATEWAY_ASR_ENDPOINT)."
        )

    whisperx_req, shape = build_whisperx_request(
        language=language,
        response_format=response_format,
        prompt=prompt,
        temperature=temperature,
        extra_body=extra_body,
    )

    # The ``model`` field is required by OpenAI's schema (clients always
    # send it) but unused on the wire; surface in the log for visibility
    # without forwarding it to WhisperX (which would reject the extra
    # form field).
    content = await file.read()
    files = {
        "file": (
            file.filename or "audio.wav",
            content,
            file.content_type or "audio/wav",
        )
    }
    logger.info(
        "audio.transcriptions.forward",
        request_id=request_id,
        endpoint=endpoint,
        model=model,  # client-requested label, e.g. "whisper-1"
        language=whisperx_req.language,
        bytes=len(content),
        response_format=shape,
    )

    # R1: gate audio through the same node-budget admission as chat so a
    # customer flooding /v1/audio/transcriptions can't bypass the OOM
    # guard. Cost is a coarse proxy — WhisperX runs on the same shared
    # GPU as vLLM, and audio length correlates with KV-cache + compute
    # pressure. ``input_chars`` = audio bytes / 50 is a heuristic tuned
    # so a 1-minute speech clip (~100 KB compressed audio) costs roughly
    # the same as a medium-sized chat prompt (~512 tokens). The
    # ``max_output_tokens`` arg seeds the reservation upper bound.
    cost = estimate_request_cost(
        request,
        input_chars=len(content) // 50,
        max_output_tokens=None,  # ASR has no client-supplied cap; falls back to default.
        model_id=model,
        has_knowledge_bases=False,
    )
    grant = admit(request, customer, cost)
    # R2 fix: gate audio behind the same TPM meter chat uses so a customer
    # at TPM cap on chat can't dodge it by switching to /v1/audio/*. The
    # RateLimitMiddleware only enforces RPM (cost=1.0); enforce_tpm is the
    # only per-route TPM site, and audio.py previously omitted it.
    try:
        enforce_tpm(request, customer, cost)
    except Exception:
        settle(request, grant, actual_output_tokens=0)
        raise

    try:
        timeout = httpx.Timeout(settings.ems_request_timeout_s, read=settings.ems_request_timeout_s)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{endpoint}/transcribe",
                    files=files,
                    data=whisperx_req.to_form(),
                    # R2 fix: propagate request_id + upstream bearer (R2 #6).
                    headers=_upstream_headers(settings.asr_api_key, request_id),
                )
        except httpx.TimeoutException as e:
            raise ASRTimeoutError("ASR upstream timed out") from e
        except httpx.RequestError as e:
            raise ASRUpstreamError(f"ASR upstream failed: {e}") from e

        if not resp.is_success:
            # R1 fix: preserve upstream 4xx (corrupt audio, unsupported
            # format, etc.) so clients see actionable status codes rather
            # than a misleading 502 that triggers SDK retry-storms.
            body_preview = resp.text[:500]
            logger.warning(
                "audio.transcriptions.upstream_non_2xx",
                request_id=request_id,
                status=resp.status_code,
                body=body_preview,
            )
            if 400 <= resp.status_code < 500:
                raise UpstreamClientError(
                    f"ASR upstream rejected request (HTTP {resp.status_code}): {body_preview}",
                    upstream_status=resp.status_code,
                )
            raise ASRUpstreamError(
                f"ASR upstream returned HTTP {resp.status_code}: {body_preview}"
            )

        translated = translate_whisperx_response(
            resp.json(),
            response_shape=shape,
            requested_language=whisperx_req.language,
        )
        return JSONResponse(content=translated)
    finally:
        # Settle the reservation regardless of outcome. No "actual output
        # tokens" concept for ASR — the entire reservation refunds.
        settle(request, grant, actual_output_tokens=0)


# --------------------------------------------------------------------------- #
# /v1/audio/speech — Kokoro (passthrough)
# --------------------------------------------------------------------------- #
@router.post("/v1/audio/speech")
async def audio_speech(request: Request) -> Response:
    """OpenAI TTS shape passthrough to Kokoro.

    Kokoro's ``POST /v1/audio/speech`` already implements OpenAI's
    schema (``{model, voice, input, response_format, speed}``) and
    returns the audio bytes with a matching Content-Type. No
    translation needed — forward the body verbatim and surface the
    upstream's bytes + Content-Type to the client.

    The auth + rate-limit middleware has already run; this route
    inherits both. RPM applies (per request); TPM doesn't — the cost
    helper has no concept of TTS audio cost in PR #13. A future PR
    could meter ``input`` length to TPM if needed.
    """
    settings = request.app.state.settings
    request_id = request.state.request_id
    customer = request.state.customer
    # R2 fix #3: per-customer entitlement gate (mirrors transcriptions).
    if not customer.audio_enabled:
        raise NotEntitledError(
            "customer is not entitled to /v1/audio/speech"
        )
    endpoint = settings.tts_endpoint.rstrip("/")
    if not endpoint:
        raise ServiceUnavailableError(
            "TTS endpoint not configured (set GATEWAY_TTS_ENDPOINT)."
        )

    body_bytes = await request.body()
    upstream_ct = request.headers.get("content-type", "application/json")

    # R1 fix: TTS cost is proportional to input character count (Kokoro
    # synthesises tokens ≈ chars / 4). Without admission, customers can
    # flood TTS while chat sees an empty node budget and gets admitted
    # despite the shared GPU pressure. Best-effort parse of ``input``
    # from the JSON body — if parse fails (binary body, malformed JSON)
    # fall back to a fixed cost so we still admit.
    #
    # R2 fix: lowercase the Content-Type before matching. Per RFC 9110
    # media types are case-insensitive, so a client posting
    # ``Content-Type: Application/JSON`` previously skipped this block
    # and the cost meter — defeating the R1 admission-for-audio fix.
    input_chars = 0
    if upstream_ct.lower().startswith("application/json"):
        try:
            payload = json.loads(body_bytes)
            if isinstance(payload, dict):
                raw_input = payload.get("input")
                # Only meter string inputs — OpenAI TTS contract requires
                # ``input: str``. A non-string would str-coerce to its
                # repr and pollute the metric with Python syntax.
                if isinstance(raw_input, str):
                    input_chars = len(raw_input)
        except (json.JSONDecodeError, TypeError):
            pass
    cost = estimate_request_cost(
        request,
        input_chars=input_chars,
        max_output_tokens=None,  # TTS has no max_tokens concept; fall back to default.
        model_id="kokoro",
        has_knowledge_bases=False,
    )
    grant = admit(request, customer, cost)
    # R2 fix: gate TTS behind the same TPM meter chat uses (see ASR comment).
    try:
        enforce_tpm(request, customer, cost)
    except Exception:
        settle(request, grant, actual_output_tokens=0)
        raise

    logger.info(
        "audio.speech.forward",
        request_id=request_id,
        endpoint=endpoint,
        request_bytes=len(body_bytes),
        input_chars=input_chars,
    )

    try:
        timeout = httpx.Timeout(settings.ems_request_timeout_s, read=settings.ems_request_timeout_s)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{endpoint}/v1/audio/speech",
                    content=body_bytes,
                    # R2 fix: request_id + upstream bearer (R2 #6) on top
                    # of the inbound Content-Type passthrough.
                    headers={
                        "Content-Type": upstream_ct,
                        **_upstream_headers(settings.tts_api_key, request_id),
                    },
                )
        except httpx.TimeoutException as e:
            raise TTSTimeoutError("TTS upstream timed out") from e
        except httpx.RequestError as e:
            raise TTSUpstreamError(f"TTS upstream failed: {e}") from e

        # R1 fix: non-2xx responses get re-wrapped through the GatewayError
        # envelope so clients see a consistent OpenAI {error:{...}} shape
        # instead of a Kokoro-flavoured JSON body labelled with our
        # audio/mpeg media_type (which would crash audio decoders).
        if not resp.is_success:
            body_preview = resp.text[:500]
            logger.warning(
                "audio.speech.upstream_non_2xx",
                request_id=request_id,
                status=resp.status_code,
                body=body_preview,
            )
            if 400 <= resp.status_code < 500:
                raise UpstreamClientError(
                    f"TTS upstream rejected request (HTTP {resp.status_code}): {body_preview}",
                    upstream_status=resp.status_code,
                )
            raise TTSUpstreamError(
                f"TTS upstream returned HTTP {resp.status_code}: {body_preview}"
            )

        # 2xx — body is audio bytes (mp3/wav/aac per ``response_format``).
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "audio/mpeg"),
        )
    finally:
        settle(request, grant, actual_output_tokens=0)
