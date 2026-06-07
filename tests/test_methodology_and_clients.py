"""Тесты для methodology rule evaluator и Claude провайдеров.

v3.1: legacy ClarificationService / ClarificationCandidate тесты удалены вместе
с самим сервисом. Methodology evaluator теперь эмитит DecisionInput (см.
тесты в `test_methodology_rule_eval.py`). Здесь остались только провайдер-тесты.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# --- ClaudeSdkClient -------------------------------------------------------


def _fake_anthropic_response(payload: dict[str, Any]) -> Any:
    tool_use_block = SimpleNamespace(type="tool_use", input=payload, name="produce_artifact")
    return SimpleNamespace(content=[tool_use_block])


def test_claude_sdk_client_builds_tool_use_request() -> None:
    from pov_generator.infrastructure import claude_sdk_client as mod

    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    expected_payload = {"summary": "ok"}

    fake_anthropic_module = MagicMock()
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(expected_payload)
    fake_anthropic_module.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        client = mod.ClaudeSdkClient(
            mod.ClaudeSdkConfig(api_key="dummy", model="claude-sonnet-4-6", max_tokens=1024)
        )
        result = client.chat_json(
            system_prompt="sys",
            user_prompt="user",
            schema=schema,
            tool_name="produce_artifact",
        )

    assert result.payload == expected_payload
    fake_anthropic_module.Anthropic.assert_called_once()
    # Конструктор должен получить api_key; timeout — implementation detail.
    kwargs_anthropic = fake_anthropic_module.Anthropic.call_args.kwargs
    assert kwargs_anthropic["api_key"] == "dummy"
    fake_client.messages.create.assert_called_once()
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["system"] == "sys"
    assert kwargs["max_tokens"] == 1024
    assert kwargs["messages"] == [{"role": "user", "content": "user"}]
    tools = kwargs["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "produce_artifact"
    assert tools[0]["input_schema"] == schema
    assert kwargs["tool_choice"] == {"type": "tool", "name": "produce_artifact"}


# --- ClaudeSubscriptionClient ---------------------------------------------


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


def _make_async_query(response_text: str):
    async def _query(prompt: str, options: Any):  # noqa: ARG001 — signature must match SDK
        yield _FakeMessage(response_text)

    return _query


@pytest.mark.parametrize(
    "response_text, expected",
    [
        ("```json\n{\"declared_goal\": \"alpha\"}\n```", {"declared_goal": "alpha"}),
        ("{\"declared_goal\": \"alpha\"}", {"declared_goal": "alpha"}),
    ],
    ids=["fenced_markdown", "raw_json"],
)
def test_claude_subscription_client_extracts_json_from_text_response(
    response_text: str, expected: dict[str, Any]
) -> None:
    from pov_generator.infrastructure import claude_subscription_client as mod

    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)
    fake_sdk.query = _make_async_query(response_text)

    with patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}):
        client = mod.ClaudeSubscriptionClient(
            mod.ClaudeSubscriptionConfig(model="claude-sonnet-4-6", max_turns=1)
        )
        client._sdk = fake_sdk  # гарантируем, что инстанс использует наш мок
        result = client.chat_json(
            system_prompt="sys",
            user_prompt="user",
            schema={"type": "object"},
        )

    assert result.payload == expected
