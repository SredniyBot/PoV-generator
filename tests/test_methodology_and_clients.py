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


# --- ClaudeSubscriptionClient: нативный structured output ------------------


def _options_factory_with_output_format():
    """Фабрика опций, имитирующая SDK с поддержкой output_format
    (датакласс-поле детектируется клиентом по __dataclass_fields__)."""

    def factory(**kw: Any) -> SimpleNamespace:
        return SimpleNamespace(**kw)

    factory.__dataclass_fields__ = {  # type: ignore[attr-defined]
        "system_prompt": None,
        "output_format": None,
    }
    return factory


def _make_structured_sdk(structured_payload: Any, captured_options: list[Any]):
    """SDK-мок: query отдаёт ResultMessage-подобное сообщение со
    structured_output и без текстового контента."""
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = _options_factory_with_output_format()

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured_options.append(options)
        yield SimpleNamespace(structured_output=structured_payload, content=None)

    fake_sdk.query = _query
    return fake_sdk


def _make_subscription_client(mod: Any, fake_sdk: Any, cli_path: str = "/fake/claude"):
    with patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}):
        client = mod.ClaudeSubscriptionClient(
            mod.ClaudeSubscriptionConfig(model="claude-sonnet-4-6", max_turns=1, cli_path=cli_path)
        )
    client._sdk = fake_sdk
    client._sdk_supports_output_format = "output_format" in getattr(
        fake_sdk.ClaudeAgentOptions, "__dataclass_fields__", {}
    )
    return client


def test_claude_subscription_uses_native_structured_output() -> None:
    """Схема уходит в options.output_format (без description — лимит CLI-арга),
    payload берётся из ResultMessage.structured_output, null'ы канонизируются."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    captured: list[Any] = []
    structured = {"declared_goal": "alpha", "optional_note": None}
    fake_sdk = _make_structured_sdk(structured, captured)
    client = _make_subscription_client(mod, fake_sdk)

    schema = {
        "type": "object",
        "required": ["declared_goal"],
        "properties": {
            "declared_goal": {"type": "string", "description": "Цель из запроса."},
            "optional_note": {"type": "string"},
        },
    }
    result = client.chat_json(system_prompt="sys", user_prompt="user", schema=schema)

    # payload — из structured_output, null вычищен (= «не заполнено»).
    assert result.payload == {"declared_goal": "alpha"}
    # Схема передана в output_format и очищена от description.
    assert len(captured) == 1
    sent = captured[0].output_format
    assert sent["type"] == "json_schema"
    assert "description" not in sent["schema"]["properties"]["declared_goal"]
    assert sent["schema"]["required"] == ["declared_goal"]


def test_claude_subscription_downgrades_on_schema_specific_error() -> None:
    """SCHEMA-специфичная ошибка (схему отверг структурный режим) → немедленный
    повтор без него в той же попытке; итог из текстового ответа."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    captured: list[Any] = []
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = _options_factory_with_output_format()

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured.append(options)
        if getattr(options, "output_format", None) is not None:
            raise RuntimeError("error: invalid json schema for --json-schema")
        yield SimpleNamespace(
            structured_output=None,
            content=[SimpleNamespace(text='{"declared_goal": "beta"}')],
        )

    fake_sdk.query = _query
    client = _make_subscription_client(mod, fake_sdk)

    result = client.chat_json(
        system_prompt="sys",
        user_prompt="user",
        schema={"type": "object", "properties": {"declared_goal": {"type": "string"}}},
    )

    assert result.payload == {"declared_goal": "beta"}
    # Первая попытка — структурная, вторая (тот же attempt) — без output_format.
    assert getattr(captured[0], "output_format", None) is not None
    assert getattr(captured[1], "output_format", None) is None


