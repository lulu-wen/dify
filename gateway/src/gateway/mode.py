"""Deployment-mode isolation strategy (PR #4).

Two Gateway deployment topologies share the same code:

- **dedicated**: each customer has their own Dify deployment (PR #1-#3
  default). The workspace itself is the tenant boundary, so the gateway
  uses customer-facing names verbatim — no App / Dataset prefixing.

- **shared**: many customers point at one Dify deployment + one workspace
  (PR #4). Soft isolation by prefixing names and filtering list responses.

Routers and the App manager should NOT branch on ``customer.dify.mode``
directly — pick a strategy via :func:`isolation_strategy_for` and call
its methods. This keeps mode-specific logic in one place and makes future
modes (e.g. per-region, per-environment) easy to add.

**Isolation limit**: this layer only protects against *normal call-path*
cross-customer leaks (listing, fetching, deleting). It does **not** protect
against Dify code bugs, admin account misuse, or DB-direct access — those
limits are documented in the project Notion overview §4.3.
"""

from __future__ import annotations

from typing import Protocol

from gateway.registry import CustomerEntry

# Use double underscore so the prefix can't collide with a name component
# that already contains a single underscore (and matches the YAML doc
# convention in the PR #4 spec). The customer_id slug pattern already
# forbids "__" so this round-trips losslessly.
_DATASET_PREFIX_SEP = "__"
# Hyphen for App names because Dify enforces a sane app-name charset (no
# underscores allowed historically), and ``-`` is universally safe.
_APP_PREFIX_SEP = "-"


class IsolationStrategy(Protocol):
    """Per-mode rules for naming App / Dataset resources in Dify."""

    @property
    def is_shared(self) -> bool:
        """``True`` when multiple customers share one Dify workspace."""

    def app_name(self, customer_id: str, model_id: str) -> str:
        """Compute the Dify App name for ``(customer_id, model_id)``.

        Dedicated mode: ``model_id`` (no prefix; the workspace is the
        customer). Shared mode: ``"{customer_id}-{model_id}"`` so two
        customers asking for the same model build distinct Apps.
        """

    def dataset_name_to_dify(self, customer_id: str, name: str) -> str:
        """Map a customer-facing dataset name to the name stored in Dify."""

    def dataset_name_from_dify(
        self, customer_id: str, dify_name: str
    ) -> str | None:
        """Reverse-map a Dify dataset name back to the customer-facing one.

        Returns ``None`` if the dataset doesn't belong to this customer
        (router uses this to filter list responses and reject cross-customer
        get/delete with 404 — never 403, which would leak existence).
        """

    def dataset_belongs_to(self, customer_id: str, dify_name: str) -> bool:
        """``True`` if ``dify_name`` was created by ``customer_id``."""


class DedicatedStrategy:
    """Each customer has their own Dify — no prefixing needed.

    All ownership checks are trivially true: the workspace ITSELF is the
    isolation boundary, so anything visible to the customer IS the
    customer's.
    """

    @property
    def is_shared(self) -> bool:
        return False

    def app_name(self, customer_id: str, model_id: str) -> str:
        return model_id

    def dataset_name_to_dify(self, customer_id: str, name: str) -> str:
        return name

    def dataset_name_from_dify(
        self, customer_id: str, dify_name: str
    ) -> str | None:
        return dify_name

    def dataset_belongs_to(self, customer_id: str, dify_name: str) -> bool:
        return True


class SharedStrategy:
    """Multiple customers in one Dify workspace — prefix everything.

    The prefix is the only thing standing between customer A and seeing /
    deleting / modifying customer B's resources from the gateway's
    happy-path. Anything that bypasses the gateway (direct DB access,
    Dify console login, ...) defeats this — it is **soft isolation only**,
    and is documented as such in the PR #4 spec.
    """

    @property
    def is_shared(self) -> bool:
        return True

    def app_name(self, customer_id: str, model_id: str) -> str:
        return f"{customer_id}{_APP_PREFIX_SEP}{model_id}"

    def dataset_name_to_dify(self, customer_id: str, name: str) -> str:
        return f"{customer_id}{_DATASET_PREFIX_SEP}{name}"

    def dataset_name_from_dify(
        self, customer_id: str, dify_name: str
    ) -> str | None:
        prefix = f"{customer_id}{_DATASET_PREFIX_SEP}"
        if not dify_name.startswith(prefix):
            return None
        return dify_name[len(prefix):]

    def dataset_belongs_to(self, customer_id: str, dify_name: str) -> bool:
        return dify_name.startswith(f"{customer_id}{_DATASET_PREFIX_SEP}")


# Strategy instances are stateless — reuse module-level singletons to avoid
# per-request allocations.
_DEDICATED = DedicatedStrategy()
_SHARED = SharedStrategy()


def isolation_strategy_for(customer: CustomerEntry) -> IsolationStrategy:
    """Pick the strategy matching ``customer.dify.mode``."""
    if customer.dify.mode == "shared":
        return _SHARED
    return _DEDICATED
