"""Mock EMS LLM endpoint — OpenAI-compatible /v1/chat/completions for benchmark dev.

Runs a tiny FastAPI app that mimics vLLM's chat-completions API enough to
drive ``translation_benchmark.py`` without a real EMS. Useful for:

* Wiring up the benchmark loop before EMS comes back online.
* Unit-testing the benchmark logic (CSV format, COMET integration, etc.).
* Demonstrating quality / latency differences across tiers without burning
  GPU time — the mock fakes translations and per-tier latency.

Three "tiers" are simulated:

* ``mock-low``  — fast, returns a deliberately mangled "translation"
                  (lowercase + suffix). Low COMET expected.
* ``mock-mid``  — medium, returns the reference text with one word dropped.
                  Mid COMET expected.
* ``mock-high`` — slow, returns the reference text verbatim. High COMET
                  expected (essentially perfect).

The mock READS the reference from the prompt-embedded test-case ID when the
benchmark script tags requests with ``X-Benchmark-Ref-Text`` header. When
the header is missing, the mock falls back to echoing the source.

Run:
    uv run python scripts/mock_ems_server.py --port 9090

Benchmark against it:
    uv run python scripts/translation_benchmark.py \\
        --endpoint http://localhost:9090 \\
        --api-key fake-key \\
        --tier mock-low mock-mid mock-high
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Mock EMS LLM (for benchmark dev)")


# Per-tier characteristics: artificial TTFT + token-delay so the latency
# distribution looks plausible. Quality is simulated by transforming the
# reference text.
TIER_PROFILES: dict[str, dict[str, Any]] = {
    "mock-low": {
        "ttft_ms": 50,
        "token_delay_ms": 5,
        "transform": lambda ref, src: _mangle_low(ref, src),
    },
    "mock-mid": {
        "ttft_ms": 150,
        "token_delay_ms": 10,
        "transform": lambda ref, src: _mangle_mid(ref, src),
    },
    "mock-high": {
        "ttft_ms": 300,
        "token_delay_ms": 20,
        "transform": lambda ref, src: ref,  # perfect
    },
}


def _mangle_low(ref: str, src: str) -> str:
    """Simulate a low-quality translation: lowercase + truncate."""
    if not ref:
        return src.lower()
    words = ref.split()
    truncated = " ".join(words[: max(1, len(words) // 2)])
    return truncated.lower() + " ..."


def _mangle_mid(ref: str, src: str) -> str:
    """Simulate a mid-quality translation: drop one word, lowercase first char."""
    if not ref:
        return src
    words = ref.split()
    if len(words) <= 3:
        return ref
    dropped = words[:1] + words[2:]
    return " ".join(dropped)


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """Return the three mock tiers as available models (OpenAI shape)."""
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "mock-ems"}
            for name in TIER_PROFILES
        ],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    model = body.get("model", "mock-mid")
    if model not in TIER_PROFILES:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"unknown mock model {model}"}},
        )
    profile = TIER_PROFILES[model]
    stream = bool(body.get("stream", False))

    # Extract last user message as the "source" text. The benchmark sends
    # prompts like "Translate ... Text: <src>"; we just take everything
    # after the last "Text:" marker if present.
    messages = body.get("messages") or []
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = str(m.get("content") or "")
            break
    src = user_msg.split("Text:", 1)[-1].strip() if "Text:" in user_msg else user_msg

    # Reference text passed via custom header (benchmark sets it so the
    # mock can produce realistic high/mid/low simulated outputs). Base64
    # encoding sidesteps httpx's ASCII-only header default for CJK refs.
    ref_b64 = request.headers.get("x-benchmark-ref-text-b64", "")
    try:
        ref = base64.b64decode(ref_b64).decode("utf-8") if ref_b64 else ""
    except (ValueError, UnicodeDecodeError):
        ref = ""
    completion = profile["transform"](ref, src)

    if stream:
        return StreamingResponse(
            _sse_stream(completion, profile, model),
            media_type="text/event-stream",
        )
    return _blocking_response(completion, model)


def _blocking_response(completion: str, model: str) -> dict[str, Any]:
    prompt_tokens = max(1, len(completion) // 4)
    completion_tokens = max(1, len(completion) // 4)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _sse_stream(completion: str, profile: dict[str, Any], model: str):
    """Emit completion as SSE chunks with simulated TTFT + per-token delay."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Initial TTFT delay before first token.
    await asyncio.sleep(profile["ttft_ms"] / 1000)

    # Stream the completion roughly word by word.
    tokens = completion.split(" ") if completion else [""]
    for i, tok in enumerate(tokens):
        delta = tok if i == 0 else " " + tok
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": {"content": delta}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        await asyncio.sleep(profile["token_delay_ms"] / 1000)

    # Terminal usage chunk (matches OpenAI include_usage shape so the
    # gateway / benchmark can extract token counts).
    final = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": max(1, len(completion) // 4),
            "completion_tokens": max(1, len(completion) // 4),
            "total_tokens": max(2, len(completion) // 2),
        },
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9090)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
