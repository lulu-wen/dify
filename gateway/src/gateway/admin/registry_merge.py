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
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from gateway.registry import CustomerEntry, CustomerRegistry

# Restrictive file mode for ``registry.yaml`` — the file holds
# ``console_password`` / ``dataset_api_key`` / ``sdk_key`` in plaintext,
# so it must NEVER end up world-readable. ``0o600`` = owner read+write,
# nothing for group / other. Codex review-4 P2.
_REGISTRY_FILE_MODE = 0o600

# Prefix every valid Dify dataset API key starts with. Mirrors
# ``startup_check._DATASET_KEY_PREFIX`` — duplicated here so the
# admin CLI can do its own L1 validation without depending on the
# runtime startup-check module. If Dify ever changes the prefix,
# both constants must move together (and the startup_check tests
# pin the expectation).
_DATASET_KEY_PREFIX = "dataset-"

# Sentinel value used by the CLI's dry-run merge phase as a stand-in
# for the real ``dataset_api_key`` before the Dify network call. It
# starts with ``dataset-`` so it passes L1, but it is NEVER a valid
# Dify key — refusing to propagate it from a stray peer entry is
# belt-and-braces against the dry-run trial entry accidentally
# getting written to disk by some future regression. Codex review-8 P2.
PLACEHOLDER_DATASET_KEY = "dataset-pending-validation-pre-network"


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

    # Probe-create a tiny file in the parent to catch permission /
    # mount-noexec / read-only-fs errors without needing to know the
    # OS-specific access bits.
    #
    # Codex review-7 P2: the probe filename must NOT be a
    # deterministic ``.{path.name}.writable-probe`` — if an operator
    # (or another tool) happens to have a file at that exact name,
    # the cleanup unlink at the end of this function would silently
    # delete it. Use :func:`tempfile.mkstemp` so the probe name is
    # uniquely generated and only our own file is removed. Same
    # pattern as :func:`write_registry_atomic` (codex review-5 P2 #2).
    try:
        probe_fd, probe_str = tempfile.mkstemp(
            prefix=f".{path.name}.writable-probe.",
            dir=parent,
        )
    except OSError as exc:
        raise RegistryMergeError(
            f"registry parent directory {parent} is not writable: {exc}"
        ) from exc

    probe = Path(probe_str)
    try:
        os.close(probe_fd)
    finally:
        # Best-effort cleanup of OUR probe — mkstemp guaranteed the
        # name is unique to this call, so unlinking is safe.
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

    Codex review-5 P2: use :func:`tempfile.mkstemp` rather than a
    deterministic ``registry.yaml.tmp`` filename plus
    ``os.open(O_CREAT|O_TRUNC, mode)``. ``O_CREAT`` without ``O_EXCL``
    silently reuses an existing file (e.g. a 0644 orphan left by a
    SIGKILL'd previous run, or one planted by another local user) and
    the ``mode`` argument to :func:`os.open` is **ignored** when the
    target file already exists — so secrets would be written into a
    permissive file before the belt-and-braces chmod could narrow it.
    :func:`tempfile.mkstemp` creates a uniquely-named file atomically
    at ``0600`` on POSIX, closing both holes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Codex review-4 P2: pick the most restrictive file mode we can
    # justify. Default is ``0600`` (owner-only). If the existing
    # registry already has stricter perms — e.g. ``0400`` (owner
    # read-only, used by some ops who prefer immutable-until-explicit
    # writes) — preserve that. POSIX-only; Windows file ACLs work
    # differently so the chmod is a no-op there.
    target_mode = _secret_file_mode(path)

    # ``mkstemp`` returns ``(fd, abs_path_str)`` — fd is already open
    # for writing and the file is guaranteed not to have existed before
    # this call (atomic O_EXCL under the hood). Prefix/suffix keep the
    # name discoverable as belonging to this write (so an orphan from
    # a process-crash window is still recognisable as ours).
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_str)

    try:
        file_obj = os.fdopen(fd, "w", encoding="utf-8")
        try:
            yaml.safe_dump(
                registry_data,
                file_obj,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
        finally:
            file_obj.close()

        # ``mkstemp`` already creates the file at ``0600`` on POSIX, so
        # the default case needs no chmod. Only chmod when the operator
        # asked for a stricter mode (e.g. preserved ``0400``) or — as
        # belt-and-braces — to confirm even on platforms where mkstemp's
        # mode behaviour is fuzzier. No-op on Windows (``target_mode``
        # is ``None``).
        if target_mode is not None and target_mode != 0o600:
            try:
                os.chmod(tmp, target_mode)
            except OSError:
                # Some filesystems (e.g. FAT32, certain network mounts)
                # don't support chmod. Don't fail the write over it —
                # the data is still going to land. Best we can do.
                pass

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


def _secret_file_mode(existing_path: Path) -> int | None:
    """Return the mode to apply to a secret-bearing file write.

    Codex review-4 P2: ``registry.yaml`` contains plaintext credentials
    (``console_password`` / ``dataset_api_key`` / ``sdk_key``) and must
    NEVER end up world-readable. ``open("w")`` with the default umask
    (often 022) creates files at ``0644``; if a previous registry was
    ``0600`` an atomic tmp+replace would silently widen perms.

    Strategy:
    - Default to ``0600`` (owner read+write only).
    - If an existing registry file is even stricter (e.g. ``0400``),
      preserve that — some operators set the registry read-only on
      purpose between updates.
    - On Windows, return ``None`` — Win32 file ACLs aren't expressible
      as POSIX mode bits; let the caller use the platform default and
      trust filesystem ACLs / parent dir perms.
    """
    if sys.platform == "win32":
        return None

    target = _REGISTRY_FILE_MODE
    if existing_path.exists():
        try:
            existing_mode = stat.S_IMODE(existing_path.stat().st_mode)
            # "Stricter" means group + other have no bits set.
            if existing_mode & 0o077 == 0:
                target = existing_mode
        except OSError:
            # Can't stat — fall back to the default.
            pass
    return target


def _find_customer_index(
    customers: list[dict[str, Any]], customer_id: str
) -> int | None:
    """Return the index of the existing customer with ``customer_id``, or None."""
    for i, c in enumerate(customers):
        if c.get("customer_id") == customer_id:
            return i
    return None


def find_shared_workspace_dataset_key(
    registry_data: dict[str, Any],
    *,
    base_url: str,
    console_email: str,
) -> str | None:
    """Return an existing ``dataset_api_key`` for the same shared workspace.

    Codex review-5 P2: in shared mode every customer in a Dify workspace
    can use the **same** workspace-scoped dataset API key (that's what
    "shared" means at the Dify side too — one workspace, many tenants).
    Dify caps ``/console/api/datasets/api-keys`` at **10 keys per
    workspace**. If the CLI provisions a fresh key on every shared
    onboarding, the 11th customer onboarding for that workspace blows
    up with a Dify-side limit error even though all previous customers
    already share a perfectly good key.

    Workspace identity is ``(base_url, console_email)`` — same login
    against the same Dify host = same workspace. ``base_url`` is
    normalised by ``rstrip("/")`` to match the rule
    :meth:`CustomerRegistry._check_dify_consistency` already uses.

    Returns the first matching ``dataset_api_key`` so the CLI can skip
    the network provisioning call entirely. Returns ``None`` when no
    matching shared-mode peer exists (truly new workspace, or only
    dedicated peers on this base_url).
    """
    normalized = base_url.rstrip("/")
    for entry in registry_data.get("customers", []):
        if not isinstance(entry, dict):
            continue
        dify = entry.get("dify")
        if not isinstance(dify, dict):
            continue
        if dify.get("mode") != "shared":
            continue
        if not isinstance(dify.get("base_url"), str):
            continue
        if dify["base_url"].rstrip("/") != normalized:
            continue
        if dify.get("console_email") != console_email:
            continue
        key = dify.get("dataset_api_key")
        if not isinstance(key, str) or not key:
            continue
        # Codex review-8 P2: only propagate keys that would pass the
        # gateway's L1 startup format check. If the peer is sitting
        # on a legacy placeholder (``dataset-not-used-in-pr1``-style),
        # a token from a different key family, or — worst case — the
        # CLI's own dry-run sentinel that should never have landed
        # on disk, reusing it just propagates the brokenness to the
        # new customer. Falling through to the provisioning path
        # gives the new entry a fresh, real key, and leaves the peer's
        # bad state for the gateway's startup check to surface
        # explicitly.
        if not key.startswith(_DATASET_KEY_PREFIX):
            continue
        if key == PLACEHOLDER_DATASET_KEY:
            continue
        return key
    return None
