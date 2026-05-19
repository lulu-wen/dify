"""Tests for shared-Dify deployment mode (PR #4).

What's verified:
    * Registry: ``dify.mode`` flag validated, ``shared_embedding_model``
      required in shared mode, cross-customer ``base_url`` consistency.
    * Mode helper: ``IsolationStrategy`` round-trip for App and Dataset names.
    * Datasets router: name prefix on create, list filter, ownership 404
      on get / retrieve / delete.
    * Files router: dataset ownership inherit before upload / list / delete.
    * R5 shared embedding: workspace-global constraint enforced.

Tests build one-off registries with mode='shared' rather than using the
shared fixture (which is dedicated). The fake Dify client returns dataset
metadata with names mirroring what was sent — this lets tests assert the
prefix round-trip.
"""

from __future__ import annotations

import io

import httpx
import pytest
from fastapi import FastAPI

from gateway.config import Settings
from gateway.dify.client import DifyClient
from gateway.main import create_app
from gateway.mode import (
    DedicatedStrategy,
    SharedStrategy,
    isolation_strategy_for,
)
from gateway.registry import (
    CustomerEntry,
    CustomerRegistry,
    DifyConnection,
    EmbeddingModelEntry,
    ModelEntry,
    SharedEmbeddingModel,
)
from tests.conftest import FakeDifyClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shared_customer(
    *,
    customer_id: str = "tenant-a",
    sdk_key: str = "bsa_tenant_a",
    base_url: str = "http://dify-shared.test",
    shared_emb: SharedEmbeddingModel | None = None,
) -> CustomerEntry:
    """Build a shared-mode customer with sensible defaults."""
    if shared_emb is None:
        shared_emb = SharedEmbeddingModel(
            name="bge-m3",
            provider="langgenius/openai_api_compatible/openai_api_compatible",
        )
    return CustomerEntry(
        sdk_key=sdk_key,
        customer_id=customer_id,
        dify=DifyConnection(
            base_url=base_url,
            console_email="admin@x",
            console_password="pw",
            dataset_api_key="ds-shared-key",
            mode="shared",
            shared_embedding_model=shared_emb,
        ),
        models=[ModelEntry(id="m1", provider="prov", name="n")],
        embedding_models=[
            EmbeddingModelEntry(
                id=f"per-customer-emb-{customer_id}",
                name=f"upstream-emb-{customer_id}",
                owner="X",
                endpoint_url="http://embed.test/v1",
                provider="langgenius/openai_api_compatible/openai_api_compatible",
            )
        ],
    )


def _app_with(customer: CustomerEntry, fake: FakeDifyClient) -> FastAPI:
    settings = Settings(registry_path="unused.yaml", log_json=False)
    registry = CustomerRegistry.from_entries([customer])
    application = create_app(settings=settings, registry=registry)

    def factory(_: CustomerEntry) -> DifyClient:  # type: ignore[return-value]
        return fake  # type: ignore[return-value]

    application.state.dify_client_factory = factory
    application.state.app_manager._client_factory = factory
    return application


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------


class TestRegistryModeFlag:
    def test_default_mode_is_dedicated(self) -> None:
        """Existing PR #1-#3 registries (no mode field) default to dedicated."""
        conn = DifyConnection(
            base_url="http://x",
            console_email="a@b",
            console_password="p",
            dataset_api_key="d",
        )
        assert conn.mode == "dedicated"
        assert conn.shared_embedding_model is None

    def test_shared_mode_without_embedding_rejected(self) -> None:
        """``dify.shared_embedding_model`` is mandatory when mode='shared'."""
        with pytest.raises(ValueError, match="shared_embedding_model is required"):
            DifyConnection(
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
                mode="shared",
            )

    def test_shared_mode_with_embedding_accepted(self) -> None:
        conn = DifyConnection(
            base_url="http://x",
            console_email="a@b",
            console_password="p",
            dataset_api_key="d",
            mode="shared",
            shared_embedding_model=SharedEmbeddingModel(name="bge-m3", provider="prov"),
        )
        assert conn.mode == "shared"
        assert conn.shared_embedding_model is not None
        assert conn.shared_embedding_model.name == "bge-m3"

    def test_unknown_mode_rejected(self) -> None:
        """Typo defence — Pydantic Literal stops bad values cold."""
        with pytest.raises(ValueError):
            DifyConnection(  # type: ignore[call-arg]
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
                mode="shred",  # typo
            )


