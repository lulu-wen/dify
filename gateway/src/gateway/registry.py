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

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

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


class SharedEmbeddingModel(BaseModel):
    """Workspace-global embedding model used when ``DifyConnection.mode == "shared"``.

    PR #4 R5: in shared mode the customer's Dify is **one workspace serving
    many customers**, and Dify's embedding plugin is workspace-scoped. So
    every dataset created in that workspace MUST bind to the same embedding
    model regardless of which customer triggered the creation. This config
    lives on ``DifyConnection`` rather than on individual ``EmbeddingModelEntry``
    rows because it is genuinely workspace-level (one value per Dify
    deployment, not per customer).

    Per-customer ``embedding_models`` (for direct ``POST /v1/embeddings``)
    keep working as before — only the dataset binding path is constrained.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, description="Embedding model name Dify recognises (served-model-name)")
    provider: str = Field(
        min_length=1,
        description="Dify plugin provider id, e.g. langgenius/openai_api_compatible/openai_api_compatible",
    )


class DifyConnection(BaseModel):
    """Connection details for the customer's Dify deployment.

    The ``mode`` flag (PR #4 R1) controls how the gateway isolates customers:

    - ``dedicated`` (default, PR #1-#3 behaviour): each customer has their own
      Dify deployment. The workspace IS the tenant boundary, so the gateway
      relies on Dify's own data isolation — no need for App / Dataset name
      prefixing.

    - ``shared`` (PR #4): multiple customers point at the same Dify base_url
      and dataset_api_key. The gateway applies soft isolation by prefixing
      App and Dataset names with ``customer_id`` and filtering list responses.
      **Not** suitable for paid production (the workspace-level Postgres / S3 /
      credentials are still shared — see Notion overview §4.3 for the full
      isolation analysis).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(min_length=1)
    console_email: str = Field(min_length=1)
    console_password: str = Field(min_length=1)
    dataset_api_key: str = Field(min_length=1)
    mode: Literal["dedicated", "shared"] = Field(
        default="dedicated",
        description=(
            "Isolation mode. `dedicated` (per-customer Dify, default) keeps "
            "PR #1-#3 behaviour. `shared` (one Dify, many customers) enables "
            "App + Dataset name prefixing + list filtering. See PR #4 spec."
        ),
    )
    shared_embedding_model: SharedEmbeddingModel | None = Field(
        default=None,
        description=(
            "Required when mode='shared': the workspace-global embedding "
            "model dataset creation must bind to. Ignored in dedicated mode."
        ),
    )

    @model_validator(mode="after")
    def _embedding_matches_mode(self) -> DifyConnection:
        """Shared mode must declare ``shared_embedding_model``; dedicated must not.

        Two failure modes this catches:

        - mode='shared' without the field → gateway has no way to honour
          Dify's workspace-level embedding constraint at dataset-create
          time. Reject up front.
        - mode='dedicated' WITH the field → the field is meaningless in
          dedicated mode, and an operator setting it suggests they meant
          to use shared mode. Refuse the ambiguous config so the operator
          notices the typo (codex review-1 P2: defence in depth against
          ``resolve_embedding_for_dataset`` picking up a stray field).
        """
        if self.mode == "shared" and self.shared_embedding_model is None:
            raise ValueError(
                "dify.shared_embedding_model is required when dify.mode='shared' "
                "(workspace-global embedding model is needed for dataset creation)"
            )
        if self.mode == "dedicated" and self.shared_embedding_model is not None:
            raise ValueError(
                "dify.shared_embedding_model must not be set when dify.mode='dedicated' "
                "(the field is only meaningful in shared mode; remove it or change mode)"
            )
        return self


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
    customer_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Per-customer identifier. In shared mode it is additionally "
            "constrained to a lowercase / hyphen slug (no underscores) "
            "because the gateway uses it as a literal prefix for App / "
            "Dataset names. See ``_shared_mode_customer_id_is_slug`` for "
            "the validator that enforces the slug rule. Dedicated mode "
            "(PR #1-#3 default) accepts any string within the length cap "
            "so existing deployments don't break (codex review-4 P2)."
        ),
    )
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

    # Mirrors ``_DIFY_DATASET_NAME_MAX`` in routers/datasets.py.
    # Duplicated here to keep registry validation self-contained — if Dify
    # ever bumps the limit, both constants need updating together (the
    # tests in test_shared_mode.py pin this expectation).
    _DIFY_DATASET_NAME_LIMIT: int = 40
    # Shared-mode customer_id constraint: lowercase ASCII + digits +
    # hyphens, must start with letter or digit. The literal "__" sequence
    # used in ``{customer_id}__{name}`` is then unambiguous (codex
    # review-1 P1). Dedicated mode does NOT enforce this — backward
    # compat with existing PR #1-#3 registries that may use uppercase /
    # underscores (codex review-4 P2).
    _SHARED_CUSTOMER_ID_PATTERN: re.Pattern[str] = re.compile(
        r"^[a-z0-9][a-z0-9-]*$"
    )

    @model_validator(mode="after")
    def _shared_mode_customer_id_is_slug(self) -> CustomerEntry:
        """Enforce slug pattern on customer_id ONLY when mode='shared'.

        Codex review-1 P1 required this for shared mode (so the prefix
        ``{customer_id}__`` can't be substring-attacked). Codex review-4
        P2 caught that a Field-level pattern would also break PR #1-#3
        deployments using IDs like ``Customer_A`` or ``acme_prod`` in
        dedicated mode. Restrict the slug rule to shared mode.
        """
        if self.dify.mode == "shared" and not self._SHARED_CUSTOMER_ID_PATTERN.match(
            self.customer_id
        ):
            raise ValueError(
                f"customer_id '{self.customer_id}' is not a valid shared-mode "
                "slug. Shared mode requires lowercase ASCII letters, digits, "
                "and hyphens only (must start with a letter or digit). Underscores "
                "are reserved as the shared-mode prefix separator. Dedicated mode "
                "has no such restriction; switch dify.mode if you want flexibility."
            )
        return self

    @model_validator(mode="after")
    def _shared_mode_customer_id_fits_name_budget(self) -> CustomerEntry:
        """Codex review-3 P2: a long customer_id leaves no name budget.

        Shared-mode datasets are stored in Dify as ``{customer_id}__{name}``,
        capped at 40 chars. If ``len(customer_id) >= 38``, the prefix
        alone uses 40+ chars and EVERY ``POST /v1/datasets`` fails — but
        the registry loads fine, so the operator only finds out at the
        first runtime request. Reject at load time with a clear message.
        """
        if self.dify.mode == "shared":
            # Reserve at least 1 char for the user-provided dataset name.
            prefix_overhead = len(self.customer_id) + 2  # "__"
            if prefix_overhead >= self._DIFY_DATASET_NAME_LIMIT:
                budget = self._DIFY_DATASET_NAME_LIMIT - 2 - 1  # 1 char min name
                raise ValueError(
                    f"customer_id '{self.customer_id}' "
                    f"({len(self.customer_id)} chars) is too long for shared mode: "
                    f"prefix '{self.customer_id}__' would use "
                    f"{prefix_overhead}/{self._DIFY_DATASET_NAME_LIMIT} of Dify's "
                    f"dataset-name budget, leaving no room for the name. "
                    f"Use a customer_id of at most {budget} chars for shared mode."
                )
        return self

    @model_validator(mode="after")
    def _no_id_collisions_across_lists(self) -> CustomerEntry:
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
    def from_entries(cls, entries: list[CustomerEntry]) -> CustomerRegistry:
        """Build a registry from a list of entries.

        Raises:
            ValueError: duplicate sdk_key, OR customers sharing the same
                Dify ``base_url`` but disagreeing on ``mode`` /
                ``shared_embedding_model`` (PR #4 R1).
        """
        by_key: dict[str, CustomerEntry] = {}
        for entry in entries:
            if entry.sdk_key in by_key:
                raise ValueError(f"duplicate sdk_key in registry: {entry.sdk_key}")
            by_key[entry.sdk_key] = entry
        cls._check_dify_consistency(by_key.values())
        return cls(by_key)

    @staticmethod
    def _check_dify_consistency(entries: Any) -> None:
        """Customers pointing at the same Dify must agree on isolation mode.

        PR #4 R1: if customer A is on ``dedicated`` and customer B is on
        ``shared`` but both point at the same ``base_url``, the gateway
        ends up in an inconsistent state — A creates datasets with raw
        names while B prefixes them, and a name collision is possible.
        Similarly the workspace-level ``shared_embedding_model`` must be
        identical for all customers on the same Dify.

        Fail loud at registry load so the issue is caught in CI / dev,
        not at the first runtime dataset operation.
        """
        # Codex review-3 P2: normalize trailing slash before grouping.
        # ``http://dify`` and ``http://dify/`` resolve to the same
        # upstream (DifyClient does ``rstrip("/")`` itself), so they
        # MUST be in the same consistency group — otherwise a typo'd
        # registry can declare mixed mode for "different" base_urls
        # that point at the same Dify and bypass the consistency check.
        groups: dict[str, list[CustomerEntry]] = defaultdict(list)
        for e in entries:
            groups[e.dify.base_url.rstrip("/")].append(e)

        for base_url, members in groups.items():
            modes = {m.dify.mode for m in members}
            if len(modes) > 1:
                ids = sorted(m.customer_id for m in members)
                raise ValueError(
                    f"customers sharing dify base_url '{base_url}' disagree on "
                    f"isolation mode: {sorted(modes)} (customers: {ids}). "
                    "All customers on the same Dify must use the same mode."
                )

            # Single mode across the group — also check shared_embedding_model
            # agrees when mode='shared' (we already validated each entry sets
            # it when shared, but two customers could disagree on which one).
            shared_models = {
                (
                    m.dify.shared_embedding_model.name,
                    m.dify.shared_embedding_model.provider,
                )
                for m in members
                if m.dify.shared_embedding_model is not None
            }
            if len(shared_models) > 1:
                ids = sorted(m.customer_id for m in members)
                raise ValueError(
                    f"customers sharing dify base_url '{base_url}' disagree on "
                    f"shared_embedding_model: {sorted(shared_models)} "
                    f"(customers: {ids}). The workspace has one embedding model."
                )

    @classmethod
    def from_yaml(cls, path: str | Path) -> CustomerRegistry:
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
