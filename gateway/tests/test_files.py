"""Tests for ``/v1/files`` — knowledge-base document upload + management (PR #3 R3).

Coverage targets:
    * Multipart upload happy path: file binary + dataset_id + indexing_technique
      reach the Dify client correctly; response carries the document id.
    * Validation: missing dataset_id, missing/empty file, empty filename.
    * List documents: dataset_id query param required; pagination forwarded.
    * Delete document: dataset_id query param required; idempotent.
    * Auth boundary: missing Bearer → 401.
    * Dify failure → OpenAI envelope (502 / 504).

Uses the shared ``FakeDifyClient`` (extended in conftest with the three
document methods) so the multipart parsing is exercised end-to-end without
hitting real HTTP.
"""

from __future__ import annotations

import io

import httpx
import pytest
from fastapi import FastAPI

from gateway.errors import DifyTimeoutError, DifyUpstreamError
from tests.conftest import FakeDifyClient

# ---------------------------------------------------------------------------
# Upload (POST /v1/files multipart)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_happy_path(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Multipart upload with all fields populated. Asserts the binary
    reaches the Dify client unchanged (the critical property — multipart
    parsing is exactly where corruption usually creeps in)."""
    file_bytes = b"RSRP=-115 indicates weak signal due to distance or obstructions."

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1&indexing_technique=high_quality",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={
                "file": ("rsrp-guide.txt", io.BytesIO(file_bytes), "text/plain"),
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "doc-uuid-1"
    assert body["object"] == "file"
    assert body["name"] == "default.txt"

    # Verify what reached the Dify client
    sent = fake_dify.calls["doc_upload"][0]
    assert sent["dataset_api_key"] == "ds-x"
    assert sent["dataset_id"] == "ds-uuid-1"
    assert sent["filename"] == "rsrp-guide.txt"
    assert sent["content"] == file_bytes  # exact bytes, no corruption
    assert sent["content_type"].startswith("text/plain")
    assert sent["indexing_technique"] == "high_quality"


@pytest.mark.asyncio
async def test_upload_file_unwraps_dify_document_envelope(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Codex review-1 P1: Dify v1.x wraps create-by-file response as
    ``{"document": {...}, "batch": "..."}``. Earlier code passed the
    outer envelope to ``_to_file`` so clients got ``id=""`` / ``name=""``
    and couldn't poll or delete the file they just uploaded.

    The fix unwraps the document payload first; this test pins both shapes
    (wrapped + unwrapped) so a future regression breaks loudly.
    """
    # Simulate the real Dify response shape (wrapped + batch sibling).
    fake_dify.file_upload_response = {
        "document": {
            "id": "doc-real-uuid",
            "name": "manual.pdf",
            "indexing_status": "waiting",
            "word_count": 0,
            "created_at": 1700000000,
        },
        "batch": "batch-id-1",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={"file": ("manual.pdf", io.BytesIO(b"%PDF-1.4 hi"), "application/pdf")},
        )

    assert r.status_code == 200
    body = r.json()
    # The critical assertions: client gets the actual document id + name,
    # NOT empty strings from the outer envelope.
    assert body["id"] == "doc-real-uuid"
    assert body["name"] == "manual.pdf"
    assert body["object"] == "file"


@pytest.mark.asyncio
async def test_upload_file_defaults_to_high_quality(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """``indexing_technique`` is optional. Default ``high_quality`` is the
    only useful choice for RAG — ``economy`` skips embeddings and breaks
    semantic retrieval. Most customers should never have to set this."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")},
        )

    assert r.status_code == 200
    sent = fake_dify.calls["doc_upload"][0]
    assert sent["indexing_technique"] == "high_quality"


@pytest.mark.asyncio
async def test_upload_file_missing_dataset_id_returns_400(app: FastAPI) -> None:
    """No ``dataset_id`` in query → 400. Without this, gateway can't know
    where to put the document, and Dify would create a stray document in
    nobody's dataset.

    Note: ``dataset_id`` moved from multipart form to query string in
    codex review-3 so ownership can be checked before parsing the body.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")},
        )

    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_upload_empty_file_returns_400(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Zero-byte upload → 400 before reaching Dify. Dify would accept it
    and create a document that vector-indexes to nothing, then a later
    chat query would silently return zero hits — confusing to debug."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
        )

    assert r.status_code == 400
    body = r.json()
    assert body["error"]["param"] == "file"
    # Critical: no Dify call.
    assert fake_dify.calls["doc_upload"] == []


