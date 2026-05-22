"""Unit tests for OpenAI-compatible request schema, especially deprecation aliases.

OpenAI deprecated ``max_tokens`` (→ ``max_completion_tokens``) and ``user``
(→ ``safety_identifier``) in 2025. Our gateway accepts both old and new names
to keep existing clients (LangChain, older openai-python, Dify itself)
working while also accepting OpenAI's newest schema.

Precedence rule: when both names are sent in the same request, the *new*
name wins (matching OpenAI's stated migration direction).
"""

from __future__ import annotations

from gateway.schemas import ChatCompletionRequest, ChatMessage


def _msg(content: str = "hi") -> ChatMessage:
    return ChatMessage(role="user", content=content)


class TestEffectiveMaxTokens:
    def test_only_old_field_returns_old(self) -> None:
        req = ChatCompletionRequest(model="m", messages=[_msg()], max_tokens=512)
        assert req.effective_max_tokens == 512

    def test_only_new_field_returns_new(self) -> None:
        req = ChatCompletionRequest(model="m", messages=[_msg()], max_completion_tokens=256)
        assert req.effective_max_tokens == 256

    def test_both_set_new_wins(self) -> None:
        req = ChatCompletionRequest(
            model="m",
            messages=[_msg()],
            max_tokens=999,
            max_completion_tokens=128,
        )
        assert req.effective_max_tokens == 128

    def test_neither_set_returns_none(self) -> None:
        req = ChatCompletionRequest(model="m", messages=[_msg()])
        assert req.effective_max_tokens is None


class TestEffectiveUser:
    def test_only_user_returns_user(self) -> None:
        req = ChatCompletionRequest(model="m", messages=[_msg()], user="alice")
        assert req.effective_user == "alice"

    def test_only_safety_identifier_returns_safety_identifier(self) -> None:
        req = ChatCompletionRequest(model="m", messages=[_msg()], safety_identifier="bob")
        assert req.effective_user == "bob"

    def test_both_set_safety_identifier_wins(self) -> None:
        req = ChatCompletionRequest(
            model="m",
            messages=[_msg()],
            user="old",
            safety_identifier="new",
        )
        assert req.effective_user == "new"

    def test_neither_set_returns_none(self) -> None:
        req = ChatCompletionRequest(model="m", messages=[_msg()])
        assert req.effective_user is None