def test_claude_subscription_transient_keeps_structured_mode(monkeypatch) -> None:
    """Транзиентный сбой CLI в структурном режиме НЕ должен отключать enforcement
    (главный баг): повтор идёт СО схемой, а не деградирует навсегда."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    monkeypatch.setenv("POV_CLAUDE_MAX_RETRIES", "2")
    monkeypatch.setattr(mod, "_FLAG_UNSUPPORTED_CLIS", set())  # изоляция от порядка тестов
    # chat_json делает `import time as _time; _time.sleep(...)` для backoff —
    # патчим сам модуль time, чтобы тест не ждал реально.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)

    captured: list[Any] = []
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = _options_factory_with_output_format()
    calls = {"n": 0}

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured.append(options)
        calls["n"] += 1
        if calls["n"] == 1:
            # Транзиент (не schema-специфичный) на первой структурной попытке.
            raise RuntimeError("Command failed with exit code 1")
        yield SimpleNamespace(structured_output={"declared_goal": "gamma"}, content=None)

    fake_sdk.query = _query
    client = _make_subscription_client(mod, fake_sdk)

    result = client.chat_json(
        system_prompt="sys",
        user_prompt="user",
        schema={"type": "object", "properties": {"declared_goal": {"type": "string"}}},
    )

    assert result.payload == {"declared_goal": "gamma"}
    # ОБЕ попытки — структурные: транзиент не сбросил enforcement.
    assert getattr(captured[0], "output_format", None) is not None
    assert getattr(captured[1], "output_format", None) is not None
    assert client._structured_disabled is False


def test_claude_subscription_unknown_option_disables_flag_per_cli_path(monkeypatch) -> None:
    """«unknown option --json-schema» — точный диагноз старого CLI: флаг
    кэшируется выключенным НА ЭТОТ cli_path до конца процесса (другие задачи на
    том же бинарнике не пробуют; другой бинарник — независим)."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    monkeypatch.setattr(mod, "_FLAG_UNSUPPORTED_CLIS", set())
    captured: list[Any] = []
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = _options_factory_with_output_format()

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured.append(options)
        if getattr(options, "output_format", None) is not None:
            raise RuntimeError("error: unknown option '--json-schema'")
        yield SimpleNamespace(
            structured_output=None,
            content=[SimpleNamespace(text='{"ok": true}')],
        )

    fake_sdk.query = _query
    client = _make_subscription_client(mod, fake_sdk, cli_path="/old/claude")
    result = client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert result.payload == {"ok": True}
    assert "/old/claude" in mod._FLAG_UNSUPPORTED_CLIS
    # Новый клиент на ТОМ ЖЕ бинарнике сразу идёт промпт-путём.
    captured.clear()
    client2 = _make_subscription_client(mod, fake_sdk, cli_path="/old/claude")
    client2.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})
    assert getattr(captured[0], "output_format", None) is None
    # А ДРУГОЙ бинарник кэшем не задет — пробует структурный режим.
    captured.clear()
    client3 = _make_subscription_client(mod, fake_sdk, cli_path="/new/claude")
    client3.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})
    assert getattr(captured[0], "output_format", None) is not None


def test_claude_subscription_oversized_schema_skips_structured_mode() -> None:
    """Схема больше лимита CLI-аргумента → структурный режим пропускается
    (одна попытка, сразу промпт-путь)."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    captured: list[Any] = []
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = _options_factory_with_output_format()

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured.append(options)
        yield SimpleNamespace(
            structured_output=None,
            content=[SimpleNamespace(text='{"ok": true}')],
        )

    fake_sdk.query = _query
    client = _make_subscription_client(mod, fake_sdk)

    huge_schema = {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string"} for i in range(1200)},
    }
    result = client.chat_json(system_prompt="s", user_prompt="u", schema=huge_schema)

    assert result.payload == {"ok": True}
    assert len(captured) == 1
    assert getattr(captured[0], "output_format", None) is None


def test_claude_subscription_completion_role_disables_tools() -> None:
    """Completion-роль (не кодовый агент): инструменты отключены (tools=[]).

    Это корневое лекарство от «Reached maximum number of turns»: без
    инструментов модель не может потратить ход на tool-use и отвечает за один
    ход. Агентская роль (с инструментами) — отдельный harness-провайдер."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    captured: list[Any] = []
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured.append(options)
        yield SimpleNamespace(structured_output=None, content=[SimpleNamespace(text='{"ok": true}')])

    fake_sdk.query = _query
    client = _make_subscription_client(mod, fake_sdk)
    client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert len(captured) == 1
    assert captured[0].tools == []  # все встроенные инструменты выключены


_EFFORT_VARS = (
    "POV_CLAUDE_EFFORT",
    "POV_CLAUDE_EFFORT_TRIVIAL",
    "POV_CLAUDE_EFFORT_STANDARD",
    "POV_CLAUDE_EFFORT_COMPLEX",
)


