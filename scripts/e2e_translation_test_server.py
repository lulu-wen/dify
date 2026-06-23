"""End-to-end translation test server for AI SDK Gateway (pre-PR #13).

Standalone FastAPI app that exposes OpenAI-compatible endpoints and proxies
to the three EMS-provided backends, doing schema translation where needed.

The goal is to verify the translation layer works against real upstream
services BEFORE integrating into the gateway (PR #13). This is not a
production component — no auth, no rate limit, no observability. Pure
sanity-check tool.

Endpoints exposed:
    POST /v1/chat/completions       → LLM (vLLM, OpenAI-compatible, passthrough)
    POST /v1/audio/transcriptions   → WhisperX (custom schema, translated)
    POST /v1/audio/speech           → Kokoro TTS (OpenAI-compatible, passthrough)
    GET  /v1/models                 → union of LLM models + tts-1, whisper-1
    GET  /health                    → liveness

Config via env vars (or defaults — EMS Traefik ingress):
    LLM_ENDPOINT  (default http://100.88.9.9/llm)    # → vLLM via LiteLLM
    ASR_ENDPOINT  (default http://100.88.9.9/asr)    # → WhisperX
    TTS_ENDPOINT  (default http://100.88.9.9/tts)    # → Kokoro
    PORT          (default 8888)

Note: the EMS cluster uses Traefik unified ingress on port 80 with
path-based routing (/llm, /asr, /tts). The earlier NodePort
URLs (e.g. http://100.88.9.9:32105) also work but are unstable when
EMS reschedules. Prefer the ingress paths.

Run:
    pip install fastapi uvicorn httpx python-multipart
    python e2e_translation_test_server.py
    # or
    uvicorn e2e_translation_test_server:app --host 0.0.0.0 --port 8888

Or via Docker:
    docker run -d --name e2e-test -p 8888:8888 \\
        -v $(pwd)/e2e_translation_test_server.py:/app/server.py \\
        -e LLM_ENDPOINT=http://100.88.9.9:32105 \\
        -e ASR_ENDPOINT=http://100.88.9.9:31597 \\
        -e TTS_ENDPOINT=http://100.88.9.9:30589 \\
        python:3.11-slim \\
        sh -c "pip install -q fastapi uvicorn httpx python-multipart && cd /app && uvicorn server:app --host 0.0.0.0 --port 8888"

Smoke tests (run from any client):

    # 1. Health
    curl http://localhost:8888/health

    # 2. List models
    curl http://localhost:8888/v1/models | jq

    # 3. Chat completion (passthrough to vLLM)
    curl http://localhost:8888/v1/chat/completions \\
        -H "Content-Type: application/json" \\
        -d '{"model": "google/gemma-2-9b-it",
             "messages": [{"role":"user","content":"用一句話介紹自己"}]}' | jq

    # 4. Streaming chat
    curl -N http://localhost:8888/v1/chat/completions \\
        -H "Content-Type: application/json" \\
        -d '{"model": "google/gemma-2-9b-it",
             "messages": [{"role":"user","content":"數 1 到 5"}],
             "stream": true}'

    # 5. Transcription (default JSON)
    curl http://localhost:8888/v1/audio/transcriptions \\
        -F "file=@/path/to/audio.wav" \\
        -F "model=whisper-1" \\
        -F "language=en" | jq

    # 6. Transcription with WhisperX speaker hints (via OpenAI extra_body trick)
    curl http://localhost:8888/v1/audio/transcriptions \\
        -F "file=@/path/to/audio.wav" \\
        -F "model=whisper-1" \\
        -F "language=zh" \\
        -F "response_format=verbose_json" \\
        -F "extra_body={\\"num_speakers\\":2}" | jq

    # 7. TTS (passthrough to Kokoro)
    curl http://localhost:8888/v1/audio/speech \\
        -H "Content-Type: application/json" \\
        -d '{"model":"tts-1","voice":"alloy","input":"hello"}' \\
        --output speech.mp3
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse, Response

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://100.88.9.9/llm").rstrip("/")
ASR_ENDPOINT = os.getenv("ASR_ENDPOINT", "http://100.88.9.9/asr").rstrip("/")
TTS_ENDPOINT = os.getenv("TTS_ENDPOINT", "http://100.88.9.9/tts").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("e2e_test_server")

app = FastAPI(title="E2E Translation Test Server", version="0.1.0")


# ----------------------------------------------------------------------
# Health / models
# ----------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + show what endpoints we're configured against."""
    return {
        "status": "ok",
        "endpoints": {"llm": LLM_ENDPOINT, "asr": ASR_ENDPOINT, "tts": TTS_ENDPOINT},
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """Union of upstream LLM models + canonical ASR + TTS model IDs."""
    data: list[dict[str, Any]] = []
    # Pull LLM model list from upstream
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LLM_ENDPOINT}/v1/models")
            resp.raise_for_status()
            data.extend(resp.json().get("data", []))
    except Exception as e:
        log.warning("llm models fetch failed: %s", e)

    # Add canonical IDs for the other two (we synthesise them since their
    # /v1/models endpoints either 404 or return service-specific lists).
    data.extend(
        [
            {
                "id": "whisper-1",
                "object": "model",
                "created": 1686935002,
                "owned_by": "whisperx",
            },
            {
                "id": "tts-1",
                "object": "model",
                "created": 1686935002,
                "owned_by": "kokoro",
            },
        ]
    )
    return {"object": "list", "data": data}