class TestRegistryCrossCustomerConsistency:
    """Customers pointing at the same Dify must agree on mode + shared model."""

    def test_mixed_mode_on_same_base_url_rejected(self) -> None:
        """One customer dedicated + another shared on same Dify → ValueError.

        Without this check, customer A would create datasets with raw names
        and customer B would prefix them — recipe for silent collisions and
        cross-customer access in shared customer's listing.
        """
        ded = CustomerEntry(
            sdk_key="bsa_a",
            customer_id="a",
            dify=DifyConnection(
                base_url="http://shared.test",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
                # default mode=dedicated
            ),
            models=[ModelEntry(id="m1", provider="p", name="n")],
        )
        shared = _shared_customer(customer_id="b", sdk_key="bsa_b", base_url="http://shared.test")
        with pytest.raises(ValueError, match="disagree on isolation mode"):
            CustomerRegistry.from_entries([ded, shared])

    def test_different_base_urls_can_have_different_modes(self) -> None:
        """Two truly-separate Dify deployments can use different modes."""
        ded = CustomerEntry(
            sdk_key="bsa_a",
            customer_id="a",
            dify=DifyConnection(
                base_url="http://dify-a.test",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
            ),
            models=[ModelEntry(id="m1", provider="p", name="n")],
        )
        shared = _shared_customer(customer_id="b", sdk_key="bsa_b", base_url="http://dify-b.test")
        # Different base_urls → no consistency conflict
        reg = CustomerRegistry.from_entries([ded, shared])
        assert len(reg) == 2

    def test_shared_customers_disagreeing_on_embedding_rejected(self) -> None:
        """Workspace has one embedding model — two customers can't disagree."""
        a = _shared_customer(
            customer_id="a",
            sdk_key="bsa_a",
            shared_emb=SharedEmbeddingModel(name="bge-m3", provider="provX"),
        )
        b = _shared_customer(
            customer_id="b",
            sdk_key="bsa_b",
            shared_emb=SharedEmbeddingModel(name="text-embedding-3-large", provider="provY"),
        )
        with pytest.raises(ValueError, match="disagree on shared_embedding_model"):
            CustomerRegistry.from_entries([a, b])

    def test_shared_customers_agreeing_on_embedding_accepted(self) -> None:
        a = _shared_customer(customer_id="a", sdk_key="bsa_a")
        b = _shared_customer(customer_id="b", sdk_key="bsa_b")
        reg = CustomerRegistry.from_entries([a, b])
        assert len(reg) == 2


# ---------------------------------------------------------------------------
# IsolationStrategy round-trip
# ---------------------------------------------------------------------------


class TestIsolationStrategy:
    """Strategy methods are pure functions; verify the round-trip directly."""

    def test_dedicated_passthrough(self) -> None:
        s = DedicatedStrategy()
        assert not s.is_shared
        assert s.app_name("tenant-a", "model-x") == "model-x"
        assert s.dataset_name_to_dify("tenant-a", "kb") == "kb"
        assert s.dataset_name_from_dify("tenant-a", "kb") == "kb"
        assert s.dataset_belongs_to("tenant-a", "kb") is True

    def test_shared_prefixes_and_strips(self) -> None:
        s = SharedStrategy()
        assert s.is_shared
        assert s.app_name("tenant-a", "gemma-3n-e4b") == "tenant-a-gemma-3n-e4b"
        assert s.dataset_name_to_dify("tenant-a", "rsrp-manuals") == "tenant-a__rsrp-manuals"
        assert s.dataset_name_from_dify("tenant-a", "tenant-a__rsrp-manuals") == "rsrp-manuals"

    def test_shared_rejects_cross_customer_dataset_name(self) -> None:
        """Customer A trying to interpret customer B's dataset → None / False."""
        s = SharedStrategy()
        assert s.dataset_name_from_dify("tenant-a", "tenant-b__kb") is None
        assert s.dataset_belongs_to("tenant-a", "tenant-b__kb") is False

    def test_shared_strategy_for_customer(self) -> None:
        """Helper picks the right strategy from a CustomerEntry."""
        shared = _shared_customer()
        assert isolation_strategy_for(shared).is_shared
        # Default-mode customer → dedicated
        ded = CustomerEntry(
            sdk_key="bsa_x",
            customer_id="x",
            dify=DifyConnection(
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
            ),
            models=[ModelEntry(id="m1", provider="p", name="n")],
        )
        assert not isolation_strategy_for(ded).is_shared


