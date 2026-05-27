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
    PLACEHOLDER_DATASET_KEY,
    RegistryMergeError,
    check_writable,
    find_shared_workspace_dataset_key,
    load_existing_registry,
    merge_customer,
    write_registry_atomic,
)
from gateway.dify.client import DifyClient
from gateway.errors import DifyTimeoutError, DifyUpstreamError, UpstreamClientError
from gateway.registry import (
    CustomerEntry,
    DifyConnection,
    EmbeddingModelEntry,
    ModelEntry,
    SharedEmbeddingModel,
)
from gateway.startup_check import is_network_failure

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
) -> tuple[str, str]:
    """Log into Dify console + create a workspace-scoped dataset API key.

    Returns ``(workspace_id, dataset_api_key)``. The workspace id is
    fetched from ``POST /console/api/workspaces/current`` right after
    login so the caller knows which Dify tenant the session landed in
    (codex review-9 P1: a Dify account can be a member of multiple
    workspaces; the active one is opaque to the caller unless we ask).

    Network failures propagate to the caller for fail-fast error
    reporting; we don't wrap them here because the CLI's error
    handler knows how to render them cleanly.
    """
    async with DifyClient(base_url=base_url, timeout_s=30.0) as client:
        session = await client.console_login(console_email, console_password)
        workspace_id = await client.console_get_current_workspace_id(session)
        dataset_api_key = await client.console_create_dataset_api_key(session)
        return workspace_id, dataset_api_key


async def _login_and_fetch_workspace_id(
    *,
    base_url: str,
    console_email: str,
    console_password: str,
) -> str:
    """Log in + fetch active workspace id. No dataset-key creation.

    Codex review-6 P2 + review-9 P1: replaces the earlier
    ``_verify_console_credentials`` helper. The shared-mode reuse
    path (review-5 P2) skips ``_provision_dataset_api_key`` so we
    don't burn a slot from Dify's 10-key-per-workspace cap. But the
    skipped call had two jobs:

    1. **Validate credentials** — without this, a typo'd or stale
       password would land in registry.yaml unvalidated and trip the
       runtime later at lazy AppManager build (review-6 P2).
    2. **Pin down WHICH workspace the operator is targeting** —
       without this, ``base_url`` + ``console_email`` are ambiguous
       when the same admin account belongs to multiple workspaces in
       one Dify deployment, and the reuse path can cross-pollute
       keys between tenants (review-9 P1).

    This helper does both in one round trip: ``console_login`` for the
    creds check, then ``console_get_current_workspace_id`` to capture
    the active tenant id. Returns the workspace_id; raises
    :class:`DifyUpstreamError` on auth / network failures so the CLI
    can render a clean error and exit before touching the registry.
    """
    async with DifyClient(base_url=base_url, timeout_s=30.0) as client:
        session = await client.console_login(console_email, console_password)
        return await client.console_get_current_workspace_id(session)


