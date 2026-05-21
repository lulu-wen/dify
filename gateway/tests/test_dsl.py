"""Tests for the Dify chat-mode App DSL builder."""

from __future__ import annotations

import yaml

from gateway.dify.dsl import DSL_VERSION, build_chat_app_dsl


def _parse(yaml_str: str) -> dict:
    return yaml.safe_load(yaml_str)


def test_dsl_emits_system_prompt_template_with_matching_user_input_form() -> None:
    """The ``pre_prompt`` must reference ``{{system_prompt}}`` and the variable
    must be declared in ``user_input_form``. Dify rejects template variables
    not declared in the form, so the two values are coupled.
    """
    dsl = build_chat_app_dsl(
        name="t", description="d", provider="p", model_name="m"
    )
    cfg = _parse(dsl)["model_config"]

    assert cfg["pre_prompt"] == "{{system_prompt}}"
    assert len(cfg["user_input_form"]) == 1

    form_item = cfg["user_input_form"][0]
    # Allowed Dify type for long text is ``paragraph`` (validated in
    # ``api/core/app/app_config/easy_ui_based_app/variables/manager.py``).
    assert "paragraph" in form_item
    spec = form_item["paragraph"]
    assert spec["variable"] == "system_prompt"
    assert spec["required"] is False


def test_dsl_version_is_a_non_empty_string() -> None:
    """``DSL_VERSION`` must be a stable identifier the AppManager can compare
    against. Empty string would falsely match an unset cached value."""
    assert isinstance(DSL_VERSION, str) and DSL_VERSION


def test_dsl_carries_model_provider_and_completion_params() -> None:
    """Ensure the rest of the DSL contract is unchanged by the system-prompt
    plumbing — model wiring is what makes the App functional at all.
    """
    dsl = build_chat_app_dsl(
        name="auto:c:m",
        description="x",
        provider="langgenius/openai_api_compatible/openai_api_compatible",
        model_name="gemma-3n-e4b",
        completion_params={"temperature": 0.2},
        knowledge_base_ids=["kb-1"],
    )
    cfg = _parse(dsl)["model_config"]

    assert cfg["model"]["provider"].endswith("openai_api_compatible")
    assert cfg["model"]["name"] == "gemma-3n-e4b"
    assert cfg["model"]["completion_params"] == {"temperature": 0.2}
    assert cfg["dataset_configs"]["datasets"]["datasets"] == [
        {"dataset": {"id": "kb-1"}}
    ]


def test_dsl_emits_empty_datasets_when_no_knowledge_base() -> None:
    dsl = build_chat_app_dsl(
        name="t", description="d", provider="p", model_name="m"
    )
    cfg = _parse(dsl)["model_config"]
    assert cfg["dataset_configs"]["datasets"]["datasets"] == []
