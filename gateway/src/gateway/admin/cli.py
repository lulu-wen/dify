"""Gateway admin CLI — operator-facing entry point.

Exposed as ``gateway-admin`` via ``[project.scripts]`` in pyproject.toml.
The runtime (``uvicorn gateway.main:app``) does NOT import this module;
keep it that way so the FastAPI app stays click-free.

Today this exposes one command:

- ``gateway-admin add-customer`` — automates the registry setup flow
  that operators were doing manually (and forgetting half of, leading
  to ``dataset-not-used-in-pr1``-style placeholder regressions).

The CLI is a thin orchestrator. It does four things, in order:

1. **Validate the operator's input** (customer_id slug for shared mode,
   model declarations, etc.) before talking to Dify.
2. **Talk to Dify Console API** to log in + create a real
   ``dataset-*`` API key. No more "go to Web UI 服務 API 那邊" step.
3. **Merge the new customer into ``registry.yaml``** via the
   atomic-write helper, re-validating the whole file against the
   same ``CustomerRegistry`` validators the runtime uses.
4. **Print the SDK key once** to the operator. The SDK key is the
   only output that's secret — everything else (customer_id, model
   names) is operator-visible config.

Failures are fail-fast: if Dify is down at onboarding time, we error
out instead of writing a half-configured registry. If the merge fails
validation, we never touch disk.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import click
import structlog

from gateway.admin.registry_merge import (
    RegistryMergeError,
    load_existing_registry,
    merge_customer,
    write_registry_atomic,
)
from gateway.dify.client import DifyClient
from gateway.errors import DifyUpstreamError
from gateway.registry import (
    CustomerEntry,
    DifyConnection,
    EmbeddingModelEntry,
    ModelEntry,
    SharedEmbeddingModel,
)

logger = structlog.get_logger(__name__)


# Default plugin provider for both LLM and embedding models. vLLM /
# SGLang / TGI / Ollama-in-OpenAI-mode / SiliconFlow / OpenRouter etc.
# all speak OpenAI-compatible HTTP — this plugin handles them all.
# Operators can override per-model via the colon syntax described below.
_DEFAULT_PROVIDER = "langgenius/openai_api_compatible/openai_api_compatible"


def _generate_sdk_key() -> str:
    """Random ``bsa_<32-char-urlsafe>`` SDK key.

    32 bytes of randomness via :func:`secrets.token_urlsafe` gives 256
    bits of entropy — well above the threshold where brute force is
    even theoretical. ``bsa_`` prefix matches the convention enforced
    by PR #5's L1 format check.
    """
    return f"bsa_{secrets.token_urlsafe(32)}"


def _parse_model_spec(spec: str) -> ModelEntry:
    """Parse the ``--model`` flag.

    Two forms accepted:

    - **Shorthand**: ``gemma-3n-e4b`` → id, name, owner all default
      from the same string; provider defaults to
      ``langgenius/openai_api_compatible/openai_api_compatible``.
    - **Explicit**: ``id:provider:name`` → all three explicit. Useful
      when ``id`` (customer-facing) differs from ``name`` (Dify
      registered model name), or when pointing at a non-default
      provider (Anthropic / Google / Cohere).
    """
    parts = spec.split(":")
    if len(parts) == 1:
        model_id = parts[0]
        return ModelEntry(id=model_id, provider=_DEFAULT_PROVIDER, name=model_id)
    if len(parts) == 3:
        model_id, provider, name = parts
        return ModelEntry(id=model_id, provider=provider, name=name)
    raise click.BadParameter(
        f"--model spec '{spec}' must be 'id' (shorthand) or 'id:provider:name' "
        "(explicit). Got "
        f"{len(parts)} colon-separated parts."
    )


def _parse_embedding_spec(spec: str, *, default_endpoint: str | None) -> EmbeddingModelEntry:
    """Parse the ``--embedding-model`` flag.

    Only shorthand form ``bge-m3`` is supported. The ``id:url`` colon
    form doesn't survive contact with URLs containing ``:`` (almost
    every URL does — ``http://host:port``). For mixed-endpoint
    setups, register the first via the CLI then edit ``registry.yaml``
    directly to add the rest — they're cold-path entries operators
    rarely add via shell pipelines anyway.

    Embedding models bypass Dify entirely — the gateway proxies
    straight to an OpenAI-compatible embedding endpoint (vLLM in
    ``--task embed`` mode by default). That's why ``endpoint_url``
    matters here but not for LLM models (those go through Dify).
    """
    if ":" in spec:
        raise click.BadParameter(
            f"--embedding-model '{spec}' must be a bare id (e.g. 'bge-m3'). "
            "URLs collide with the colon separator — use "
            "--embedding-endpoint-url to set the URL for all embedding "
            "models added in this invocation."
        )
    if default_endpoint is None:
        raise click.BadParameter(
            f"--embedding-model '{spec}' requires --embedding-endpoint-url "
            "to be set (e.g. 'http://vllm-embed:8000/v1'). Without it the "
            "gateway has no idea where to proxy embedding requests."
        )
    return EmbeddingModelEntry(
        id=spec,
        name=spec,
        endpoint_url=default_endpoint,
        provider=_DEFAULT_PROVIDER,
    )


async def _provision_dataset_api_key(
    *,
    base_url: str,
    console_email: str,
    console_password: str,
) -> str:
    """Log into Dify console + create a workspace-scoped dataset API key.

    Returns the freshly-minted ``dataset-*`` token. Network failures
    propagate to the caller for fail-fast error reporting; we don't
    wrap them here because the CLI's error handler knows how to render
    them cleanly.
    """
    async with DifyClient(base_url=base_url, timeout_s=30.0) as client:
        session = await client.console_login(console_email, console_password)
        return await client.console_create_dataset_api_key(session)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """AI SDK Gateway — admin tasks."""


@cli.command("add-customer")
@click.option(
    "--customer-id",
    required=True,
    help=(
        "Stable identifier (lowercase + hyphens for shared mode). "
        "Used in dataset name prefixes (shared mode) and App naming."
    ),
)
@click.option(
    "--dify-base-url",
    required=True,
    help="Dify base URL, e.g. http://localhost (no trailing /v1).",
)
@click.option(
    "--dify-admin-email",
    required=True,
    help="Dify console admin email for this customer's workspace.",
)
@click.option(
    "--dify-admin-password",
    default=None,
    help=(
        "Dify console admin password. If omitted, prompted interactively "
        "(recommended — keeps the value out of shell history)."
    ),
)
@click.option(
    "--mode",
    type=click.Choice(["dedicated", "shared"], case_sensitive=False),
    default="dedicated",
    show_default=True,
    help="Isolation mode. See main project Notion for the trade-offs.",
)
@click.option(
    "--shared-embedding-name",
    default=None,
    help=(
        "Required when --mode shared. The workspace-global embedding "
        "model name (e.g. 'bge-m3') Dify uses for every dataset bound "
        "in this shared workspace."
    ),
)
@click.option(
    "--shared-embedding-provider",
    default=_DEFAULT_PROVIDER,
    show_default=True,
    help="Plugin provider for the shared embedding model.",
)
@click.option(
    "--model",
    "model_specs",
    multiple=True,
    required=True,
    help=(
        "LLM model. Repeatable. Two forms: shorthand 'gemma-3n-e4b' or "
        "explicit 'id:provider:name'. At least one required."
    ),
)
@click.option(
    "--embedding-model",
    "embedding_specs",
    multiple=True,
    default=(),
    help=(
        "Embedding model. Repeatable. Forms: 'id', 'id:endpoint_url', or "
        "'id:endpoint_url:provider'. Optional."
    ),
)
@click.option(
    "--embedding-endpoint-url",
    default=None,
    help=(
        "Default endpoint URL for shorthand --embedding-model entries. "
        "Ignored when the explicit form is used."
    ),
)
@click.option(
    "--sdk-key",
    default=None,
    help=(
        "Reuse a specific SDK key instead of generating one. Useful for "
        "scripted re-onboarding. Must start with 'bsa_'."
    ),
)
@click.option(
    "--registry-path",
    default="./registry.yaml",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to the registry.yaml the gateway loads at startup.",
)
@click.option(
    "--force/--no-force",
    default=False,
    help=(
        "Overwrite an existing entry with the same customer_id. "
        "Refuses by default — protects against accidental SDK key rotation."
    ),
)
def add_customer(
    customer_id: str,
    dify_base_url: str,
    dify_admin_email: str,
    dify_admin_password: str | None,
    mode: str,
    shared_embedding_name: str | None,
    shared_embedding_provider: str,
    model_specs: tuple[str, ...],
    embedding_specs: tuple[str, ...],
    embedding_endpoint_url: str | None,
    sdk_key: str | None,
    registry_path: Path,
    force: bool,
) -> None:
    """Onboard a new customer end-to-end.

    Workflow: parse flags → log into Dify → auto-create dataset API key
    → build registry entry → atomically merge into registry.yaml →
    print the new SDK key to stdout exactly once. Restart the gateway
    after success to pick up the new entry.
    """
    # 1. Prompt for password if not given. Avoids shell-history leak.
    if dify_admin_password is None:
        dify_admin_password = click.prompt(
            "Dify admin password",
            hide_input=True,
        )

    # 1a. Normalise --mode case BEFORE any side-effecting work. Click's
    # ``case_sensitive=False`` accepts ``SHARED`` / ``Shared`` but passes
    # the original case through to us. ``DifyConnection.mode`` is
    # ``Literal["dedicated", "shared"]`` and pydantic only accepts the
    # lowercase form. Without normalising here, the CLI would: log into
    # Dify successfully → create a real dataset_api_key → THEN fail at
    # CustomerEntry validation, leaving the freshly-created key
    # orphaned on the Dify side (PR #6 self-review P2-1).
    mode = mode.lower()

    # 2. Generate / validate SDK key.
    if sdk_key is None:
        sdk_key = _generate_sdk_key()
    elif not sdk_key.startswith("bsa_"):
        raise click.BadParameter(
            "--sdk-key must start with 'bsa_' (matches the L1 format check "
            "the gateway enforces at startup)."
        )

    # 3. Parse model / embedding specs into pydantic models BEFORE
    #    talking to Dify. If specs are malformed we fail before
    #    incurring a network round-trip.
    try:
        models = [_parse_model_spec(s) for s in model_specs]
    except click.BadParameter:
        raise
    embedding_models: list[EmbeddingModelEntry] = []
    for spec in embedding_specs:
        embedding_models.append(
            _parse_embedding_spec(spec, default_endpoint=embedding_endpoint_url)
        )

    # 4. Build the SharedEmbeddingModel if shared mode.
    shared_embedding = None
    if mode == "shared":
        if shared_embedding_name is None:
            raise click.BadParameter(
                "--shared-embedding-name is required when --mode shared. "
                "In shared mode Dify's embedding plugin is workspace-scoped, "
                "so every dataset binds to the same model regardless of which "
                "customer creates it."
            )
        shared_embedding = SharedEmbeddingModel(
            name=shared_embedding_name,
            provider=shared_embedding_provider,
        )

    # 5. Talk to Dify — login + create dataset key.
    click.echo(f"Connecting to Dify at {dify_base_url} ...", err=True)
    try:
        dataset_api_key = asyncio.run(
            _provision_dataset_api_key(
                base_url=dify_base_url,
                console_email=dify_admin_email,
                console_password=dify_admin_password,
            )
        )
    except DifyUpstreamError as exc:
        click.echo(f"ERROR: Dify rejected onboarding request: {exc}", err=True)
        click.echo(
            "Common causes: wrong console password, Dify container down, "
            "or workspace doesn't allow programmatic key creation. Verify "
            "the credentials work in the Dify Web UI first.",
            err=True,
        )
        sys.exit(2)
    except Exception as exc:
        click.echo(f"ERROR: could not reach Dify at {dify_base_url}: {exc}", err=True)
        sys.exit(2)

    click.echo(
        f"Dataset API key created: {dataset_api_key[:16]}... (provisioned by gateway-admin)",
        err=True,
    )

    # 6. Build the CustomerEntry. Pydantic will catch field-level
    #    issues (slug rule in shared mode, etc.) here.
    try:
        new_entry = CustomerEntry(
            sdk_key=sdk_key,
            customer_id=customer_id,
            dify=DifyConnection(
                base_url=dify_base_url,
                console_email=dify_admin_email,
                console_password=dify_admin_password,
                dataset_api_key=dataset_api_key,
                mode=mode,  # type: ignore[arg-type]
                shared_embedding_model=shared_embedding,
            ),
            models=models,
            embedding_models=embedding_models,
        )
    except Exception as exc:
        click.echo(f"ERROR: customer entry validation failed: {exc}", err=True)
        sys.exit(3)

    # 7. Merge into registry.yaml. The merge helper runs the full
    #    cross-customer validator the gateway uses at boot, so anything
    #    that would 500 the gateway at startup fails here instead.
    try:
        existing = load_existing_registry(registry_path)
        merged = merge_customer(existing, new_entry, force=force)
        write_registry_atomic(registry_path, merged)
    except RegistryMergeError as exc:
        click.echo(f"ERROR: registry merge failed: {exc}", err=True)
        sys.exit(4)

    # 8. Success. The SDK key is the ONE secret value the operator
    #    needs to capture from this run. Print it on stdout (not
    #    stderr — operators frequently redirect to `tee` etc.) and
    #    warn that this is the only chance.
    click.echo(
        f"Customer '{customer_id}' added to {registry_path}.",
        err=True,
    )
    click.echo(
        "Restart the gateway to load the new customer (uvicorn does not "
        "hot-reload registry.yaml).",
        err=True,
    )
    click.echo("")
    click.echo("SDK key (give this to the client developer ONCE — store securely):")
    click.echo(sdk_key)


def main() -> None:
    """Entry point for ``[project.scripts]`` wiring."""
    cli()


if __name__ == "__main__":
    main()
