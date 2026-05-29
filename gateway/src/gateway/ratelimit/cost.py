"""Cheap request-cost estimation (Phase 1b).

Edge nodes are bottlenecked by KV-cache memory, not request count, so the
limiter needs a rough *token* cost per request to gate admission. This is
deliberately a chars/4 heuristic, NOT a real tokenizer:

- The goal is preventing OOM, which only needs a conservative upper bound.
- A real tokenizer is a heavy per-model dependency on a cold path; the
  accuracy it buys doesn't change the admit/reject decision in practice.

See the Edge AI Rate Limiting design doc, appendix B.2.
"""

from __future__ import annotations

from gateway.ratelimit.types import RequestCost

# Chars per token for the chars/4 heuristic. English ~4 chars/token; CJK is
# denser (~1-2 chars/token), so chars/4 UNDER-counts CJK input tokens. That
# under-count is acceptable: it only makes the estimate less conservative on
# CJK-heavy prompts, and max_output_tokens (the larger term for short prompts)
# is exact. Revisit if CJK RAG contexts dominate.
_CHARS_PER_TOKEN = 4

# Rough KV-cache bytes per token for the informational ``est_kv_bytes``.
# Order-of-magnitude only (a few KB/token for a 4-8B model); NOT used by the
# admission decision, which compares token_cost against node_token_budget.
_EST_KV_BYTES_PER_TOKEN = 2048


def estimate_cost(
    *,
    input_chars: int,
    max_output_tokens: int,
    model_id: str,
) -> RequestCost:
    """Estimate the token cost of one request.

    ``input_chars`` is the total character count of the prompt/input (the
    caller computes it — chat sums message contents, embeddings measures the
    input text). ``max_output_tokens`` is the worst-case generation length;
    callers pass a configured fallback when the client omits it, and 0 for
    requests that don't generate (embeddings).
    """
    input_tokens = max(0, input_chars) // _CHARS_PER_TOKEN
    token_cost = input_tokens + max(0, max_output_tokens)
    return RequestCost(
        input_tokens=input_tokens,
        max_output_tokens=max(0, max_output_tokens),
        model_id=model_id,
        token_cost=token_cost,
        est_kv_bytes=token_cost * _EST_KV_BYTES_PER_TOKEN,
    )
