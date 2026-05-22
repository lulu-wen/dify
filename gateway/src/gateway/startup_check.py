"""Registry startup health check (PR #5).

Validates every customer entry before serving traffic so configuration
mistakes (placeholder dataset keys, wrong console password, unreachable
Dify) surface at boot instead of mid-request 401 / 502.

Failure modes this catches that PR #1-#4 lets through:

- ``dataset_api_key: "dataset-not-used-in-pr1"`` (or any placeholder)
  passes pydantic validation because it's a non-empty string. The first
  ``POST /v1/datasets`` call from the SDK then 401s with
  ``Access token is invalid`` — confusing for operators who think the
  gateway is broken.
- Dify container is stopped, but gateway still boots and rejects every
  request with 502 ``dify_upstream_error``.
- Console password was rotated in Dify Web UI without updating the
  registry — AppManager's lazy build then fails with auth-shaped errors
  the first time anyone tries chat.

Layered design:

- **L1 Format** — regex / prefix checks on key strings. Zero-cost,
  synchronous. Runs first across the whole registry.
- **L2 Connectivity** — TCP-level reachability of ``dify.base_url`` via
  the same httpx client the gateway will use at runtime. Differentiates
  "Dify is down" from "wrong credentials".
- **L3 Console auth** — Real ``POST /console/api/login``. Validates
  ``console_email`` + ``console_password`` work for App build.
- **L4 Dataset auth** — ``GET /v1/datasets?limit=1`` with the stored
  ``dataset_api_key``. Validates the key is non-placeholder + has
  workspace access.

L2-L4 run per-customer in parallel via :func:`asyncio.gather`, so one
slow customer doesn't delay the others. L1 is synchronous and runs
first, before any network.

Modes (selected via :class:`gateway.config.Settings.strict_startup`):

- **Strict** — abort startup on any failure. Raises so uvicorn exits
  with non-zero status, which is what container orchestrators (k8s,
  docker compose) need to mark the pod unhealthy.
- **Warn-only** (default) — log warnings, continue. Suits dev where
  Dify might still be coming up via docker compose.

Why we don't always run in strict mode: docker compose / k8s startup
ordering isn't deterministic; sometimes the gateway boots a few seconds
before Dify is ready. Warn-only lets the gateway recover when Dify
finishes booting. Production sets ``GATEWAY_STRICT_STARTUP=1`` to
trade tolerance for fail-fast guarantees.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import structlog

from gateway.dify.client import DifyClient
from gateway.errors import DifyTimeoutError, DifyUpstreamError, UpstreamClientError
from gateway.registry import CustomerEntry, CustomerRegistry

logger = structlog.get_logger(__name__)


_SDK_KEY_PREFIX = "bsa_"
_DATASET_KEY_PREFIX = "dataset-"

# Length cap for redacted previews of secret-shaped strings in log messages.
# Long enough to confirm "right prefix" without leaking the random tail.
_KEY_PREVIEW_LEN = 16


@dataclass(frozen=True)
class CheckIssue:
    """One health-check failure scoped to a single customer.

    ``level`` is one of ``"L1"`` / ``"L2"`` / ``"L3"`` / ``"L4"``;
    ``hint`` carries an actionable suggestion when the failure has a
    known fix (e.g. how to regenerate a dataset API key). Hint is
    optional because some L2 / L4 failures are environment-specific.
    """

    customer_id: str
    level: str
    message: str
    hint: str | None = None


def _redact(secret: str, *, length: int = _KEY_PREVIEW_LEN) -> str:
    """Show only the leading slice of a secret for diagnostic output.

    We intentionally keep the prefix (so operators can recognise the key
    family — ``bsa_``, ``dataset-``, ``app-``) while hiding the
    high-entropy tail that would let a log reader actually use the key.
    """
    if not secret:
        return "<empty>"
    return secret[:length] + ("..." if len(secret) > length else "")


def check_format(customer: CustomerEntry) -> list[CheckIssue]:
    """L1: format / prefix checks. Synchronous, no I/O.

    Catches placeholder strings that pass pydantic but fail at runtime —
    notably ``dataset-not-used-in-pr1`` written when only the chat path
    was being verified, which 401s the first time the customer calls
    ``POST /v1/datasets``.
    """
    issues: list[CheckIssue] = []

    sdk = customer.sdk_key
    if not sdk.startswith(_SDK_KEY_PREFIX):
        issues.append(
            CheckIssue(
                customer_id=customer.customer_id,
                level="L1",
                message=(
                    f"sdk_key must start with '{_SDK_KEY_PREFIX}' "
                    f"(got '{_redact(sdk)}')"
                ),
                hint=(
                    "SDK keys are operator-issued; the prefix is a convention "
                    "that surfaces 'pasted the wrong key' typos at startup."
                ),
            )
        )

    ds_key = customer.dify.dataset_api_key
    if not ds_key.startswith(_DATASET_KEY_PREFIX):
        issues.append(
            CheckIssue(
                customer_id=customer.customer_id,
                level="L1",
                message=(
                    f"dataset_api_key must start with '{_DATASET_KEY_PREFIX}' "
                    f"(got '{_redact(ds_key)}')"
                ),
                hint=(
                    "Generate one in Dify Web UI → Knowledge → 服務 API → "
                    "建立新的金鑰. Placeholders like 'dataset-not-used-in-pr1' "
                    "pass startup but trigger 401 on the first /v1/datasets call."
                ),
            )
        )

    return issues


async def _check_runtime(
    customer: CustomerEntry,
    client: DifyClient,
) -> list[CheckIssue]:
    """L2-L4: live network checks against the customer's Dify deployment.

    L3 (console_login) is called first because it exercises both L2 (TCP
    connectivity) and L3 (credentials) in one round-trip — we split them
    apart in the resulting :class:`CheckIssue` by exception type, so
    operators get the right hint without us making two requests.

    L4 (dataset key) is independent of L3 — they use different bearer
    tokens — so we still try it even if L3 fails. The exception is L2:
    if the network is unreachable, L4 will fail with the same error
    and we skip it to avoid duplicating the message.
    """
    issues: list[CheckIssue] = []

    network_down = False

    # L3 first (also covers L2 implicitly).
    try:
        await client.console_login(
            customer.dify.console_email,
            customer.dify.console_password,
        )
    except (httpx.RequestError, DifyTimeoutError, OSError) as exc:
        # Network-shaped failure → L2.
        network_down = True
        issues.append(
            CheckIssue(
                customer_id=customer.customer_id,
                level="L2",
                message=f"cannot reach {customer.dify.base_url}: {exc}",
                hint=(
                    "Verify the Dify deployment is running (e.g. "
                    "`docker compose ps`) and that base_url resolves + is "
                    "routable from where the gateway runs."
                ),
            )
        )
    except DifyUpstreamError as exc:
        # Auth-shaped failure → L3.
        issues.append(
            CheckIssue(
                customer_id=customer.customer_id,
                level="L3",
                message=f"console_login rejected: {exc}",
                hint=(
                    "Confirm console_email + console_password in the registry "
                    "match the Dify admin account. A password rotated in Dify "
                    "Web UI requires updating the registry too."
                ),
            )
        )

    # Skip L4 only if L2 actually failed — L3 failure shouldn't block it.
    if network_down:
        return issues

    # L4 dataset auth.
    try:
        await client.list_datasets(
            dataset_api_key=customer.dify.dataset_api_key,
            page=1,
            limit=1,
        )
    except (DifyUpstreamError, UpstreamClientError) as exc:
        issues.append(
            CheckIssue(
                customer_id=customer.customer_id,
                level="L4",
                message=f"dataset_api_key rejected by Dify: {exc}",
                hint=(
                    "The key may be revoked, belong to a different workspace, "
                    "or still be the PR #1 placeholder. Regenerate in Dify "
                    "Web UI → Knowledge → 服務 API."
                ),
            )
        )
    except (httpx.RequestError, DifyTimeoutError, OSError) as exc:
        # Network blip between L3 success and L4 — unusual but recoverable.
        issues.append(
            CheckIssue(
                customer_id=customer.customer_id,
                level="L4",
                message=f"dataset_api_key check network error: {exc}",
            )
        )

    return issues


async def validate_registry(
    registry: CustomerRegistry,
    client_factory: Callable[[CustomerEntry], DifyClient],
) -> list[CheckIssue]:
    """Run L1-L4 against every customer; return aggregated issues.

    L1 runs first, synchronously, across the whole registry. L2-L4
    then run per-customer in parallel — one customer with a slow / down
    Dify shouldn't block checks of other customers.

    ``client_factory`` is injected so tests can pass a fake. Production
    uses the same factory the routers use (gateway/main.py).
    """
    issues: list[CheckIssue] = []

    # L1 first — sync, blocks before any network.
    for customer in registry.customers():
        issues.extend(check_format(customer))

    # L2-L4 in parallel per-customer. Snapshot the list once so the zip
    # below pairs results with the same customer objects we kicked off.
    customers = registry.customers()
    runtime_tasks = [
        _check_runtime(customer, client_factory(customer))
        for customer in customers
    ]

    if not runtime_tasks:
        return issues

    runtime_results = await asyncio.gather(*runtime_tasks, return_exceptions=True)
    for customer, result in zip(customers, runtime_results, strict=True):
        if isinstance(result, BaseException):
            # An exception type not handled by ``_check_runtime``'s try/except.
            # Should never happen in practice — log loud so the bug is obvious.
            issues.append(
                CheckIssue(
                    customer_id=customer.customer_id,
                    level="L2",
                    message=(
                        f"unexpected error during runtime check: "
                        f"{type(result).__name__}: {result}"
                    ),
                )
            )
        else:
            issues.extend(result)

    return issues


async def run_startup_check(
    registry: CustomerRegistry,
    client_factory: Callable[[CustomerEntry], DifyClient],
    *,
    strict: bool,
) -> None:
    """Orchestrator called from the FastAPI lifespan.

    Logs every issue regardless of mode (so SIEM / journal capture them).
    Raises :class:`RuntimeError` only when ``strict=True`` AND issues
    were found, which causes uvicorn to exit non-zero.
    """
    issues = await validate_registry(registry, client_factory)

    if not issues:
        logger.info("startup.health_check_ok", customers=len(registry))
        return

    for issue in issues:
        logger.warning(
            "startup.health_check_issue",
            customer_id=issue.customer_id,
            level=issue.level,
            message=issue.message,
            hint=issue.hint,
        )

    if strict:
        logger.error(
            "startup.aborted",
            issue_count=len(issues),
            strict_mode=True,
        )
        raise RuntimeError(
            f"GATEWAY_STRICT_STARTUP=1: {len(issues)} startup health check "
            "issue(s); see logs for details. Set strict_startup=False to "
            "downgrade to warn-only."
        )

    logger.warning(
        "startup.health_check_warn_only",
        issue_count=len(issues),
        message=(
            "Startup health check found issues but strict_startup=False; "
            "continuing. Requests against these customers may still 401/502."
        ),
    )
