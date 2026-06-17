"""Prometheus ``/metrics`` exposition endpoint (PR #12a).

Pull-based: Prometheus scrapers hit this endpoint on a schedule
(default 15s in the ops stack), we synchronously read the current
state of all module-level metric singletons and return the
text-exposition payload.

No auth on the endpoint itself — that's Prometheus convention. In
production the gateway container's ``8080`` port should be reachable
only from the ops network; an outward-facing reverse proxy must NOT
proxy ``/metrics`` to the internet without an IP allowlist (metric
labels can leak internal state — model names, customer counts).
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from gateway.observability.metrics import render_metrics

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Render current metric state in Prometheus text exposition format."""
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
