"""Shared pytest fixtures for gateway tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI

from gateway.config import Settings
from gateway.dify.client import ConsoleSession, DifyClient
from gateway.main import create_app
from gateway.registry import (
    CustomerEntry,
    CustomerRegistry,
    DifyConnection,
    EmbeddingModelEntry,
    ModelEntry,
)


def make_customer(
    sdk_key: str = "bsa_test_a",
    customer_id: str = "test-a",
    model_ids: tuple[str, ...] = ("m1",),
    embedding_model_ids: tuple[str, ...] = ("emb1",),
    knowledge_bases: list[str] | None = None,
) -> CustomerEntry:
    return CustomerEntry(
        sdk_key=sdk_key,
        customer_id=customer_id,
        dify=DifyConnection(
            base_url="http://dify.test",
            console_email="admin@x",
            console_password="pw",
            dataset_api_key="ds-x",
        ),
        models=[
            ModelEntry(id=mid, provider="prov", name="n", completion_params={})
            for mid in model_ids
        ],
        embedding_models=[
            EmbeddingModelEntry(
                id=eid,
                name=f"upstream-{eid}",
                owner="TestPublisher",
                endpoint_url="http://embed.test/v1",
                api_key="EMPTY",
                dimensions=1024,
                # PR #3 review-2 P2: dataset creation now requires provider.
                # Default fixture sets it so the bulk of tests get a sane
                # baseline; tests that exercise the "provider missing"
                # branch build their own entry.
                provider="langgenius/openai_api_compatible/openai_api_compatible",
            )
            for eid in embedding_model_ids
        ],
        knowledge_bases=knowledge_bases or [],
    )


class FakeDifyClient:
    """Async fake DifyClient: scriptable for chat-messages flows.

    Tests assign:
        * ``blocking_response``: dict returned by ``chat_messages_blocking``.
        * ``streaming_lines``: list of SSE lines yielded by
          ``chat_messages_streaming``.
        * ``console_token``: returned by ``console_login``.
        * ``import_app_ids`` / ``api_key_tokens``: deque of values.
    """

    def __init__(self) -> None:
        self.blocking_response: dict[str, Any] = {
            "id": "msg-1",
            "answer": "default reply",
            "conversation_id": "conv-1",
            "metadata": {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
        }
        self.streaming_lines: list[str] = [
            'data: {"event":"message","answer":"hi"}',
            'data: {"event":"message_end","metadata":{},"conversation_id":"conv-s"}',
        ]
        # Set to an exception instance to simulate a pre-flight failure
        # (e.g. Dify returns 5xx before any chunk is sent).
        self.streaming_pre_flight_error: BaseException | None = None
        self.console_session = ConsoleSession(access_token="acc-1", csrf_token="csrf-1")
        self.import_app_ids = ["app-id-1", "app-id-2", "app-id-3"]
        self.api_key_tokens = ["app-key-1", "app-key-2", "app-key-3"]

        # Dataset (PR #3 R2/R4/R5) scriptable responses. Tests assign whatever
        # shape they want to assert on; defaults are deliberately minimal so a
        # test that doesn't override these still exercises the schema.
        self.dataset_create_response: dict[str, Any] = {
            "id": "ds-uuid-1",
            "name": "default-ds",
            "description": "",
            "indexing_technique": "high_quality",
            "embedding_model": "upstream-emb1",
            "embedding_model_provider": None,
            "document_count": 0,
            "word_count": 0,
            "created_at": 1700000000,
        }
        self.dataset_list_response: dict[str, Any] = {
            "data": [],
            "has_more": False,
            "limit": 20,
            "total": 0,
            "page": 1,
        }
        self.dataset_get_response: dict[str, Any] | None = None
        self.dataset_retrieve_response: dict[str, Any] = {"query": {}, "records": []}
        # File/document responses (R3).
        self.file_upload_response: dict[str, Any] = {
            "id": "doc-uuid-1",
            "name": "default.txt",
            "indexing_status": "waiting",
            "word_count": 0,
            "created_at": 1700000000,
        }
        self.file_list_response: dict[str, Any] = {
            "data": [],
            "has_more": False,
            "limit": 20,
            "total": 0,
            "page": 1,
        }
        # Set to an exception instance to simulate a Dify failure on a given op.
        self.dataset_error: BaseException | None = None
        self.file_error: BaseException | None = None

        self.calls: dict[str, list[Any]] = {
            "blocking": [],
            "streaming": [],
            "login": [],
            "import": [],
            "api_key": [],
            "delete": [],
            "dataset_create": [],
            "dataset_list": [],
            "dataset_get": [],
            "dataset_delete": [],
            "dataset_retrieve": [],
            "doc_upload": [],
            "doc_list": [],
            "doc_delete": [],
        }

    async def chat_messages_blocking(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["blocking"].append(kwargs)
        return self.blocking_response

    @asynccontextmanager
    async def open_chat_stream(self, **kwargs: Any) -> AsyncIterator[AsyncIterator[str]]:
        self.calls["streaming"].append(kwargs)
        if self.streaming_pre_flight_error is not None:
            # Simulate Dify failure during stream open (before any byte yielded).
            raise self.streaming_pre_flight_error

        async def gen() -> AsyncIterator[str]:
            for line in self.streaming_lines:
                yield line

        yield gen()

    async def console_login(self, email: str, password: str) -> ConsoleSession:
        self.calls["login"].append((email, password))
        return self.console_session

    async def console_import_app(self, session: ConsoleSession, yaml_content: str) -> str:
        self.calls["import"].append((session, yaml_content))
        return self.import_app_ids.pop(0)

    async def console_create_app_api_key(self, session: ConsoleSession, app_id: str) -> str:
        self.calls["api_key"].append((session, app_id))
        return self.api_key_tokens.pop(0)

    async def console_delete_app(self, session: ConsoleSession, app_id: str) -> None:
        self.calls["delete"].append((session, app_id))

    # ----- PR #3 dataset methods -----

    async def create_dataset(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["dataset_create"].append(kwargs)
        if self.dataset_error is not None:
            raise self.dataset_error
        return self.dataset_create_response

    async def list_datasets(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["dataset_list"].append(kwargs)
        if self.dataset_error is not None:
            raise self.dataset_error
        return self.dataset_list_response

    async def get_dataset(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["dataset_get"].append(kwargs)
        if self.dataset_error is not None:
            raise self.dataset_error
        if self.dataset_get_response is not None:
            return self.dataset_get_response
        return self.dataset_create_response

    async def delete_dataset(self, **kwargs: Any) -> None:
        self.calls["dataset_delete"].append(kwargs)
        if self.dataset_error is not None:
            raise self.dataset_error

    async def retrieve_dataset(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["dataset_retrieve"].append(kwargs)
        if self.dataset_error is not None:
            raise self.dataset_error
        return self.dataset_retrieve_response

    async def create_document_by_file(self, **kwargs: Any) -> dict[str, Any]:
        # Capture the body separately so tests can inspect bytes received.
        self.calls["doc_upload"].append(kwargs)
        if self.file_error is not None:
            raise self.file_error
        return self.file_upload_response

    async def list_documents(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["doc_list"].append(kwargs)
        if self.file_error is not None:
            raise self.file_error
        return self.file_list_response

    async def delete_document(self, **kwargs: Any) -> None:
        self.calls["doc_delete"].append(kwargs)
        if self.file_error is not None:
            raise self.file_error

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_dify() -> FakeDifyClient:
    return FakeDifyClient()


@pytest.fixture
def registry() -> CustomerRegistry:
    return CustomerRegistry.from_entries([make_customer(model_ids=("m1", "m2"))])


@pytest.fixture
def settings() -> Settings:
    # Defaults are fine; tests don't need a real registry path because the
    # ``create_app`` factory accepts an injected registry.
    return Settings(registry_path="unused.yaml", log_json=False)


@pytest.fixture
def app(
    settings: Settings,
    registry: CustomerRegistry,
    fake_dify: FakeDifyClient,
) -> FastAPI:
    """Build a test FastAPI app with the FakeDifyClient injected."""
    application = create_app(settings=settings, registry=registry)

    # Override the client factory so all customers share the FakeDifyClient.
    def factory(_: CustomerEntry) -> DifyClient:  # type: ignore[return-value]
        return fake_dify  # type: ignore[return-value]

    application.state.dify_client_factory = factory
    application.state.app_manager._client_factory = factory
    return application
