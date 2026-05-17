"""Tests for ``POST /v1/embeddings``.

Uses respx to intercept the upstream HTTP call instead of mocking the
``invoke_embeddings`` function — that exercises the real httpx request
construction and is closer to a true integration test.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI


def _upstream_response(
    *,
    embeddings: list[list[float]],
    model: str = "upstream-emb1",
    prompt_tokens: int = 10,
) -> httpx.Response:
    """Build a realistic OpenAI-shaped embeddings response."""
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": vec}
                for i, vec in enumerate(embeddings)
            ],
            "model": model,
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        },
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embeddings_single_string_input(app: FastAPI) -> None:
    """OpenAI accepts a single string for ``input``; we forward + return a
    single-element ``data`` array."""
    with respx.mock(base_url="http://embed.test") as m:
        route = m.post("/v1/embeddings").mock(
            return_value=_upstream_response(embeddings=[[0.1, 0.2, 0.3]])
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "emb1", "input": "Hello world"},
            )

    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    # Echo the customer-facing model id, not the upstream's served name.
    assert body["model"] == "emb1"
    assert body["usage"]["prompt_tokens"] == 10

    # Verify what was actually sent upstream.
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer EMPTY"
    payload = sent.read()
    assert b'"model":"upstream-emb1"' in payload  # mapped to upstream name
    assert b'"input":"Hello world"' in payload


@pytest.mark.asyncio
async def test_embeddings_list_input(app: FastAPI) -> None:
    """List input → list response (same length)."""
    with respx.mock(base_url="http://embed.test") as m:
        m.post("/v1/embeddings").mock(
            return_value=_upstream_response(embeddings=[[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]])
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "emb1", "input": ["one", "two", "three"]},
            )

    body = r.json()
    assert len(body["data"]) == 3
    assert [d["index"] for d in body["data"]] == [0, 1, 2]


@pytest.mark.asyncio
async def test_embeddings_forwards_optional_params(app: FastAPI) -> None:
    """``encoding_format`` and ``dimensions`` pass through to the upstream
    (the upstream may ignore what it doesn't support)."""
    with respx.mock(base_url="http://embed.test") as m:
        route = m.post("/v1/embeddings").mock(
            return_value=_upstream_response(embeddings=[[0.0] * 512])
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={
                    "model": "emb1",
                    "input": "x",
                    "encoding_format": "float",
                    "dimensions": 512,
                },
            )

    payload = route.calls.last.request.read()
    assert b'"encoding_format":"float"' in payload
    assert b'"dimensions":512' in payload


@pytest.mark.asyncio
async def test_embeddings_safety_identifier_preferred(app: FastAPI) -> None:
    """R6 alias: ``safety_identifier`` wins over ``user`` when both sent;
    forwarded to upstream as ``user`` (most upstreams only know that name)."""
    with respx.mock(base_url="http://embed.test") as m:
        route = m.post("/v1/embeddings").mock(
            return_value=_upstream_response(embeddings=[[0.0]])
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={
                    "model": "emb1",
                    "input": "x",
                    "user": "legacy-id",
                    "safety_identifier": "new-id",
                },
            )

    payload = route.calls.last.request.read()
    assert b'"user":"new-id"' in payload


@pytest.mark.asyncio
async def test_embeddings_user_fallback_to_customer_request(app: FastAPI) -> None:
    """When neither ``user`` nor ``safety_identifier`` is supplied, the
    gateway synthesises ``<customer_id>:<request_id>`` so the upstream
    always sees a stable identifier."""
    with respx.mock(base_url="http://embed.test") as m:
        route = m.post("/v1/embeddings").mock(
            return_value=_upstream_response(embeddings=[[0.0]])
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "emb1", "input": "x"},
            )

    payload = route.calls.last.request.read().decode()
    # Synthetic id has form "<customer_id>:<request_id>" where request_id is hex.
    assert '"user":"test-a:' in payload


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embeddings_unknown_model_returns_404(app: FastAPI) -> None:
    """Unknown embedding model → 404 with OpenAI envelope."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"model": "not-a-real-model", "input": "x"},
        )

    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_embeddings_missing_auth_returns_401(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/embeddings",
            json={"model": "emb1", "input": "x"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_embeddings_chat_model_id_not_treated_as_embedding(app: FastAPI) -> None:
    """A model id registered as an LLM (chat) must NOT be acceptable on the
    embeddings endpoint — the two namespaces are distinct in the registry.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer bsa_test_a"},
            # 'm1' is registered as an LLM in the fixture, not as embedding.
            json={"model": "m1", "input": "x"},
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_embeddings_upstream_5xx_returns_502(app: FastAPI) -> None:
    """Upstream returns 5xx → 502 with OpenAI envelope (real server failure)."""
    with respx.mock(base_url="http://embed.test") as m:
        m.post("/v1/embeddings").mock(return_value=httpx.Response(503, text="overloaded"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "emb1", "input": "x"},
            )

    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "dify_upstream_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [400, 413, 422])
async def test_embeddings_upstream_4xx_passes_through(
    app: FastAPI, upstream_status: int
) -> None:
    """Upstream 4xx → pass through the status code as ``invalid_request_error``.

    Codex review-2 [P2] regression: previously every non-2xx became 502
    ``dify_upstream_error``, which misled clients about who's at fault when
    their own input was bad (e.g. ``dimensions`` not supported by the
    upstream model, oversize input). The upstream's original status must be
    preserved and its message surfaced so the client can fix their request.
    """
    body_text = f"upstream complaint at status {upstream_status}"
    with respx.mock(base_url="http://embed.test") as m:
        m.post("/v1/embeddings").mock(
            return_value=httpx.Response(upstream_status, text=body_text)
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "emb1", "input": "x", "dimensions": 999999},
            )

    assert r.status_code == upstream_status
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "upstream_invalid_request"
    # Upstream message must reach the client so they can debug their input.
    assert body_text in body["error"]["message"]


@pytest.mark.asyncio
async def test_embeddings_upstream_timeout_returns_504(app: FastAPI) -> None:
    with respx.mock(base_url="http://embed.test") as m:
        m.post("/v1/embeddings").mock(side_effect=httpx.TimeoutException("read timeout"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "emb1", "input": "x"},
            )

    assert r.status_code == 504
    assert r.json()["error"]["code"] == "dify_timeout"


# ---------------------------------------------------------------------------
# /v1/models integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_endpoint_includes_embedding_models(app: FastAPI) -> None:
    """The same /v1/models list surfaces both LLM and embedding entries —
    OpenAI's spec is type-agnostic, clients differentiate by id pattern."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get("/v1/models", headers={"Authorization": "Bearer bsa_test_a"})

    body = r.json()
    ids = {m["id"] for m in body["data"]}
    # Fixture seeds LLMs ("m1", "m2") + embedding ("emb1").
    assert {"m1", "m2", "emb1"} <= ids

    # Embedding entry carries its registered publisher.
    emb = next(m for m in body["data"] if m["id"] == "emb1")
    assert emb["owned_by"] == "TestPublisher"
