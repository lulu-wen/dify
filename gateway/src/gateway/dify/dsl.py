"""Dify App DSL builder.

Generates the YAML payload accepted by ``POST /console/api/apps/imports``
(``mode=yaml-content``). The DSL produced is intentionally minimal: a
``chat`` mode App with a single model + optional knowledge base attachments.

For PR#1 we use Dify's basic ``chat`` mode (not Chatflow / advanced-chat) to
keep the surface narrow. Chatflow support can be added in a later PR if
custom workflow nodes (variable assignment, branching) become a requirement.

``pre_prompt`` template + ``user_input_form``:
    Dify's chat App wraps ``pre_prompt`` as ``role: system`` in the LLM call
    (see ``api/core/prompt/prompt_templates/advanced_prompt_templates.py``
    CHAT_APP_CHAT_PROMPT_CONFIG). We declare a single ``system_prompt``
    template variable so the gateway can inject per-request system messages
    (assembled from OpenAI ``messages: [system, ..., user]`` plus any prior
    conversation turns) by passing ``inputs.system_prompt``. Without this
    plumbing, OpenAI-style system messages were silently dropped by Dify
    because ``inputs`` keys not referenced in ``pre_prompt`` are discarded.
"""

from __future__ import annotations

from typing import Any

import yaml

DSL_VERSION = "v5-rag-top-k-cap"
"""Bump when ``build_chat_app_dsl`` output changes in a way that requires
existing cached Apps to be rebuilt. :class:`AppManager` records this on each
:class:`CachedApp` and forces a rebuild when the constant disagrees with the
cached entry. Single source of truth: only this module produces DSL, so this
constant covers every meaningful change."""


def build_chat_app_dsl(
    *,
    name: str,
    description: str,
    provider: str,
    model_name: str,
    completion_params: dict[str, Any] | None = None,
    knowledge_base_ids: list[str] | None = None,
    retrieval_top_k: int | None = None,
) -> str:
    """Render a Dify ``chat`` mode App into YAML.

    Args:
        name: Human-readable App name (visible in Dify UI).
        description: Free-text description.
        provider: Dify model provider id (e.g.
            ``langgenius/openai_api_compatible/openai_api_compatible``).
        model_name: Provider-internal model name (matches what was registered
            via the model provider plugin).
        completion_params: Per-request defaults (``temperature``, ``max_tokens``,
            etc.). Empty dict if omitted.
        knowledge_base_ids: Dify Dataset IDs to attach. Empty list if omitted.
        retrieval_top_k: Max chunks Dify retrieves per chat request (set as
            ``dataset_configs.top_k`` in multiple-retrieval mode). Only
            emitted when KBs are attached. Caps RAG-injected context so the
            admission reservation (which adds ``top_k * chunk_tokens``) is a
            true upper bound, not a guess (codex 1b review-5 P2). None = use
            Dify's default (4).

    Returns:
        UTF-8 YAML string suitable for ``yaml-content`` import.
    """
    # ``enabled: True`` is mandatory — Dify's DatasetConfigManager.convert
    # (api/core/app/app_config/easy_ui_based_app/dataset/manager.py) silently
    # drops any dataset entry without ``enabled=true``, leaving the App with
    # an empty dataset list and no retrieval. Live RAG verification on
    # 2026-05-21 hit this: dataset was created, document indexed, retrieve
    # endpoint returned hits, but chat-with-RAG returned no references.
    datasets_block: list[dict[str, Any]] = [
        {"dataset": {"id": kb_id, "enabled": True}}
        for kb_id in (knowledge_base_ids or [])
    ]

    payload: dict[str, Any] = {
        "app": {
            "description": description,
            "icon": "🤖",
            "icon_background": "#FFEAD5",
            "mode": "chat",
            "name": name,
        },
        "model_config": {
            "model": {
                "provider": provider,
                "name": model_name,
                "mode": "chat",
                "completion_params": dict(completion_params or {}),
            },
            # Single template variable wired to ``inputs.system_prompt`` on every
            # ``/v1/chat-messages`` call. Dify expands this into the chat App's
            # system-role prompt before the LLM sees it. Without the matching
            # ``user_input_form`` declaration below, Dify rejects the import.
            "pre_prompt": "{{system_prompt}}",
            "user_input_form": [
                {
                    "paragraph": {
                        "label": "System prompt (gateway-injected)",
                        "variable": "system_prompt",
                        "required": False,
                    }
                }
            ],
            "dataset_configs": _build_dataset_configs(datasets_block, retrieval_top_k),
            # The following keys are not strictly required by every Dify version
            # but are emitted to keep the import deterministic across versions.
            "opening_statement": "",
            "suggested_questions": [],
            "speech_to_text": {"enabled": False},
            "text_to_speech": {"enabled": False, "voice": "", "language": ""},
            "more_like_this": {"enabled": False},
            "sensitive_word_avoidance": {"enabled": False, "type": "", "configs": []},
            "agent_mode": {"enabled": False, "tools": []},
        },
    }

    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _build_dataset_configs(
    datasets_block: list[dict[str, Any]],
    retrieval_top_k: int | None,
) -> dict[str, Any]:
    """Compose Dify's ``dataset_configs`` block.

    Always emits ``retrieval_model: multiple`` + the dataset list (an empty
    list when no KBs are attached). When KBs ARE attached and a
    ``retrieval_top_k`` is supplied, emits ``top_k`` so Dify retrieves at
    most that many chunks — bounding RAG-injected context to match what the
    gateway's admission reservation accounts for (codex 1b review-5 P2).
    See ``api/core/app/app_config/easy_ui_based_app/dataset/manager.py``:
    Dify reads ``dataset_configs.top_k`` (default 4) in multiple mode.
    """
    cfg: dict[str, Any] = {
        "retrieval_model": "multiple",
        "datasets": {"datasets": datasets_block},
    }
    if datasets_block and retrieval_top_k is not None:
        cfg["top_k"] = retrieval_top_k
    return cfg
