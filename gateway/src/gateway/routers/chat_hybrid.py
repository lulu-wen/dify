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
2. ``customer.dify`` block must be populated — otherwise 503
   ``service_unavailable`` (registry is internally inconsistent for this
   customer — they said they want RAG but have no Dify backend).
3. ``body.dataset_ids`` (when provided) must be a subset of
   ``customer.knowledge_bases`` — otherwise 404
   :class:`UnknownDatasetError`. This is the shared-mode safety boundary
   that prevents customer A from referencing customer B's dataset UUID;
   the 404 envelope is identical to "dataset doesn't exist" so the
   gateway never leaks existence (PR #4 review-7 hotfix pattern).

The dispatcher itself does NOT validate ``body.model`` — the delegated
router handles that against its own model whitelist (chat.py looks at
``customer.models``, thin-proxy does the same).

Per-request dataset override (v1 scope note):

    ``body.dataset_ids`` is **validated** for scope safety, but the v1
    implementation continues to use ``customer.knowledge_bases`` for the
    actual Dify retrieval — the override is currently a no-op beyond
    the security check. A follow-up PR can thread the override through
    ``AppManager`` or via Dify's ``dataset_configs`` extra-body. This
    keeps the schema contract honest now without coupling PR #14 to
    AppManager's internals.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request

from gateway.errors import (
    NotEntitledError,
    ServiceUnavailableError,
    UnknownDatasetError,
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

    # Default route: thin-proxy. ``None`` and ``False`` both go here.
    if not body.use_rag:
        if body.dataset_ids:
            # Client asked for specific datasets but turned off RAG —
            # logically inconsistent. Honour the explicit use_rag=false
            # but tell operators something looks off.
            logger.warning(
                "hybrid.dataset_ids_ignored_use_rag_false",
                request_id=request_id,
                customer_id=customer.customer_id,
                dataset_ids=body.dataset_ids,
            )
        return await chat_thin_proxy_router.chat_completions_thin_proxy(
            request, body
        )

    # RAG path — must clear entitlement + Dify-config + scope checks.
    _check_rag_entitlement(customer, body, request_id=request_id)
    return await chat_router.chat_completions(request, body)


def _check_rag_entitlement(
    customer: CustomerEntry,
    body: ChatCompletionRequest,
    *,
    request_id: str,
) -> None:
    """Raise the appropriate GatewayError if the RAG path is not allowed.

    Three gates, in security-rationale order:

    1. **Per-customer feature flag** (cheapest, blanket): rag_enabled.
    2. **Backend availability** (registry consistency): Dify config.
    3. **Per-request scope** (data isolation): dataset_ids subset.

    All raise distinct error codes so dashboards / SDKs can route
    on the specific failure mode without parsing the message.
    """
    if not customer.rag_enabled:
        # Same shape as audio entitlement (PR #13 R2 #3) — 403 with a
        # distinct code lets clients surface the right "upgrade your plan"
        # CTA. NOT 404; the feature exists, you just don't have it.
        raise NotEntitledError(
            "RAG is not enabled for this customer (see registry rag_enabled)"
        )

    dify = customer.dify
    if dify is None or not dify.base_url:
        # The customer registry promises RAG but doesn't have a Dify
        # backend — that's an internal misconfiguration, not the client's
        # fault. 503 surfaces it to operators (alerting on
        # service_unavailable) without leaking the registry shape.
        logger.error(
            "hybrid.rag_enabled_without_dify_config",
            request_id=request_id,
            customer_id=customer.customer_id,
        )
        raise ServiceUnavailableError(
            "RAG backend is not available for this customer"
        )

    if body.dataset_ids is not None:
        # Per-request scope validation. The set difference is the source
        # of truth — customer.knowledge_bases is the canonical owned-set,
        # populated at registry-load time with the shared-mode tenant
        # prefixing applied (PR #4). Empty list is allowed and means
        # "no retrieval, but still run the Dify DSL" — a legitimate way
        # to A/B compare RAG-on vs RAG-off without changing endpoints.
        owned = set(customer.knowledge_bases)
        requested = set(body.dataset_ids)
        unknown = requested - owned
        if unknown:
            # PR #4 R7 pattern: don't distinguish "exists but not yours"
            # from "doesn't exist". The error message names the unknown
            # ids so legitimate operators can debug their own requests,
            # but the 404 code + envelope shape match every other
            # dataset-not-found case so a cross-tenant probe can't tell
            # the difference.
            raise UnknownDatasetError(
                f"unknown dataset_id(s): {', '.join(sorted(unknown))}"
            )