def test_effort_by_complexity(monkeypatch) -> None:
    """Effort соразмерен сложности; все значения — валидные уровни CLI."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    for var in _EFFORT_VARS:
        monkeypatch.delenv(var, raising=False)

    assert mod.effort_for_complexity("trivial") in mod._VALID_EFFORTS
    assert mod.effort_for_complexity("complex") in mod._VALID_EFFORTS
    # Дефолты: trivial — самый дешёвый (low), complex — глубже trivial.
    order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
    assert order[mod.effort_for_complexity("trivial")] <= order[mod.effort_for_complexity("complex")]
    # Неизвестный/None уровень → как standard.
    standard = mod.effort_for_complexity("standard")
    assert mod.effort_for_complexity(None) == standard
    assert mod.effort_for_complexity("bogus") == standard


def test_effort_env_overrides(monkeypatch) -> None:
    """Per-уровень env переопределяет дефолт; глобальный бьёт всё; мусор — игнор."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    for var in _EFFORT_VARS:
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("POV_CLAUDE_EFFORT_STANDARD", "high")
    assert mod.effort_for_complexity("standard") == "high"
    # Невалидное значение игнорируется → дефолт уровня.
    monkeypatch.setenv("POV_CLAUDE_EFFORT_TRIVIAL", "ultra")
    assert mod.effort_for_complexity("trivial") == mod._EFFORT_BY_COMPLEXITY["trivial"]
    # Глобальный перебивает per-уровень.
    monkeypatch.setenv("POV_CLAUDE_EFFORT", "max")
    assert mod.effort_for_complexity("standard") == "max"
    assert mod.effort_for_complexity("complex") == "max"


def test_reasoning_tokens_estimated_as_output_minus_answer() -> None:
    """Токены размышления = output − размер ответа (thinking ⊆ output у Claude).

    Имитируем actual-usage с большим output при компактном ответе → почти всё
    это размышление."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    captured: list[Any] = []
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured.append(options)
        # Компактный ответ (~немного токенов), но usage сообщает большой output.
        yield SimpleNamespace(
            structured_output=None,
            content=[SimpleNamespace(text='{"ok": true}')],
            usage={"input_tokens": 10, "output_tokens": 9000},
            total_cost_usd=0.5,
        )

    fake_sdk.query = _query
    client = _make_subscription_client(mod, fake_sdk)
    result = client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert result.usage is not None
    assert result.usage.output_tokens == 9000
    # Ответ '{"ok": true}' ≈ единицы токенов → размышление ≈ почти весь output.
    assert result.usage.reasoning_tokens is not None
    assert result.usage.reasoning_tokens > 8000


def test_claude_subscription_completion_sets_effort() -> None:
    """Completion-роль задаёт глубину рассуждения через --effort (SDK-поле
    options.effort) — корень замедления opus-4-8 был в дефолтном effort=high."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    captured: list[Any] = []
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        captured.append(options)
        yield SimpleNamespace(structured_output=None, content=[SimpleNamespace(text='{"ok": true}')])

    fake_sdk.query = _query
    # Явный effort на конфиге переопределяет env/дефолт.
    with patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}):
        client = mod.ClaudeSubscriptionClient(
            mod.ClaudeSubscriptionConfig(model="m", cli_path="/fake/claude", effort="low")
        )
    client._sdk = fake_sdk
    client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert captured[0].effort == "low"


def test_claude_subscription_completion_default_max_turns() -> None:
    """Дефолт max_turns для completion-роли — единый источник правды (датакласс).

    Значение > 1 — безопасный запас на служебную дошлифовку CLI под
    ``--json-schema`` (инструменты выключены, агентского зацикливания нет).
    Провайдер-адаптер, не получив max_turns, обязан брать ровно этот дефолт."""
    from pov_generator.infrastructure import claude_subscription_client as mod
    from pov_generator.infrastructure.llm.providers.claude_subscription import (
        ClaudeSubscriptionProvider,
    )

    assert mod._COMPLETION_MAX_TURNS > 1
    assert mod.ClaudeSubscriptionConfig(model="m").max_turns == mod._COMPLETION_MAX_TURNS
    # Адаптер без явного max_turns не дублирует число, а опирается на дефолт.
    provider = ClaudeSubscriptionProvider(model="m")
    assert provider._client._config.max_turns == mod._COMPLETION_MAX_TURNS


def test_max_turns_error_is_not_transient() -> None:
    """Исчерпание ходов — детерминированный исход, а не транзиент: НЕ ретраим
    (повтор с той же конфигурацией бессмысленен), даём точный диагноз."""
    from pov_generator.infrastructure import claude_subscription_client as mod

    msg = "Claude Code returned an error result: Reached maximum number of turns (1)"
    assert mod._is_transient_cli_error(msg) is False


