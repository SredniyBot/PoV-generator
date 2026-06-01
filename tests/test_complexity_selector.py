"""Тесты для complexity pre-selector (W3.2).

Selector — отдельный модуль, который перед запуском leaf-задачи решает,
оставлять ли declared `template.complexity` или повысить/понизить по
фактическому контексту.

Проверяем:
1. Default off — selector НЕ меняет declared complexity (только проверяет env).
2. Stub-режим повышает complexity при многих domain packs.
3. Stub-режим понижает complexity при пустом контексте.
4. LLM-режим вызывает реальный провайдер с правильной схемой.
5. Provider unavailable → fallback на declared, не ломает workflow.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pov_generator.application.complexity_selector_service import select_complexity


def _make_state(
    *,
    domain_packs: tuple[str, ...] = (),
    business_request: str = "Простой PoC.",
    goal: str | None = None,
) -> SimpleNamespace:
    """Минимальный shim ProjectState — selector читает только эти поля
    через .manifest, .knowledge, .process."""
    return SimpleNamespace(
        manifest=SimpleNamespace(business_request=business_request),
        knowledge=SimpleNamespace(goal_statement=lambda: goal),
        process=SimpleNamespace(
            active_domain_pack_records={ref: object() for ref in domain_packs},
            active_gaps={},
        ),
    )


def _make_template(complexity: str = "standard") -> SimpleNamespace:
    return SimpleNamespace(
        complexity=complexity,
        summary="тестовая задача",
        name="Test task",
        ref=SimpleNamespace(as_string=lambda: "common.test@1.0.0"),
    )


def test_selector_off_returns_declared_template_complexity(monkeypatch) -> None:
    monkeypatch.delenv("POV_COMPLEXITY_SELECTOR", raising=False)
    selection = select_complexity(template=_make_template("standard"), state=_make_state())
    assert selection.complexity == "standard"
    assert selection.source == "template"


def test_selector_stub_promotes_complex_on_many_domain_packs(monkeypatch) -> None:
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR", "stub")
    state = _make_state(
        domain_packs=(
            "ml.predictive_analytics@1.0.0",
            "security.enterprise_compliance@1.0.0",
            "integration.enterprise_integration@1.0.0",
        )
    )
    selection = select_complexity(template=_make_template("standard"), state=state)
    assert selection.complexity == "complex"
    assert selection.source == "stub"
    assert "complex" in selection.rationale.lower() or "3" in selection.rationale


def test_selector_stub_keeps_declared_when_context_matches(monkeypatch) -> None:
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR", "stub")
    state = _make_state(business_request="Достаточно длинный бизнес-запрос на несколько строк." * 3)
    selection = select_complexity(template=_make_template("standard"), state=state)
    assert selection.complexity == "standard"
    assert selection.source == "stub"


def test_selector_stub_demotes_complex_when_context_is_empty(monkeypatch) -> None:
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR", "stub")
    selection = select_complexity(template=_make_template("complex"), state=_make_state(business_request="short"))
    assert selection.complexity == "standard"
    assert selection.source == "stub"


def test_selector_llm_calls_provider_with_correct_schema(monkeypatch) -> None:
    """Когда POV_COMPLEXITY_SELECTOR=on, selector должен позвать LLM-провайдер
    и вернуть его ответ как `source=llm`."""
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR", "on")
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR_PROVIDER", "claude_sdk")

    from pov_generator.application import complexity_selector_service as mod
    from pov_generator.infrastructure.llm import LLMResult

    fake_response = {"complexity": "complex", "rationale": "LLM решил — многомодальный сценарий."}
    fake_provider = MagicMock()
    fake_provider.chat_json.return_value = LLMResult(payload=fake_response)

    # После рефакторинга на LLMProviderRegistry мокаем точку резолва
    # провайдера, а не конкретный клиент.
    from pov_generator.infrastructure.llm import LLMProviderRegistry

    with patch.object(LLMProviderRegistry, "get", return_value=fake_provider):
        selection = select_complexity(template=_make_template("standard"), state=_make_state())

    assert selection.complexity == "complex"
    assert selection.source == "llm"
    assert "LLM" in selection.rationale or "многомодальный" in selection.rationale
    fake_provider.chat_json.assert_called_once()
    kwargs = fake_provider.chat_json.call_args.kwargs
    assert "complexity" in kwargs["schema"]["properties"]
    assert kwargs["schema"]["properties"]["complexity"]["enum"] == ["trivial", "standard", "complex"]


def test_selector_falls_back_on_provider_error(monkeypatch) -> None:
    """Если LLM-провайдер кинул ConflictError (нет ключа / нет SDK), selector
    не должен валить workflow — возвращает declared."""
    from pov_generator.common.errors import ConflictError as RealConflictError

    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR", "on")
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR_PROVIDER", "claude_sdk")

    from pov_generator.infrastructure.llm import LLMProviderRegistry

    with patch.object(LLMProviderRegistry, "get", side_effect=RealConflictError("нет ключа")):
        selection = select_complexity(template=_make_template("standard"), state=_make_state())

    assert selection.complexity == "standard"
    assert selection.source == "template"
    assert "fallback" in selection.rationale.lower() or "complexity-selector" in selection.rationale


@pytest.mark.parametrize("llm_value, expected", [
    ("trivial", "trivial"),
    ("standard", "standard"),
    ("complex", "complex"),
    ("super-complex", "standard"),  # неизвестное значение → fallback на declared
])
def test_selector_coerces_invalid_llm_output(monkeypatch, llm_value: str, expected: str) -> None:
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR", "on")
    monkeypatch.setenv("POV_COMPLEXITY_SELECTOR_PROVIDER", "claude_sdk")

    from pov_generator.infrastructure.llm import LLMResult

    fake_provider = MagicMock()
    fake_provider.chat_json.return_value = LLMResult(payload={"complexity": llm_value, "rationale": "ok"})

    from pov_generator.infrastructure.llm import LLMProviderRegistry

    with patch.object(LLMProviderRegistry, "get", return_value=fake_provider):
        selection = select_complexity(template=_make_template("standard"), state=_make_state())

    assert selection.complexity == expected
