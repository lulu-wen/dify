"""``/v1/models`` router — list models permitted for the authenticated customer.

OpenAI's ``owned_by`` field identifies *who published the model*, not who has
access to it. For our gateway, the published-by identity is the gateway itself
(we surface upstream models like ``qwen3.6-35b`` on behalf of the customer's
Dify + vLLM deployment). Putting ``customer_id`` here would be semantically
wrong and inconsistent with other OpenAI-compatible providers:

    OpenAI            -> "openai" / "system"
    Together AI       -> "togethercomputer"
    Groq              -> "groq"
    Fireworks AI      -> "fireworks-ai"
    vLLM (raw)        -> "vllm"
    AI SDK Gateway    -> "ai-sdk-gateway"  <-- us

Tenant identity belongs in the request side (SDK key, log context, metrics
labels), not in the model object's metadata.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from gateway.registry import CustomerEntry
from gateway.schemas import ModelInfo, ModelList

router = APIRouter()

# Stable identifier for the publisher of the models surfaced by this gateway.
# Change this string when the product is rebranded externally.
GATEWAY_OWNER = "ai-sdk-gateway"


@router.get("/v1/models")
async def list_models(request: Request) -> ModelList:
    customer: CustomerEntry = request.state.customer
    return ModelList(
        data=[ModelInfo(id=m.id, owned_by=GATEWAY_OWNER) for m in customer.models]
    )
