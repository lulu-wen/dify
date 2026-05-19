"""``/v1/datasets`` router — knowledge base CRUD.

Wraps Dify's Service API ``/v1/datasets`` endpoints. Customers use the same
SDK key they use for chat / embeddings; the gateway swaps it internally for
the customer's ``dataset_api_key`` (registry's ``DifyConnection``).

Why have this layer at all (instead of telling customers to call Dify directly)?
    1. **Auth**: clients use one SDK key, not one per Dify resource.
    2. **R5 embedding lazy-provisioning**: clients pass a customer-facing
       embedding model id (e.g. ``"bge-m3"``); the gateway resolves it from
       the registry and injects the right Dify ``embedding_model`` +
       ``embedding_model_provider`` pair. Clients never see Dify plugin
       namespaces.
    3. **Error envelope**: Dify's 4xx ``invalid_param`` shape becomes the
       gateway's OpenAI-style ``error.code`` envelope, consistent with
       chat / embeddings.
    4. **PATCH is intentionally absent**: changing a dataset's embedding
       model after documents are vectorised would silently break retrieval.
       Customers wanting different embedding go delete + recreate.

What we do NOT touch:
    * Dataset IDs in URLs are Dify-issued UUIDs, surfaced verbatim. No
      gateway-side slugging. Analogous to OpenAI's ``file-abc123`` ids.
    * The retrieval / files endpoints live in their own routers (R3 / R4).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.dify.client import DifyClient
from gateway.errors import InvalidRequestError, UnknownModelError
from gateway.registry import CustomerEntry, EmbeddingModelEntry
from gateway.schemas import Dataset, DatasetCreateRequest, DatasetList

logger = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# R5 — embedding model lazy-provisioning
# ---------------------------------------------------------------------------


def resolve_embedding_for_dataset(
    customer: CustomerEntry, requested_id: str | None
) -> EmbeddingModelEntry:
    """Pick which embedding model the new dataset should bind to.

    Resolution order:
        1. Client supplied an explicit ``embedding_model`` id — must exist
           in the customer's registered ``embedding_models``. Otherwise 404.
        2. No id supplied — fall back to the customer's first registered
           embedding model (the "default").
        3. No registered embedding models at all — 400 with a clear
           remediation message.

    Raises:
        UnknownModelError: client asked for an id the customer cannot use.
        InvalidRequestError: customer has no embedding models configured.
    """
    if requested_id is not None:
        entry = customer.find_embedding_model(requested_id)
        if entry is None:
            raise UnknownModelError(
                f"embedding model '{requested_id}' is not enabled for this customer",
                param="embedding_model",
            )
        return entry

    if not customer.embedding_models:
        raise InvalidRequestError(
            (
                "no embedding model configured for this customer; "
                "pass `embedding_model` explicitly or register a default in "
                "the customer's `embedding_models` registry section"
            ),
            param="embedding_model",
        )
    return customer.embedding_models[0]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/v1/datasets")
async def create_dataset(request: Request, body: DatasetCreateRequest) -> Any:
    """Create a new dataset bound to a chosen embedding model.

    The embedding model is **locked in at creation time** (Dify behaviour);
    documents added later are all vectorised with the same model. To switch
    embedding models, delete this dataset and create a new one.
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)

    embedding = resolve_embedding_for_dataset(customer, body.embedding_model)

    payload: dict[str, Any] = {
        "name": body.name,
        "description": body.description,
        "indexing_technique": body.indexing_technique,
        "embedding_model": embedding.name,
    }
    if embedding.provider is not None:
        payload["embedding_model_provider"] = embedding.provider

    dify_resp = await dify_client.create_dataset(
        dataset_api_key=customer.dify.dataset_api_key,
        payload=payload,
    )

    logger.info(
        "datasets.created",
        dataset_id=dify_resp.get("id"),
        embedding_model=embedding.id,
        indexing_technique=body.indexing_technique,
    )

    return JSONResponse(content=_to_dataset(dify_resp))


@router.get("/v1/datasets")
async def list_datasets(request: Request) -> Any:
    """List datasets visible to the customer.

    Forwards ``page`` / ``limit`` / ``keyword`` query params to Dify.
    Defaults: page=1, limit=20 (matches Dify's defaults).
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)

    page = _int_query(request, "page", default=1, minimum=1)
    limit = _int_query(request, "limit", default=20, minimum=1, maximum=100)
    keyword = request.query_params.get("keyword") or None

    dify_resp = await dify_client.list_datasets(
        dataset_api_key=customer.dify.dataset_api_key,
        page=page,
        limit=limit,
        keyword=keyword,
    )

    entries = [_to_dataset(d) for d in (dify_resp.get("data") or [])]
    envelope = DatasetList(
        data=[Dataset(**e) for e in entries],
        has_more=bool(dify_resp.get("has_more", False)),
        total=int(dify_resp.get("total", 0)),
        page=int(dify_resp.get("page", page)),
        limit=int(dify_resp.get("limit", limit)),
    )
    return JSONResponse(content=envelope.model_dump(exclude_none=True))


@router.get("/v1/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, request: Request) -> Any:
    """Fetch a single dataset's metadata by Dify UUID."""
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)

    dify_resp = await dify_client.get_dataset(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
    )
    return JSONResponse(content=_to_dataset(dify_resp))


@router.delete("/v1/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str, request: Request) -> Any:
    """Delete a dataset by Dify UUID.

    Idempotent: returns 204 whether or not the dataset existed (Dify 404 →
    treated as already-deleted in the client). This matches the semantics
    of ``DELETE`` in OpenAI's spec and avoids forcing clients to handle 404
    separately from a normal cleanup loop.
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)

    await dify_client.delete_dataset(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
    )
    logger.info("datasets.deleted", dataset_id=dataset_id)
    return JSONResponse(content={"id": dataset_id, "deleted": True})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dataset(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape a Dify dataset object into the gateway's surfaced fields.

    Dify returns a much larger payload (permission, plugin ids, partial
    member list, ...). We keep only what the customer needs and let
    ``extra="allow"`` on ``Dataset`` pass through anything extra a client
    explicitly asks for.
    """
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "description": raw.get("description") or "",
        "indexing_technique": raw.get("indexing_technique"),
        "embedding_model": raw.get("embedding_model"),
        "embedding_model_provider": raw.get("embedding_model_provider"),
        "document_count": int(raw.get("document_count") or 0),
        "word_count": int(raw.get("word_count") or 0),
        "created_at": raw.get("created_at"),
    }


def _int_query(
    request: Request,
    name: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    """Parse a positive-int query param, falling back to ``default``.

    Bad values (non-int, below ``minimum``, above ``maximum``) surface as
    ``InvalidRequestError`` so the client gets an OpenAI envelope instead
    of FastAPI's default 422. Pagination is a hot debug area; clear errors
    save support time.
    """
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise InvalidRequestError(f"{name} must be an integer", param=name)
    if value < minimum:
        raise InvalidRequestError(f"{name} must be >= {minimum}", param=name)
    if maximum is not None and value > maximum:
        raise InvalidRequestError(f"{name} must be <= {maximum}", param=name)
    return value
