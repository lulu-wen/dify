"""Tests for the admin CLI (PR #6).

Coverage:
- Spec parsing (model + embedding shorthand vs explicit)
- Registry merge (insert / refuse-duplicate / force-overwrite / atomic write)
- End-to-end ``add-customer`` via Click's ``CliRunner``, with DifyClient mocked
- Failure modes: Dify down, wrong creds, invalid customer_id
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from click.testing import CliRunner

from gateway.admin.cli import (
    _generate_sdk_key,
    _parse_embedding_spec,
    _parse_model_spec,
    cli,
)
from gateway.admin.registry_merge import (
    RegistryMergeError,
    load_existing_registry,
    merge_customer,
    write_registry_atomic,
)
from gateway.dify.client import ConsoleSession
from gateway.errors import DifyUpstreamError
from gateway.registry import (
    CustomerEntry,
    DifyConnection,
    ModelEntry,
)

# --------------------------------------------------------------------------- #
# SDK key generator
# --------------------------------------------------------------------------- #


class TestGenerateSdkKey:
    def test_starts_with_bsa_prefix(self) -> None:
        """Conforms to PR #5's L1 format check so startup_check passes."""
        assert _generate_sdk_key().startswith("bsa_")

    def test_high_entropy_no_collisions(self) -> None:
        """secrets.token_urlsafe(32) -> 256 bits; collision probability
        is astronomical. We just sanity-check 100 calls return uniques."""
        keys = {_generate_sdk_key() for _ in range(100)}
        assert len(keys) == 100


# --------------------------------------------------------------------------- #
# Spec parsing
# --------------------------------------------------------------------------- #