async def _verify_dataset_api_key(
    *,
    base_url: str,
    dataset_api_key: str,
) -> bool:
    """Return ``True`` iff Dify accepts ``dataset_api_key``, ``False`` if rejected.

    Codex review-10 P2: the shared-mode reuse path's string-level
    checks (``dataset-`` prefix + a known-placeholder blocklist) can't
    tell a *valid* dataset key from a *documented legacy placeholder*
    like ``dataset-not-used-in-pr1`` — both start with ``dataset-`` and
    the placeholder space is explicitly open-ended ("or any
    placeholder", startup_check.py). The only authoritative test is
    asking Dify, which is exactly what the L4 startup check does:
    list one dataset row with the key.

    - HTTP 4xx auth / permission rejection → ``False``. The caller
      falls through to provisioning a fresh key rather than copying
      a dead one into the new customer.
    - Network / timeout errors → re-raised, so the CLI fails fast
      instead of silently burning a dataset-key slot on an uncertain
      reuse. (We've already logged in successfully against this
      ``base_url`` moments earlier, so a network failure here is rare
      and worth surfacing.)
    """
    async with DifyClient(base_url=base_url, timeout_s=30.0) as client:
        try:
            await client.list_datasets(
                dataset_api_key=dataset_api_key, page=1, limit=1
            )
        except (DifyUpstreamError, DifyTimeoutError, UpstreamClientError) as exc:
            if is_network_failure(exc):
                raise
            # A non-network upstream error == Dify rejected the key
            # (401/403 placeholder / revoked / wrong-workspace token).
            return False
    return True


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
        "Embedding model id. Repeatable. Bare id only (no ':' allowed); "
        "use --embedding-endpoint-url to set the endpoint and the default "
        "OpenAI-compatible provider applies. For non-default provider, "
        "edit registry.yaml after onboarding. Optional."
    ),
)
@click.option(
    "--embedding-endpoint-url",
    default=None,
    help=(
        "Endpoint URL for --embedding-model entries (one URL shared by all "
        "of them in the current invocation; per-model endpoints require "
        "post-onboarding registry editing)."
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

    # 5. DRY-RUN validation — build the would-be CustomerEntry with a
    # PLACEHOLDER dataset_api_key and simulate the registry merge.
    # This catches every deterministic local failure (bad slug for
    # shared mode, customer_id collision, base_url cross-customer
    # conflict, etc.) BEFORE we touch Dify.
    #
    # Codex review-2 P2: without this, the CLI would create a real
    # ``dataset-*`` key on Dify, THEN fail at CustomerEntry/registry
    # validation, leaving an orphan credential in Dify the operator
    # has no easy way to discover or clean up. The mode-case fix from
    # self-review P2-1 only handled one specific input — this
    # generalises to every local check.
    #
    # The placeholder starts with ``dataset-`` so it passes PR #5's
    # L1 format check; we replace it with the real key on success.
    # Codex review-8 P2: moved to ``registry_merge.PLACEHOLDER_DATASET_KEY``
    # so the shared-mode reuse path can refuse to propagate it if it ever
    # leaks into a peer entry.

    def _build_entry(
        dataset_api_key: str, workspace_id: str | None = None
    ) -> CustomerEntry:
        return CustomerEntry(
            sdk_key=sdk_key,
            customer_id=customer_id,
            dify=DifyConnection(
                base_url=dify_base_url,
                console_email=dify_admin_email,
                console_password=dify_admin_password,
                dataset_api_key=dataset_api_key,
                workspace_id=workspace_id,
                mode=mode,  # type: ignore[arg-type]
                shared_embedding_model=shared_embedding,
            ),
            models=models,
            embedding_models=embedding_models,
        )

    try:
        trial_entry = _build_entry(PLACEHOLDER_DATASET_KEY)
    except Exception as exc:
        click.echo(f"ERROR: customer entry validation failed: {exc}", err=True)
        sys.exit(3)

    try:
        existing = load_existing_registry(registry_path)
        # ``merge_customer`` builds an in-memory ``CustomerRegistry``
        # via ``from_entries`` which is exactly what the gateway runs
        # at boot. If the merge raises here, the gateway would refuse
        # to start, so we'd much rather fail now (zero side-effects)
        # than after creating a Dify-side key.
        merge_customer(existing, trial_entry, force=force)
    except RegistryMergeError as exc:
        click.echo(f"ERROR: registry merge would fail: {exc}", err=True)
        sys.exit(4)

    # 5a. Filesystem preflight — if we can't write to ``registry_path``,
    # fail BEFORE the network call. Catches "parent dir doesn't exist",
    # "permission denied", "path is a directory" classes of error.
    # Codex review-3 P2 — without this, write_registry_atomic could
    # raise OSError post-network and leave an orphan dataset key on
    # Dify side that the operator never sees.
    try:
        check_writable(registry_path)
    except RegistryMergeError as exc:
        click.echo(f"ERROR: registry path not writable: {exc}", err=True)
        sys.exit(4)

    # 5b. Shared-mode workspace fingerprint + dataset key reuse.
    #
    # Codex review-9 P1: Dify accounts can belong to MULTIPLE workspaces.
    # ``base_url + console_email`` is not a unique workspace identity —
    # the same admin can log in twice and land in different tenants.
    # Before we can decide whether a peer's dataset_api_key is safe to
    # reuse, we must KNOW which tenant our session is currently in.
    # So for shared mode we always login + fetch workspace_id first,
    # match the peer on that, and only THEN decide reuse vs provision.
    #
    # This login also covers the credential-verification step that
    # review-6 P2 added (typo'd password lands in registry otherwise),
    # since login failure here halts the flow before the registry
    # write.
    #
    # Dedicated mode skips this branch entirely — it always provisions
    # its own dataset key (review-5 only optimised shared mode), so
    # the workspace_id capture happens inside
    # ``_provision_dataset_api_key`` later in step 6.
    reused_dataset_api_key: str | None = None
    fetched_workspace_id: str | None = None
    if mode == "shared":
        click.echo(
            f"Verifying console credentials + fetching workspace id "
            f"against {dify_base_url} ...",
            err=True,
        )
        try:
            fetched_workspace_id = asyncio.run(
                _login_and_fetch_workspace_id(
                    base_url=dify_base_url,
                    console_email=dify_admin_email,
                    console_password=dify_admin_password,
                )
            )
        except DifyUpstreamError as exc:
            click.echo(
                f"ERROR: Dify rejected console credentials: {exc}", err=True
            )
            click.echo(
                "Shared-mode onboarding requires the supplied "
                "console_email + console_password to be valid for this "
                "workspace — they land in registry.yaml as the truth the "
                "runtime uses for lazy App / Dataset creation. Most likely "
                "cause: mistyped or stale password. Verify against the Dify "
                "Web UI login screen, then re-run.",
                err=True,
            )
            sys.exit(2)
        except Exception as exc:
            click.echo(
                f"ERROR: could not reach Dify at {dify_base_url}: {exc}",
                err=True,
            )
            sys.exit(2)

        candidate_dataset_api_key = find_shared_workspace_dataset_key(
            existing,
            base_url=dify_base_url,
            workspace_id=fetched_workspace_id,
        )

        # Codex review-10 P2: the candidate passed cheap string checks
        # (prefix + known-placeholder blocklist), but the placeholder
        # space is open-ended — a peer could be holding a legacy
        # ``dataset-not-used-in-pr1``-shaped string we don't have in
        # the blocklist, or a revoked / wrong-workspace token. Before
        # committing to reuse, verify the candidate actually works
        # against Dify (same check as L4 startup). A rejection means
        # the peer's key is dead → fall through to provisioning a
        # fresh real key rather than copying the dead one forward.
        if candidate_dataset_api_key is not None:
            click.echo(
                f"Verifying candidate dataset key "
                f"({candidate_dataset_api_key[:16]}...) against Dify "
                f"before reuse ...",
                err=True,
            )
            try:
                key_is_valid = asyncio.run(
                    _verify_dataset_api_key(
                        base_url=dify_base_url,
                        dataset_api_key=candidate_dataset_api_key,
                    )
                )
            except Exception as exc:
                click.echo(
                    f"ERROR: could not verify candidate dataset key against "
                    f"Dify at {dify_base_url}: {exc}",
                    err=True,
                )
                sys.exit(2)

            if key_is_valid:
                reused_dataset_api_key = candidate_dataset_api_key
            else:
                click.echo(
                    "Candidate dataset key was rejected by Dify (likely a "
                    "legacy placeholder or revoked token in the peer entry). "
                    "Provisioning a fresh key for this customer instead; the "
                    "peer's stale key is left untouched for the gateway "
                    "startup check to flag.",
                    err=True,
                )

    if reused_dataset_api_key is not None:
        click.echo(
            f"Reusing existing workspace dataset key "
            f"({reused_dataset_api_key[:16]}...) from a shared-mode peer "
            f"in workspace {fetched_workspace_id}. Dify caps each "
            f"workspace at 10 dataset keys; reuse keeps the quota intact "
            f"and skips the dataset-key creation round-trip.",
            err=True,
        )
        dataset_api_key = reused_dataset_api_key
        # fetched_workspace_id already set above for shared-mode reuse.
    else:
        # 6. Local validation + filesystem preflight passed — safe to
        # talk to Dify. ``_provision_dataset_api_key`` returns the
        # workspace_id alongside the new dataset key so we can pin the
        # entry to the tenant the session actually landed in
        # (codex review-9 P1).
        click.echo(f"Connecting to Dify at {dify_base_url} ...", err=True)
        try:
            fetched_workspace_id, dataset_api_key = asyncio.run(
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

    # 7. Rebuild the entry with the real dataset_api_key and do the
    # final merge + atomic write. The local-validation + writable
    # preflight passes already proved this should succeed; the only
    # way the writes below can fail now is a rare race (disk filled
    # up between preflight and write, parent dir deleted, etc.).
    # Codex review-3 P2: catching OSError here is defence in depth.
    # If the write does fail post-network, log the dataset key prefix
    # so the operator can find + delete the orphan in Dify Web UI.
    try:
        new_entry = _build_entry(dataset_api_key, workspace_id=fetched_workspace_id)
        existing = load_existing_registry(registry_path)
        merged = merge_customer(existing, new_entry, force=force)
        write_registry_atomic(registry_path, merged)
    except (RegistryMergeError, OSError) as exc:
        click.echo(f"ERROR: registry write failed after Dify key provisioning: {exc}", err=True)
        # Only emit the orphan warning when WE created the key in this
        # invocation. Reused shared-mode keys belong to another customer
        # already in registry.yaml; revoking would break their datasets.
        # Codex review-5 P2.
        if reused_dataset_api_key is None:
            click.echo(
                f"ORPHAN WARNING: a Dify dataset key was created "
                f"({dataset_api_key[:16]}...) but the registry write failed. "
                f"To avoid an orphan credential in Dify, manually revoke this "
                f"key in Dify Web UI → 知識庫 → 服務 API → 管理金鑰. "
                f"Re-run 'gateway-admin add-customer' after fixing the "
                f"filesystem issue.",
                err=True,
            )
        else:
            click.echo(
                "No orphan key to revoke — the dataset key was reused from "
                "an existing shared-mode peer. Re-run 'gateway-admin "
                "add-customer' after fixing the filesystem issue.",
                err=True,
            )
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
