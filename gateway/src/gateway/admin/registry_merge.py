"""Registry YAML merge helpers used by the admin CLI.

Why this lives in its own module:

- The CLI shouldn't itself know how to parse / validate / write
  ``registry.yaml``. Those operations are the same shape we'd want
  if a future ``gateway-admin remove-customer`` / ``rotate-keys``
  command shows up.
- The runtime never writes to ``registry.yaml``, so this code path is
  intentionally segregated from ``gateway.registry`` (which is
  read-only at runtime).

Atomicity:

We never write directly to ``registry.yaml``. The CLI:

1. Reads existing YAML (or starts from ``{"customers": []}`` if absent)
2. Inserts the new customer entry
3. **Re-validates the whole registry via ``CustomerRegistry.from_entries``**
   so duplicate ``customer_id`` / cross-customer base_url disagreement /
   etc. are caught before we touch disk
4. Writes to ``<path>.tmp`` then ``os.replace`` it onto ``<path>``

``os.replace`` is atomic on POSIX and "near-atomic" on Windows (the
filesystem-level swap is atomic; what isn't is the brief window where
both files might exist if the process is SIGKILL'd between
``write+close`` and ``replace``). Good enough — the worst case is an
``.tmp`` orphan, which we clean up on next run.

Caveat — comments are not preserved:

``yaml.safe_load`` → ``yaml.safe_dump`` round-trip strips YAML comments.
Operators who hand-edit ``registry.yaml`` with explanatory comments
will lose them on every ``gateway-admin add-customer`` invocation
(self-review P2-2). The fix is to swap PyYAML for ``ruamel.yaml``
(which preserves comments), but that's a new top-level dependency
just for this one ergonomic concern. Documented here so operators
keep narrative notes in a sibling file (``registry.notes.md`` etc.)
or commit messages rather than as inline YAML comments.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from gateway.registry import CustomerEntry, CustomerRegistry


class RegistryMergeError(Exception):
    """Raised when a customer cannot be merged into the registry.

    Wraps the underlying cause (file not readable, duplicate
    customer_id, validation failure) so the CLI can present a single
    actionable message to the operator instead of leaking pydantic
    tracebacks.
    """


def load_existing_registry(path: Path) -> dict[str, Any]:
    """Return the parsed YAML, or an empty skeleton if the file is absent.

    Reads the file directly (not via ``CustomerRegistry.from_yaml``)
    because that path raises on the empty / placeholder registries
    operators have when adding their first customer. We tolerate
    "file does not exist" and "file exists with empty customers"
    here, and let the validator below catch genuine schema problems.
    """
    if not path.exists():
        return {"customers": []}

    # Codex review-2 P3: convert ``yaml.YAMLError`` / ``OSError`` into
    # ``RegistryMergeError`` so the CLI handler gets a clean message
    # instead of an unhandled traceback. Malformed YAML happens any
    # time an operator hand-edits the file, which is exactly when a
    # clear "what went wrong" matters most.
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise RegistryMergeError(
            f"registry at {path} is not valid YAML: {exc}. "
            f"Fix the file by hand or move it aside and let "
            f"gateway-admin start fresh."
        ) from exc
    except OSError as exc:
        raise RegistryMergeError(
            f"could not read registry at {path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise RegistryMergeError(
            f"registry at {path} must be a YAML mapping at the top level "
            f"(got {type(raw).__name__})"
        )
    raw.setdefault("customers", [])
    if not isinstance(raw["customers"], list):
        raise RegistryMergeError(
            f"registry.yaml 'customers' must be a list (got {type(raw['customers']).__name__})"
        )

    # Codex review-3 P3: each customer entry must itself be a mapping.
    # ``customers: [null]`` or ``customers: [- "bad string"]`` previously
    # escaped as ``AttributeError`` (from ``.get()`` on a non-dict) instead
    # of the intended ``RegistryMergeError``. Validate up front so the CLI
    # handler reports a clean error and no network call fires.
    for i, item in enumerate(raw["customers"]):
        if not isinstance(item, dict):
            raise RegistryMergeError(
                f"registry.yaml customers[{i}] must be a mapping "
                f"(got {type(item).__name__}: {item!r}). "
                f"Fix the file by hand — each customer entry is an object "
                f"with sdk_key / customer_id / dify / models fields."
            )
    return raw


def check_writable(path: Path) -> None:
    """Preflight check that ``path`` can be written to.

    Raises :class:`RegistryMergeError` for the common causes of "I called
    Dify, then the write blew up" — the CLI invokes this BEFORE the Dify
    network call so a misconfigured filesystem doesn't leave us with an
    orphan ``dataset-*`` key on the Dify side.

    Codex review-3 P2. Covers:
    - Path exists but is not a regular file (e.g., directory)
    - Parent directory doesn't exist and cannot be created
    - Parent directory exists but is read-only

    Does NOT cover: "disk full at write time". That race is rare and
    unrecoverable; the CLI's post-network ``except OSError`` block
    handles it by logging the dataset key prefix so the operator can
    find + delete the orphan in Dify Web UI.
    """
    if path.exists() and not path.is_file():
        raise RegistryMergeError(
            f"registry path {path} exists but is not a regular file"
        )

    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RegistryMergeError(
            f"cannot create parent directory {parent}: {exc}"
        ) from exc

    # Probe-touch a tiny file in the parent. Catches permission
    # errors without needing to know the OS-specific access bits.
    probe = parent / f".{path.name}.writable-probe"
    try:
        probe.touch()
    except OSError as exc:
        raise RegistryMergeError(
            f"registry parent directory {parent} is not writable: {exc}"
        ) from exc
    finally:
        # Best-effort cleanup; if it failed before touch we wouldn't
        # have a probe to remove anyway.
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def merge_customer(
    registry_data: dict[str, Any],
    new_entry: CustomerEntry,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Insert ``new_entry`` into ``registry_data['customers']``.

    Validates the result by round-tripping through
    :meth:`CustomerRegistry.from_entries`, which catches duplicate
    ``customer_id``, cross-customer base_url / mode disagreement, and
    other registry-wide invariants. The validator is the same one the
    runtime uses, so passing here means the runtime will accept the
    file.

    ``force=True`` permits replacing an existing customer with the
    same ``customer_id`` (and same ``sdk_key`` — operators should not
    be able to silently re-issue an SDK key with the same admin
    command). Default refuses, requiring the operator to use a
    dedicated ``rotate-keys`` flow (future) for replacements.
    """
    customers = list(registry_data.get("customers", []))

    # Build the new dict-shape entry from the pydantic model so we
    # match the on-disk YAML format exactly.
    new_entry_dict = new_entry.model_dump(mode="json", exclude_none=True)

    existing_index = _find_customer_index(customers, new_entry.customer_id)
    if existing_index is not None and not force:
        raise RegistryMergeError(
            f"customer '{new_entry.customer_id}' already exists in registry. "
            "Use --force to overwrite (verify with operator before doing so — "
            "this rotates the SDK key and breaks active clients)."
        )

    if existing_index is not None:
        customers[existing_index] = new_entry_dict
    else:
        customers.append(new_entry_dict)

    merged = {**registry_data, "customers": customers}

    # Validate by parsing through the runtime's CustomerRegistry — same
    # validators the live gateway uses, so this catches anything the
    # gateway would reject at startup.
    try:
        entries = [CustomerEntry.model_validate(c) for c in customers]
        CustomerRegistry.from_entries(entries)
    except Exception as exc:
        raise RegistryMergeError(
            f"merged registry failed validation: {exc}"
        ) from exc

    return merged


def write_registry_atomic(path: Path, registry_data: dict[str, Any]) -> None:
    """Write ``registry_data`` to ``path`` via tmp-file + ``os.replace``.

    The tmp file lives alongside the target (same directory) so the
    final rename is on the same filesystem — required for ``os.replace``
    to be atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                registry_data,
                f,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup so a partial write doesn't leave .tmp
        # files lying around. If the unlink itself fails we don't
        # mask the original error.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _find_customer_index(
    customers: list[dict[str, Any]], customer_id: str
) -> int | None:
    """Return the index of the existing customer with ``customer_id``, or None."""
    for i, c in enumerate(customers):
        if c.get("customer_id") == customer_id:
            return i
    return None
