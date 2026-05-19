"""Tests for ``/v1/datasets`` (PR #3 R2 + R5).

Coverage targets:
    * Happy-path CRUD: create, list, get, delete return the right envelope.
    * R5 embedding lazy-provisioning:
        - explicit ``embedding_model`` → registry lookup, forwarded to Dify
        - omitted ``embedding_model`` → fallback to customer's first
        - no embedding models on customer → 400 with clear message
        - explicit ``embedding_model`` that isn't in registry → 404
    * Auth boundary: missing Bearer → 401.
    * Dify failure → OpenAI envelope (502 / 504), not raw stack.

Uses the existing ``FakeDifyClient`` (see conftest) so we test the router
end-to-end without mocking HTTP. Tests that need a customer with a fully
populated ``embedding_models`` config build a one-off registry.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway.config import Settings
from gateway.dify.client import DifyClient
from gateway.errors import DifyTimeoutError, DifyUpstreamError
from gateway.main import create_app
from gateway.registry import (
    CustomerEntry,
    CustomerRegistry,
    DifyConnection,
    EmbeddingModelEntry,
    ModelEntry,
)
from tests.conftest import FakeDifyClient


def _customer(
    *,
    sdk_key: str = "bsa_test_a",
    embedding_models: list[EmbeddingModelEntry] | None = None,
) -> CustomerEntry:
    """Build a one-off customer with a tunable embedding_models list.

    Default mirrors the shared fixture's ``emb1`` but tests can pass
    ``embedding_models=[]`` to exercise the no-default branch or pass an
    entry with ``provider`` set to verify the Dify payload includes it.
    """
    if embedding_models is None:
        embedding_models = [
            EmbeddingModelEntry(
                id="emb1",
                name="upstream-emb1",
                owner="TestPublisher",
                endpoint_url="http://embed.test/v1",
                api_key="EMPTY",
                dimensions=1024,
            )
        ]
    return CustomerEntry(
        sdk_key=sdk_key,
        customer_id="test-a",
        dify=DifyConnection(
            base_url="http://dify.test",
            console_email="admin@x",
            console_password="pw",
            dataset_api_key="ds-x",
        ),
        models=[ModelEntry(id="m1", provider="prov", name="n")],
        embedding_models=embedding_models,
    )


def _app_with_customer(customer: CustomerEntry, fake: FakeDifyClient) -> FastAPI:
    settings = Settings(registry_path="unused.yaml", log_json=False)
    registry = CustomerRegistry.from_entries([customer])
    application = create_app(settings=settings, registry=registry)

    def factory(_: CustomerEntry) -> DifyClient:  # type: ignore[return-value]
        return fake  # type: ignore[return-value]

    application.state.dify_client_factory = factory
    application.state.app_manager._client_factory = factory  # noqa: SLF001
    return application


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_dataset_with_explicit_embedding_model(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Client passes ``embedding_model="emb1"`` → registry resolves it →
    Dify receives ``embedding_model`` (and ``embedding_model_provider``
    when registry has it). Response carries the Dify-issued UUID and
    customer-facing fields.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={
                "name": "rsrp-manuals",
                "description": "RSRP fault diagnostics",
                "embedding_model": "emb1",
                "indexing_technique": "high_quality",
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "ds-uuid-1"
    assert body["name"] == "default-ds"
    assert body["indexing_technique"] == "high_quality"

    # Verify what reached the Dify client
    sent = fake_dify.calls["dataset_create"][0]
    assert sent["dataset_api_key"] == "ds-x"
    payload = sent["payload"]
    assert payload["name"] == "rsrp-manuals"
    assert payload["description"] == "RSRP fault diagnostics"
    assert payload["indexing_technique"] == "high_quality"
    # Resolved embedding ``name`` (upstream-served name), not the customer-facing id.
    assert payload["embedding_model"] == "upstream-emb1"
    # No provider on default fixture — must be omitted from Dify payload.
    assert "embedding_model_provider" not in payload


@pytest.mark.asyncio
async def test_create_dataset_forwards_provider_when_registry_has_it(
    fake_dify: FakeDifyClient,
) -> None:
    """R5: when the customer's embedding entry has a ``provider`` (Dify
    plugin namespace), the gateway forwards it as
    ``embedding_model_provider`` so Dify can resolve the plugin
    unambiguously. Customers with multiple embedding plugins MUST set
    this; customers with a single one can omit it.
    """
    customer = _customer(
        embedding_models=[
            EmbeddingModelEntry(
                id="emb1",
                name="bge-m3",
                owner="BAAI",
                endpoint_url="http://embed.test/v1",
                provider="langgenius/openai_api_compatible/openai_api_compatible",
            )
        ]
    )
    app = _app_with_customer(customer, fake_dify)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "with-provider", "embedding_model": "emb1"},
        )

    payload = fake_dify.calls["dataset_create"][0]["payload"]
    assert payload["embedding_model"] == "bge-m3"
    assert (
        payload["embedding_model_provider"]
        == "langgenius/openai_api_compatible/openai_api_compatible"
    )


@pytest.mark.asyncio
async def test_list_datasets_forwards_pagination_params(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """``page`` / ``limit`` / ``keyword`` query params reach Dify; envelope
    shape matches OpenAI list convention (``object: "list"`` + ``data: [...]``).
    """
    fake_dify.dataset_list_response = {
        "data": [
            {"id": "u1", "name": "kb-1", "document_count": 3, "word_count": 1024},
            {"id": "u2", "name": "kb-2", "document_count": 0, "word_count": 0},
        ],
        "has_more": True,
        "limit": 2,
        "total": 5,
        "page": 1,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/datasets?page=1&limit=2&keyword=rsrp",
            headers={"Authorization": "Bearer bsa_test_a"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["has_more"] is True
    assert body["total"] == 5
    assert [d["id"] for d in body["data"]] == ["u1", "u2"]

    sent = fake_dify.calls["dataset_list"][0]
    assert sent["page"] == 1
    assert sent["limit"] == 2
    assert sent["keyword"] == "rsrp"


@pytest.mark.asyncio
async def test_list_datasets_uses_defaults_when_no_query_params(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        await cli.get(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
        )

    sent = fake_dify.calls["dataset_list"][0]
    assert sent["page"] == 1
    assert sent["limit"] == 20
    assert sent["keyword"] is None


@pytest.mark.asyncio
async def test_get_dataset_returns_metadata(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.dataset_get_response = {
        "id": "abc-uuid",
        "name": "specific",
        "description": "stuff",
        "indexing_technique": "high_quality",
        "embedding_model": "upstream-emb1",
        "embedding_model_provider": "langgenius/...",
        "document_count": 7,
        "word_count": 5000,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/datasets/abc-uuid",
            headers={"Authorization": "Bearer bsa_test_a"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "abc-uuid"
    assert body["document_count"] == 7

    sent = fake_dify.calls["dataset_get"][0]
    assert sent["dataset_id"] == "abc-uuid"


@pytest.mark.asyncio
async def test_delete_dataset_returns_idempotent_envelope(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.delete(
            "/v1/datasets/abc-uuid",
            headers={"Authorization": "Bearer bsa_test_a"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body == {"id": "abc-uuid", "deleted": True}
    sent = fake_dify.calls["dataset_delete"][0]
    assert sent["dataset_id"] == "abc-uuid"


# ---------------------------------------------------------------------------
# R5 embedding lazy-provisioning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_dataset_fallback_to_default_embedding(
    fake_dify: FakeDifyClient,
) -> None:
    """No ``embedding_model`` in request body → gateway picks the customer's
    first registered embedding model. This is the common case for customers
    that only have one embedding service configured."""
    customer = _customer(
        embedding_models=[
            EmbeddingModelEntry(
                id="bge-m3",
                name="upstream-bge",
                owner="BAAI",
                endpoint_url="http://embed.test/v1",
            )
        ]
    )
    app = _app_with_customer(customer, fake_dify)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "no-embedding-spec"},
        )

    assert r.status_code == 200
    payload = fake_dify.calls["dataset_create"][0]["payload"]
    assert payload["embedding_model"] == "upstream-bge"


@pytest.mark.asyncio
async def test_create_dataset_unknown_embedding_returns_404(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Asking for an embedding the customer doesn't have → 404
    ``model_not_found``, same code chat / embeddings use. Client can have a
    single error handler covering all model-not-found cases."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "x", "embedding_model": "not-registered"},
        )

    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "model_not_found"
    assert body["error"]["param"] == "embedding_model"
    # Gateway must NOT have reached Dify at all if the resolution failed first.
    assert fake_dify.calls["dataset_create"] == []


@pytest.mark.asyncio
async def test_create_dataset_no_embedding_anywhere_returns_400(
    fake_dify: FakeDifyClient,
) -> None:
    """Customer has zero embedding models registered AND request body has
    no ``embedding_model`` → 400 with a remediation message naming the
    exact field to set. Gateway must NOT call Dify (the dataset would be
    created but useless without an embedding model)."""
    customer = _customer(embedding_models=[])
    app = _app_with_customer(customer, fake_dify)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "x"},
        )

    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "invalid_request"
    assert "embedding_model" in body["error"]["message"]
    assert fake_dify.calls["dataset_create"] == []


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_dataset_requires_auth(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post("/v1/datasets", json={"name": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_dataset_rejects_empty_name(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": ""},
        )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_create_dataset_rejects_unknown_indexing_technique(app: FastAPI) -> None:
    """Pydantic Literal guards the surface — only `"high_quality"` and
    `"economy"` are valid. Defends the gateway from accepting whatever the
    customer ships and letting Dify silently downgrade."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "x", "indexing_technique": "bogus"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_list_datasets_rejects_bad_pagination(app: FastAPI) -> None:
    """Non-int / negative ``limit`` → OpenAI 400 envelope (not 422)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/datasets?limit=notanumber",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "limit"


# ---------------------------------------------------------------------------
# Dify upstream failures (envelope contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dify_5xx_during_create_returns_502(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.dataset_error = DifyUpstreamError("Dify returned HTTP 503: overloaded")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "x", "embedding_model": "emb1"},
        )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "dify_upstream_error"


@pytest.mark.asyncio
async def test_dify_timeout_during_list_returns_504(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.dataset_error = DifyTimeoutError("Dify list-datasets timed out")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "dify_timeout"


# ---------------------------------------------------------------------------
# Regression: explicit `embedding_model` short-circuits before Dify call
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R4 — POST /v1/datasets/{id}/retrieve (pure retrieval channel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_uses_dataset_default_when_no_knobs(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """When the client passes only ``query``, the gateway must NOT send a
    ``retrieval_model`` — Dify uses the dataset's bake-in retrieval config.
    This avoids accidentally overriding the customer's tuned settings."""
    fake_dify.dataset_retrieve_response = {
        "query": {"content": "RSRP=-115"},
        "records": [
            {
                "segment": {
                    "id": "seg-1",
                    "content": "RSRP below -110 indicates weak signal.",
                    "document": {"id": "doc-1", "name": "rsrp-guide.pdf"},
                },
                "score": 0.87,
            }
        ],
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": "RSRP=-115"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["query"] == "RSRP=-115"
    assert len(body["data"]) == 1
    hit = body["data"][0]
    assert hit["content"].startswith("RSRP below -110")
    assert hit["score"] == 0.87
    assert hit["document_id"] == "doc-1"
    assert hit["document_name"] == "rsrp-guide.pdf"
    assert hit["segment_id"] == "seg-1"

    # Critical: payload sent to Dify must NOT carry retrieval_model.
    sent = fake_dify.calls["dataset_retrieve"][0]
    assert sent["dataset_id"] == "abc-uuid"
    assert sent["payload"] == {"query": "RSRP=-115"}


@pytest.mark.asyncio
async def test_retrieve_builds_retrieval_model_when_top_k_set(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """One knob (top_k) supplied → gateway fills in the rest of Dify's
    required RetrievalModel fields with sensible defaults. Required because
    Dify rejects a partial ``retrieval_model`` payload."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": "x", "top_k": 5},
        )

    sent = fake_dify.calls["dataset_retrieve"][0]
    rm = sent["payload"]["retrieval_model"]
    assert rm["top_k"] == 5
    assert rm["search_method"] == "semantic_search"
    assert rm["reranking_enable"] is False
    assert rm["score_threshold_enabled"] is False
    assert rm["score_threshold"] is None


@pytest.mark.asyncio
async def test_retrieve_score_threshold_enables_flag(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """When client passes ``score_threshold``, gateway sets
    ``score_threshold_enabled=True`` and forwards the float. Forgetting the
    flag is the #1 source of «why is my threshold being ignored» in Dify."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": "x", "score_threshold": 0.6},
        )

    rm = fake_dify.calls["dataset_retrieve"][0]["payload"]["retrieval_model"]
    assert rm["score_threshold"] == 0.6
    assert rm["score_threshold_enabled"] is True


@pytest.mark.asyncio
async def test_retrieve_search_method_forwarded(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": "x", "search_method": "hybrid_search"},
        )

    rm = fake_dify.calls["dataset_retrieve"][0]["payload"]["retrieval_model"]
    assert rm["search_method"] == "hybrid_search"


@pytest.mark.asyncio
async def test_retrieve_empty_records_returns_empty_list(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """No hits → ``data: []`` (not 404). Empty result is a valid query
    outcome; clients use list length to decide whether to fall back."""
    fake_dify.dataset_retrieve_response = {"query": {"content": "x"}, "records": []}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": "no-match"},
        )

    assert r.status_code == 200
    assert r.json()["data"] == []


@pytest.mark.asyncio
async def test_retrieve_rejects_empty_query(app: FastAPI) -> None:
    """Pydantic min_length=1 → 400 OpenAI envelope (not 422)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": ""},
        )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_retrieve_rejects_top_k_out_of_range(app: FastAPI) -> None:
    """Pydantic ge=1, le=100 — defend against the customer asking for
    ``top_k=10000`` and OOMing the vector store."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": "x", "top_k": 9999},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_retrieve_requires_auth(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post("/v1/datasets/abc-uuid/retrieve", json={"query": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_retrieve_dify_5xx_returns_502(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.dataset_error = DifyUpstreamError("Dify returned HTTP 503: overloaded")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets/abc-uuid/retrieve",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"query": "x"},
        )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "dify_upstream_error"


@pytest.mark.asyncio
async def test_unknown_embedding_does_not_leak_dataset_into_dify(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Defensive: even if Dify *would* accept any embedding name, the
    gateway must short-circuit on a registry miss to avoid creating an
    orphan dataset in Dify that the customer cannot list (because their
    registry doesn't know about the embedding the dataset uses)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "x", "embedding_model": "ghost"},
        )
    assert r.status_code == 404
    # The proof: Dify client received zero calls.
    assert fake_dify.calls["dataset_create"] == []
