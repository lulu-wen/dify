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
from gateway.schemas import (
    Dataset,
    DatasetCreateRequest,
    DatasetList,
    DatasetRetrieveRequest,
    DatasetRetrieveResponse,
    RetrievedSegment,
)

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

    The selected entry **must** have ``provider`` set (codex review-2 P2):
    Dify only honours the explicit ``embedding_model`` when both ``model``
    and ``provider`` are supplied; otherwise it silently falls back to the
    workspace default. Letting that happen would create a dataset indexed
    with the wrong embedding model — a debugging nightmare since retrieval
    just returns no hits. Reject up front instead.

    Raises:
        UnknownModelError: client asked for an id the customer cannot use.
        InvalidRequestError: customer has no embedding models configured,
            OR the selected entry has no ``provider`` (cannot be safely
            bound to a Dify dataset).
    """
    if requested_id is not None:
        entry = customer.find_embedding_model(requested_id)
        if entry is None:
            raise UnknownModelError(
                f"embedding model '{requested_id}' is not enabled for this customer",
                param="embedding_model",
            )
    else:
        if not customer.embedding_models:
            raise InvalidRequestError(
                (
                    "no embedding model configured for this customer; "
                    "pass `embedding_model` explicitly or register a default in "
                    "the customer's `embedding_models` registry section"
                ),
                param="embedding_model",
            )
        entry = customer.embedding_models[0]

    if entry.provider is None:
        raise InvalidRequestError(
            (
                f"embedding model '{entry.id}' is missing the `provider` field; "
                "datasets need both `embedding_model` and `embedding_model_provider` "
                "to bind reliably (Dify silently falls back to the workspace default "
                "otherwise). Set `provider` on the registry entry "
                "(e.g. \"langgenius/openai_api_compatible/openai_api_compatible\")"
            ),
            param="embedding_model",
        )
    return entry


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

    # ``resolve_embedding_for_dataset`` guarantees ``provider`` is non-None.
    payload: dict[str, Any] = {
        "name": body.name,
        "description": body.description,
        "indexing_technique": body.indexing_technique,
        "embedding_model": embedding.name,
        "embedding_model_provider": embedding.provider,
    }

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


@router.post("/v1/datasets/{dataset_id}/retrieve")
async def retrieve_dataset(
    dataset_id: str, request: Request, body: DatasetRetrieveRequest
) -> Any:
    """Pure-retrieval channel (hit-testing) — return top-k chunks for a query.

    No LLM call, no RAG augmentation. The customer can use this to build a
    search-only UI, run their own ranking pipeline, or evaluate retrieval
    quality. Output shape mirrors OpenAI's list-style envelope.

    Retrieval-model handling:
        * If the client provides ``top_k`` / ``score_threshold`` /
          ``search_method``, the gateway builds a full ``retrieval_model``
          payload (Dify requires several mandatory sub-fields together).
        * If all are omitted, the gateway sends NO ``retrieval_model`` and
          Dify uses the dataset's bake-in default — typically what the
          customer wants when they trust their dataset's pre-tuned settings.
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)

    payload: dict[str, Any] = {"query": body.query}
    retrieval_model = _build_retrieval_model(body)
    if retrieval_model is not None:
        payload["retrieval_model"] = retrieval_model

    dify_resp = await dify_client.retrieve_dataset(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
        payload=payload,
    )

    response = DatasetRetrieveResponse(
        query=body.query,
        data=[_to_segment(r) for r in (dify_resp.get("records") or [])],
    )
    logger.info(
        "datasets.retrieved",
        dataset_id=dataset_id,
        query_len=len(body.query),
        hits=len(response.data),
    )
    return JSONResponse(content=response.model_dump(exclude_none=True))


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


def _build_retrieval_model(body: DatasetRetrieveRequest) -> dict[str, Any] | None:
    """Build a Dify ``retrieval_model`` payload, or None to use dataset default.

    Dify's ``RetrievalModel`` requires several fields together (``search_method``,
    ``reranking_enable``, ``top_k``, ``score_threshold_enabled``). If the
    client supplies any one knob we have to fill in the rest with sensible
    defaults; if they supply none, we send no override and Dify uses its
    own per-dataset defaults — usually what the customer wants.
    """
    if body.top_k is None and body.score_threshold is None and body.search_method is None:
        return None
    return {
        "search_method": body.search_method or "semantic_search",
        "reranking_enable": False,
        "top_k": body.top_k if body.top_k is not None else 3,
        "score_threshold_enabled": body.score_threshold is not None,
        "score_threshold": body.score_threshold,
    }


def _to_segment(raw: dict[str, Any]) -> RetrievedSegment:
    """Shape a Dify retrieval record into the gateway's exposed segment.

    Dify wraps each hit as ``{"segment": {...}, "score": float, ...}``;
    inside ``segment`` is content + document metadata. Flatten to the
    client-facing fields and let ``extra="allow"`` carry through anything
    advanced clients might want (e.g. positions, child chunks).
    """
    segment = raw.get("segment") or {}
    document = segment.get("document") or {}
    return RetrievedSegment(
        content=segment.get("content", ""),
        score=raw.get("score") if isinstance(raw.get("score"), (int, float)) else None,
        document_id=document.get("id"),
        document_name=document.get("name"),
        segment_id=segment.get("id"),
    )


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
    except ValueError as exc:
        raise InvalidRequestError(f"{name} must be an integer", param=name) from exc
    if value < minimum:
        raise InvalidRequestError(f"{name} must be >= {minimum}", param=name)
    if maximum is not None and value > maximum:
        raise InvalidRequestError(f"{name} must be <= {maximum}", param=name)
    return value
