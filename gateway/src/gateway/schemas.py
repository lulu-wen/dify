"""OpenAI-compatible Pydantic schemas for request/response bodies.

The gateway exposes the OpenAI Chat Completions surface, with a single deviation:
``choices[0].message.metadata`` carries gateway-specific data (currently
``references`` populated from Dify's ``retriever_resources``). Standard OpenAI
clients ignore unknown fields, but our SDK examples surface this metadata.

Reference:
    * https://platform.openai.com/docs/api-reference/chat
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------- Request ----------


class ChatMessage(BaseModel):
    """A single message in a chat completion request.

    Tool messages are accepted on input but the gateway does not surface tool
    calling outwards in PR#1; they pass through to Dify if the App supports it.
    """

    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """Subset of OpenAI Chat Completions request that the gateway understands.

    Unknown fields are accepted (``extra="allow"``) and forwarded where
    semantically sensible. ``extra_body`` extensions are namespaced so they do
    not collide with future OpenAI additions.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, description="Model id (validated against customer registry)")
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = Field(default=False)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    # Token limit: OpenAI deprecated ``max_tokens`` in favor of ``max_completion_tokens``
    # for reasoning models (o1, o3, GPT-5 thinking). Both are accepted; ``max_completion_tokens``
    # takes precedence when both are set. Downstream (vLLM/Dify) only recognises
    # ``max_tokens``, so the router forwards whichever value was resolved as ``max_tokens``.
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)

    # End-user identifier: OpenAI deprecated ``user`` in favor of ``safety_identifier``.
    # Dify's chat-messages API requires ``user``, so we accept both and forward
    # whichever was provided (preferring ``safety_identifier`` when both set).
    user: str | None = Field(default=None, description="Stable end-user identifier (deprecated alias)")
    safety_identifier: str | None = Field(
        default=None,
        description="OpenAI 2025+ replacement for ``user``; preferred when both are provided",
    )

    # Gateway extensions (kept under ``extra_body`` for client SDK compatibility).
    # The OpenAI Python SDK flattens ``extra_body={"foo":...}`` into top-level
    # JSON fields, so these appear at the request root despite being passed via
    # ``extra_body`` from the caller's perspective.
    conversation_id: str | None = Field(default=None)
    llm_model: str | None = Field(
        default=None,
        description=(
            "Override the model used for app selection. When provided, the "
            "gateway resolves the Dify App via ``(customer, llm_model)``; "
            "otherwise it falls back to the standard ``model`` field."
        ),
    )

    @property
    def effective_max_tokens(self) -> int | None:
        """Resolve the token cap honoring OpenAI's deprecation precedence."""
        return self.max_completion_tokens if self.max_completion_tokens is not None else self.max_tokens

    @property
    def effective_user(self) -> str | None:
        """Resolve the end-user identifier honoring OpenAI's deprecation precedence."""
        return self.safety_identifier if self.safety_identifier is not None else self.user


# ---------- Response (non-streaming) ----------


class Reference(BaseModel):
    """A single retrieved chunk surfaced to the client.

    Sourced from Dify's ``metadata.retriever_resources``.
    """

    model_config = ConfigDict(extra="allow")

    content: str
    score: float | None = None
    document_name: str | None = None
    document_id: str | None = None
    segment_id: str | None = None


class MessageMetadata(BaseModel):
    """Gateway-specific metadata attached to assistant messages.

    Lives at ``choices[0].message.metadata``. Survives ``model_dump()`` from the
    official OpenAI Python SDK because it is part of the parsed model.
    """

    model_config = ConfigDict(extra="allow")

    references: list[Reference] = Field(default_factory=list)
    conversation_id: str | None = None
    request_id: str | None = None


class ChatResponseMessage(BaseModel):
    """Assistant message in a non-streaming response."""

    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"] = "assistant"
    content: str
    metadata: MessageMetadata | None = None


class ChatChoice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 0
    message: ChatResponseMessage
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls"] = "stop"


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible non-streaming response."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatChoice]
    usage: Usage = Field(default_factory=Usage)


# ---------- Streaming chunks ----------


class DeltaMessage(BaseModel):
    """Incremental delta in a streaming response.

    ``reasoning_content`` matches OpenAI's o1-style streaming surface — when a
    reasoning model (Qwen3 ``<think>``, DeepSeek-R1, o1-family) emits its
    thinking trace, those chunks come down with ``delta.reasoning_content``
    populated and ``content`` left null. Once thinking is done and the model
    starts producing the user-facing answer, subsequent chunks carry
    ``delta.content`` and ``reasoning_content`` is left null.

    The gateway maps Dify's ``agent_thought`` SSE event → ``reasoning_content``
    and Dify's ``message`` event → ``content``. Standard OpenAI clients read
    both fields off the same ``delta`` so existing SDK code keeps working.
    """

    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"] | None = None
    content: str | None = None
    reasoning_content: str | None = None


class ChatChunkChoice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 0
    delta: DeltaMessage
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls"] | None = None


class ChatCompletionChunk(BaseModel):
    """OpenAI-compatible streaming chunk (SSE ``data:`` payload)."""

    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatChunkChoice]


# ---------- /v1/models ----------


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "ai-sdk-gateway"


class ModelList(BaseModel):
    model_config = ConfigDict(extra="allow")

    object: Literal["list"] = "list"
    data: list[ModelInfo]


# ---------- /v1/embeddings ----------


