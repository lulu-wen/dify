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
        # App naming preserves legacy ``{customer_id}:{model_id}`` shape
        # (PR #4 review-2: avoid orphaning existing Apps in Dify).
        assert s.app_name("tenant-a", "model-x") == "tenant-a:model-x"
        assert s.dataset_name_to_dify("tenant-a", "kb") == "kb"
        assert s.dataset_name_from_dify("tenant-a", "kb") == "kb"
        assert s.dataset_belongs_to("tenant-a", "kb") is True

    def test_shared_prefixes_and_strips(self) -> None:
        s = SharedStrategy()
        assert s.is_shared
        # Same colon format as dedicated mode (review-2: one App-naming
        # contract across modes). Distinct per customer thanks to the
        # customer_id segment, so no collision in a shared workspace.
        assert s.app_name("tenant-a", "gemma-3n-e4b") == "tenant-a:gemma-3n-e4b"
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
            "/v1/files?dataset_id=ds-uuid-B",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
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
            "/v1/files?dataset_id=ds-uuid-A",
            headers={"Authorization": "Bearer bsa_tenant_a"},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
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
    """Codex review-1 P1 + review-4 P2: customer_id slug pattern is
    enforced ONLY in shared mode. Dedicated mode (PR #1-#3 default)
    accepts any string within the length cap so existing deployments
    don't break on the PR #4 upgrade.
    """

    def _shared_conn(self) -> DifyConnection:
        return DifyConnection(
            base_url="http://x",
            console_email="a@b",
            console_password="p",
            dataset_api_key="d",
            mode="shared",
            shared_embedding_model=SharedEmbeddingModel(name="bge-m3", provider="prov"),
        )

    def _dedicated_conn(self) -> DifyConnection:
        return DifyConnection(
            base_url="http://x",
            console_email="a@b",
            console_password="p",
            dataset_api_key="d",
        )

    # ---- Shared mode: pattern enforced ----

    def test_shared_customer_id_with_underscore_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a valid shared-mode slug"):
            CustomerEntry(
                sdk_key="bsa_x",
                customer_id="acme_beta",
                dify=self._shared_conn(),
                models=[ModelEntry(id="m", provider="p", name="n")],
            )

    def test_shared_customer_id_with_double_underscore_rejected(self) -> None:
        """The exact attack codex flagged: ``acme__beta`` would
        substring-match ``acme`` as owner under shared-mode prefix logic."""
        with pytest.raises(ValueError, match="not a valid shared-mode slug"):
            CustomerEntry(
                sdk_key="bsa_x",
                customer_id="acme__beta",
                dify=self._shared_conn(),
                models=[ModelEntry(id="m", provider="p", name="n")],
            )

    def test_shared_customer_id_uppercase_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a valid shared-mode slug"):
            CustomerEntry(
                sdk_key="bsa_x",
                customer_id="Acme",
                dify=self._shared_conn(),
                models=[ModelEntry(id="m", provider="p", name="n")],
            )

    def test_shared_customer_id_hyphen_lowercase_accepted(self) -> None:
        entry = CustomerEntry(
            sdk_key="bsa_x",
            customer_id="tenant-a-1",
            dify=self._shared_conn(),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        assert entry.customer_id == "tenant-a-1"

    # ---- Dedicated mode: pattern NOT enforced (review-4 P2 regression) ----

    def test_dedicated_customer_id_with_underscore_accepted(self) -> None:
        """Codex review-4 P2: PR #1-#3 deployments may have customer_ids
        like ``acme_prod`` (underscore) from before PR #4. The slug rule
        must NOT apply in dedicated mode — only when shared mode is opted
        in via ``dify.mode: shared``."""
        entry = CustomerEntry(
            sdk_key="bsa_x",
            customer_id="acme_prod",  # underscore — fine in dedicated mode
            dify=self._dedicated_conn(),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        assert entry.customer_id == "acme_prod"

    def test_dedicated_customer_id_uppercase_accepted(self) -> None:
        """Same backward-compat principle: ``Customer_A`` from a PR #1-#3
        registry must keep loading."""
        entry = CustomerEntry(
            sdk_key="bsa_x",
            customer_id="Customer_A",
            dify=self._dedicated_conn(),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        assert entry.customer_id == "Customer_A"


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
                "/v1/files?dataset_id=missing-uuid",
                headers={"Authorization": "Bearer bsa_tenant_a"},
                files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
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


class TestReview2Fix_AppManagerWiresStrategy:
    """Codex review-2 P2: AppManager must call ``strategy.app_name`` so the
    contract has one source of truth, not a hard-coded string in the
    builder."""

    @pytest.mark.asyncio
    async def test_dedicated_app_name_uses_strategy(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """First chat call should trigger an App build whose DSL name
        matches ``auto:{strategy.app_name(...)}``."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            await cli.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bsa_test_a"},
                json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            )
        # Inspect the YAML DSL sent to console_import_app.
        assert fake_dify.calls["import"], "App was not built"
        _session, yaml_content = fake_dify.calls["import"][0]
        # Default fixture customer_id is "test-a", model_id "m1".
        # Strategy returns "test-a:m1" → DSL name "auto:test-a:m1".
        assert "auto:test-a:m1" in yaml_content


class TestReview2Fix_SharedDatasetNameLength:
    """Codex review-2 P2: the gateway must reject names that, once
    prefixed with ``{customer_id}__``, would exceed Dify's 40-char limit."""

    @pytest.mark.asyncio
    async def test_long_prefixed_name_rejected_at_gateway(
        self, fake_dify: FakeDifyClient
    ) -> None:
        # customer_id="tenant-abcdefghij" (16) + "__" (2) + "very-long-kb-name" (17) = 35 OK
        # customer_id="tenant-abcdefghij" + "__" + "a-much-longer-kb-name-here" (26) = 44 > 40
        customer = _shared_customer(
            customer_id="tenant-abcdefghij",
            sdk_key="bsa_long",
            base_url="http://shared.test",
        )
        app = _app_with(customer, fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/datasets",
                headers={"Authorization": "Bearer bsa_long"},
                json={"name": "a-much-longer-kb-name-here"},
            )
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["param"] == "name"
        # Message must include the budget so the operator knows how much
        # they have left after the customer_id prefix.
        assert "40-char limit" in body["error"]["message"]
        # Critical: no Dify call.
        assert fake_dify.calls["dataset_create"] == []

    @pytest.mark.asyncio
    async def test_short_prefixed_name_accepted(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Belt-and-braces: a name that fits after prefix still works."""
        customer = _shared_customer(customer_id="tenant-a")
        app = _app_with(customer, fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/datasets",
                headers={"Authorization": "Bearer bsa_tenant_a"},
                json={"name": "kb"},  # 8+2+2 = 12 chars, well under 40
            )
        assert r.status_code == 200


class TestReview2Fix_OwnershipBeforeFileRead:
    """Codex review-2 P2: shared-mode file upload must verify ownership
    BEFORE reading the request body. Otherwise an attacker who learned a
    foreign UUID can force the gateway to spool large uploads to memory /
    disk only to 404 afterward."""

    @pytest.mark.asyncio
    async def test_upload_to_foreign_dataset_skips_file_read(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """The test asserts the ownership check fired against a
        not-yet-read upload. The fake doesn't read multipart so we
        instead assert no doc_upload was attempted and the 404 came back
        before any Dify file-create call."""
        fake_dify.dataset_get_response = {
            "id": "ds-uuid-B",
            "name": "tenant-b__kb",
        }
        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/files?dataset_id=ds-uuid-B",
                headers={"Authorization": "Bearer bsa_tenant_a"},
                files={"file": ("x.txt", io.BytesIO(b"large payload " * 1000), "text/plain")},
            )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "dataset_not_found"
        # Critical: the upload code path didn't reach Dify's create-by-file.
        assert fake_dify.calls["doc_upload"] == []


class TestReview3Fix_UploadDatasetIdInQuery:
    """Codex review-3 P2: ``dataset_id`` SHOULD live in the query for
    shared-mode pre-flight; review-6 P1 then narrowed this to «query is
    required in shared mode only, dedicated still accepts form» so PR #3
    backward compat is preserved.

    (The original review-3 test that asserted form-only is rejected in
    dedicated mode was removed: that behaviour was the regression
    review-6 fixed. Coverage of «dedicated accepts form» now lives in
    ``TestReview6Fix_FileUploadBackwardCompat``.)
    """

    @pytest.mark.asyncio
    async def test_upload_query_indexing_technique_forwarded(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """``indexing_technique`` is also a query param now (review-3
        consequence: anything outside the multipart body)."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/files?dataset_id=ds-uuid-1&indexing_technique=economy",
                headers={"Authorization": "Bearer bsa_test_a"},
                files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
            )
        assert r.status_code == 200
        assert fake_dify.calls["doc_upload"][0]["indexing_technique"] == "economy"


class TestReview3Fix_BaseUrlNormalization:
    """Codex review-3 P2: trailing slash on base_url must not split
    consistency-check groups. Otherwise mixed-mode configs can sneak
    through by typing one base_url with a trailing slash."""

    def test_trailing_slash_grouped_same(self) -> None:
        """``http://dify`` and ``http://dify/`` MUST be one group so the
        mode-consistency check fires when they disagree."""
        # Both customers on the "same" Dify but with differing base_url
        # spellings AND differing modes — without normalization this
        # would be accepted; the normalization rejects it.
        a = _shared_customer(
            customer_id="a",
            sdk_key="bsa_a",
            base_url="http://dify-shared.test",
        )
        # b uses trailing-slash + dedicated mode
        b = CustomerEntry(
            sdk_key="bsa_b",
            customer_id="b",
            dify=DifyConnection(
                base_url="http://dify-shared.test/",  # trailing slash
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
                # default mode=dedicated
            ),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        with pytest.raises(ValueError, match="disagree on isolation mode"):
            CustomerRegistry.from_entries([a, b])


class TestReview3Fix_SharedCustomerIdLengthBudget:
    """Codex review-3 P2: customer_id too long for shared-mode prefix +
    name budget → reject at registry load, not at first dataset op."""

    def test_overflowing_customer_id_in_shared_mode_rejected(self) -> None:
        """Customer_id 38 chars + ``__`` (2) = 40 chars, leaving 0 for
        the dataset name. The registry must refuse to load this."""
        long_id = "x" * 38  # exactly at the breaking point
        with pytest.raises(ValueError, match="too long for shared mode"):
            CustomerEntry(
                sdk_key="bsa_x",
                customer_id=long_id,
                dify=DifyConnection(
                    base_url="http://x",
                    console_email="a@b",
                    console_password="p",
                    dataset_api_key="d",
                    mode="shared",
                    shared_embedding_model=SharedEmbeddingModel(
                        name="bge-m3", provider="prov"
                    ),
                ),
                models=[ModelEntry(id="m", provider="p", name="n")],
            )

    def test_short_customer_id_in_shared_mode_accepted(self) -> None:
        """Belt-and-braces: a normal-length customer_id still works."""
        entry = CustomerEntry(
            sdk_key="bsa_x",
            customer_id="tenant-a",  # 8 chars, plenty of budget
            dify=DifyConnection(
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
                mode="shared",
                shared_embedding_model=SharedEmbeddingModel(
                    name="bge-m3", provider="prov"
                ),
            ),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        assert entry.customer_id == "tenant-a"

    def test_dedicated_mode_ignores_length_budget(self) -> None:
        """In dedicated mode the prefix doesn't apply, so a long
        customer_id is fine. This is a regression test against
        accidentally tightening dedicated-mode validation."""
        long_id = "x" * 50  # would fail shared, but dedicated is OK
        entry = CustomerEntry(
            sdk_key="bsa_x",
            customer_id=long_id,
            dify=DifyConnection(
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
            ),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        assert entry.customer_id == long_id


class TestReview6Fix_FileUploadBackwardCompat:
    """Codex review-6 P1: PR #3 R3 contract was multipart-form `dataset_id`.
    PR #4 review-3 broke that by moving to query-only. Restore form-field
    fallback for dedicated mode (no security cost) while keeping shared
    mode strictly query-only (the cheap-fail pre-flight matters)."""

    @pytest.mark.asyncio
    async def test_dedicated_upload_with_form_dataset_id_accepted(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """PR #3 contract: dedicated-mode client sends `dataset_id` in the
        multipart form. Must still work after PR #4 — the round-3 move
        to query was a backward-compat break codex flagged."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/files",  # NO query params
                headers={"Authorization": "Bearer bsa_test_a"},
                files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
                data={"dataset_id": "ds-uuid-1", "indexing_technique": "high_quality"},
            )
        assert r.status_code == 200
        sent = fake_dify.calls["doc_upload"][0]
        assert sent["dataset_id"] == "ds-uuid-1"
        assert sent["indexing_technique"] == "high_quality"

    @pytest.mark.asyncio
    async def test_dedicated_upload_with_query_dataset_id_accepted(
        self, app: FastAPI, fake_dify: FakeDifyClient
    ) -> None:
        """Sanity: dedicated mode accepts BOTH query and form (forward
        compat with new clients writing to PR #4 conventions)."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/files?dataset_id=ds-uuid-1",
                headers={"Authorization": "Bearer bsa_test_a"},
                files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
            )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_shared_upload_form_only_dataset_id_rejected(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Shared mode must NOT fall back to form — the whole point of
        moving to query was to enable ownership pre-flight before body
        parse. Form-only dataset_id in shared mode → 400 with explicit
        message naming the shared-mode requirement."""
        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/files",  # NO query
                headers={"Authorization": "Bearer bsa_tenant_a"},
                files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
                data={"dataset_id": "ds-uuid-A"},
            )
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["param"] == "dataset_id"
        assert "shared mode" in body["error"]["message"]


class TestReview6Fix_SharedEmbeddingResolveByCustomerId:
    """Codex review-6 P2: shared-mode embedding resolution must accept
    customer-facing IDs (the ones `/v1/models` advertises) — not only the
    Dify served name. Previously the resolver compared `requested_id`
    directly to `shared.name`, rejecting valid customer-facing aliases."""

    @pytest.mark.asyncio
    async def test_shared_create_accepts_customer_facing_embedding_id(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Customer's `embedding_models` registry maps `id='bge-m3-public'`
        to `name='bge-m3'` (the Dify served name). Shared workspace's
        `shared_embedding_model.name` is also `'bge-m3'`. The customer
        passes their public id; gateway resolves and accepts."""
        customer = _shared_customer(
            customer_id="tenant-a",
            sdk_key="bsa_tenant_a",
            shared_emb=SharedEmbeddingModel(
                name="bge-m3",
                provider="langgenius/openai_api_compatible/openai_api_compatible",
            ),
        )
        # Override embedding_models to have a customer-facing alias that
        # resolves to the workspace's name.
        customer_with_alias = customer.model_copy(
            update={
                "embedding_models": [
                    EmbeddingModelEntry(
                        id="bge-m3-public",  # customer-facing alias
                        name="bge-m3",  # actually the workspace's name
                        owner="BAAI",
                        endpoint_url="http://embed.test/v1",
                        provider="langgenius/openai_api_compatible/openai_api_compatible",
                    )
                ]
            }
        )
        app = _app_with(customer_with_alias, fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/datasets",
                headers={"Authorization": "Bearer bsa_tenant_a"},
                # Customer passes their public alias, NOT the Dify name.
                json={"name": "kb", "embedding_model": "bge-m3-public"},
            )
        assert r.status_code == 200
        payload = fake_dify.calls["dataset_create"][0]["payload"]
        # Wire-level sees the resolved Dify name.
        assert payload["embedding_model"] == "bge-m3"

    @pytest.mark.asyncio
    async def test_shared_create_mismatched_customer_alias_rejected(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Customer's alias resolves to a different name than the
        workspace's shared model — 400 with both the customer's alias
        AND the resolved name in the message for debuggability."""
        customer = _shared_customer(
            shared_emb=SharedEmbeddingModel(name="bge-m3", provider="prov"),
        )
        customer_with_bad_alias = customer.model_copy(
            update={
                "embedding_models": [
                    EmbeddingModelEntry(
                        id="text-embed-large",
                        name="text-embedding-3-large",  # NOT the workspace's
                        owner="OpenAI",
                        endpoint_url="http://embed.test/v1",
                        provider="prov",
                    )
                ]
            }
        )
        app = _app_with(customer_with_bad_alias, fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.post(
                "/v1/datasets",
                headers={"Authorization": "Bearer bsa_tenant_a"},
                json={"name": "kb", "embedding_model": "text-embed-large"},
            )
        assert r.status_code == 400
        msg = r.json()["error"]["message"]
        assert "text-embed-large" in msg
        assert "text-embedding-3-large" in msg  # the resolved name
        assert "bge-m3" in msg  # the workspace's required name


class TestReview8Fix_SharedKeywordOnPublicName:
    """Codex review-8 P2: shared-mode list filtering by ``keyword`` must
    match the customer-facing (post-strip) name — not the prefixed Dify
    name. Otherwise ``keyword=tenant-a`` (the caller's own id) would
    match every owned dataset because they all start with ``tenant-a__``.
    """

    @pytest.mark.asyncio
    async def test_keyword_matches_only_public_name(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Caller has 3 datasets: ``rsrp-handbook``, ``alarm-codes``,
        ``rsrp-faq``. Search ``keyword=rsrp`` → only the two with
        ``rsrp`` in the public name."""
        async def list_datasets_fake(**kwargs: object) -> dict[str, object]:
            fake_dify.calls["dataset_list"].append(kwargs)
            return {
                "data": [
                    {"id": "u1", "name": "tenant-a__rsrp-handbook"},
                    {"id": "u2", "name": "tenant-a__alarm-codes"},
                    {"id": "u3", "name": "tenant-a__rsrp-faq"},
                ],
                "has_more": False,
                "total": 3,
                "limit": 100,
                "page": 1,
            }

        fake_dify.list_datasets = list_datasets_fake  # type: ignore[assignment]

        app = _app_with(_shared_customer(customer_id="tenant-a"), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.get(
                "/v1/datasets?keyword=rsrp",
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )

        body = r.json()
        assert body["total"] == 2
        names = sorted(d["name"] for d in body["data"])
        assert names == ["rsrp-faq", "rsrp-handbook"]

    @pytest.mark.asyncio
    async def test_keyword_matching_customer_id_does_not_match_all(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """The exact attack codex described: customer searches for their
        own id and should NOT match every dataset (none of the public
        names actually contain it)."""
        async def list_datasets_fake(**kwargs: object) -> dict[str, object]:
            fake_dify.calls["dataset_list"].append(kwargs)
            return {
                "data": [
                    {"id": "u1", "name": "tenant-a__handbook"},
                    {"id": "u2", "name": "tenant-a__alarms"},
                ],
                "has_more": False,
                "total": 2,
                "limit": 100,
                "page": 1,
            }

        fake_dify.list_datasets = list_datasets_fake  # type: ignore[assignment]

        app = _app_with(_shared_customer(customer_id="tenant-a"), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.get(
                "/v1/datasets?keyword=tenant-a",  # the customer_id itself
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )

        body = r.json()
        # Public names are "handbook" and "alarms" — neither contains
        # "tenant-a", so the filtered list is empty.
        assert body["total"] == 0
        assert body["data"] == []

    @pytest.mark.asyncio
    async def test_shared_list_does_not_forward_keyword_to_dify(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Critical implementation property: keyword forwarded to Dify
        would match prefixed names and over-include. The gateway must
        pass ``keyword=None`` upstream and filter locally."""
        async def list_datasets_fake(**kwargs: object) -> dict[str, object]:
            fake_dify.calls["dataset_list"].append(kwargs)
            return {"data": [], "has_more": False, "total": 0, "limit": 100, "page": 1}

        fake_dify.list_datasets = list_datasets_fake  # type: ignore[assignment]

        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            await cli.get(
                "/v1/datasets?keyword=anything",
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )

        # The Dify call must NOT carry the caller's keyword.
        sent = fake_dify.calls["dataset_list"][0]
        assert sent.get("keyword") is None


class TestReview8Fix_DedicatedCustomerIdNoLengthCap:
    """Codex review-8 P2: PR #1-#3 deployments may have customer_ids
    longer than 64 chars. The PR #4 max_length=64 field cap was a
    backward-compat regression in dedicated mode (same pattern as
    review-4 with the slug rule). Length is only meaningful in shared
    mode where it interacts with Dify's 40-char dataset prefix budget."""

    def test_long_dedicated_customer_id_accepted(self) -> None:
        """100-char dedicated customer_id loads fine."""
        long_id = "x" * 100
        entry = CustomerEntry(
            sdk_key="bsa_x",
            customer_id=long_id,
            dify=DifyConnection(
                base_url="http://x",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
            ),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        assert entry.customer_id == long_id


class TestReview7Fix_GlobalCustomerIdUniqueness:
    """Codex review-7 P2: customer_id must be globally unique across the
    whole registry — not just within a base_url group (review-6 stopped
    short). AppManager's app cache is keyed by ``(customer_id, model_id)``
    alone, console sessions by ``customer_id``, GC by
    ``find_by_customer_id``. Duplicates ANYWHERE would route one
    deployment's cached App key / session to the other.

    Review-6 had narrower tests asserting that cross-base_url duplicates
    were OK and that dedicated-mode same-base_url duplicates were OK.
    Both were wrong — codex caught them in review-7. Replaced with the
    correct invariant.
    """

    def test_globally_duplicate_customer_id_rejected(self) -> None:
        """Two customers with same customer_id anywhere → reject."""
        a = _shared_customer(customer_id="tenant-a", sdk_key="bsa_a")
        b = _shared_customer(customer_id="tenant-a", sdk_key="bsa_b")
        with pytest.raises(ValueError, match="duplicate customer_id"):
            CustomerRegistry.from_entries([a, b])

    def test_duplicate_customer_id_different_base_url_rejected(self) -> None:
        """Even across different Dify deployments — AppManager cache /
        session keys don't include base_url, so the duplicate would
        cross-pollute."""
        a = _shared_customer(
            customer_id="tenant-a", sdk_key="bsa_a", base_url="http://dify-1.test"
        )
        b = _shared_customer(
            customer_id="tenant-a", sdk_key="bsa_b", base_url="http://dify-2.test"
        )
        with pytest.raises(ValueError, match="duplicate customer_id"):
            CustomerRegistry.from_entries([a, b])

    def test_dedicated_duplicate_customer_id_also_rejected(self) -> None:
        """Same global rule applies in dedicated mode — review-6's
        carve-out for dedicated mode was wrong (cache collision applies
        there too)."""
        a = CustomerEntry(
            sdk_key="bsa_a",
            customer_id="tenant-a",
            dify=DifyConnection(
                base_url="http://dedi-1.test",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
            ),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        b = CustomerEntry(
            sdk_key="bsa_b",
            customer_id="tenant-a",
            dify=DifyConnection(
                base_url="http://dedi-2.test",
                console_email="a@b",
                console_password="p",
                dataset_api_key="d",
            ),
            models=[ModelEntry(id="m", provider="p", name="n")],
        )
        with pytest.raises(ValueError, match="duplicate customer_id"):
            CustomerRegistry.from_entries([a, b])

    def test_distinct_customer_ids_accepted(self) -> None:
        """Sanity: distinct customer_ids on the same base_url + shared
        mode (the originally documented configuration) still works."""
        a = _shared_customer(customer_id="tenant-a", sdk_key="bsa_a")
        b = _shared_customer(customer_id="tenant-b", sdk_key="bsa_b")
        reg = CustomerRegistry.from_entries([a, b])
        assert len(reg) == 2


class TestReview5Fix_IdempotentSharedDelete:
    """Codex review-5 P2: shared-mode DELETE must preserve the idempotent
    contract for already-missing datasets / files. Cross-customer DELETE
    still 404s (don't pretend customer B's data was deleted), but the
    missing-UUID case matches dedicated mode's behaviour.
    """

    @pytest.mark.asyncio
    async def test_shared_delete_missing_dataset_returns_idempotent_success(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """A stale dataset UUID (already gone) → 200 ``deleted: true``,
        matching dedicated mode + the DELETE endpoint's documented
        idempotent contract."""
        from gateway.errors import UpstreamClientError

        fake_dify.dataset_error = UpstreamClientError(
            "Dify rejected request (HTTP 404): dataset not found",
            upstream_status=404,
        )
        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.delete(
                "/v1/datasets/already-gone",
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )

        assert r.status_code == 200
        assert r.json() == {"id": "already-gone", "deleted": True}
        # Crucially: the actual delete must NOT have fired (no point —
        # Dify already returned 404 on the get).
        assert fake_dify.calls["dataset_delete"] == []

    @pytest.mark.asyncio
    async def test_shared_delete_foreign_dataset_still_returns_404(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Regression: foreign UUID must still 404. The review-5 fix
        only relaxes the *missing* case, not the *foreign* case."""
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
        assert fake_dify.calls["dataset_delete"] == []

    @pytest.mark.asyncio
    async def test_shared_delete_file_missing_dataset_returns_idempotent_success(
        self, fake_dify: FakeDifyClient
    ) -> None:
        """Same pattern for ``DELETE /v1/files/{id}``: dataset gone →
        200, matching delete_document's idempotent contract."""
        from gateway.errors import UpstreamClientError

        fake_dify.dataset_error = UpstreamClientError(
            "Dify rejected request (HTTP 404): dataset not found",
            upstream_status=404,
        )
        app = _app_with(_shared_customer(), fake_dify)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cli:
            r = await cli.delete(
                "/v1/files/doc-1?dataset_id=already-gone",
                headers={"Authorization": "Bearer bsa_tenant_a"},
            )

        assert r.status_code == 200
        assert r.json() == {"id": "doc-1", "deleted": True}
        assert fake_dify.calls["doc_delete"] == []


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
