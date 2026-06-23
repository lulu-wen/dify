"""``/v1/chat/completions`` hybrid-mode dispatcher (PR #14).

When ``settings.effective_mode == "hybrid"``, this router replaces the
mode-specific chat routers and dispatches each request based on
``body.use_rag``:

* ``use_rag`` is False / missing → forward directly to vLLM via the
  PR #13 thin-proxy path (``chat_thin_proxy.chat_completions_thin_proxy``).
* ``use_rag`` is True → orchestrate through Dify via the PR #1-#12 path
  (``chat.chat_completions``), which runs the App's DSL with RAG.

Entitlement is enforced here so the underlying routers stay
single-purpose:

1. ``customer.rag_enabled`` must be True — otherwise 403 ``not_entitled``.
2. ``body.dataset_ids`` is REJECTED with 400 in v1 (R1 #2) until the
   override is threaded through ``AppManager``. Accepting it without
   wiring would silently retrieve from ALL of the customer's knowledge
   bases regardless of the override — a security-sensitive contract
   violation. A follow-up PR will lift the rejection once the wiring
   exists; the schema field stays declared so SDK / OpenAPI clients
   don't break when that lands.

The dispatcher itself does NOT validate ``body.model`` — the delegated
router handles that against its own model whitelist (chat.py looks at
``customer.models``, thin-proxy does the same).

R1 review fixes baked in:

* #2 — Reject ``body.dataset_ids != None`` at the door so the schema
  promise matches runtime behaviour.
* #3 — Log when a rag_enabled customer omits ``use_rag`` (silent
  thin-proxy is the worst UX for paying tenants).
* #4 — Removed the dead ``if dify is None`` branch since
  CustomerEntry.dify is non-Optional (registry validation prevents
  the state).
* #6 — Emit ``hybrid.rag_dispatch`` info log on the success path so
  dashboards can count RAG attempts independently of downstream
  outcome.
* #9 — Reject foreign dataset_ids on BOTH use_rag branches; previous
  warn-only handling left a cross-tenant probe oracle.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request

from gateway.errors import (
    InvalidRequestError,
    NotEntitledError,
)
from gateway.registry import CustomerEntry
from gateway.routers import chat as chat_router
from gateway.routers import chat_thin_proxy as chat_thin_proxy_router
from gateway.schemas import ChatCompletionRequest

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions_hybrid(
    request: Request, body: ChatCompletionRequest
) -> Any:
    """Hybrid chat completions — dispatch on ``body.use_rag``."""
    customer: CustomerEntry = request.state.customer
    request_id: str = request.state.request_id

    # R1 #2: dataset_ids override is reserved for a follow-up PR. Until the
    # wiring through AppManager lands, reject any non-None value. Same
    # check on BOTH branches (R1 #9) so a use_rag=false caller probing
    # cross-tenant UUIDs gets the same 400, not a quiet warning.
    if body.dataset_ids is not None:
        raise InvalidRequestError(
            "dataset_ids override is not yet supported; omit the field "
            "and the customer's default knowledge_bases will be used",
            param="dataset_ids",
        )

    # Default route: thin-proxy. ``None`` and ``False`` both go here.
    if not body.use_rag:
        # R1 #3: a rag_enabled customer that omits use_rag is most likely
        # running an SDK predating PR #14 — silent thin-proxy means
        # paying customers get plain-LLM answers with zero signal. Warn
        # so operators notice and prod the SDK update.
        if body.use_rag is None and customer.rag_enabled:
            logger.warning(
                "hybrid.use_rag_omitted_for_rag_enabled_customer",
                request_id=request_id,
                customer_id=customer.customer_id,
            )
        return await chat_thin_proxy_router.chat_completions_thin_proxy(
            request, body
        )

    # RAG path.
    _check_rag_entitlement(customer, request_id=request_id)

    # R1 #6: explicit success log on the RAG branch so dashboards can
    # count RAG attempts even when chat.py later raises (UnknownModelError,
    # AppManager build failure, etc.). Without this signal, a metric
    # like "how often is this customer using RAG?" undercounts every
    # rejected attempt.
    logger.info(
        "hybrid.rag_dispatch",
        request_id=request_id,
        customer_id=customer.customer_id,
        model=body.model,
    )
    return await chat_router.chat_completions(request, body)


def _check_rag_entitlement(
    customer: CustomerEntry,
    *,
    request_id: str,
) -> None:
    """Raise the appropriate GatewayError if the RAG path is not allowed.

    Only one gate fires in v1 now that dataset_ids is rejected up-front
    and ``customer.dify`` is non-Optional (registry validation enforces
    it). Kept as a separate function for unit-test seamability.
    """
    if not customer.rag_enabled:
        # Same shape as audio entitlement (PR #13 R2 #3) — 403 with a
        # distinct code lets clients surface the right "upgrade your plan"
        # CTA. NOT 404; the feature exists, you just don't have it.
        raise NotEntitledError(
            "RAG is not enabled for this customer (see registry rag_enabled)"
        )
    # NOTE on R1 #4: the previous version also checked
    # ``if dify is None or not dify.base_url`` here. CustomerEntry.dify
    # is non-Optional and DifyConnection.base_url has min_length=1, so
    # that state is unreachable in production — registry load fails with
    # ValidationError before we get here. Removed to avoid misleading
    # operators about a 503 path that never fires.
