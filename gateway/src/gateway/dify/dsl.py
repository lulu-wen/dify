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

DSL_VERSION = "v2-system-prompt"
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

    Returns:
        UTF-8 YAML string suitable for ``yaml-content`` import.
    """
    datasets_block: list[dict[str, Any]] = [
        {"dataset": {"id": kb_id}} for kb_id in (knowledge_base_ids or [])
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
            "dataset_configs": {
                "retrieval_model": "multiple",
                "datasets": {"datasets": datasets_block},
            },
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
