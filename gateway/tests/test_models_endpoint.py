"""Tests for ``GET /v1/models``."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from gateway.routers.models import GATEWAY_OWNER


@pytest.mark.asyncio
async def test_models_endpoint_returns_customer_models(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/models",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    # The fixture customer was seeded with model_ids=("m1","m2").
    assert "m1" in ids
    assert "m2" in ids
    # OpenAI ``owned_by`` semantics: who published the model, not who rents it.
    # Tenant identity is conveyed via the SDK key, not via this field.
    assert all(m["owned_by"] == GATEWAY_OWNER for m in body["data"])


@pytest.mark.asyncio
async def test_models_endpoint_owned_by_is_constant_across_customers(app: FastAPI) -> None:
    """Regression: ``owned_by`` must NOT leak customer identity. Different
    customers (in a multi-customer registry) should all see the same publisher
    string for the gateway-published models they have access to."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/models",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    body = r.json()
    owners = {m["owned_by"] for m in body["data"]}
    assert owners == {GATEWAY_OWNER}, (
        f"Expected single owner '{GATEWAY_OWNER}', got {owners}"
    )


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
