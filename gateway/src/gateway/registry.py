"""Customer registry: maps SDK keys to Dify deployments + permitted models.

Loads a YAML file at startup and resolves SDK keys to ``CustomerEntry`` objects.
The registry is intentionally read-only at runtime; reload requires process restart.

Format (see ``registry.example.yaml``):

.. code-block:: yaml

    customers:
      - sdk_key: "bsa_dev_..."
        customer_id: "customer-a"
        dify:
          base_url: "http://dify-customer-a:5001"
          console_email: "..."
          console_password: "..."
          dataset_api_key: "..."
        models:
          - id: "qwen3.6-35b"
            provider: "..."
            name: "..."
            completion_params: {temperature: 0.3}
        knowledge_bases: []

Invariants:
    * SDK keys are unique across the registry; duplicates raise on load.
    * Each customer must declare at least one model.
    * Model IDs are unique within a customer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelEntry(BaseModel):
    """A single model the customer is allowed to invoke.

    ``id`` is the customer-facing identifier passed in ``extra_body.llm_model``.
    ``provider``/``name``/``completion_params`` are written into Dify Apps when
    the gateway lazy-builds an App for ``(customer_id, model_id)``.

    ``owner`` follows OpenAI's ``owned_by`` semantics — it identifies the
    *publisher* of the underlying model (e.g., ``"openai"`` for GPT,
    ``"meta-llama"`` for Llama, ``"Qwen"`` for Qwen3, ``"BAAI"`` for
    bge-m3). Defaults to the gateway identifier when an upstream publisher
    is unknown or not relevant (e.g., customer-specific fine-tuned models).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    owner: str = Field(default="ai-sdk-gateway", min_length=1)
    completion_params: dict[str, Any] = Field(default_factory=dict)


