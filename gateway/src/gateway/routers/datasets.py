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
from gateway.errors import (
    InvalidRequestError,
    UnknownDatasetError,
    UnknownModelError,
)
from gateway.mode import IsolationStrategy, isolation_strategy_for
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
) -> tuple[str, str]:
    """Pick the ``(model_name, model_provider)`` pair the dataset binds to.

    Resolution depends on the customer's isolation mode (PR #4 R5):

    **Shared mode**: the Dify workspace has exactly one embedding plugin
    active and every customer's dataset must bind to it (workspace-level
    constraint). The pair comes from ``customer.dify.shared_embedding_model``.
    If the client passes an explicit ``embedding_model`` that doesn't
    match the workspace's name, the request is rejected with 400 — better
    than letting Dify silently fall back to the workspace default.

    **Dedicated mode**: the original PR #3 R5 behaviour. Pick from the
    customer's registered ``embedding_models``: explicit id wins, otherwise
    use the first registered entry. The entry must have ``provider`` set
    (PR #3 review-2 P2).

    Returns:
        ``(embedding_model_name, embedding_model_provider)`` tuple ready
        to drop into the Dify dataset-create payload.

    Raises:
        UnknownModelError: client asked for an id the customer cannot use
            (dedicated mode only).
        InvalidRequestError: dedicated-mode customer has no embedding
            models configured / selected entry has no provider, OR
            shared-mode client passed an embedding_model that doesn't
            match the workspace's required model.
    """
    # Shared mode: workspace-global embedding model wins. Per-customer
    # ``embedding_models`` are still usable for direct /v1/embeddings calls,
    # but they cannot bind a dataset — Dify only has one embedding plugin
    # active per workspace.
    shared = customer.dify.shared_embedding_model
    if shared is not None:
        if requested_id is not None and requested_id != shared.name:
            raise InvalidRequestError(
                (
                    f"shared-mode workspace requires embedding_model="
                    f"'{shared.name}' for dataset creation; received '{requested_id}'. "
                    "Per-customer embedding_models can still be used directly via "
                    "POST /v1/embeddings, but datasets bind to the workspace-global model."
                ),
                param="embedding_model",
            )
        return shared.name, shared.provider

    # Dedicated mode (PR #3 R5 behaviour) — pick from the customer's list.
    entry = _resolve_dedicated_embedding(customer, requested_id)
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
    return entry.name, entry.provider


def _resolve_dedicated_embedding(
    customer: CustomerEntry, requested_id: str | None
) -> EmbeddingModelEntry:
    """Pick an EmbeddingModelEntry for dedicated mode (PR #3 R5 logic)."""
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
# R3/R4 — cross-customer ownership verification (shared mode)
# ---------------------------------------------------------------------------