# ----------------------------------------------------------------------
# /v1/chat/completions — LLM (passthrough)
# ----------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Pure passthrough to the LLM endpoint (vLLM, OpenAI compatible).

    Honours ``stream`` flag: streaming requests are forwarded as SSE.
    """
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    is_stream = bool(body.get("stream"))
    log.info("chat.completions stream=%s model=%s", is_stream, body.get("model"))

    if is_stream:
        # Streaming SSE forward
        async def event_stream():
            timeout = httpx.Timeout(300.0, read=300.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{LLM_ENDPOINT}/v1/chat/completions",
                    json=body,
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    if resp.status_code != 200:
                        text = await resp.aread()
                        yield f"data: {{\"error\": {json.dumps(text.decode())}}}\n\n".encode()
                        return
                    async for chunk in resp.aiter_raw():
                        yield chunk

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Blocking
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{LLM_ENDPOINT}/v1/chat/completions", json=body)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ----------------------------------------------------------------------
# /v1/audio/transcriptions — WhisperX (schema translated)
# ----------------------------------------------------------------------
@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
    extra_body: str | None = Form(None),
) -> JSONResponse:
    """OpenAI ``/v1/audio/transcriptions`` → WhisperX ``/transcribe`` translation.

    OpenAI → WhisperX mapping:
        file (binary)            → file
        model                    → ignored (WhisperX uses fixed whisper-large-v3)
        language                 → language (None becomes "auto")
        prompt                   → ignored (logged warning)
        response_format          → drives our response shape
        temperature              → ignored
        extra_body.num_speakers  → num_speakers
        extra_body.min_speakers  → min_speakers
        extra_body.max_speakers  → max_speakers
    """
    if prompt:
        log.warning("transcriptions.prompt_ignored prompt=%r", prompt[:50])
    if temperature != 0.0:
        log.warning("transcriptions.temperature_ignored t=%s", temperature)

    if response_format not in ("json", "verbose_json"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"response_format='{response_format}' not supported. "
                "Use 'json' (default) or 'verbose_json'. "
                "text/srt/vtt unsupported by WhisperX backend."
            ),
        )

    # Parse extra_body for WhisperX speaker hints
    whisperx_form: dict[str, Any] = {"language": language or "auto"}
    if extra_body:
        try:
            extras = json.loads(extra_body)
            for k in ("num_speakers", "min_speakers", "max_speakers"):
                if k in extras and isinstance(extras[k], int):
                    whisperx_form[k] = str(extras[k])
        except json.JSONDecodeError:
            log.warning("transcriptions.extra_body_invalid_json")

    # Read file into memory
    content = await file.read()
    files = {"file": (file.filename or "audio.wav", content, file.content_type or "audio/wav")}

    log.info(
        "transcriptions forward model=%s language=%s response_format=%s file=%d bytes",
        model,
        whisperx_form["language"],
        response_format,
        len(content),
    )

    # Forward to WhisperX. Note: WhisperX has NO top-level ``text`` field —
    # everything lives in ``segments[].text``. We concat below.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, read=300.0)) as client:
            resp = await client.post(
                f"{ASR_ENDPOINT}/transcribe",
                files=files,
                data=whisperx_form,
            )
            resp.raise_for_status()
            whisperx_result = resp.json()
    except httpx.HTTPStatusError as e:
        log.exception("transcriptions.upstream_error")
        return JSONResponse(
            status_code=e.response.status_code,
            content={"error": {"message": e.response.text, "type": "upstream_error"}},
        )
    except Exception as e:
        log.exception("transcriptions.upstream_failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(e), "type": "upstream_error"}},
        )

    # ---- Translate WhisperX response → OpenAI shape ----
    # WhisperX output schema (typical):
    #   {
    #     "segments": [
    #       {"start": 0.0, "end": 1.5, "text": "...", "speaker": "SPEAKER_00",
    #        "words": [{"word": "...", "start": .., "end": .., "speaker": "..."}, ...]},
    #       ...
    #     ],
    #     "language": "zh"
    #   }
    segments = whisperx_result.get("segments", [])
    detected_language = whisperx_result.get("language", whisperx_form["language"])
    # Concatenate all segments' text — WhisperX doesn't return a top-level text field
    full_text = " ".join((s.get("text") or "").strip() for s in segments if s.get("text"))

    if response_format == "json":
        # Minimal OpenAI shape
        return JSONResponse(content={"text": full_text})

    # response_format == "verbose_json"
    # OpenAI shape: {task, language, duration, text, segments[], words[]}
    duration = 0.0
    if segments:
        duration = float(segments[-1].get("end", 0.0))

    openai_segments = []
    openai_words = []
    for i, seg in enumerate(segments):
        openai_seg = {
            "id": i,
            "seek": 0,
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": (seg.get("text") or "").strip(),
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 0.0,
            "no_speech_prob": 0.0,
        }
        # OpenAI schema doesn't have speaker but extra fields are tolerated by clients.
        if "speaker" in seg:
            openai_seg["speaker"] = seg["speaker"]
        openai_segments.append(openai_seg)

        for w in seg.get("words") or []:
            word_entry = {
                "word": w.get("word", ""),
                "start": float(w.get("start", 0.0)),
                "end": float(w.get("end", 0.0)),
            }
            if "speaker" in w:
                word_entry["speaker"] = w["speaker"]
            openai_words.append(word_entry)

    return JSONResponse(
        content={
            "task": "transcribe",
            "language": detected_language,
            "duration": duration,
            "text": full_text,
            "segments": openai_segments,
            "words": openai_words,
        }
    )


# ----------------------------------------------------------------------
# /v1/audio/speech — Kokoro TTS (passthrough)
# ----------------------------------------------------------------------
@app.post("/v1/audio/speech")
async def speech(request: Request) -> Response:
    """Pure passthrough to Kokoro. It already exposes the OpenAI shape.

    Returns the raw audio bytes with the upstream's Content-Type.
    """
    body_bytes = await request.body()
    log.info("speech forward size=%d", len(body_bytes))
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{TTS_ENDPOINT}/v1/audio/speech",
                content=body_bytes,
                headers={
                    "Content-Type": request.headers.get(
                        "content-type", "application/json"
                    )
                },
            )
    except Exception as e:
        log.exception("speech.upstream_failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(e), "type": "upstream_error"}},
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "audio/mpeg"),
    )


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8888"))
    log.info("starting e2e test server on :%d", port)
    log.info("  LLM_ENDPOINT = %s", LLM_ENDPOINT)
    log.info("  ASR_ENDPOINT = %s", ASR_ENDPOINT)
    log.info("  TTS_ENDPOINT = %s", TTS_ENDPOINT)
    uvicorn.run(app, host="0.0.0.0", port=port)
