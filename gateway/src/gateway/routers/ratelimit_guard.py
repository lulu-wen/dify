"""Router-side glue for Phase 1b rate limiting (TPM + cost admission).

Keeps the ``gateway.ratelimit`` package FastAPI-free: this module bridges
``request.app.state`` (settings, limiter, quota_store) and the parsed
request body to the pure limiter/quota primitives, and raises the
gateway's domain errors. Used by the chat and embeddings routers.

Why here and not in middleware: TPM and admission need the parsed request
body (token counts), which BaseHTTPMiddleware can't read without consuming
the stream. RPM (no body) stays in middleware; TPM/admission live here,
after FastAPI has parsed the body — and inside the exception handler, so
these helpers can ``raise`` normally.
"""

from __future__ import annotations

from fastapi import Request

from gateway.errors import OverloadError, RateLimitError
from gateway.ratelimit import (
    ActionCode,
    AdmissionGrant,
    RequestCost,
    estimate_cost,
    jittered_retry_after,
)
from gateway.registry import CustomerEntry


def estimate_request_cost(
    request: Request,
    *,
    input_chars: int,
    max_output_tokens: int | None,
    model_id: str,
    has_knowledge_bases: bool = False,
) -> RequestCost:
    """Estimate cost, filling a config fallback when max_output is unset.

    A client omitting ``max_tokens`` would otherwise leave the pre-charge
    unbounded; we substitute ``settings.default_max_output_tokens`` (an
    estimation-only cap — it doesn't change what's sent to Dify/vLLM).

    When ``has_knowledge_bases`` is True, add a RAG allowance of
    ``default_kb_top_k * default_kb_chunk_tokens`` to cover the chunks Dify
    injects into the prompt at retrieval time. AppManager caps Dify's
    actual ``dataset_configs.top_k`` to the same value, so this allowance is
    a real upper bound, not a guess (codex 1b review-5 P2).
    """
    settings = request.app.state.settings
    effective_max = (
        max_output_tokens
        if max_output_tokens is not None and max_output_tokens > 0
        else settings.default_max_output_tokens
    )
    rag_allowance = (
        settings.default_kb_top_k * settings.default_kb_chunk_tokens
        if has_knowledge_bases
        else 0
    )
    return estimate_cost(
        input_chars=input_chars,
        max_output_tokens=effective_max,
        model_id=model_id,
        rag_allowance_tokens=rag_allowance,
    )


def enforce_tpm(request: Request, customer: CustomerEntry, cost: RequestCost) -> None:
    """Meter tokens-per-minute for the customer; raise 429 if over.

    Effective TPM is ``customer.tpm_limit`` or ``settings.default_tpm``.
    ``<= 0`` means unlimited (TPM opt-in) → no check. The advisory action
    is ``REDUCE_MAX_TOKENS``: shrinking the request is the client's most
    direct lever to get under a per-minute token cap.
    """
    settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    tpm = customer.tpm_limit if customer.tpm_limit is not None else settings.default_tpm
    if tpm <= 0:
        return
    limiter = request.app.state.rate_limiter
    decision = limiter.check(
        f"{customer.customer_id}:tpm",
        units_per_min=tpm,
        burst=settings.default_tpm_burst,
        cost=float(cost.token_cost),
    )
    if not decision.allowed:
        raise RateLimitError(
            f"token rate limit exceeded: {tpm} tokens/min for customer "
            f"'{customer.customer_id}' (this request ~{cost.token_cost} tokens)",
            action=ActionCode.REDUCE_MAX_TOKENS,
            retry_after_s=jittered_retry_after(decision.retry_after_s),
        )


def admit(request: Request, customer: CustomerEntry, cost: RequestCost) -> AdmissionGrant:
    """Reserve node budget for the request; raise 503 if the node is full.

    Returns the grant (with ``charge_id``) on success — the caller MUST
    ``settle`` it in a ``finally``. When admission is disabled
    (``rate_limit_enabled=false``) returns a no-charge grant so callers
    can settle unconditionally without branching.
    """
    settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return AdmissionGrant(admitted=True, charge_id=None, action=None)
    quota_store = request.app.state.quota_store
    grant: AdmissionGrant = quota_store.try_admit(tenant=customer.customer_id, cost=cost)
    if not grant.admitted:
        raise OverloadError(
            f"node at capacity: admitting ~{cost.token_cost} tokens would "
            f"exceed the node budget. Retry shortly.",
            action=grant.action,
            retry_after_s=jittered_retry_after(None),
        )
    return grant


def settle(request: Request, grant: AdmissionGrant, *, actual_output_tokens: int) -> None:
    """Release a reservation. Safe to call with a no-charge grant or twice."""
    if grant.charge_id is None:
        return
    request.app.state.quota_store.settle(
        grant.charge_id, actual_output_tokens=actual_output_tokens
    )