class DifyConnection(BaseModel):
    """Connection details for the customer's Dify deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(min_length=1)
    console_email: str = Field(min_length=1)
    console_password: str = Field(min_length=1)
    dataset_api_key: str = Field(min_length=1)


class EmbeddingModelEntry(BaseModel):
    """An embedding model the customer can call via ``POST /v1/embeddings``.

    Unlike :class:`ModelEntry`, embedding models bypass Dify entirely — the
    gateway proxies the request straight to an OpenAI-compatible embedding
    endpoint (typically vLLM serving in ``--task embed`` mode). Dify is
    irrelevant for pure vectorisation: there's no prompt, no RAG, no agent,
    no need for App-level orchestration.

    Attributes:
        id: Customer-facing model id; what the client passes in the
            ``model`` field of an embeddings request.
        name: Model name to send downstream (matches the upstream service's
            ``--served-model-name`` / model registry).
        owner: Publisher identity surfaced in ``/v1/models`` (OpenAI ``owned_by``
            semantics). Defaults to the gateway identifier.
        endpoint_url: OpenAI-compatible base URL, e.g. ``http://vllm-embed:8000/v1``.
            Used by ``POST /v1/embeddings`` to proxy directly to the upstream.
        api_key: Bearer token sent to the endpoint; vLLM ignores it by default
            but other OpenAI-compatible services may require a real key.
        dimensions: Native output dimensions (informational; some models
            support truncation via the request's ``dimensions`` parameter).
        provider: Dify plugin provider id (e.g.
            ``langgenius/openai_api_compatible/openai_api_compatible``) used
            when the gateway creates a dataset bound to this embedding model
            (PR #3 R2 + R5). Optional — if unset, ``POST /v1/datasets`` will
            send only ``embedding_model`` to Dify and let Dify resolve the
            provider, which works when the customer's Dify has exactly one
            embedding plugin installed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    owner: str = Field(default="ai-sdk-gateway", min_length=1)
    endpoint_url: str = Field(min_length=1)
    api_key: str = Field(default="EMPTY")
    dimensions: int | None = Field(default=None, gt=0)
    provider: str | None = Field(default=None, min_length=1)


class CustomerEntry(BaseModel):
    """A fully resolved customer registry row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sdk_key: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    dify: DifyConnection
    models: list[ModelEntry] = Field(min_length=1)
    embedding_models: list[EmbeddingModelEntry] = Field(default_factory=list)
    knowledge_bases: list[str] = Field(default_factory=list)

    @field_validator("models")
    @classmethod
    def _unique_model_ids(cls, models: list[ModelEntry]) -> list[ModelEntry]:
        ids = [m.id for m in models]
        if len(ids) != len(set(ids)):
            raise ValueError("model ids must be unique within a customer")
        return models

    @field_validator("embedding_models")
    @classmethod
    def _unique_embedding_model_ids(
        cls, models: list[EmbeddingModelEntry]
    ) -> list[EmbeddingModelEntry]:
        ids = [m.id for m in models]
        if len(ids) != len(set(ids)):
            raise ValueError("embedding model ids must be unique within a customer")
        return models

    @model_validator(mode="after")
    def _no_id_collisions_across_lists(self) -> "CustomerEntry":
        """Reject the same ``id`` appearing in both ``models`` and ``embedding_models``.

        ``/v1/models`` flattens both lists into a single OpenAI-shaped list,
        and the per-customer dispatchers (``find_model`` vs
        ``find_embedding_model``) assume the id namespace is shared. If the
        same id pointed at both an LLM and an embedding model, ``/v1/models``
        would advertise duplicate entries, and a client calling
        ``model="x"`` against ``/v1/chat/completions`` would silently win
        over the embedding side (or vice versa) — confusing behaviour that
        depends on lookup order.

        Forbid the collision at config-load time so it surfaces as a clear
        validation error, not a runtime mystery.
        """
        llm_ids = {m.id for m in self.models}
        emb_ids = {e.id for e in self.embedding_models}
        overlap = llm_ids & emb_ids
        if overlap:
            raise ValueError(
                f"model ids collide across LLM and embedding lists: {sorted(overlap)}"
            )
        return self

    def find_model(self, model_id: str) -> ModelEntry | None:
        """Return the LLM model entry matching ``model_id`` or None."""
        return next((m for m in self.models if m.id == model_id), None)

    def find_embedding_model(self, model_id: str) -> EmbeddingModelEntry | None:
        """Return the embedding model entry matching ``model_id`` or None."""
        return next((m for m in self.embedding_models if m.id == model_id), None)

    def default_model(self) -> ModelEntry:
        """Return the first declared LLM model (fallback for ``llm_model`` omission)."""
        return self.models[0]


class CustomerRegistry:
    """In-memory registry indexed by SDK key.

    Construct via :meth:`from_yaml` to load from disk. The class itself is just
    a thin wrapper over a ``dict[str, CustomerEntry]``; tests can build instances
    directly via :meth:`from_entries`.
    """

    def __init__(self, by_sdk_key: dict[str, CustomerEntry]) -> None:
        self._by_sdk_key = by_sdk_key

    @classmethod
    def from_entries(cls, entries: list[CustomerEntry]) -> "CustomerRegistry":
        """Build a registry from a list of entries (raises on duplicate SDK keys)."""
        by_key: dict[str, CustomerEntry] = {}
        for entry in entries:
            if entry.sdk_key in by_key:
                raise ValueError(f"duplicate sdk_key in registry: {entry.sdk_key}")
            by_key[entry.sdk_key] = entry
        return cls(by_key)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CustomerRegistry":
        """Load and validate a registry YAML file.

        Raises:
            FileNotFoundError: path does not exist.
            ValueError: malformed YAML, schema violation, or duplicate keys.
        """
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"registry file not found: {p}")

        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ValueError(f"invalid YAML in {p}: {e}") from e

        if not isinstance(raw, dict) or "customers" not in raw:
            raise ValueError(f"registry root must be a mapping with key 'customers' in {p}")

        try:
            entries = [CustomerEntry.model_validate(c) for c in raw["customers"]]
        except Exception as e:
            raise ValueError(f"registry validation failed in {p}: {e}") from e

        return cls.from_entries(entries)

    def lookup(self, sdk_key: str) -> CustomerEntry | None:
        """Return the customer for ``sdk_key`` or None if unknown."""
        return self._by_sdk_key.get(sdk_key)

    def find_by_customer_id(self, customer_id: str) -> CustomerEntry | None:
        """Return the (first) customer entry whose customer_id matches.

        Note:
            Currently O(N). The registry is intended to hold ≲100 customers per
            gateway instance; if this assumption changes, add an inverted index.
        """
        for entry in self._by_sdk_key.values():
            if entry.customer_id == customer_id:
                return entry
        return None

    def customers(self) -> list[CustomerEntry]:
        """Return all customer entries (no order guarantee)."""
        return list(self._by_sdk_key.values())

    def __len__(self) -> int:
        return len(self._by_sdk_key)

    def __contains__(self, sdk_key: str) -> bool:
        return sdk_key in self._by_sdk_key
