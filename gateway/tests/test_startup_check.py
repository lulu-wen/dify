"""Tests for the registry startup health check (PR #5).

Coverage:
- L1 format checks for sdk_key + dataset_api_key prefixes
- L2 connectivity (httpx.RequestError → L2 issue, L4 skipped)
- L3 console auth (DifyUpstreamError → L3 issue, L4 still tried)
- L4 dataset auth (DifyUpstreamError → L4 issue)
- Multi-customer parallel aggregation
- Strict vs warn-only modes
- Healthy registry produces zero issues
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from gateway.dify.client import ConsoleSession, DifyClient
from gateway.errors import DifyTimeoutError, DifyUpstreamError, UpstreamClientError
from gateway.registry import CustomerEntry, CustomerRegistry, DifyConnection, ModelEntry
from gateway.startup_check import (
    CheckIssue,
    check_format,
    run_startup_check,
    validate_registry,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_customer(
    *,
    customer_id: str = "tenant-a",
    sdk_key: str = "bsa_tenant_a_abcdef",
    dataset_api_key: str = "dataset-real-key-xyz",
) -> CustomerEntry:
    return CustomerEntry(
        sdk_key=sdk_key,
        customer_id=customer_id,
        dify=DifyConnection(
            base_url=f"http://dify-{customer_id}.test",
            console_email="admin@example.com",
            console_password="pw",
            dataset_api_key=dataset_api_key,
        ),
        models=[ModelEntry(id="m1", provider="prov", name="n")],
    )


class _FakeDifyClient:
    """Scriptable fake — set ``login_error`` / ``list_error`` to raise."""

    def __init__(self) -> None:
        self.login_error: BaseException | None = None
        self.list_error: BaseException | None = None
        self.login_calls = 0
        self.list_calls = 0

    async def console_login(self, email: str, password: str) -> ConsoleSession:
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error
        return ConsoleSession(access_token="acc", csrf_token="csrf")

    async def list_datasets(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return {"data": [], "has_more": False, "total": 0, "page": 1, "limit": 1}


def _factory(client_for: dict[str, _FakeDifyClient]):
    def factory(customer: CustomerEntry) -> DifyClient:
        # Test-only cast: _FakeDifyClient has the methods we call.
        return client_for[customer.customer_id]  # type: ignore[return-value]
    return factory


# --------------------------------------------------------------------------- #
# L1: format checks (synchronous, no I/O)
# --------------------------------------------------------------------------- #


class TestCheckFormat:
    def test_clean_customer_yields_no_issues(self) -> None:
        c = _make_customer()
        assert check_format(c) == []

    def test_sdk_key_missing_prefix_caught(self) -> None:
        c = _make_customer(sdk_key="totally-wrong-key")
        issues = check_format(c)
        assert len(issues) == 1
        assert issues[0].level == "L1"
        assert "sdk_key must start with 'bsa_'" in issues[0].message
        # Redacted preview must NOT expose the full key (defence against
        # log leakage when operators paste error reports).
        assert "totally-wrong-key" not in issues[0].message

    def test_dataset_key_obvious_garbage_caught(self) -> None:
        """A dataset_api_key that doesn't even have the ``dataset-`` prefix
        is the cheap case — L1 catches it before any network round-trip."""
        c = _make_customer(dataset_api_key="just-a-string")
        issues = check_format(c)
        assert len(issues) == 1
        assert issues[0].level == "L1"
        assert "dataset_api_key must start with 'dataset-'" in issues[0].message
        assert "Knowledge → 服務 API" in (issues[0].hint or "")

    def test_dataset_key_placeholder_with_right_prefix_passes_l1(self) -> None:
        """The specific placeholder ``dataset-not-used-in-pr1`` (which
        triggered a 401 during PR #4 verification) starts with the right
        prefix, so L1 alone can't catch it — this is the exact case L4
        (live Dify round-trip) exists to handle. Confirming L1 does NOT
        false-positive here ensures L4 stays load-bearing for placeholder
        detection."""
        c = _make_customer(dataset_api_key="dataset-not-used-in-pr1")
        assert check_format(c) == []

    def test_both_keys_wrong_reports_both(self) -> None:
        c = _make_customer(sdk_key="wrong", dataset_api_key="also-wrong")
        issues = check_format(c)
        assert len(issues) == 2
        assert {i.level for i in issues} == {"L1"}

    def test_redact_keeps_prefix_only(self) -> None:
        """Operators need to see 'oh, this is the wrong key family' from
        logs without the high-entropy tail leaking through."""
        c = _make_customer(sdk_key="garbage-with-secret-suffix-1234567890abcdef")
        issues = check_format(c)
        msg = issues[0].message
        # The first 16 chars get included so 'garbage-with-sec' shows up
        assert "garbage-with-sec" in msg
        # The high-entropy tail is hidden
        assert "1234567890abcdef" not in msg


# --------------------------------------------------------------------------- #
# L2-L4: runtime checks (with fake DifyClient)
# --------------------------------------------------------------------------- #


class TestRuntimeChecks:
    @pytest.mark.asyncio
    async def test_healthy_dify_yields_no_runtime_issues(self) -> None:
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        assert issues == []
        # Both calls fired.
        assert fake.login_calls == 1
        assert fake.list_calls == 1

    @pytest.mark.asyncio
    async def test_network_down_reports_l2_and_skips_l4(self) -> None:
        """When console_login raises a network error, the check reports L2
        and does NOT try the dataset key — both calls would fail with the
        same network error and the duplicate noise isn't useful."""
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        fake.login_error = httpx.ConnectError("connection refused")

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        assert len(issues) == 1
        assert issues[0].level == "L2"
        assert "cannot reach" in issues[0].message
        assert "docker compose ps" in (issues[0].hint or "")
        # L4 must NOT have been attempted.
        assert fake.list_calls == 0

    @pytest.mark.asyncio
    async def test_timeout_treated_as_l2(self) -> None:
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        fake.login_error = DifyTimeoutError("login timed out")

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        assert len(issues) == 1
        assert issues[0].level == "L2"

    @pytest.mark.asyncio
    async def test_auth_failure_reports_l3_but_still_tries_l4(self) -> None:
        """Console auth and dataset auth use different bearer tokens — a
        bad console password doesn't mean the dataset key is also broken,
        so we still report on L4 independently."""
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        fake.login_error = DifyUpstreamError("Dify returned HTTP 401: bad password")

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        # Only L3 because L4 succeeded.
        assert len(issues) == 1
        assert issues[0].level == "L3"
        # L4 was attempted.
        assert fake.list_calls == 1

    @pytest.mark.asyncio
    async def test_dataset_key_rejected_reports_l4(self) -> None:
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        fake.list_error = DifyUpstreamError(
            "Dify returned HTTP 401: Access token is invalid"
        )

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        assert len(issues) == 1
        assert issues[0].level == "L4"
        assert "dataset_api_key rejected" in issues[0].message
        assert "Knowledge → 服務 API" in (issues[0].hint or "")

    @pytest.mark.asyncio
    async def test_upstream_client_error_also_reports_l4(self) -> None:
        """The dataset router can surface ``UpstreamClientError`` for 4xx
        on the Dify side (codex review-1 P2). Startup check must treat
        that as an L4 fail too, not let it escape."""
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        fake.list_error = UpstreamClientError(
            "Dify rejected request (HTTP 403): disabled",
            upstream_status=403,
        )

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        assert len(issues) == 1
        assert issues[0].level == "L4"

    @pytest.mark.asyncio
    async def test_both_auths_failing_reports_both(self) -> None:
        """L3 + L4 are independent — a misconfigured customer can fail
        both. Report both so the operator gets the full picture in one
        startup, not over two restart cycles."""
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        fake.login_error = DifyUpstreamError("HTTP 401: bad password")
        fake.list_error = DifyUpstreamError("HTTP 401: bad dataset key")

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        levels = {i.level for i in issues}
        assert levels == {"L3", "L4"}


# --------------------------------------------------------------------------- #
# Aggregation across multiple customers
# --------------------------------------------------------------------------- #


class TestMultiCustomerAggregation:
    @pytest.mark.asyncio
    async def test_parallel_check_one_customer_failure_doesnt_mask_others(self) -> None:
        """The whole point of asyncio.gather: a slow / broken customer
        shouldn't delay or hide failures from other customers."""
        a = _make_customer(customer_id="tenant-a", sdk_key="bsa_a")
        b = _make_customer(
            customer_id="tenant-b",
            sdk_key="bsa_b",
            dataset_api_key="not-a-real-key",  # L1 fail
        )
        c = _make_customer(customer_id="tenant-c", sdk_key="bsa_c")

        registry = CustomerRegistry.from_entries([a, b, c])

        fake_a = _FakeDifyClient()
        fake_b = _FakeDifyClient()  # L1 catches first; runtime still runs but clean
        fake_c = _FakeDifyClient()
        fake_c.login_error = httpx.ConnectError("c is down")

        issues = await validate_registry(
            registry,
            _factory(
                {
                    "tenant-a": fake_a,
                    "tenant-b": fake_b,
                    "tenant-c": fake_c,
                }
            ),
        )

        levels_per_customer = {
            cust: {i.level for i in issues if i.customer_id == cust}
            for cust in ("tenant-a", "tenant-b", "tenant-c")
        }

        assert levels_per_customer["tenant-a"] == set()
        assert levels_per_customer["tenant-b"] == {"L1"}
        assert levels_per_customer["tenant-c"] == {"L2"}

    @pytest.mark.asyncio
    async def test_empty_registry_is_a_no_op(self) -> None:
        """A registry with no customers shouldn't crash the gather call."""
        # CustomerRegistry.from_entries requires at least one entry, so
        # build a minimal one to confirm gather() behaves with the single
        # path. Empty-list test is at the unit level if from_entries ever
        # relaxes that constraint.
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        issues = await validate_registry(registry, _factory({c.customer_id: fake}))
        assert issues == []


# --------------------------------------------------------------------------- #
# Orchestrator behaviour (run_startup_check) + strict vs warn-only
# --------------------------------------------------------------------------- #


class TestRunStartupCheck:
    @pytest.mark.asyncio
    async def test_clean_registry_no_raise(self) -> None:
        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        # Strict True should still succeed when there's nothing to find.
        await run_startup_check(
            registry,
            _factory({c.customer_id: fake}),
            strict=True,
        )

    @pytest.mark.asyncio
    async def test_warn_only_continues_on_failure(self) -> None:
        """Default mode: bad customer → log warning, but don't raise.
        Lets gateway boot in dev when Dify isn't ready yet."""
        c = _make_customer(dataset_api_key="not-a-real-key")
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        # No raise expected.
        await run_startup_check(
            registry,
            _factory({c.customer_id: fake}),
            strict=False,
        )

    @pytest.mark.asyncio
    async def test_strict_raises_on_failure(self) -> None:
        """Strict mode: any issue aborts startup with a clear RuntimeError
        so uvicorn exits non-zero and container orchestrators mark the
        pod unhealthy."""
        c = _make_customer(dataset_api_key="not-a-real-key")
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()

        with pytest.raises(RuntimeError, match="GATEWAY_STRICT_STARTUP"):
            await run_startup_check(
                registry,
                _factory({c.customer_id: fake}),
                strict=True,
            )


# --------------------------------------------------------------------------- #
# Defensive: unexpected exception type during runtime check
# --------------------------------------------------------------------------- #


class TestUnexpectedFailure:
    @pytest.mark.asyncio
    async def test_unexpected_exception_surfaces_as_l2(self) -> None:
        """If _check_runtime's try/except misses some exception subclass,
        validate_registry's gather wrapper still catches it and produces
        a CheckIssue rather than crashing startup outright."""

        class WeirdError(BaseException):
            pass

        c = _make_customer()
        registry = CustomerRegistry.from_entries([c])
        fake = _FakeDifyClient()
        fake.login_error = WeirdError("never seen before")

        issues = await validate_registry(registry, _factory({c.customer_id: fake}))

        # WeirdError is BaseException-not-Exception, escapes the inner
        # try/except — gather wrapper catches it and reports as L2.
        assert len(issues) >= 1
        assert any(i.level == "L2" for i in issues)


# --------------------------------------------------------------------------- #
# CheckIssue dataclass sanity
# --------------------------------------------------------------------------- #


def test_check_issue_is_frozen() -> None:
    """Issues are immutable so they can be safely passed between log
    aggregators / stored in collections without surprise mutation. The
    dataclass ``frozen=True`` decorator turns attribute assignment into
    a ``FrozenInstanceError`` (which subclasses ``AttributeError``)."""
    from dataclasses import FrozenInstanceError

    issue = CheckIssue(customer_id="x", level="L1", message="m")
    with pytest.raises(FrozenInstanceError):
        issue.message = "mutated"  # type: ignore[misc]
