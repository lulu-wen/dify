"""Gateway admin tooling — operator-side CLI.

Separate from the runtime gateway (``gateway.main``) on purpose:

- ``gateway.main`` is what uvicorn loads when serving traffic. It must
  not depend on click / interactive prompts / file-writing helpers.
- ``gateway.admin`` is invoked from the shell by the operator. It
  reuses the same :class:`gateway.dify.client.DifyClient` and
  :class:`gateway.registry.CustomerRegistry` types so onboarding
  produces registries the runtime is guaranteed to accept.

Today this package only exposes one command, ``gateway-admin add-customer``,
wired through ``[project.scripts]`` in ``pyproject.toml``. The directory
layout (``admin/`` subpackage) is provisioned for future commands —
``remove-customer``, ``rotate-keys``, ``health-check-only``, etc.
"""
