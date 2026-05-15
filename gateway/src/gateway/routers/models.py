"""``/v1/models`` router — list models permitted for the authenticated customer.

OpenAI's ``owned_by`` field identifies *who published the model*, not who
has access to it. We follow this convention literally:

    gpt-4               -> "openai"        (OpenAI published it)
    Llama-3-70b-chat    -> "meta-llama"    (Meta published it)
    Qwen3.6-35B         -> "Qwen"          (Alibaba/Qwen team published it)
    bge-m3              -> "BAAI"          (Beijing Academy of AI published it)
    customer fine-tune  -> "org-<customer>" (the org that fine-tuned)

Default fallback is the gateway identifier (``"ai-sdk-gateway"``) for cases
where the upstream publisher is unknown or the model is gateway-internal.

Tenant identity (who is *renting* access) is **never** in ``owned_by`` —
it belongs to the request side: SDK key auth, log context, metrics labels.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from gateway.registry import CustomerEntry
from gateway.schemas import ModelInfo, ModelList

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request) -> ModelList:
    customer: CustomerEntry = request.state.customer
    return ModelList(
        data=[ModelInfo(id=m.id, owned_by=m.owner) for m in customer.models]
    )
