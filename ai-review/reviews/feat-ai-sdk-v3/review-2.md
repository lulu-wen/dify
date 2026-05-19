# Codex Review #2 — feat-ai-sdk-v3

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr2` (PR #2 head).
> Diff: 10 commits (5 features + CI + 3 round-1 fixes + 2 docs), ~2800 LOC.
> Raw output: `review-2.raw.md`.

## Summary

Two of three findings are the new gateway CI catching itself: ruff rules
the workflow runs will reject patterns in the same diff that introduced
the workflow. The third is a real Dify embedding-provider trap: when the
registry entry omits `provider`, Dify silently falls back to the workspace
default — clean dataset binding requires both fields.

| Severity | Count |
|---|---|
| [P1] | 2 |
| [P2] | 1 |

## Full review comments

- [P1] **Replace FastAPI default calls before enabling Ruff CI** —
  `gateway/src/gateway/routers/files.py:44-49`
  With the new `gateway-ci.yml` running `ruff check .` and `B` rules
  enabled, these `FastapiFile(...)` / `Form(...)` calls in function
  defaults are reported by bugbear B008. The gateway CI will fail on
  every PR touching this package unless these parameters are rewritten
  with `Annotated[...]` metadata or the rule is explicitly configured
  for FastAPI.

- [P1] **Chain pagination parse errors for Ruff B904** —
  `gateway/src/gateway/routers/datasets.py:332-333`
  The newly added CI runs Ruff with bugbear rules, and this `except
  ValueError` re-raising a different exception without `from exc` or
  `from None` is flagged as B904; the duplicate `_int_query` helper in
  `files.py` has the same issue. As written, gateway lint fails even
  though the runtime behavior is otherwise fine.

- [P2] **Require provider when binding dataset embeddings** —
  `gateway/src/gateway/routers/datasets.py:117-120`
  When a registry embedding entry omits `provider`, this sends only
  `embedding_model` to Dify. Dify's dataset creation only honors the
  requested embedding when both provider and model are present;
  otherwise it falls back to the tenant default, so a request for a
  non-default gateway embedding can silently create a dataset indexed
  with the wrong model. Reject missing providers for dataset creation
  or resolve the provider before forwarding.

## Gate

**FAIL** (2 × [P1]). All three findings will be fixed before review #3.
