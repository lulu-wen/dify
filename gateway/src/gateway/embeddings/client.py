"""Async HTTP client for OpenAI-compatible embedding endpoints.

Embeddings bypass Dify entirely — there's no orchestration, no RAG, no
App-level state. The gateway proxies straight to whichever OpenAI-compatible
service is registered for the model (typically vLLM in ``--task embed`` mode,
but any HTTP service following the OpenAI ``/v1/embeddings`` spec works).

We do **not** keep a long-lived ``AsyncClient`` per endpoint here — embeddings
calls are usually one-shot, batched, and customer/endpoint variability means
pooling buys little. If high-throughput becomes a need (e.g. ingesting large
corpora) we can introduce a per-endpoint client cache as a follow-up.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from gateway.errors import DifyTimeoutError, DifyUpstreamError, UpstreamClientError

logger = structlog.get_logger(__name__)

_ERR_BODY_TRUNCATE = 500

# Upstream 4xx statuses that genuinely describe a *caller* mistake the
# gateway couldn't pre-validate (request shape / size / parameter limits).
# Everything else in 4xx — 401, 403, 404, 408, 429, etc. — describes an
# upstream-side failure (bad API key, throttling, misconfigured model) and
# must NOT be reported to the SDK caller as ``invalid_request_error``.
_REQUEST_SHAPE_STATUSES: frozenset[int] = frozenset({400, 413, 422})


async def invoke_embeddings(
    *,
    endpoint_url: str,
    api_key: str,
    body: dict[str, Any],
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """POST ``/embeddings`` against an OpenAI-compatible endpoint.

    Args:
        endpoint_url: Base URL ending in ``/v1`` (we append ``/embeddings``).
            Trailing slashes tolerated.
        api_key: Bearer token. Pass any non-empty string for endpoints that
            don't validate (e.g., vLLM defaults).
        body: Raw OpenAI embeddings request body (model, input, ...).
        timeout_s: HTTP timeout.

    Returns:
        Parsed JSON response from the upstream.

    Raises:
        DifyTimeoutError: Timed out waiting for response.
        UpstreamClientError: Upstream returned a **request-shape** 4xx
            (400 / 413 / 422) — the caller's request was rejected for
            reasons the gateway could not pre-validate (unsupported
            ``dimensions``, oversized input, unknown encoding). Preserves
            the upstream status so the client sees the correct 4xx.
        DifyUpstreamError: Anything else non-success: 5xx, transport
            failure, *or* non-shape 4xx (401/403/404/429/...). The latter
            are gateway-side problems (bad upstream API key, upstream rate
            limit, upstream model misconfigured) and must not be misreported
            to the caller as their own ``invalid_request_error``. Also raised
            when a 2xx response body is missing or non-JSON-object shaped.
    """
    base = endpoint_url.rstrip("/")
    url = f"{base}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise DifyTimeoutError("Embedding endpoint timed out") from e
    except httpx.RequestError as e:
        raise DifyUpstreamError(f"Embedding request failed: {e}") from e

    if not resp.is_success:
        body_preview = _truncate(resp.text)
        logger.warning(
            "embeddings.upstream_error",
            status=resp.status_code,
            url=url,
            body=body_preview,
        )
        # Only genuine request-shape errors (400 / 413 / 422) are caller
        # mistakes. Everything else in 4xx — 401/403 (gateway's upstream
        # api_key is wrong/expired), 429 (upstream throttled the gateway),
        # 404 (upstream doesn't know this served model) — is gateway-side
        # and must surface as an upstream failure, never as the caller's
        # ``invalid_request_error``.
        if resp.status_code in _REQUEST_SHAPE_STATUSES:
            raise UpstreamClientError(
                f"Embedding endpoint rejected request (HTTP {resp.status_code}): {body_preview}",
                upstream_status=resp.status_code,
            )
        raise DifyUpstreamError(
            f"Embedding endpoint returned HTTP {resp.status_code}: {body_preview}"
        )

    # 2xx but the body could still be junk — a proxy can return an HTML
    # error page with status 200, or a misconfigured upstream might return
    # a JSON array / null where an object is expected. Either case would
    # propagate as an unhandled 500 to the SDK caller (not an OpenAI
    # envelope). Translate both into ``DifyUpstreamError``.
    try:
        parsed = resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise DifyUpstreamError(
            f"Embedding endpoint returned non-JSON body: {_truncate(resp.text)}"
        ) from e
    if not isinstance(parsed, dict):
        raise DifyUpstreamError(
            f"Embedding endpoint returned non-object JSON ({type(parsed).__name__}): {_truncate(resp.text)}"
        )
    return parsed


def _truncate(text: str) -> str:
    if len(text) <= _ERR_BODY_TRUNCATE:
        return text
    return text[:_ERR_BODY_TRUNCATE] + "...(truncated)"