# ---------------------------------------------------------------------------
# Datasets router — shared-mode flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_create_dataset_prefixes_name(
    fake_dify: FakeDifyClient,
) -> None:
    """Customer sends ``name="rsrp"`` → Dify receives ``tenant-a__rsrp``."""
    app = _app_with(_shared_customer(), fake_dify)
    # Fake echoes whatever Dify saw to simulate Dify's response shape.
    fake_dify.dataset_create_response = {
        "id": "ds-uuid-1",
        "name": "tenant-a__rsrp",
        "description": "",
        "indexing_technique": "high_quality",
        "embedding_model": "bge-m3",
        "embedding_model_provider": "langgenius/openai_api_compatible/openai_api_compatible",
        "document_count": 0,
        "word_count": 0,
        "created_at": 1700000000,
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            json={"name": "rsrp"},
        )

    assert r.status_code == 200
    body = r.json()
    # Client sees their original name back (prefix stripped on response).
    assert body["name"] == "rsrp"

    sent = fake_dify.calls["dataset_create"][0]["payload"]
    # Dify saw the prefixed name.
    assert sent["name"] == "tenant-a__rsrp"
    # Shared embedding model wins over any per-customer entry.
    assert sent["embedding_model"] == "bge-m3"


@pytest.mark.asyncio
async def test_shared_create_rejects_embedding_mismatch(
    fake_dify: FakeDifyClient,
) -> None:
    """Client passes embedding_model that doesn't match workspace → 400."""
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            json={"name": "kb", "embedding_model": "some-other-embedding"},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["param"] == "embedding_model"
    assert "shared-mode workspace" in body["error"]["message"]
    # Critical: no Dify call.
    assert fake_dify.calls["dataset_create"] == []


@pytest.mark.asyncio
async def test_shared_create_omitted_embedding_uses_workspace_default(
    fake_dify: FakeDifyClient,
) -> None:
    """Client doesn't specify embedding_model → workspace default applies."""
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            json={"name": "kb"},
        )
    assert r.status_code == 200
    payload = fake_dify.calls["dataset_create"][0]["payload"]
    assert payload["embedding_model"] == "bge-m3"
    assert (
        payload["embedding_model_provider"]
        == "langgenius/openai_api_compatible/openai_api_compatible"
    )


@pytest.mark.asyncio
async def test_shared_list_filters_other_customers_datasets(
    fake_dify: FakeDifyClient,
) -> None:
    """Dify returns mixed datasets from many customers → gateway filters
    to only this customer's, names stripped."""
    fake_dify.dataset_list_response = {
        "data": [
            {"id": "u1", "name": "tenant-a__kb-1", "document_count": 3, "word_count": 0},
            {"id": "u2", "name": "tenant-b__kb-1", "document_count": 5, "word_count": 0},
            {"id": "u3", "name": "tenant-a__kb-2", "document_count": 0, "word_count": 0},
            {"id": "u4", "name": "no-prefix-dataset", "document_count": 0, "word_count": 0},
        ],
        "has_more": False,
        "total": 4,
        "page": 1,
        "limit": 20,
    }
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_tenant_a"},
        )

    assert r.status_code == 200
    body = r.json()
    # Only tenant-a's datasets, with prefix stripped.
    assert [d["id"] for d in body["data"]] == ["u1", "u3"]
    assert [d["name"] for d in body["data"]] == ["kb-1", "kb-2"]
    # Total reflects the filtered count, not Dify's workspace-wide.
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_shared_get_cross_customer_returns_404(
    fake_dify: FakeDifyClient,
) -> None:
    """Customer A asks for customer B's dataset UUID → 404 dataset_not_found.

    The envelope must be IDENTICAL to a real miss — no existence leak."""
    fake_dify.dataset_get_response = {
        "id": "ds-uuid-B",
        "name": "tenant-b__kb-1",
        "indexing_technique": "high_quality",
        "embedding_model": "bge-m3",
        "embedding_model_provider": "x",
        "document_count": 0,
        "word_count": 0,
    }
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/datasets/ds-uuid-B",
            headers={"Authorization": "Bearer bsa_tenant_a"},
        )

    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "dataset_not_found"
    # No information about who actually owns it.
    assert "tenant-b" not in body["error"]["message"]


