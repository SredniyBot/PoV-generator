"""Тесты для extract'нутых stub-фикстур (W3.3).

Acceptance из BACKLOG W3.3:
- Добавление нового task_template с _статическим_ stub'ом НЕ требует
  правки Python — только добавления JSON в `templates/stub_fixtures/`.

Эти тесты проверяют:
1. Все ранее захардкоженные artifact_role'ы покрыты фикстурами.
2. Loader корректно подставляет placeholder'ы.
3. Compose-кейсы (requirements_spec / review_report /
   solution_tradeoff_matrix) намеренно НЕ в фикстурах — остались в
   Python, потому что зависят от parsed_inputs / domain flags.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "templates" / "stub_fixtures"


EXPECTED_STATIC_ROLES = {
    "clarification_notes",
    "request_fact_sheet",
    "goal_hypothesis",
    "constraint_inventory",
    "ambiguity_gap_report",
    "normalized_request",
    "business_outcome_model",
    "scope_boundary_matrix",
    "stakeholder_map",
    "decision_ownership_matrix",
    "operating_model_outline",
    "stakeholder_operating_model",
    "user_story_map",
    "alternatives_analysis",
    "solution_option_inventory",
    "delivery_scope_definition",
    "acceptance_model_definition",
    "delivery_acceptance_plan",
    "dependency_map",
    "implementation_dependency_plan",
    "predictive_problem_definition",
    "data_landscape_assessment",
    "security_compliance_constraints",
    "integration_operating_model",
    "ui_requirements_outline",
    "system_context_definition",
    "component_decomposition",
    "interaction_view",
}

# Эти роли намеренно остались в Python — они compose'аются из входных
# артефактов / domain flags, простой JSON-фикстурой не покрывается.
COMPOSE_ROLES_STILL_IN_PYTHON = {
    "requirements_spec",
    "review_report",
    "solution_tradeoff_matrix",
    "design_document",
}


def test_all_static_roles_have_fixture_files() -> None:
    """Каждый artifact_role, который раньше был в _execute_stub как чисто
    статический payload, теперь имеет JSON-фикстуру."""
    actual = {p.stem for p in FIXTURE_ROOT.glob("*.json")}
    missing = EXPECTED_STATIC_ROLES - actual
    assert not missing, f"Нет фикстур для: {sorted(missing)}"


def test_compose_roles_have_no_fixture_file() -> None:
    """Compose-кейсы НЕ должны иметь фикстуру — иначе loader подхватит её
    и обойдёт сложную compose-логику в Python."""
    actual = {p.stem for p in FIXTURE_ROOT.glob("*.json")}
    leaked = COMPOSE_ROLES_STILL_IN_PYTHON & actual
    assert not leaked, f"Compose-роли просочились в фикстуры: {sorted(leaked)}"


def test_loader_substitutes_placeholders() -> None:
    """{{goal}} / {{business_request}} должны замениться на реальные значения,
    placeholder'ы не должны утечь в payload."""
    runtime = _make_runtime()
    service = ExecutionService(runtime, ContextService(runtime))
    payload = service._load_stub_fixture(
        "clarification_notes",
        business_request="Нужно ТЗ для PoV.",
        goal="Подготовить структурированное ТЗ.",
    )
    assert payload is not None
    assert payload["clarified_goal"] == "Подготовить структурированное ТЗ."
    assert "{{" not in json.dumps(payload, ensure_ascii=False)


def test_loader_fallback_goal_when_state_goal_missing() -> None:
    """Если у проекта нет явного goal, loader подставляет универсальный
    fallback с business_request — не оставляет placeholder."""
    runtime = _make_runtime()
    service = ExecutionService(runtime, ContextService(runtime))
    payload = service._load_stub_fixture(
        "business_outcome_model",
        business_request="бриф",
        goal=None,
    )
    assert payload is not None
    assert payload["primary_business_goal"].endswith("бриф") or "бриф" in payload["primary_business_goal"]


def test_loader_returns_none_for_unknown_role() -> None:
    """Loader должен честно вернуть None для роли без фикстуры —
    это сигнал для compose-веток в Python."""
    runtime = _make_runtime()
    service = ExecutionService(runtime, ContextService(runtime))
    assert service._load_stub_fixture("nonexistent_artifact", business_request="x", goal=None) is None


def test_short_request_substitution_truncates_at_160_chars() -> None:
    """normalized_request использует business_request[:160] для краткой сводки."""
    runtime = _make_runtime()
    service = ExecutionService(runtime, ContextService(runtime))
    long_request = "А" * 300
    payload = service._load_stub_fixture(
        "normalized_request",
        business_request=long_request,
        goal=None,
    )
    assert payload is not None
    summary = payload["request_summary"]
    assert "А" * 160 in summary
    assert "А" * 161 not in summary


def _make_runtime():
    """Minimal stand-in для runtime — fixture loader не использует runtime,
    но конструктор ExecutionService требует его."""
    return SimpleNamespace()


@pytest.mark.parametrize("role", sorted(EXPECTED_STATIC_ROLES))
def test_each_fixture_is_valid_json_dict(role: str) -> None:
    """Каждая фикстура читается как JSON-объект — guard против синтаксических
    ошибок при ручном редактировании фикстур."""
    path = FIXTURE_ROOT / f"{role}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload, f"Фикстура {role}.json пустая"