class TestParseModelSpec:
    def test_shorthand_defaults_to_openai_compatible_provider(self) -> None:
        m = _parse_model_spec("gemma-3n-e4b")
        assert m.id == "gemma-3n-e4b"
        assert m.name == "gemma-3n-e4b"
        assert m.provider.endswith("openai_api_compatible/openai_api_compatible")

    def test_explicit_form_passes_all_three_through(self) -> None:
        m = _parse_model_spec(
            "claude-sonnet:langgenius/anthropic/anthropic:claude-3-5-sonnet-20241022"
        )
        assert m.id == "claude-sonnet"
        assert m.provider == "langgenius/anthropic/anthropic"
        assert m.name == "claude-3-5-sonnet-20241022"

    def test_two_part_spec_rejected(self) -> None:
        """Only 1 or 3 colon-separated parts are valid. 2 is ambiguous —
        the operator probably forgot either the provider or the name."""
        import click

        with pytest.raises(click.BadParameter, match="must be 'id'"):
            _parse_model_spec("id:something")

    def test_four_part_spec_rejected(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _parse_model_spec("a:b:c:d")


class TestParseEmbeddingSpec:
    def test_shorthand_requires_default_endpoint(self) -> None:
        import click

        with pytest.raises(click.BadParameter, match="--embedding-endpoint-url"):
            _parse_embedding_spec("bge-m3", default_endpoint=None)

    def test_shorthand_with_default_endpoint_ok(self) -> None:
        e = _parse_embedding_spec(
            "bge-m3", default_endpoint="http://vllm-embed:8000/v1"
        )
        assert e.id == "bge-m3"
        assert e.endpoint_url == "http://vllm-embed:8000/v1"
        assert e.provider is not None

    def test_colon_in_spec_rejected(self) -> None:
        """URLs contain ``:``; the explicit ``id:url`` colon form would
        confuse the parser when callers (reasonably) want to embed a
        URL. We reject and tell them to use --embedding-endpoint-url."""
        import click

        with pytest.raises(click.BadParameter, match="URLs collide"):
            _parse_embedding_spec(
                "bge-m3:http://other:9000/v1", default_endpoint=None
            )


# --------------------------------------------------------------------------- #
# Registry merge
# --------------------------------------------------------------------------- #


def _make_entry(
    *,
    customer_id: str = "tenant-a",
    sdk_key: str = "bsa_tenant_a_abc",
    dataset_api_key: str = "dataset-real-key-xyz",
    base_url: str | None = None,
) -> CustomerEntry:
    """Build a minimal valid CustomerEntry for tests."""
    return CustomerEntry(
        sdk_key=sdk_key,
        customer_id=customer_id,
        dify=DifyConnection(
            base_url=base_url or f"http://dify-{customer_id}.test",
            console_email="admin@example.com",
            console_password="pw",
            dataset_api_key=dataset_api_key,
        ),
        models=[ModelEntry(id="m1", provider="prov", name="n")],
    )


class TestRegistryMerge:
    def test_load_missing_file_returns_empty_skeleton(self, tmp_path: Path) -> None:
        """First-ever onboarding has no registry yet — return the
        skeleton so merge_customer has somewhere to insert."""
        result = load_existing_registry(tmp_path / "does-not-exist.yaml")
        assert result == {"customers": []}

    def test_load_existing_file_ok(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.yaml"
        path.write_text("customers:\n  - sdk_key: bsa_x\n", encoding="utf-8")
        result = load_existing_registry(path)
        assert result == {"customers": [{"sdk_key": "bsa_x"}]}

    def test_load_non_dict_yaml_rejected(self, tmp_path: Path) -> None:
        """Top-level list isn't valid — registry must be a mapping."""
        path = tmp_path / "registry.yaml"
        path.write_text("- not a dict\n- still not a dict\n", encoding="utf-8")
        with pytest.raises(RegistryMergeError, match="mapping"):
            load_existing_registry(path)

    def test_merge_appends_new_customer(self) -> None:
        existing = {"customers": []}
        new = _make_entry(customer_id="tenant-a")
        merged = merge_customer(existing, new)
        assert len(merged["customers"]) == 1
        assert merged["customers"][0]["customer_id"] == "tenant-a"

    def test_merge_refuses_duplicate_without_force(self) -> None:
        a = _make_entry(customer_id="tenant-a", sdk_key="bsa_orig")
        existing = {"customers": [a.model_dump(mode="json", exclude_none=True)]}

        a_rotated = _make_entry(customer_id="tenant-a", sdk_key="bsa_new")
        with pytest.raises(RegistryMergeError, match="already exists"):
            merge_customer(existing, a_rotated, force=False)

    def test_merge_overwrites_with_force(self) -> None:
        a = _make_entry(customer_id="tenant-a", sdk_key="bsa_orig")
        existing = {"customers": [a.model_dump(mode="json", exclude_none=True)]}

        a_rotated = _make_entry(customer_id="tenant-a", sdk_key="bsa_new")
        merged = merge_customer(existing, a_rotated, force=True)
        assert len(merged["customers"]) == 1
        assert merged["customers"][0]["sdk_key"] == "bsa_new"

    def test_merge_runs_full_cross_customer_validation(self) -> None:
        """The merge invokes the same ``CustomerRegistry.from_entries``
        validator the runtime uses, so cross-customer invariants are
        enforced before disk is touched. We probe one such invariant:
        two distinct customer_ids sharing the same sdk_key — would let
        a request from tenant-b authenticate as tenant-a if it ever
        slipped through, so it must fail."""
        a = _make_entry(customer_id="tenant-a", sdk_key="bsa_shared")
        existing = {"customers": [a.model_dump(mode="json", exclude_none=True)]}

        # Different customer_id, same sdk_key → registry-level invariant
        # violation, caught by the validator inside merge_customer.
        collide = _make_entry(customer_id="tenant-b", sdk_key="bsa_shared")
        with pytest.raises(RegistryMergeError, match="failed validation"):
            merge_customer(existing, collide)

    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.yaml"
        write_registry_atomic(path, {"customers": []})
        assert path.exists()
        assert yaml.safe_load(path.read_text()) == {"customers": []}

    def test_atomic_write_cleans_up_tmp_on_failure(self, tmp_path: Path) -> None:
        """If write fails mid-flight, the .tmp file shouldn't leak."""
        path = tmp_path / "registry.yaml"

        # Patch yaml.safe_dump to raise after the file is opened.
        with patch("gateway.admin.registry_merge.yaml.safe_dump", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                write_registry_atomic(path, {"customers": []})

        # Neither the target nor the .tmp should exist.
        assert not path.exists()
        assert not path.with_suffix(".yaml.tmp").exists()


# --------------------------------------------------------------------------- #
# End-to-end ``add-customer`` via CliRunner
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_provision_dataset_key() -> Any:
    """Mock _provision_dataset_api_key so tests don't talk to real Dify.

    The function is what touches the network — replacing it covers the
    full DifyClient interaction (login + create_dataset_api_key) without
    needing a fake HTTP layer.
    """
    with patch(
        "gateway.admin.cli._provision_dataset_api_key",
        new=AsyncMock(return_value="dataset-mocked-key-12345678"),
    ) as mocked:
        yield mocked


class TestAddCustomerCommand:
    def test_happy_path_creates_registry_and_prints_sdk_key(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "add-customer",
                "--customer-id", "tenant-a",
                "--dify-base-url", "http://localhost",
                "--dify-admin-email", "admin@example.com",
                "--dify-admin-password", "pw",
                "--model", "gemma-3n-e4b",
                "--registry-path", str(registry_path),
            ],
        )

        assert result.exit_code == 0, result.output
        # SDK key printed on stdout (single line, easy for operators to copy)
        sdk_line = [line for line in result.output.splitlines() if line.startswith("bsa_")]
        assert len(sdk_line) == 1
        assert sdk_line[0].startswith("bsa_")

        # Registry file written + parses + contains the new customer
        assert registry_path.exists()
        loaded = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        assert loaded["customers"][0]["customer_id"] == "tenant-a"
        # Dify-provisioned key made it through end-to-end
        assert loaded["customers"][0]["dify"]["dataset_api_key"] == "dataset-mocked-key-12345678"

    def test_dify_unreachable_exits_with_code_2(
        self, tmp_path: Path
    ) -> None:
        """When Dify is down, the CLI must fail-fast — no registry written."""
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        with patch(
            "gateway.admin.cli._provision_dataset_api_key",
            new=AsyncMock(side_effect=DifyUpstreamError("Dify console login failed: ConnectError")),
        ):
            result = runner.invoke(
                cli,
                [
                    "add-customer",
                    "--customer-id", "tenant-a",
                    "--dify-base-url", "http://localhost",
                    "--dify-admin-email", "admin@example.com",
                    "--dify-admin-password", "pw",
                    "--model", "gemma-3n-e4b",
                    "--registry-path", str(registry_path),
                ],
            )

        assert result.exit_code == 2
        assert "Dify rejected" in result.output
        # Critical: registry must NOT exist — partial state is worse than no state
        assert not registry_path.exists()

    def test_explicit_sdk_key_must_have_prefix(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "add-customer",
                "--customer-id", "tenant-a",
                "--dify-base-url", "http://localhost",
                "--dify-admin-email", "admin@example.com",
                "--dify-admin-password", "pw",
                "--model", "gemma-3n-e4b",
                "--sdk-key", "wrong-prefix",
                "--registry-path", str(registry_path),
            ],
        )

        assert result.exit_code != 0
        assert "must start with 'bsa_'" in result.output

    def test_explicit_sdk_key_preserved(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """If --sdk-key is given, generation is skipped and the registry
        entry uses the operator-supplied value."""
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "add-customer",
                "--customer-id", "tenant-a",
                "--dify-base-url", "http://localhost",
                "--dify-admin-email", "admin@example.com",
                "--dify-admin-password", "pw",
                "--model", "gemma-3n-e4b",
                "--sdk-key", "bsa_operator_supplied_key",
                "--registry-path", str(registry_path),
            ],
        )

        assert result.exit_code == 0, result.output
        loaded = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        assert loaded["customers"][0]["sdk_key"] == "bsa_operator_supplied_key"

    def test_duplicate_customer_id_refused(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Adding the same customer_id twice without --force fails."""
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        # First call: succeed
        runner.invoke(cli, [
            "add-customer",
            "--customer-id", "tenant-a",
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", "pw",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        # Second call: should refuse
        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "tenant-a",   # same customer_id
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", "pw",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        assert result.exit_code == 4
        assert "already exists" in result.output

    def test_shared_mode_requires_shared_embedding(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "tenant-a",
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", "pw",
            "--mode", "shared",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        assert result.exit_code != 0
        assert "--shared-embedding-name is required" in result.output

    def test_uppercase_mode_normalised_before_dify_call(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Self-review P2-1 regression: --mode SHARED (uppercase) must
        be lowercased BEFORE we talk to Dify, not after. If we did the
        normalisation after the Dify round-trip, an uppercase --mode
        would create a dataset key on Dify side and then fail at
        pydantic validation, leaving an orphan key the operator has to
        clean up manually."""
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "tenant-a",
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", "pw",
            "--mode", "SHARED",                       # ← uppercase
            "--shared-embedding-name", "bge-m3",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        # Whole thing succeeds — pydantic accepts the lowercased value.
        assert result.exit_code == 0, result.output
        loaded = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        assert loaded["customers"][0]["dify"]["mode"] == "shared"

    def test_password_never_appears_in_output(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Defensive: SDK key is the only secret the CLI prints. The
        Dify admin password must never make it to stdout or stderr,
        even when the operator captures output for an issue report."""
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        # Use a distinctive password we can grep for.
        secret_password = "S3cret-D1fy-Adm1n-Pwd"

        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "tenant-a",
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", secret_password,
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        assert result.exit_code == 0, result.output
        # Password must not appear anywhere in stdout/stderr the
        # operator might paste into Slack / a bug report.
        assert secret_password not in result.output
        # Password is still persisted to registry.yaml — the CLI's job
        # is not to redact disk state, only operator-visible output.
        loaded = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        assert loaded["customers"][0]["dify"]["console_password"] == secret_password


# --------------------------------------------------------------------------- #
# Bonus: ConsoleSession fixture covers DifyClient.console_create_dataset_api_key
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_console_create_dataset_api_key_extracts_token() -> None:
    """Direct test of the DifyClient extension — uses respx-style
    Response mocking via patching the underlying httpx client."""
    from gateway.dify.client import DifyClient

    async with DifyClient(base_url="http://dify.test") as client:
        # Patch the internal httpx client's post to return a fake response.
        with patch.object(
            client._http,
            "post",
            new=AsyncMock(
                return_value=AsyncMock(
                    is_success=True,
                    status_code=200,
                    json=lambda: {"token": "dataset-test-token-xyz"},
                )
            ),
        ):
            session = ConsoleSession(access_token="acc", csrf_token="csrf")
            token = await client.console_create_dataset_api_key(session)
            assert token == "dataset-test-token-xyz"


# --------------------------------------------------------------------------- #
# Codex review-2: local validation must run BEFORE Dify network call
# --------------------------------------------------------------------------- #


class TestNoDifyOrphanOnLocalFailure:
    """Codex review-2 P2: any deterministic local failure (duplicate
    customer_id, bad slug for shared mode, malformed registry, etc.)
    must surface BEFORE we create a real ``dataset-*`` key on Dify.
    Otherwise the operator gets an orphan credential they have no
    easy way to discover.

    The mocking strategy: ``mock_provision_dataset_key`` patches the
    function that does the actual network call. Asserting it was
    called zero times proves the failure happened before the Dify
    round-trip.
    """

    def test_duplicate_customer_id_fails_before_network(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Re-running ``add-customer`` for an existing customer_id (no
        ``--force``) must fail at registry merge BEFORE the network
        call — otherwise we'd create a second Dify dataset key that
        nobody ends up referencing."""
        registry_path = tmp_path / "registry.yaml"

        # Seed the registry with an existing customer.
        registry_path.write_text(
            yaml.safe_dump({
                "customers": [
                    {
                        "sdk_key": "bsa_existing_a",
                        "customer_id": "tenant-a",
                        "dify": {
                            "base_url": "http://localhost",
                            "console_email": "admin@x",
                            "console_password": "pw",
                            "dataset_api_key": "dataset-real-existing",
                            "mode": "dedicated",
                        },
                        "models": [
                            {"id": "m1", "provider": "prov", "name": "n"},
                        ],
                    }
                ]
            }),
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "tenant-a",                # duplicate
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", "pw",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        # Non-zero exit, clear merge error
        assert result.exit_code != 0
        assert "registry merge would fail" in result.output

        # Critical: Dify network call must NOT have fired.
        assert mock_provision_dataset_key.call_count == 0, (
            "_provision_dataset_api_key was called despite local "
            "validation failure — this creates orphan dataset keys on "
            "Dify side (codex review-2 P2)."
        )

    def test_bad_slug_in_shared_mode_fails_before_network(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Shared-mode requires customer_id to match a slug regex
        (lowercase + hyphens). Uppercase / underscores fail pydantic
        validation. That failure must happen pre-network so a
        misspelled customer_id doesn't leave an orphan Dify key."""
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "Tenant_A",                # bad slug for shared mode
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", "pw",
            "--mode", "shared",
            "--shared-embedding-name", "bge-m3",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        assert result.exit_code != 0
        assert mock_provision_dataset_key.call_count == 0, (
            "Slug validation must fail BEFORE Dify is touched."
        )

    def test_malformed_yaml_gives_clean_error_not_traceback(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Codex review-2 P3: a hand-edited registry that ends up as
        invalid YAML should give the operator a clean ``Error: ...``
        message, not a python traceback. Also: no network call."""
        registry_path = tmp_path / "registry.yaml"
        # Write YAML that breaks the parser (unterminated quote).
        registry_path.write_text('customers:\n  - sdk_key: "bad_quote', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "tenant-a",
            "--dify-base-url", "http://localhost",
            "--dify-admin-email", "admin@x",
            "--dify-admin-password", "pw",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        assert result.exit_code != 0
        # Clean error message, not a python traceback
        assert "is not valid YAML" in result.output
        assert "Traceback" not in result.output
        # No Dify call
        assert mock_provision_dataset_key.call_count == 0