@pytest.mark.asyncio
async def test_shared_get_own_dataset_succeeds_with_stripped_name(
    fake_dify: FakeDifyClient,
) -> None:
    fake_dify.dataset_get_response = {
        "id": "ds-uuid-A",
        "name": "tenant-a__rsrp",
        "indexing_technique": "high_quality",
        "embedding_model": "bge-m3",
        "embedding_model_provider": "x",
        "document_count": 7,
        "word_count": 1000,
    }
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/datasets/ds-uuid-A",
            headers={"Authorization": "Bearer bsa_tenant_a"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "ds-uuid-A"
    assert body["name"] == "rsrp"


@pytest.mark.asyncio
async def test_shared_delete_cross_customer_returns_404(
    fake_dify: FakeDifyClient,
) -> None:
    """Delete check must happen before the actual delete fires."""
    fake_dify.dataset_get_response = {
        "id": "ds-uuid-B",
        "name": "tenant-b__kb-1",
    }
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.delete(
            "/v1/datasets/ds-uuid-B",
            headers={"Authorization": "Bearer bsa_tenant_a"},
        )

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "dataset_not_found"
    # The actual delete must NOT have been called (only the ownership get).
    assert fake_dify.calls["dataset_delete"] == []


@pytest.mark.asyncio
async def test_shared_retrieve_cross_customer_returns_404(
    fake_dify: FakeDifyClient,
) -> None:
    fake_dify.dataset_get_response = {
        "id": "ds-uuid-B",
        "name": "tenant-b__kb-1",
    }
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/datasets/ds-uuid-B/retrieve",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            json={"query": "secret"},
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "dataset_not_found"
    assert fake_dify.calls["dataset_retrieve"] == []


# ---------------------------------------------------------------------------
# Files router — shared-mode ownership inherit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_upload_to_other_customers_dataset_returns_404(
    fake_dify: FakeDifyClient,
) -> None:
    """Customer A guesses/learns customer B's dataset UUID, tries to upload.
    Gateway must refuse before touching Dify's create-by-file endpoint."""
    fake_dify.dataset_get_response = {
        "id": "ds-uuid-B",
        "name": "tenant-b__kb-1",
    }
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
            data={"dataset_id": "ds-uuid-B"},
        )

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "dataset_not_found"
    # Critical: file never reached Dify.
    assert fake_dify.calls["doc_upload"] == []


@pytest.mark.asyncio
async def test_shared_upload_to_own_dataset_succeeds(
    fake_dify: FakeDifyClient,
) -> None:
    """Same flow, this time the dataset belongs to the caller — upload goes
    through."""
    fake_dify.dataset_get_response = {
        "id": "ds-uuid-A",
        "name": "tenant-a__rsrp",
    }
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post(
            "/v1/files",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
            data={"dataset_id": "ds-uuid-A"},
        )

    assert r.status_code == 200
    assert fake_dify.calls["doc_upload"]


@pytest.mark.asyncio
async def test_shared_list_files_cross_customer_returns_404(
    fake_dify: FakeDifyClient,
) -> None:
    fake_dify.dataset_get_response = {"id": "ds-B", "name": "tenant-b__kb"}
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get(
            "/v1/files?dataset_id=ds-B",
            headers={"Authorization": "Bearer bsa_tenant_a"},
        )
    assert r.status_code == 404
    assert fake_dify.calls["doc_list"] == []


