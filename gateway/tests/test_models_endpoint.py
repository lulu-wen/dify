"""Tests for ``GET /v1/models``."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_models_endpoint_returns_customer_models(app: FastAPI) -> None:
    """Models endpoint surfaces both LLM and embedding entries, each
    carrying its registry-declared publisher in ``owned_by``.

    The conftest fixture builds:
      * LLM models ('m1', 'm2') without explicit owner → fall back to the
        gateway identifier ('ai-sdk-gateway').
      * Embedding model 'emb1' with explicit owner='TestPublisher'.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/models",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"

    by_id = {m["id"]: m for m in body["data"]}
    assert {"m1", "m2", "emb1"} <= by_id.keys()

    # LLM rows fall back to the gateway default (fixture doesn't set ``owner``).
    assert by_id["m1"]["owned_by"] == "ai-sdk-gateway"
    assert by_id["m2"]["owned_by"] == "ai-sdk-gateway"
    # The embedding row carries the explicit publisher from the fixture.
    assert by_id["emb1"]["owned_by"] == "TestPublisher"


@pytest.mark.asyncio
async def test_models_endpoint_owned_by_does_not_leak_customer_id(app: FastAPI) -> None:
    """Regression: ``owned_by`` must NOT contain the requesting customer's
    identifier — that's a tenant-leak. It must reflect the model publisher
    (or the gateway default), independent of who is asking."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/models",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    body = r.json()
    for m in body["data"]:
        assert m["owned_by"] != "test-a", "owned_by leaked customer_id"


def test_model_entry_owner_defaults_to_gateway() -> None:
    """ModelEntry's ``owner`` defaults to the gateway identifier when not
    specified — sensible fallback for unknown publishers."""
    from gateway.registry import ModelEntry

    m = ModelEntry(id="x", provider="p", name="n")
    assert m.owner == "ai-sdk-gateway"


def test_model_entry_owner_can_be_overridden() -> None:
    """ModelEntry's ``owner`` is configurable per model in the registry."""
    from gateway.registry import ModelEntry

    m = ModelEntry(id="qwen3.6-35b", provider="p", name="n", owner="Qwen")
    assert m.owner == "Qwen"


@pytest.mark.asyncio
async def test_models_endpoint_requires_auth(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get("/v1/models")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_endpoint_does_not_require_auth(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