async def _verify_dataset_ownership(
    dify_client: DifyClient,
    customer: CustomerEntry,
    strategy: IsolationStrategy,
    dataset_id: str,
) -> dict[str, Any]:
    """Fetch a dataset and ensure it belongs to this customer.

    In dedicated mode the workspace IS the customer's, so anything visible
    via the customer's ``dataset_api_key`` belongs to them; we skip the
    ownership check to save a Dify roundtrip. In shared mode we fetch the
    dataset, inspect its name against the customer's prefix, and raise
    ``UnknownDatasetError`` (404) if it belongs to someone else.

    The 404 is deliberate (not 403): a 403 would leak the existence of the
    other customer's dataset. The customer sees the same envelope whether
    the dataset is missing or just inaccessible.

    Returns the Dify response so the caller can reuse it (e.g. for the
    happy-path get_dataset response — avoids fetching twice).
    """
    dify_meta = await dify_client.get_dataset(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
    )
    if strategy.is_shared:
        dify_name = dify_meta.get("name", "")
        if not strategy.dataset_belongs_to(customer.customer_id, dify_name):
            raise UnknownDatasetError(
                f"dataset '{dataset_id}' not found",
                param="dataset_id",
            )
    return dify_meta


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/v1/datasets")
async def create_dataset(request: Request, body: DatasetCreateRequest) -> Any:
    """Create a new dataset bound to a chosen embedding model.

    The embedding model is **locked in at creation time** (Dify behaviour);
    documents added later are all vectorised with the same model. To switch
    embedding models, delete this dataset and create a new one.

    Shared-mode (PR #4 R3): the dataset name is prefixed with
    ``{customer_id}__`` before sending to Dify, so two customers asking for
    name "kb" build distinct Dify datasets. The response strips the prefix
    so the client sees the name they sent.
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)
    strategy = isolation_strategy_for(customer)

    embedding_name, embedding_provider = resolve_embedding_for_dataset(
        customer, body.embedding_model
    )

    payload: dict[str, Any] = {
        "name": strategy.dataset_name_to_dify(customer.customer_id, body.name),
        "description": body.description,
        "indexing_technique": body.indexing_technique,
        "embedding_model": embedding_name,
        "embedding_model_provider": embedding_provider,
    }

    dify_resp = await dify_client.create_dataset(
        dataset_api_key=customer.dify.dataset_api_key,
        payload=payload,
    )

    logger.info(
        "datasets.created",
        dataset_id=dify_resp.get("id"),
        embedding_model=embedding_name,
        indexing_technique=body.indexing_technique,
        mode=customer.dify.mode,
    )

    return JSONResponse(content=_to_dataset(dify_resp, customer, strategy))


@router.get("/v1/datasets")
async def list_datasets(request: Request) -> Any:
    """List datasets visible to the customer.

    Forwards ``page`` / ``limit`` / ``keyword`` query params to Dify.
    Defaults: page=1, limit=20 (matches Dify's defaults).

    Shared-mode (PR #4 R3): the gateway filters Dify's response to only
    include datasets owned by this customer (name starts with the
    ``{customer_id}__`` prefix). The reported ``total`` reflects the
    filtered count, not Dify's workspace total — this means pagination is
    approximate in shared mode (a request for page=2 may return fewer
    items than ``limit`` because other customers' datasets were filtered
    out). True paginated isolation would require fetching all pages and
    paging client-side; PR #4 trades that for simplicity.
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)
    strategy = isolation_strategy_for(customer)

    page = _int_query(request, "page", default=1, minimum=1)
    limit = _int_query(request, "limit", default=20, minimum=1, maximum=100)
    keyword = request.query_params.get("keyword") or None

    dify_resp = await dify_client.list_datasets(
        dataset_api_key=customer.dify.dataset_api_key,
        page=page,
        limit=limit,
        keyword=keyword,
    )

    raw_data = dify_resp.get("data") or []
    if strategy.is_shared:
        # Filter to this customer's datasets only (soft isolation).
        raw_data = [
            d
            for d in raw_data
            if strategy.dataset_belongs_to(customer.customer_id, d.get("name", ""))
        ]

    entries = [_to_dataset(d, customer, strategy) for d in raw_data]
    envelope = DatasetList(
        data=[Dataset(**e) for e in entries],
        has_more=bool(dify_resp.get("has_more", False)),
        # In shared mode the filtered count is more useful than Dify's
        # workspace-wide count (which would leak other customers' counts).
        total=len(entries) if strategy.is_shared else int(dify_resp.get("total", 0)),
        page=int(dify_resp.get("page", page)),
        limit=int(dify_resp.get("limit", limit)),
    )
    return JSONResponse(content=envelope.model_dump(exclude_none=True))


@router.get("/v1/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, request: Request) -> Any:
    """Fetch a single dataset's metadata by Dify UUID.

    Shared-mode (PR #4 R3): if the dataset belongs to a different customer,
    returns 404 ``dataset_not_found`` — same envelope as a real miss, so
    callers can't distinguish «exists but not yours» from «doesn't exist».
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)
    strategy = isolation_strategy_for(customer)

    dify_resp = await _verify_dataset_ownership(
        dify_client, customer, strategy, dataset_id
    )
    return JSONResponse(content=_to_dataset(dify_resp, customer, strategy))


@router.post("/v1/datasets/{dataset_id}/retrieve")
async def retrieve_dataset(
    dataset_id: str, request: Request, body: DatasetRetrieveRequest
) -> Any:
    """Pure-retrieval channel (hit-testing) — return top-k chunks for a query.

    Shared-mode: ownership check first; cross-customer access → 404.
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)
    strategy = isolation_strategy_for(customer)

    if strategy.is_shared:
        # One extra Dify call in shared mode to verify ownership before
        # forwarding the retrieve. Dedicated mode skips this (workspace IS
        # the customer, no cross-tenant possible).
        await _verify_dataset_ownership(dify_client, customer, strategy, dataset_id)

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

    Idempotent: returns 200 whether or not the dataset existed (Dify 404 →
    treated as already-deleted in the client). Shared-mode (PR #4 R3):
    cross-customer delete returns 404 ``dataset_not_found`` to avoid
    revealing the dataset belongs to someone else.
    """
    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)
    strategy = isolation_strategy_for(customer)

    if strategy.is_shared:
        # Verify ownership before delete. If the dataset doesn't exist in
        # Dify, the get_dataset call inside _verify will surface a 404
        # naturally (UpstreamClientError 404 from PR #3 review-3). For a
        # cross-customer dataset, we raise UnknownDatasetError ourselves.
        try:
            await _verify_dataset_ownership(
                dify_client, customer, strategy, dataset_id
            )
        except UnknownDatasetError:
            raise
        # else fall through to delete

    await dify_client.delete_dataset(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
    )
    logger.info("datasets.deleted", dataset_id=dataset_id, mode=customer.dify.mode)
    return JSONResponse(content={"id": dataset_id, "deleted": True})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dataset(
    raw: dict[str, Any],
    customer: CustomerEntry,
    strategy: IsolationStrategy,
) -> dict[str, Any]:
    """Shape a Dify dataset object into the gateway's surfaced fields.

    In shared mode the Dify ``name`` carries the ``{customer_id}__`` prefix
    used for soft isolation; we strip it before returning so the client
    sees the same name they sent on create. If the dataset somehow lacks
    the prefix (shouldn't happen for own datasets, but defensive), we fall
    back to the raw name rather than dropping the entry.
    """
    dify_name = raw.get("name", "")
    customer_facing_name = (
        strategy.dataset_name_from_dify(customer.customer_id, dify_name) or dify_name
    )
    return {
        "id": raw.get("id", ""),
        "name": customer_facing_name,
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
