"""Tests for the admin CLI (PR #6).

Coverage:
- Spec parsing (model + embedding shorthand vs explicit)
- Registry merge (insert / refuse-duplicate / force-overwrite / atomic write)
- End-to-end ``add-customer`` via Click's ``CliRunner``, with DifyClient mocked
- Failure modes: Dify down, wrong creds, invalid customer_id
"""

from __future__ import annotations

import os
import stat
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
    find_shared_workspace_dataset_key,
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

    def test_registry_with_non_mapping_customer_entry_fails_cleanly(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Codex review-3 P3: ``customers: [null]`` or
        ``customers: [- "bad-string"]`` previously crashed in
        ``_find_customer_index`` with AttributeError when ``.get()``
        was called on a non-dict. Now must surface as a clean
        RegistryMergeError before any network call."""
        registry_path = tmp_path / "registry.yaml"
        # A list with a non-mapping entry (None / string instead of dict)
        registry_path.write_text(
            "customers:\n  - null\n",
            encoding="utf-8",
        )

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
        assert "must be a mapping" in result.output
        assert "Traceback" not in result.output
        assert mock_provision_dataset_key.call_count == 0

    def test_unwritable_registry_path_fails_before_network(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Codex review-3 P2: if the registry's parent dir cannot be
        created (e.g., parent path is itself a regular file), the
        writable preflight must catch it BEFORE the network call.
        Otherwise ``write_registry_atomic`` raises OSError post-network
        and we leave an orphan dataset key on Dify side.

        Click's own ``type=click.Path(dir_okay=False)`` catches the
        simpler "path is a directory" case at flag-parsing time — this
        test exercises the case Click doesn't see: parent that can't
        be turned into a writable directory.
        """
        # ``blockage`` is a regular file. Asking the CLI to write a
        # registry "inside" it can't work because the parent isn't a
        # directory and ``parent.mkdir`` would fail.
        blockage = tmp_path / "blockage"
        blockage.write_text("not a directory", encoding="utf-8")
        registry_path = blockage / "registry.yaml"

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
        assert "registry path not writable" in result.output
        assert "Traceback" not in result.output
        # The preflight must run BEFORE Dify is touched.
        assert mock_provision_dataset_key.call_count == 0, (
            "Writable preflight must fire BEFORE Dify is touched "
            "(codex review-3 P2 — orphan-key avoidance)."
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


# --------------------------------------------------------------------------- #
# Codex review-4: registry.yaml must NOT end up world-readable
# --------------------------------------------------------------------------- #


class TestRegistryFilePermissions:
    """Codex review-4 P2: ``registry.yaml`` contains plaintext secrets
    (``console_password``, ``dataset_api_key``, ``sdk_key``). The
    atomic write path used to call ``tmp.open("w")`` which honours the
    umask — on a typical Linux box (umask 022) that produces a
    ``0644`` (world-readable) file. ``os.replace`` then made the new
    perms the registry's, silently widening a previously-private file.

    These tests assert the post-write mode is ``0600`` (owner-only)
    and that an existing stricter mode is preserved. POSIX-only —
    Windows file ACLs use a different model.
    """

    @pytest.mark.skipif(
        os.name != "posix", reason="POSIX file mode bits"
    )
    def test_newly_created_registry_is_owner_only(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Fresh registry from add-customer must be 0600 — even when
        the operator's shell umask is 022."""
        registry_path = tmp_path / "registry.yaml"
        runner = CliRunner()

        # Force a permissive umask so we'd FAIL if the code didn't
        # explicitly set restrictive mode.
        old_umask = os.umask(0o022)
        try:
            result = runner.invoke(cli, [
                "add-customer",
                "--customer-id", "tenant-a",
                "--dify-base-url", "http://localhost",
                "--dify-admin-email", "admin@x",
                "--dify-admin-password", "pw",
                "--model", "gemma-3n-e4b",
                "--registry-path", str(registry_path),
            ])
        finally:
            os.umask(old_umask)

        assert result.exit_code == 0, result.output
        mode_bits = stat.S_IMODE(registry_path.stat().st_mode)
        # Group + other must have ZERO bits.
        assert mode_bits & 0o077 == 0, (
            f"registry.yaml is mode {oct(mode_bits)}; expected 0600-like "
            f"(no group/other bits). Plaintext credentials cannot be "
            f"world-readable."
        )
        # And specifically owner-read+write (not e.g. owner-execute).
        assert mode_bits & 0o600 == 0o600

    @pytest.mark.skipif(
        os.name != "posix", reason="POSIX file mode bits"
    )
    def test_existing_stricter_mode_is_preserved(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Some ops keep registry.yaml at 0400 (owner-read-only)
        between updates and ``chmod u+w`` before running the CLI.
        After the CLI write, if the file was ALREADY stricter than
        0600 we shouldn't widen it back to 0600."""
        registry_path = tmp_path / "registry.yaml"
        # Seed with a valid empty registry at 0400.
        registry_path.write_text("customers: []\n", encoding="utf-8")
        os.chmod(registry_path, 0o400)
        # Then re-enable owner write so the CLI can replace it.
        # (Real ops would chmod u+w manually too.)
        os.chmod(registry_path, 0o600)
        # Drop it back to 0400 — what the post-write should preserve.
        os.chmod(registry_path, 0o400)
        # Now make it writable just for the test's restore step:
        # actually, write_registry_atomic uses os.replace (not in-place
        # open), so target perms don't need to allow write. The 0400
        # is exactly the case we want to test.

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

        assert result.exit_code == 0, result.output
        mode_bits = stat.S_IMODE(registry_path.stat().st_mode)
        assert mode_bits == 0o400, (
            f"existing mode 0400 was widened to {oct(mode_bits)} on write"
        )


# --------------------------------------------------------------------------- #
# Codex review-5: shared-mode dataset-key reuse + exclusive temp file
# --------------------------------------------------------------------------- #


class TestFindSharedWorkspaceDatasetKey:
    """Codex review-5 P2 #1 unit tests: locate an existing shared-mode
    peer's ``dataset_api_key`` in the on-disk registry so the next
    shared onboarding for the same workspace can reuse it instead of
    burning one of Dify's 10 workspace-scoped keys.
    """

    def test_returns_key_for_matching_shared_peer(self) -> None:
        registry_data: dict[str, Any] = {
            "customers": [
                {
                    "sdk_key": "bsa_peer",
                    "customer_id": "peer",
                    "dify": {
                        "base_url": "http://shared-dify",
                        "console_email": "ws-admin@example.com",
                        "console_password": "pw",
                        "dataset_api_key": "dataset-workspace-key-9876",
                        "mode": "shared",
                        "shared_embedding_model": {
                            "name": "bge-m3",
                            "provider": "p",
                        },
                    },
                    "models": [{"id": "m1", "provider": "p", "name": "n"}],
                }
            ]
        }
        found = find_shared_workspace_dataset_key(
            registry_data,
            base_url="http://shared-dify",
            console_email="ws-admin@example.com",
        )
        assert found == "dataset-workspace-key-9876"

    def test_trailing_slash_in_base_url_still_matches(self) -> None:
        """Dify client rstrips trailing slashes so ``http://x`` and
        ``http://x/`` resolve to the same upstream — peer lookup must
        agree, otherwise an operator could end up with parallel keys
        for what is functionally one workspace."""
        registry_data: dict[str, Any] = {
            "customers": [
                {
                    "sdk_key": "bsa_peer",
                    "customer_id": "peer",
                    "dify": {
                        "base_url": "http://shared-dify/",  # trailing slash
                        "console_email": "ws-admin@example.com",
                        "console_password": "pw",
                        "dataset_api_key": "dataset-workspace-key",
                        "mode": "shared",
                    },
                    "models": [{"id": "m1", "provider": "p", "name": "n"}],
                }
            ]
        }
        found = find_shared_workspace_dataset_key(
            registry_data,
            base_url="http://shared-dify",  # no slash
            console_email="ws-admin@example.com",
        )
        assert found == "dataset-workspace-key"

    def test_different_console_email_is_different_workspace(self) -> None:
        """Same Dify host, different login = different workspace.
        Don't cross-reuse, otherwise tenant boundary leaks."""
        registry_data: dict[str, Any] = {
            "customers": [
                {
                    "sdk_key": "bsa_peer",
                    "customer_id": "peer",
                    "dify": {
                        "base_url": "http://shared-dify",
                        "console_email": "workspace-1@example.com",
                        "console_password": "pw",
                        "dataset_api_key": "dataset-ws1-key",
                        "mode": "shared",
                    },
                    "models": [{"id": "m1", "provider": "p", "name": "n"}],
                }
            ]
        }
        # Different email → different Dify workspace.
        assert (
            find_shared_workspace_dataset_key(
                registry_data,
                base_url="http://shared-dify",
                console_email="workspace-2@example.com",
            )
            is None
        )

    def test_dedicated_peer_is_not_a_match(self) -> None:
        """The optimisation only applies to shared mode. Dedicated peers
        on the same host (rare but possible during migration) must NOT
        donate their dataset key — dedicated keys aren't workspace-wide."""
        registry_data: dict[str, Any] = {
            "customers": [
                {
                    "sdk_key": "bsa_peer",
                    "customer_id": "peer",
                    "dify": {
                        "base_url": "http://dify",
                        "console_email": "admin@example.com",
                        "console_password": "pw",
                        "dataset_api_key": "dataset-dedicated-key",
                        "mode": "dedicated",
                    },
                    "models": [{"id": "m1", "provider": "p", "name": "n"}],
                }
            ]
        }
        assert (
            find_shared_workspace_dataset_key(
                registry_data,
                base_url="http://dify",
                console_email="admin@example.com",
            )
            is None
        )

    def test_no_peers_returns_none(self) -> None:
        assert (
            find_shared_workspace_dataset_key(
                {"customers": []},
                base_url="http://dify",
                console_email="x@y",
            )
            is None
        )

    def test_malformed_entries_skipped_not_crash(self) -> None:
        """If somebody hand-edits the registry into shape with stray
        non-dict entries, the lookup must skip them rather than crash —
        a defensive complement to ``load_existing_registry`` (which now
        rejects non-mappings at load) for the case where the caller
        passes a raw dict that hasn't been through ``load_existing_registry``."""
        registry_data: dict[str, Any] = {
            "customers": [
                None,
                "not a dict",
                {"customer_id": "no-dify-block"},  # missing dify
                {"customer_id": "dify-not-dict", "dify": "wrong shape"},
                {
                    "sdk_key": "bsa_real_peer",
                    "customer_id": "real-peer",
                    "dify": {
                        "base_url": "http://dify",
                        "console_email": "admin@x",
                        "console_password": "pw",
                        "dataset_api_key": "dataset-real",
                        "mode": "shared",
                    },
                    "models": [{"id": "m1", "provider": "p", "name": "n"}],
                },
            ]
        }
        found = find_shared_workspace_dataset_key(
            registry_data,
            base_url="http://dify",
            console_email="admin@x",
        )
        assert found == "dataset-real"


class TestSharedModeKeyReuseEndToEnd:
    """Codex review-5 P2 #1 e2e tests: the CLI must skip
    ``_provision_dataset_api_key`` entirely when adding a new shared-mode
    customer to a workspace that already has a shared-mode peer in
    registry.yaml. The key signal: ``mock_provision_dataset_key.call_count
    == 0`` AND the new entry's ``dataset_api_key`` equals the peer's.
    """

    def _seed_shared_peer(self, registry_path: Path) -> str:
        """Write registry.yaml containing one shared-mode customer.
        Returns the dataset_api_key that subsequent onboardings should reuse."""
        existing_key = "dataset-shared-workspace-key-from-peer"
        registry_path.write_text(
            yaml.safe_dump({
                "customers": [
                    {
                        "sdk_key": "bsa_peer_one",
                        "customer_id": "peer-one",
                        "dify": {
                            "base_url": "http://shared-dify",
                            "console_email": "ws-admin@example.com",
                            "console_password": "pw",
                            "dataset_api_key": existing_key,
                            "mode": "shared",
                            "shared_embedding_model": {
                                "name": "bge-m3",
                                "provider": (
                                    "langgenius/openai_api_compatible/"
                                    "openai_api_compatible"
                                ),
                            },
                        },
                        "models": [{"id": "m1", "provider": "p", "name": "n"}],
                    }
                ]
            }),
            encoding="utf-8",
        )
        return existing_key

    def test_second_shared_customer_reuses_peer_key_no_network(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        registry_path = tmp_path / "registry.yaml"
        existing_key = self._seed_shared_peer(registry_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "peer-two",
            "--dify-base-url", "http://shared-dify",
            "--dify-admin-email", "ws-admin@example.com",
            "--dify-admin-password", "pw",
            "--mode", "shared",
            "--shared-embedding-name", "bge-m3",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        assert result.exit_code == 0, result.output

        # The Dify provisioning function must NOT have been called —
        # we reused the peer's key, no new key was burned out of the
        # workspace's 10-key budget.
        assert mock_provision_dataset_key.call_count == 0, (
            "_provision_dataset_api_key was called even though a "
            "shared-mode peer key already exists for this workspace "
            "(codex review-5 P2 — Dify caps at 10 keys/workspace)."
        )

        # And the new entry's dataset_api_key matches the peer's.
        loaded = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        new = next(c for c in loaded["customers"] if c["customer_id"] == "peer-two")
        assert new["dify"]["dataset_api_key"] == existing_key

        # User-visible message confirms reuse.
        assert "Reusing existing workspace dataset key" in result.output

    def test_shared_with_different_workspace_email_still_provisions(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Different console_email = different Dify workspace even on
        the same host. Must provision a fresh key, not cross-reuse."""
        registry_path = tmp_path / "registry.yaml"
        self._seed_shared_peer(registry_path)  # admin = ws-admin@example.com

        runner = CliRunner()
        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "peer-two",
            "--dify-base-url", "http://shared-dify",
            "--dify-admin-email", "OTHER-ws-admin@example.com",  # different ws
            "--dify-admin-password", "pw",
            "--mode", "shared",
            "--shared-embedding-name", "bge-m3",
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        assert result.exit_code == 0, result.output
        # Different workspace -> must call Dify for a fresh key.
        assert mock_provision_dataset_key.call_count == 1

    def test_dedicated_mode_never_reuses_even_with_shared_peer_present(
        self, tmp_path: Path, mock_provision_dataset_key: Any
    ) -> None:
        """Dedicated mode onboarding must always provision its own key,
        even when a shared-mode peer happens to live on the same host."""
        registry_path = tmp_path / "registry.yaml"
        self._seed_shared_peer(registry_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "add-customer",
            "--customer-id", "peer-two",
            "--dify-base-url", "http://shared-dify",
            "--dify-admin-email", "ws-admin@example.com",
            "--dify-admin-password", "pw",
            "--mode", "dedicated",   # ← not shared
            "--model", "gemma-3n-e4b",
            "--registry-path", str(registry_path),
        ])

        # Mixing dedicated + shared on the same base_url is rejected
        # by from_entries (mode disagreement), so this exits non-zero.
        # The point of this test is: the rejection happened locally,
        # and the provisioner was NOT called.
        assert result.exit_code != 0
        assert mock_provision_dataset_key.call_count == 0


class TestExclusiveTempFile:
    """Codex review-5 P2 #2: ``write_registry_atomic`` must not reuse a
    pre-existing ``registry.yaml.tmp`` file. The previous implementation
    used a deterministic ``.tmp`` filename plus
    ``os.open(..., O_CREAT | O_TRUNC, target_mode)`` — but ``O_CREAT``
    without ``O_EXCL`` reopens an existing file and silently IGNORES
    the mode argument. If an attacker (or a SIGKILL'd previous run) had
    placed an empty 0644 ``registry.yaml.tmp`` next to the target, the
    secrets would have been written into it before the post-write
    chmod could narrow permissions.

    Fix: use :func:`tempfile.mkstemp` which atomically creates a
    uniquely-named file at 0600 on POSIX, so we never touch any
    pre-existing predictable filename.
    """

    def test_preexisting_deterministic_tmp_is_not_touched(
        self, tmp_path: Path
    ) -> None:
        """The legacy ``<name>.tmp`` filename must be left alone —
        we use a randomised name now, so any pre-existing predictable
        ``.tmp`` is irrelevant to our write."""
        path = tmp_path / "registry.yaml"
        legacy_tmp = path.with_suffix(".yaml.tmp")
        legacy_tmp.write_text("ATTACKER-CONTROLLED-PRE-EXISTING-CONTENT\n", encoding="utf-8")

        write_registry_atomic(path, {"customers": []})

        # Target written successfully.
        assert path.exists()
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"customers": []}
        # The pre-existing deterministic-name file was not overwritten,
        # not unlinked, and not turned into the registry. Its content
        # is untouched, proving we wrote to a different (random) name.
        assert legacy_tmp.exists()
        assert legacy_tmp.read_text(encoding="utf-8") == (
            "ATTACKER-CONTROLLED-PRE-EXISTING-CONTENT\n"
        )

    @pytest.mark.skipif(
        os.name != "posix", reason="POSIX file mode bits"
    )
    def test_write_succeeds_at_0600_with_preexisting_permissive_legacy_tmp(
        self, tmp_path: Path
    ) -> None:
        """The classic attack: a 0644 ``registry.yaml.tmp`` placed by
        another user. With the old code, ``O_CREAT`` would reopen it
        and the mode argument would be ignored — secrets briefly at
        0644 before chmod. With mkstemp, our write goes to a brand-new
        randomly-named file at 0600 from the start."""
        path = tmp_path / "registry.yaml"
        legacy_tmp = path.with_suffix(".yaml.tmp")
        legacy_tmp.write_text("attacker-placed\n", encoding="utf-8")
        os.chmod(legacy_tmp, 0o644)

        # Force a permissive umask too, to make the assertion meaningful.
        old_umask = os.umask(0o022)
        try:
            write_registry_atomic(path, {"customers": []})
        finally:
            os.umask(old_umask)

        mode_bits = stat.S_IMODE(path.stat().st_mode)
        assert mode_bits & 0o077 == 0, (
            f"registry.yaml ended up at {oct(mode_bits)} despite a "
            f"pre-existing permissive tmp file — mkstemp must give us "
            f"a fresh 0600 file. (codex review-5 P2 #2)"
        )

    def test_atomic_write_cleans_up_random_tmp_on_failure(
        self, tmp_path: Path
    ) -> None:
        """If yaml.safe_dump raises mid-write, the randomly-named tmp
        file must be unlinked so we don't leak it. (The legacy test
        only checked for the deterministic name; with mkstemp we
        instead glob for residue.)"""
        path = tmp_path / "registry.yaml"

        with patch(
            "gateway.admin.registry_merge.yaml.safe_dump",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                write_registry_atomic(path, {"customers": []})

        # No target written.
        assert not path.exists()
        # No leftover tmp file (deterministic OR random) in the dir.
        residue = [
            p for p in tmp_path.iterdir() if ".registry.yaml" in p.name or p.name.endswith(".tmp")
        ]
        assert residue == [], f"unexpected tmp residue: {residue}"
