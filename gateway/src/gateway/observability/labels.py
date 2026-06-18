"""Shared label-derivation helpers for Prometheus instrumentation (PR #12a R1).

Centralised here so callers (PrometheusMiddleware, ratelimit_guard's
admission_total emit sites, future routers) all use the same logic.

R1 finding fix: ratelimit_guard's 3 emission sites previously used raw
``request.url.path`` directly, which would explode label cardinality on
path-param routes (``/v1/datasets/{id}/...``). Moving the normaliser here
lets them use ``normalise_route`` the same way the middleware does.
"""

from __future__ import annotations

# Paths that should NOT count toward request-duration metrics. Including
# the empty string covers ``"/".rstrip("/")``.
#
# R1 finding fix: original ``_EXCLUDED_PATHS`` used exact string match,
# so trailing-slash variants (``/health/``) and FastAPI's auto-mounted
# OpenAPI surfaces (``/docs``, ``/openapi.json``, ``/redoc``) leaked into
# the histogram. Now normalise with ``rstrip("/")`` and include the
# OpenAPI paths.
_EXCLUDED_NORMALISED: frozenset[str] = frozenset(
    {
        "",  # matches "/" after rstrip
        "/metrics",
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
)


def is_excluded_path(path: str) -> bool:
    """Return True for paths that should bypass request-duration instrumentation.

    Normalises trailing slashes so ``/health`` and ``/health/`` both match.
    """
    return path.rstrip("/") in _EXCLUDED_NORMALISED


def normalise_route(path: str) -> str:
    """Collapse path parameters so the ``route`` label stays low-cardinality.

    Today the gateway has two path-param-bearing route shapes:
        * ``/v1/datasets/{dataset_id}[/...]``
        * ``/v1/files/{file_id}[/...]``

    Plus nested document/segment IDs under ``/v1/datasets/{id}/documents/{doc_id}/...``
    where every segment past the first ID is itself a UUID that would
    explode cardinality if left raw.

    Strategy: known prefix-based collapse — walk a small list of
    ``(prefix, depth_to_collapse)`` rules. Anything not matching a known
    prefix returns unchanged; PR #12b's ``_normalise_route_strict`` (TBD)
    will additionally collapse heuristic-UUID segments to fail closed for
    unknown future routes.

    Examples::

        /v1/chat/completions                                  -> /v1/chat/completions
        /v1/datasets/abc                                      -> /v1/datasets/:id
        /v1/datasets/abc/documents                            -> /v1/datasets/:id/documents
        /v1/datasets/abc/documents/xyz                        -> /v1/datasets/:id/documents/:id
        /v1/datasets/abc/documents/xyz/segments/p             -> /v1/datasets/:id/documents/:id/segments/:id
        /v1/files/abc                                         -> /v1/files/:id
    """
    # /v1/datasets/{id}/... — collapse positions 3, 5, 7 (UUIDs at odd depths)
    if path.startswith("/v1/datasets/"):
        return _collapse_uuid_positions(path, positions=(3, 5, 7))
    if path.startswith("/v1/files/"):
        return _collapse_uuid_positions(path, positions=(3,))
    return path


def _collapse_uuid_positions(path: str, *, positions: tuple[int, ...]) -> str:
    """Replace the path segment at each of ``positions`` with ``:id``.

    Empty segments and out-of-range positions are left alone. Idempotent
    for already-collapsed paths (``:id`` stays ``:id``).
    """
    parts = path.split("/")
    for i in positions:
        if i < len(parts) and parts[i]:
            parts[i] = ":id"
    return "/".join(parts)


def status_class(status_code: int) -> str:
    """Bucket HTTP status codes into ``Nxx`` form, clamped to known classes.

    R1 finding fix: original ``_status_class`` returned ``f"{code // 100}xx"``
    unconditionally, so a buggy upstream returning 999 produced ``9xx`` and a
    negative code produced ``-1xx``. Clamp to {2xx, 3xx, 4xx, 5xx, other} so
    the label vocabulary stays bounded.
    """
    if 200 <= status_code < 600:
        return f"{status_code // 100}xx"
    return "other"
