"""``/v1/files`` router — knowledge-base document upload + management.

Wraps Dify's ``/datasets/{uuid}/document/*`` Service API. Semantically these
are KB documents (will be chunked + vectorised), not OpenAI finetuning files,
but the surface follows OpenAI's Files API naming (``POST /v1/files``,
``id`` + ``object: "file"``) so the standard SDK's ``client.files.*``
helpers work without custom code.

Why a top-level ``/v1/files`` instead of nested ``/v1/datasets/{id}/files``?
    * Matches OpenAI's surface (clients reach for ``client.files.create``).
    * ``dataset_id`` becomes a form field / query param instead of a path
      segment, which keeps the multipart shape clean (no path-vs-form
      mixing on the upload request).

Multipart-upload memory note:
    FastAPI buffers ``UploadFile`` in a ``SpooledTemporaryFile`` (default
    1 MB threshold, then spills to disk). The gateway reads the whole file
    into memory before forwarding to Dify — fine for typical KB docs
    (<100 MB). True request-body streaming is a follow-up if multi-GB docs
    become a customer ask.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# ``Request.form()`` returns Starlette's UploadFile (which FastAPI's
# ``UploadFile`` historically re-exports but may not be ``isinstance``-
# compatible across versions). Use the Starlette one for the runtime
# type check to keep things robust.
from starlette.datastructures import UploadFile

from gateway.dify.client import DifyClient
from gateway.errors import InvalidRequestError, UnknownDatasetError, UpstreamClientError
from gateway.mode import isolation_strategy_for
from gateway.registry import CustomerEntry
from gateway.schemas import File, FileList

logger = structlog.get_logger(__name__)

router = APIRouter()


_VALID_INDEXING_TECHNIQUES = frozenset({"high_quality", "economy"})


@router.post("/v1/files")
async def upload_file(request: Request) -> Any:
    """Upload a document into a knowledge-base dataset.

    Dify chunks + vectorises the document asynchronously; the response
    carries the document id and a current ``indexing_status`` that the
    client can poll via ``GET /v1/files`` if needed.

    Codex review-3 P2: ``dataset_id`` is read from the **query string**
    (not the multipart body) so the ownership check can run BEFORE the
    request body is touched. Earlier versions used a ``Form()`` parameter
    which forced FastAPI to spool the multipart body up-front, defeating
    the cheap-fail goal of the ownership gate. The body is now only
    parsed via ``await request.form()`` *after* ownership is verified.

    Wire format:
        POST /v1/files?dataset_id=<uuid>[&indexing_technique=...]
        Content-Type: multipart/form-data
        body: file=<binary>
    """
    dataset_id = request.query_params.get("dataset_id")
    if not dataset_id:
        raise InvalidRequestError(
            "dataset_id query parameter is required", param="dataset_id"
        )

    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)

    # PR #4 R4 + review-2/3 P2: verify ownership BEFORE any multipart
    # parsing. ``request.form()`` triggers Starlette's multipart parser
    # which spools to disk for large uploads; we want a foreign-UUID
    # rejection to happen before the parser does any work.
    await _verify_dataset_ownership_for_files(dify_client, customer, dataset_id)

    indexing_technique = (
        request.query_params.get("indexing_technique") or "high_quality"
    )
    if indexing_technique not in _VALID_INDEXING_TECHNIQUES:
        raise InvalidRequestError(
            f"indexing_technique must be 'high_quality' or 'economy', "
            f"got '{indexing_technique}'",
            param="indexing_technique",
        )

    # Now safe to parse the body — ownership check passed.
    form = await request.form()
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise InvalidRequestError(
            "multipart field 'file' is required and must be a file upload",
            param="file",
        )
    if not file.filename:
        raise InvalidRequestError(
            "uploaded file must include a filename", param="file"
        )

    content = await file.read()
    if not content:
        raise InvalidRequestError("uploaded file is empty", param="file")

    dify_resp = await dify_client.create_document_by_file(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
        filename=file.filename,
        content=content,
        content_type=file.content_type or "application/octet-stream",
        indexing_technique=indexing_technique,
    )

    # Dify v1.x wraps create-by-file as ``{"document": {...}, "batch": "..."}``;
    # older / non-standard versions return the document fields at the top
    # level. Unwrap so downstream code always sees the document payload
    # (otherwise client gets id="", name="" — codex review-1 P1).
    document_payload = _unwrap_document(dify_resp)

    logger.info(
        "files.uploaded",
        dataset_id=dataset_id,
        filename=file.filename,
        size_bytes=len(content),
        document_id=document_payload.get("id"),
    )

    # Round-trip through the schema so ``object: "file"`` is added and the
    # client sees an envelope identical to entries in ``GET /v1/files``.
    return JSONResponse(
        content=File(**_to_file(document_payload)).model_dump(exclude_none=True)
    )


@router.get("/v1/files")
async def list_files(request: Request) -> Any:
    """List documents in a dataset.

    ``dataset_id`` is a required query param. ``page`` / ``limit`` /
    ``keyword`` are forwarded to Dify.
    """
    dataset_id = request.query_params.get("dataset_id")
    if not dataset_id:
        raise InvalidRequestError(
            "dataset_id query parameter is required", param="dataset_id"
        )

    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)

    await _verify_dataset_ownership_for_files(dify_client, customer, dataset_id)

    page = _int_query(request, "page", default=1, minimum=1)
    limit = _int_query(request, "limit", default=20, minimum=1, maximum=100)
    keyword = request.query_params.get("keyword") or None

    dify_resp = await dify_client.list_documents(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
        page=page,
        limit=limit,
        keyword=keyword,
    )

    entries = [_to_file(d) for d in (dify_resp.get("data") or [])]
    envelope = FileList(
        data=[File(**e) for e in entries],
        has_more=bool(dify_resp.get("has_more", False)),
        total=int(dify_resp.get("total", 0)),
        page=int(dify_resp.get("page", page)),
        limit=int(dify_resp.get("limit", limit)),
    )
    return JSONResponse(content=envelope.model_dump(exclude_none=True))


@router.delete("/v1/files/{file_id}")
async def delete_file(file_id: str, request: Request) -> Any:
    """Delete a document from its dataset.

    ``dataset_id`` is required as a query param because a Dify document id
    isn't globally addressable — it lives under one dataset. Without this,
    we'd need to fetch every dataset and scan, which doesn't scale.

    Idempotent contract (PR #3 R3 + codex review-5 P2): 404 from Dify
    means already-deleted → 200. Shared-mode pre-flight preserves the
    same: a missing dataset returns 200 (cleanup loop friendly), a
    foreign dataset returns 404 (don't acknowledge customer B's data).
    """
    dataset_id = request.query_params.get("dataset_id")
    if not dataset_id:
        raise InvalidRequestError(
            "dataset_id query parameter is required", param="dataset_id"
        )

    customer: CustomerEntry = request.state.customer
    dify_client: DifyClient = request.app.state.dify_client_factory(customer)
    strategy = isolation_strategy_for(customer)

    if strategy.is_shared:
        # Inline the verify so a missing dataset is treated as already-
        # deleted (matches Dify's own 404-on-document semantics + PR #3
        # delete_document idempotency).
        try:
            meta = await dify_client.get_dataset(
                dataset_api_key=customer.dify.dataset_api_key,
                dataset_id=dataset_id,
            )
        except UpstreamClientError as exc:
            if exc.status_code == 404:
                logger.info(
                    "files.deleted",
                    dataset_id=dataset_id,
                    document_id=file_id,
                    status="dataset-missing",
                )
                return JSONResponse(content={"id": file_id, "deleted": True})
            raise
        if not strategy.dataset_belongs_to(
            customer.customer_id, meta.get("name", "")
        ):
            raise UnknownDatasetError(
                f"dataset '{dataset_id}' not found", param="dataset_id"
            )

    await dify_client.delete_document(
        dataset_api_key=customer.dify.dataset_api_key,
        dataset_id=dataset_id,
        document_id=file_id,
    )
    logger.info("files.deleted", dataset_id=dataset_id, document_id=file_id)
    return JSONResponse(content={"id": file_id, "deleted": True})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _verify_dataset_ownership_for_files(
    dify_client: DifyClient,
    customer: CustomerEntry,
    dataset_id: str,
) -> None:
    """In shared mode, ensure the dataset belongs to this customer (PR #4 R4).

    Files are scoped to a dataset, so file-level isolation reduces to
    dataset-level: if the caller owns the dataset, they can act on its
    files. Cross-customer access → 404 dataset_not_found (same envelope
    as a real miss, no existence leak).

    Dedicated mode skips this check (the customer's Dify only has their
    own datasets, by construction).
    """
    strategy = isolation_strategy_for(customer)
    if not strategy.is_shared:
        return
    try:
        meta = await dify_client.get_dataset(
            dataset_api_key=customer.dify.dataset_api_key,
            dataset_id=dataset_id,
        )
    except UpstreamClientError as exc:
        # Codex review-1 P1: normalize missing-vs-foreign to the same
        # envelope so callers can't probe other tenants' UUIDs by error
        # code. See datasets.py `_verify_dataset_ownership` for context.
        if exc.status_code == 404:
            raise UnknownDatasetError(
                f"dataset '{dataset_id}' not found",
                param="dataset_id",
            ) from exc
        raise
    dify_name = meta.get("name", "")
    if not strategy.dataset_belongs_to(customer.customer_id, dify_name):
        raise UnknownDatasetError(
            f"dataset '{dataset_id}' not found",
            param="dataset_id",
        )


def _to_file(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape a Dify document object into the gateway's surfaced fields.

    Dify's document payload is large (data_source_info, doc_type, position,
    archived, ...). Keep the fields a client typically needs and let
    ``extra="allow"`` on ``File`` pass through the rest.
    """
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "indexing_status": raw.get("indexing_status"),
        "word_count": int(raw.get("word_count") or 0),
        "created_at": raw.get("created_at"),
    }


def _unwrap_document(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the document payload from a create-by-file response.

    Dify v1.x wraps the payload as ``{"document": {...}, "batch": "..."}``;
    older versions return the document fields directly. Codex review-1 P1
    flagged the unwrap was missing — the previous code passed the outer
    envelope to ``_to_file`` and clients got ``id=""`` even though the
    document was created.
    """
    inner = raw.get("document")
    if isinstance(inner, dict):
        return inner
    return raw


def _int_query(
    request: Request,
    name: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    """Same helper as datasets router — kept local to avoid a shared utils
    module for two tiny functions. If a third router needs it, hoist then."""
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
