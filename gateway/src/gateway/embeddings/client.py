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

from typing import Any

import httpx
import structlog

from gateway.errors import DifyTimeoutError, DifyUpstreamError, UpstreamClientError

logger = structlog.get_logger(__name__)

_ERR_BODY_TRUNCATE = 500


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
        UpstreamClientError: Upstream returned 4xx (the caller's request was
            rejected for reasons the gateway could not pre-validate, e.g.
            unsupported ``dimensions`` or oversized input). Preserves the
            upstream's status code so the client sees the correct 4xx.
        DifyUpstreamError: Upstream returned 5xx or had a transport failure.
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
        # 4xx: client mistake the gateway couldn't pre-validate (e.g. bad
        # ``dimensions``, oversize input). Pass it through so the caller sees
        # the real 4xx, not a misleading 502.
        if 400 <= resp.status_code < 500:
            raise UpstreamClientError(
                f"Embedding endpoint rejected request (HTTP {resp.status_code}): {body_preview}",
                upstream_status=resp.status_code,
            )
        # 5xx or other non-success: real upstream failure.
        raise DifyUpstreamError(
            f"Embedding endpoint returned HTTP {resp.status_code}: {body_preview}"
        )

    return resp.json()


def _truncate(text: str) -> str:
    if len(text) <= _ERR_BODY_TRUNCATE:
        return text
    return text[:_ERR_BODY_TRUNCATE] + "...(truncated)"