@pytest.mark.asyncio
async def test_upload_rejects_invalid_indexing_technique(app: FastAPI) -> None:
    """Unknown ``indexing_technique`` query value → 400. Defence against
    typos slipping past gateway-side validation into Dify."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1&indexing_technique=bogus",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_upload_requires_auth(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1",
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_dify_failure_returns_502(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.file_error = DifyUpstreamError("Dify create-by-file: HTTP 503")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "dify_upstream_error"


@pytest.mark.asyncio
async def test_upload_dify_timeout_returns_504(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.file_error = DifyTimeoutError("Dify create-by-file timed out")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files?dataset_id=ds-uuid-1",
            headers={"Authorization": "Bearer bsa_test_a"},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "dify_timeout"


# ---------------------------------------------------------------------------
# List (GET /v1/files?dataset_id=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_forwards_dataset_id_and_pagination(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    fake_dify.file_list_response = {
        "data": [
            {
                "id": "doc-1",
                "name": "manual.pdf",
                "indexing_status": "completed",
                "word_count": 12345,
            },
            {
                "id": "doc-2",
                "name": "spec.md",
                "indexing_status": "indexing",
                "word_count": 800,
            },
        ],
        "has_more": False,
        "total": 2,
        "limit": 50,
        "page": 1,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/files?dataset_id=ds-uuid-1&page=1&limit=50&keyword=rsrp",
            headers={"Authorization": "Bearer bsa_test_a"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["total"] == 2
    assert [d["id"] for d in body["data"]] == ["doc-1", "doc-2"]
    assert body["data"][0]["object"] == "file"
    assert body["data"][0]["indexing_status"] == "completed"

    sent = fake_dify.calls["doc_list"][0]
    assert sent["dataset_id"] == "ds-uuid-1"
    assert sent["page"] == 1
    assert sent["limit"] == 50
    assert sent["keyword"] == "rsrp"


@pytest.mark.asyncio
async def test_list_files_missing_dataset_id_returns_400(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/files",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "dataset_id"


@pytest.mark.asyncio
async def test_list_files_requires_auth(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get("/v1/files?dataset_id=ds-uuid-1")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Delete (DELETE /v1/files/{id}?dataset_id=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_file_happy_path(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.delete(
            "/v1/files/doc-1?dataset_id=ds-uuid-1",
            headers={"Authorization": "Bearer bsa_test_a"},
        )

    assert r.status_code == 200
    assert r.json() == {"id": "doc-1", "deleted": True}
    sent = fake_dify.calls["doc_delete"][0]
    assert sent["dataset_id"] == "ds-uuid-1"
    assert sent["document_id"] == "doc-1"


@pytest.mark.asyncio
async def test_delete_file_missing_dataset_id_returns_400(app: FastAPI) -> None:
    """Document ids aren't globally addressable in Dify (they live under
    one dataset). Without dataset_id, gateway can't route to the right
    delete endpoint. 400 with a clear message beats Dify's 404."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.delete(
            "/v1/files/doc-1",
            headers={"Authorization": "Bearer bsa_test_a"},
        )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "dataset_id"


@pytest.mark.asyncio
async def test_delete_file_requires_auth(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.delete("/v1/files/doc-1?dataset_id=ds-uuid-1")
    assert r.status_code == 401