def test_claude_subscription_max_turns_message_is_actionable(monkeypatch) -> None:
    """При исчерпании ходов сообщение указывает на max_turns/tools, а не на
    обманчивый «противоречивый ответ подписки»."""
    from pov_generator.common.errors import ConflictError
    from pov_generator.infrastructure import claude_subscription_client as mod

    monkeypatch.setattr(mod, "_FLAG_UNSUPPORTED_CLIS", set())
    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)

    async def _query(prompt: str, options: Any):  # noqa: ARG001
        raise RuntimeError("Claude Code returned an error result: Reached maximum number of turns (1)")
        yield  # pragma: no cover — делает функцию async-генератором

    fake_sdk.query = _query
    client = _make_subscription_client(mod, fake_sdk)

    with pytest.raises(ConflictError) as excinfo:
        client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    text = str(excinfo.value)
    assert "лимит ходов" in text
    assert "POV_CLAUDE_MAX_TURNS" in text
    assert "противоречив" not in text  # не должно деградировать в старый диагноз


# --- OpenRouterClient: structured output ------------------------------------


class _FakeHttpResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def read(self) -> bytes:
        import json as _json

        return _json.dumps(self._body).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _openrouter_response(content: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_openrouter_sends_strict_schema_and_strips_nulls(monkeypatch) -> None:
    """Схема трансформируется в strict-подмножество (все поля required,
    опциональные — nullable), а null'ы ответа вычищаются."""
    import json as _json

    from pov_generator.infrastructure import openrouter_client as mod

    bodies: list[dict[str, Any]] = []

    def fake_urlopen(http_request: Any, timeout: int = 0):  # noqa: ARG001
        bodies.append(_json.loads(http_request.data.decode("utf-8")))
        return _FakeHttpResponse(_openrouter_response('{"name": "X", "note": null}'))

    monkeypatch.setattr(mod.request, "urlopen", fake_urlopen)
    client = mod.OpenRouterClient(mod.OpenRouterConfig(api_key="k", model="openai/gpt-4.1-mini"))
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}, "note": {"type": "string"}},
    }
    result = client.chat_json(system_prompt="sys json", user_prompt="user", schema=schema)

    assert result.payload == {"name": "X"}  # null вычищен
    sent = bodies[0]["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["strict"] is True
    strict_schema = sent["json_schema"]["schema"]
    assert strict_schema["required"] == ["name", "note"]
    assert strict_schema["additionalProperties"] is False
    assert {"type": "null"} in strict_schema["properties"]["note"]["anyOf"]


def test_openrouter_open_schema_uses_json_object_mode(monkeypatch) -> None:
    """«Unstructured» контракт (additionalProperties: true) не выражается в
    strict — первая же попытка идёт в json_object."""
    import json as _json

    from pov_generator.infrastructure import openrouter_client as mod

    bodies: list[dict[str, Any]] = []

    def fake_urlopen(http_request: Any, timeout: int = 0):  # noqa: ARG001
        bodies.append(_json.loads(http_request.data.decode("utf-8")))
        return _FakeHttpResponse(_openrouter_response('{"anything": 1}'))

    monkeypatch.setattr(mod.request, "urlopen", fake_urlopen)
    client = mod.OpenRouterClient(mod.OpenRouterConfig(api_key="k", model="m"))
    result = client.chat_json(
        system_prompt="sys json",
        user_prompt="user",
        schema={"type": "object", "additionalProperties": True},
    )

    assert result.payload == {"anything": 1}
    assert bodies[0]["response_format"] == {"type": "json_object"}


def test_openrouter_degrades_on_http_400_and_parses_fenced(monkeypatch) -> None:
    """HTTP 400 на json_schema → json_object → без response_format; в финальном
    режиме модель может обернуть ответ в ```json``` — парсинг терпим."""
    import io
    import json as _json
    from urllib import error as _error

    from pov_generator.infrastructure import openrouter_client as mod

    bodies: list[dict[str, Any]] = []

    def fake_urlopen(http_request: Any, timeout: int = 0):  # noqa: ARG001
        body = _json.loads(http_request.data.decode("utf-8"))
        bodies.append(body)
        if body.get("response_format") is not None:
            raise _error.HTTPError(
                "url", 400, "Bad Request", None, io.BytesIO(b"response_format unsupported")
            )
        return _FakeHttpResponse(_openrouter_response('```json\n{"name": "Y"}\n```'))

    monkeypatch.setattr(mod.request, "urlopen", fake_urlopen)
    client = mod.OpenRouterClient(mod.OpenRouterConfig(api_key="k", model="m"))
    schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
    result = client.chat_json(system_prompt="sys json", user_prompt="user", schema=schema)

    assert result.payload == {"name": "Y"}
    modes = [
        (body.get("response_format") or {}).get("type", "none") for body in bodies
    ]
    assert modes == ["json_schema", "json_object", "none"]
