"""Тесты DecisionIdentificationService с mock LLM (v3.6).

Реальный LLM не вызываем — это unit-тест, проверяющий:
- корректность парсинга ответа в Decision-объекты
- fallback'ы при невалидном ответе
- передачу параметров (project_id, task_id) в Decision-записи

История: модуль ранее назывался ``decision_planning_service``; legacy-
alias ``DecisionPlanningService`` оставлен в коде для обратной
совместимости. Импортируем под новым именем.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pov_generator.application.decision_identification_service import (
    DecisionIdentificationService as DecisionPlanningService,
)
from pov_generator.common.errors import ConflictError
from pov_generator.infrastructure.llm.protocol import LLMResult


@dataclass
class _StubLLM:
    """Минимальный mock провайдера: возвращает заданный ответ."""

    name: str = "stub"
    model: str = "stub-model"
    last_system_prompt: str | None = None
    last_user_prompt: str | None = None
    response: dict[str, Any] = None  # type: ignore[assignment]

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        tool_name: str = "produce_artifact",
        tool_description: str = "",
    ) -> LLMResult:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return LLMResult(payload=self.response or {"decisions": []}, usage=None)


class _StubRegistry:
    """Mock LLMProviderRegistry: возвращает заданный StubLLM."""

    def __init__(self, llm: _StubLLM) -> None:
        self._llm = llm

    def get(self, **kwargs) -> _StubLLM:
        return self._llm

    def resolve_for_purpose(self, *args, **kwargs) -> _StubLLM:
        return self._llm


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def _basic_response() -> dict[str, Any]:
    return {
        "decisions": [
            {
                "title": "Выбор СУБД",
                "description": "Какую СУБД использовать для основного сервиса",
                "alternatives": [
                    {
                        "option_id": "opt-postgres",
                        "label": "PostgreSQL",
                        "description": "Реляционная",
                        "pros": ["ACID"],
                        "cons": ["оверхед на простых таблицах"],
                        "confidence": 0.85,
                    },
                    {
                        "option_id": "opt-mongo",
                        "label": "MongoDB",
                        "description": "Документная",
                        "pros": ["гибкая схема"],
                        "cons": ["слабее join'ы"],
                        "confidence": 0.4,
                    },
                ],
                "proposed_option_id": "opt-postgres",
                "rationale": "В контексте реляционных запросов — Postgres",
                "level": "architecture",
                "level_rationale": "Затрагивает несколько компонентов",
                "confidence": 0.85,
                "category": "tech_stack",
            }
        ]
    }


def _make_service(response: dict[str, Any]) -> tuple[DecisionPlanningService, _StubLLM]:
    llm = _StubLLM(response=response)
    return DecisionPlanningService(llm_registry=_StubRegistry(llm)), llm  # type: ignore[arg-type]


def test_plan_returns_decisions_parsed_from_llm_response() -> None:
    service, llm = _make_service(_basic_response())
    result = service.plan_for_task(
        project_id="p-1",
        task_id="task-arch",
        task_title="Architecture",
        artifact_role="architecture_proposal",
        task_summary="Spec architecture",
        context_text="Business request: build a service",
    )
    assert len(result.decisions) == 1
    d = result.decisions[0]
    assert d.title == "Выбор СУБД"
    assert d.project_id == "p-1"
    assert d.source_task_id == "task-arch"
    assert d.source == "identification"
    assert d.status == "proposed"
    assert d.chosen_option_id == "opt-postgres"
    assert d.category == "tech_stack"
    assert d.description == "Какую СУБД использовать для основного сервиса"
    assert d.level == "architecture"
    assert d.confidence == 0.85
    assert len(d.alternatives) == 2


def test_planning_passes_criteria_to_llm_system_prompt() -> None:
    """Системный промпт должен содержать критерии классификации, иначе
    LLM не сможет последовательно проставлять level. Это инвариант
    Фазы 2 — критерии — стабильная часть промпта."""
    service, llm = _make_service(_basic_response())
    service.plan_for_task(
        project_id="p-1",
        task_id="task-1",
        task_title="x",
        artifact_role="x",
        task_summary="x",
        context_text="x",
    )
    assert llm.last_system_prompt is not None
    assert "business" in llm.last_system_prompt
    assert "architecture" in llm.last_system_prompt
    assert "detail" in llm.last_system_prompt
    # Ключевая фраза из правил тай-брейкера
    assert "при сомнении" in llm.last_system_prompt.lower()


def test_planning_includes_task_context_in_user_prompt() -> None:
    service, llm = _make_service(_basic_response())
    service.plan_for_task(
        project_id="p-1",
        task_id="task-1",
        task_title="Generate spec",
        artifact_role="requirements_spec",
        task_summary="Build the spec",
        context_text="UNIQUE_CONTEXT_MARKER",
    )
    assert "Generate spec" in llm.last_user_prompt
    assert "requirements_spec" in llm.last_user_prompt
    assert "UNIQUE_CONTEXT_MARKER" in llm.last_user_prompt


# ---------------------------------------------------------------------------
# Defensive parsing
# ---------------------------------------------------------------------------


def test_empty_decisions_list_is_valid() -> None:
    """LLM имеет право сказать «всё ясно из контекста, ничего планировать
    не надо». Это валидный результат, не ошибка."""
    service, _llm = _make_service({"decisions": []})
    result = service.plan_for_task(
        project_id="p-1",
        task_id="task-1",
        task_title="x",
        artifact_role="x",
        task_summary="x",
        context_text="x",
    )
    assert result.decisions == ()


def test_decision_without_alternatives_is_skipped() -> None:
    """Защитное чтение: decision с < 2 альтернативами — это псевдовыбор
    (заглушка «принять рекомендацию» или вообще ничего); v3.4 требует
    минимум 2 содержательных альтернативы, иначе запись пропускается."""
    response = {
        "decisions": [
            {
                "title": "Bad — no alternatives",
                "description": "",
                "alternatives": [],
                "proposed_option_id": "x",
                "rationale": "",
                "level": "detail",
                "level_rationale": "",
                "confidence": 0.5,
            },
            {
                "title": "Bad — only one alternative",
                "description": "",
                "alternatives": [
                    {"option_id": "opt-only", "label": "Only", "description": "", "confidence": 0.7},
                ],
                "proposed_option_id": "opt-only",
                "rationale": "",
                "level": "detail",
                "level_rationale": "",
                "confidence": 0.5,
                "category": "tech_stack",
            },
            {
                "title": "Good — has two alternatives",
                "description": "",
                "alternatives": [
                    {"option_id": "opt-1", "label": "One", "description": "", "confidence": 0.7},
                    {"option_id": "opt-2", "label": "Two", "description": "", "confidence": 0.4},
                ],
                "proposed_option_id": "opt-1",
                "rationale": "",
                "level": "detail",
                "level_rationale": "",
                "confidence": 0.5,
                "category": "tech_stack",
            },
        ]
    }
    service, _llm = _make_service(response)
    result = service.plan_for_task(
        project_id="p-1",
        task_id="task-1",
        task_title="x",
        artifact_role="x",
        task_summary="x",
        context_text="x",
    )
    assert len(result.decisions) == 1
    assert result.decisions[0].title == "Good — has two alternatives"


def test_invalid_proposed_option_falls_back_to_first_alternative() -> None:
    """Если LLM указала proposed_option_id, которого нет в альтернативах —
    fallback на первую альтернативу. Не падаем, не теряем decision."""
    response = {
        "decisions": [
            {
                "title": "X",
                "description": "",
                "alternatives": [
                    {"option_id": "real-1", "label": "First", "description": "", "confidence": 0.6},
                    {"option_id": "real-2", "label": "Second", "description": "", "confidence": 0.4},
                ],
                "proposed_option_id": "ghost-option-id",
                "rationale": "",
                "level": "detail",
                "level_rationale": "",
                "confidence": 0.5,
                "category": "tech_stack",
            }
        ]
    }
    service, _llm = _make_service(response)
    result = service.plan_for_task(
        project_id="p-1",
        task_id="task-1",
        task_title="x",
        artifact_role="x",
        task_summary="x",
        context_text="x",
    )
    assert result.decisions[0].chosen_option_id == "real-1"


def test_invalid_level_falls_back_to_architecture() -> None:
    """Невалидный level от LLM → fallback на architecture (наиболее
    консервативно: surface в control+expert, скрыто в balanced)."""
    response = {
        "decisions": [
            {
                "title": "X",
                "description": "",
                "alternatives": [
                    {"option_id": "opt-1", "label": "L1", "description": "", "confidence": 0.7},
                    {"option_id": "opt-2", "label": "L2", "description": "", "confidence": 0.5},
                ],
                "proposed_option_id": "opt-1",
                "rationale": "",
                "level": "INVALID_LEVEL",
                "level_rationale": "",
                "confidence": 0.5,
                "category": "tech_stack",
            }
        ]
    }
    service, _llm = _make_service(response)
    result = service.plan_for_task(
        project_id="p-1",
        task_id="task-1",
        task_title="x",
        artifact_role="x",
        task_summary="x",
        context_text="x",
    )
    assert result.decisions[0].level == "architecture"


def test_decisions_field_not_a_list_raises_conflict() -> None:
    response = {"decisions": "not a list"}
    service, _llm = _make_service(response)
    with pytest.raises(ConflictError, match="должно быть массивом"):
        service.plan_for_task(
            project_id="p-1",
            task_id="task-1",
            task_title="x",
            artifact_role="x",
            task_summary="x",
            context_text="x",
        )


def test_llm_call_failure_wraps_to_conflict() -> None:
    """Если LLM-провайдер бросил — оборачиваем в ConflictError с
    контекстом, не пробрасываем сырое исключение наверх."""

    class _FailingLLM:
        name = "fail"
        model = "x"

        def chat_json(self, **kwargs):
            raise RuntimeError("network down")

    class _FailingRegistry:
        def resolve_for_purpose(self, *args, **kwargs):
            return _FailingLLM()

    service = DecisionPlanningService(llm_registry=_FailingRegistry())  # type: ignore[arg-type]
    with pytest.raises(ConflictError, match="Ошибка выявления решений"):
        service.plan_for_task(
            project_id="p-1",
            task_id="task-1",
            task_title="x",
            artifact_role="x",
            task_summary="x",
            context_text="x",
        )
