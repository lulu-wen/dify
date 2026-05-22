"""``/v1/embeddings`` router — OpenAI-compatible vectorisation.

Proxies the request to the customer's registered embedding endpoint
(typically vLLM in ``--task embed`` mode). Dify is intentionally bypassed:
embeddings have no prompt, no RAG, no orchestration — Dify would only add
latency and complexity.

Why proxy at all (instead of telling clients to call vLLM directly)?
    1. Auth: clients use the same SDK key + Gateway endpoint for chat and
       embeddings; no second credential to manage.
    2. Per-customer routing: same registry decides which embedding service
       a given SDK key may use.
    3. Logging / quota / metrics: all customer traffic flows through one
       choke point.

What we accept / forward (R6 alias precedence + R7 dimensions passthrough):
    * ``input``: string or list-of-strings, forwarded unchanged.
    * ``encoding_format``: ``float`` (default) or ``base64``, forwarded.
    * ``dimensions``: optional truncation hint; forwarded if provided.
    * ``user`` / ``safety_identifier``: deprecation aliases; forward the
      effective value as ``user`` (most upstreams only recognise that name).

What we override:
    * The wire-level ``model`` sent upstream becomes the registry entry's
      ``name`` (in case the customer-facing id differs from the upstream
      served name).
    * The response's ``model`` echoes the customer-facing id, not the
      upstream name. Clients see consistency with what they sent.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.embeddings.client import invoke_embeddings
from gateway.errors import UnknownModelError
from gateway.registry import CustomerEntry, EmbeddingModelEntry
from gateway.schemas import EmbeddingsRequest

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/v1/embeddings")
async def create_embeddings(request: Request, body: EmbeddingsRequest) -> Any:
    customer: CustomerEntry = request.state.customer
    request_id: str = request.state.request_id

    model_entry = _resolve_model(customer, body.model)

    # Build upstream request body. Forward only OpenAI-standard fields;
    # the upstream may accept extras (e.g. ``dimensions``) and will ignore
    # what it doesn't know.
    upstream_body: dict[str, Any] = {
        "model": model_entry.name,
        "input": body.input,
    }
    if body.encoding_format is not None:
        upstream_body["encoding_format"] = body.encoding_format
    if body.dimensions is not None:
        upstream_body["dimensions"] = body.dimensions

    # Dify/vLLM expect ``user``; supply the resolved value (safety_identifier
    # preferred per R6). Falls back to ``<customer_id>:<request_id>`` so the
    # upstream always sees a stable identifier.
    upstream_body["user"] = body.effective_user or f"{customer.customer_id}:{request_id}"

    upstream_response = await invoke_embeddings(
        endpoint_url=model_entry.endpoint_url,
        api_key=model_entry.api_key,
        body=upstream_body,
    )

    # Echo the customer-facing model id in the response (the upstream might
    # have used its own served name — clients should see what they asked for).
    upstream_response["model"] = body.model

    logger.info(
        "embeddings.completed",
        model=body.model,
        upstream_model=model_entry.name,
        input_count=1 if isinstance(body.input, str) else len(body.input),
        prompt_tokens=upstream_response.get("usage", {}).get("prompt_tokens", 0),
    )
    return JSONResponse(content=upstream_response)


def _resolve_model(customer: CustomerEntry, model_id: str) -> EmbeddingModelEntry:
    """Return the embedding model entry or raise UnknownModelError.

    Note:
        We deliberately reuse ``UnknownModelError`` (R7 → HTTP 404 ``model_not_found``)
        so both chat and embedding return the same OpenAI-shaped error envelope
        for the same logical condition.
    """
    entry = customer.find_embedding_model(model_id)
    if entry is None:
        raise UnknownModelError(
            f"embedding model '{model_id}' is not enabled for this customer",
            param="model",
        )
    return entry