class EmbeddingsRequest(BaseModel):
    """OpenAI-compatible embeddings request.

    Reference: https://platform.openai.com/docs/api-reference/embeddings/create

    The ``input`` field accepts a single string or a list of strings. OpenAI
    also accepts list-of-tokens (int) and list-of-list-of-tokens; we do not
    implement those here — most clients (LangChain, LlamaIndex) only send
    strings, and the upstream (vLLM) accepts whatever we forward.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    input: str | list[str] = Field(description="A single string or list of strings to embed")
    encoding_format: Literal["float", "base64"] | None = Field(
        default=None,
        description="Defaults to 'float' upstream. Pass through unchanged.",
    )
    dimensions: int | None = Field(
        default=None,
        gt=0,
        description="Truncate output dimensions. Only supported by some models.",
    )

    # OpenAI 2025 deprecation aliases — accept both, prefer new.
    user: str | None = Field(default=None, description="Stable end-user identifier (deprecated alias)")
    safety_identifier: str | None = Field(
        default=None,
        description="OpenAI 2025+ replacement for ``user``",
    )

    @property
    def effective_user(self) -> str | None:
        return self.safety_identifier if self.safety_identifier is not None else self.user


class EmbeddingData(BaseModel):
    model_config = ConfigDict(extra="allow")

    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float] | str  # str = base64-encoded when encoding_format="base64"


class EmbeddingsUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    total_tokens: int = 0


class EmbeddingsResponse(BaseModel):
    """OpenAI-compatible embeddings response."""

    model_config = ConfigDict(extra="allow")

    object: Literal["list"] = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingsUsage = Field(default_factory=EmbeddingsUsage)


# ---------- /v1/datasets ----------


class DatasetCreateRequest(BaseModel):
    """Body of ``POST /v1/datasets``.

    Customer-facing surface is intentionally smaller than Dify's
    ``DatasetCreatePayload`` — only the fields a client should set are
    accepted. Gateway resolves ``embedding_model_provider`` from the
    registry; clients pass just ``embedding_model`` (and may omit it
    entirely to use the customer's default).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=400)
    indexing_technique: Literal["high_quality", "economy"] = Field(
        default="high_quality",
        description=(
            "``high_quality`` runs the documents through an embedding model "
            "and stores vectors in Qdrant — required for semantic retrieval. "
            "``economy`` is keyword-only and much cheaper but worse recall."
        ),
    )
    embedding_model: str | None = Field(
        default=None,
        description=(
            "Customer-facing embedding model id (must match an entry in the "
            "customer's ``embedding_models`` registry). If omitted, the "
            "gateway falls back to the customer's first registered embedding "
            "model. If neither is present, the request is rejected."
        ),
    )


class Dataset(BaseModel):
    """A dataset entry as surfaced to the SDK client.

    Mirrors Dify's ``dataset_detail_fields`` output but with the noisy
    internal fields (provider plugin ids, indexing settings, partial
    member list, ...) stripped. We use ``extra="allow"`` so the customer
    can still see any extra Dify fields if they need to.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str | None = None
    indexing_technique: str | None = None
    embedding_model: str | None = None
    embedding_model_provider: str | None = None
    document_count: int = 0
    word_count: int = 0
    created_at: int | None = None


class DatasetList(BaseModel):
    """Response envelope for ``GET /v1/datasets``."""

    model_config = ConfigDict(extra="allow")

    object: Literal["list"] = "list"
    data: list[Dataset]
    has_more: bool = False
    total: int = 0
    page: int = 1
    limit: int = 20


class DatasetRetrieveRequest(BaseModel):
    """Body of ``POST /v1/datasets/{id}/retrieve``.

    Pure-retrieval (hit-testing) channel — no LLM call, no RAG augmentation,
    just the top-k chunks from the dataset's vector index. Used by clients
    who want to run their own ranking / display / evaluation pipeline.

    The simple top-level fields (``top_k``, ``score_threshold``,
    ``search_method``) are the common knobs. If they're all omitted, the
    gateway forwards no ``retrieval_model`` and Dify uses the dataset's
    bake-in default. If any is set, the gateway builds a full
    ``retrieval_model`` payload with sensible defaults for the rest.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=250)
    top_k: int | None = Field(default=None, ge=1, le=100)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    search_method: Literal[
        "semantic_search", "hybrid_search", "full_text_search", "keyword_search"
    ] | None = None


class RetrievedSegment(BaseModel):
    """A single retrieved chunk (subset of Dify's segment payload).

    Clients get the chunk content + score + provenance (which document,
    which segment within the document) so they can build their own UI.
    Extra Dify fields pass through via ``extra="allow"``.
    """

    model_config = ConfigDict(extra="allow")

    content: str
    score: float | None = None
    document_id: str | None = None
    document_name: str | None = None
    segment_id: str | None = None


class DatasetRetrieveResponse(BaseModel):
    """Response envelope for ``POST /v1/datasets/{id}/retrieve``."""

    model_config = ConfigDict(extra="allow")

    object: Literal["list"] = "list"
    query: str
    data: list[RetrievedSegment]


def make_metadata(
    references: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
    request_id: str | None = None,
) -> MessageMetadata:
    """Convenience factory used by the chat router and tests."""
    refs = [Reference(**r) for r in references] if references else []
    return MessageMetadata(references=refs, conversation_id=conversation_id, request_id=request_id)