@pytest.mark.asyncio
async def test_shared_delete_file_cross_customer_returns_404(
    fake_dify: FakeDifyClient,
) -> None:
    fake_dify.dataset_get_response = {"id": "ds-B", "name": "tenant-b__kb"}
    app = _app_with(_shared_customer(), fake_dify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.delete(
            "/v1/files/doc-1?dataset_id=ds-B",
            headers={"Authorization": "Bearer bsa_tenant_a"},
        )
    assert r.status_code == 404
    assert fake_dify.calls["doc_delete"] == []


# ---------------------------------------------------------------------------
# Dedicated mode unchanged regression (sanity)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review #1 regression tests
# ---------------------------------------------------------------------------


class TestReviewFix_CustomerIdSlug:
    """Codex review-1 P1: customer_id must enforce slug pattern.

    Without it, ``acme`` could match datasets owned by ``acme__beta``
    because both start with ``acme__``. Slug pattern (no underscores)
    makes the prefix unambiguous.
    """

    def test_customer_id_with_underscore_rejected(self) -> None:
        with pytest.raises(ValueError):
            CustomerEntry(
                sdk_key="bsa_x",
                customer_id="acme_beta",  # underscore not allowed
                dify=DifyConnection(
                    base_url="http://x",
                    console_email="a@b",
                    console_password="p",
                    dataset_api_key="d",
                ),
                models=[ModelEntry(id="m", provider="p", name="n")],
            )

    def test_customer_id_with_double_underscore_rejected(self) -> None:
        """The exact attack codex flagged: ``acme__beta`` would
        substring-match ``acme`` as owner."""
        with pytest.raises(ValueError):
            CustomerEntry(
                sdk_key="bsa_x",
                customer_id="acme__beta",
                dify=DifyConnection(
                    base_url="http://x",
                    console_email="a@b",
                    console_password="p",
                    dataset_api_key="d",
                ),
                models=[ModelEntry(id="m", provider="p", name="n")],
            )

    def test_customer_id_uppercase_rejected(self) -> None:
        with pytest.raises(ValueError):
            CustomerEntry(
                sdk_key="bsa_x",
                customer_id="Acme",
                dify=DifyConnection(
                    base_url="http://x",
                    console_email="a@b",
                    console_password="p",
                    dataset_api_key="d",
                ),
                models=[ModelEntry(id="m", provider="p", name="n")],
            )

    def test_customer_id_hyphen_lowercase_accepted(self) -> None:
        entry = CustomerEntry(
            sdk_key="bsa_x",
            customer_id="tenant-a-1",
            dify=DifyConnection(
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
            ),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        assert entry.customer_id == "tenant-a-1"


class TestReviewFix_DatasetNotFoundNormalization:
    """Codex review-1 P1: missing UUID and foreign UUID must produce the
    same error envelope so a caller can't distinguish them by code."""

    @pytest.mark.asyncio
    async def test_shared_get_missing_uuid_returns_dataset_not_found(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Dify returns 404 → gateway must rewrite to ``dataset_not_found``
        (same code as the foreign-UUID case)."""
        from gateway.errors import UpstreamClientError

        fake_dify.dataset_error = UpstreamClientError(
            "Dify rejected request (HTTP 404): dataset not found",
            upstream_status=404,
        )
        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.get(
                "/v1/datasets/totally-fake-uuid",
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )
        assert r.status_code == 404
        body = r.json()
        # Critical: same code as foreign-UUID case (test_shared_get_cross_customer_returns_404)
        assert body["error"]["code"] == "dataset_not_found"

    @pytest.mark.asyncio
    async def test_shared_file_upload_missing_dataset_returns_dataset_not_found(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Same normalization in the files ownership helper."""
        from gateway.errors import UpstreamClientError

        fake_dify.dataset_error = UpstreamClientError(
            "Dify rejected request (HTTP 404): dataset not found",
            upstream_status=404,
        )
        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/files",
                headers={"Authorization": "Bearer bsa_tenant_a"},
                files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
                data={"dataset_id": "missing-uuid"},
            )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "dataset_not_found"
        assert fake_dify.calls["doc_upload"] == []

    @pytest.mark.asyncio
    async def test_dedicated_get_missing_uuid_keeps_upstream_envelope(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """Regression: in dedicated mode, the 404 from Dify must surface
        as its original ``upstream_invalid_request`` (no normalization).
        Dedicated customers only see their own datasets anyway, so leaking
        existence-vs-not is moot."""
        from gateway.errors import UpstreamClientError

        fake_dify.dataset_error = UpstreamClientError(
            "Dify rejected request (HTTP 404): dataset not found",
            upstream_status=404,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.get(
                "/v1/datasets/missing",
                headers={"Authorization": "Bearer bsa_test_a"},
            )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "upstream_invalid_request"


class TestReviewFix_SharedListPagination:
    """Codex review-1 P2: shared list must walk all Dify pages and
    paginate client-side so customers see consistent results regardless
    of where their datasets fall in Dify's workspace-wide pagination."""

    @pytest.mark.asyncio
    async def test_shared_list_walks_multiple_dify_pages(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Tenant A's datasets live on Dify's page 2; tenant A asking for
        gateway page=1, limit=20 should still see them."""
        page_1 = {
            "data": [
                # All page 1 items belong to tenant-b
                {"id": f"u-b-{i}", "name": f"tenant-b__kb-{i}"}
                for i in range(100)
            ],
            "has_more": True,
            "total": 150,
            "limit": 100,
            "page": 1,
        }
        page_2 = {
            "data": [
                # Mixed: 30 tenant-a + 20 tenant-b
                *[{"id": f"u-a-{i}", "name": f"tenant-a__kb-{i}"} for i in range(30)],
                *[{"id": f"u-b-x-{i}", "name": f"tenant-b__extra-{i}"} for i in range(20)],
            ],
            "has_more": False,
            "total": 150,
            "limit": 100,
            "page": 2,
        }
        pages = [page_1, page_2]

        async def list_datasets_fake(**kwargs: object) -> dict[str, object]:
            page = int(kwargs.get("page", 1))  # type: ignore[arg-type]
            fake_dify.calls["dataset_list"].append(kwargs)
            return pages[page - 1] if page <= len(pages) else {
                "data": [], "has_more": False, "total": 0, "limit": 100, "page": page,
            }

        fake_dify.list_datasets = list_datasets_fake  # type: ignore[assignment]

        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.get(
                "/v1/datasets?page=1&limit=20",
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )

        assert r.status_code == 200
        body = r.json()
        # 30 tenant-a datasets total, page 1 with limit 20 → 20 items, has_more=True
        assert body["total"] == 30
        assert len(body["data"]) == 20
        assert body["has_more"] is True
        # Names stripped, ids are tenant-a's only
        assert all(d["id"].startswith("u-a-") for d in body["data"])

    @pytest.mark.asyncio
    async def test_shared_list_second_page_shows_remaining(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """page=2 with limit=20 should return the remaining 10 datasets."""
        pages = [
            {
                "data": [{"id": f"u-a-{i}", "name": f"tenant-a__kb-{i}"} for i in range(25)],
                "has_more": False,
                "total": 25,
                "limit": 100,
                "page": 1,
            }
        ]

        async def list_datasets_fake(**kwargs: object) -> dict[str, object]:
            page = int(kwargs.get("page", 1))  # type: ignore[arg-type]
            fake_dify.calls["dataset_list"].append(kwargs)
            return pages[page - 1] if page <= len(pages) else {
                "data": [], "has_more": False, "total": 0, "limit": 100, "page": page,
            }

        fake_dify.list_datasets = list_datasets_fake  # type: ignore[assignment]

        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.get(
                "/v1/datasets?page=2&limit=20",
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )

        body = r.json()
        assert body["total"] == 25
        assert len(body["data"]) == 5  # 25 - 20
        assert body["has_more"] is False


class TestReviewFix_DedicatedRejectsSharedEmbedding:
    """Codex review-1 P2: dedicated mode must NOT accept
    ``shared_embedding_model`` (it would be ignored at request time, but
    the operator should know the field is wrong)."""

    def test_dedicated_with_shared_embedding_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"must not be set when dify.mode='dedicated'"):
            DifyConnection(
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
                shared_embedding_model=SharedEmbeddingModel(name="bge-m3", provider="x"),
            )


@pytest.mark.asyncio
async def test_dedicated_mode_no_prefix_no_filter(
    app: FastAPI, fake_dify: FakeDifyClient
) -> None:
    """Belt-and-braces: confirm the shared changes didn't bleed into the
    default fixture (which is dedicated mode)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
        await cli.post(
            "/v1/datasets",
            headers={"Authorization": "Bearer bsa_test_a"},
            json={"name": "raw-name"},
        )

    payload = fake_dify.calls["dataset_create"][0]["payload"]
    # Dedicated mode: name forwarded as-is, no prefix.
    assert payload["name"] == "raw-name"
