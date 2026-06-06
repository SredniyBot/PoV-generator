"""Тесты артефакта оценки реализуемости (common.feasibility_assessment).

Проверяем:
* схема принимает корректный payload и отклоняет неполный
  (нет capabilities; у части нет feasibility; вердикт вне enum);
* render_markdown выдаёт строку на каждую часть, подсвечивает проблемные
  (conditional/uncertain/infeasible) и опускает пустые секции.
"""

from __future__ import annotations

import pytest

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.common.errors import ValidationError

ROLE = "feasibility_assessment"


def _full_payload() -> dict:
    return {
        "capabilities": [
            {
                "name": "Приём показаний",
                "origin": "scope_boundary_matrix: in_scope",
                "feasibility": "feasible",
                "rationale": "Стандартный приём структурированных данных.",
                "blockers": [],
                "prerequisites": ["Формат входных данных"],
                "confidence": 0.85,
                "covered_by": "capability.backend",
                "matched_capability": "backend.data_ingestion",
            },
            {
                "name": "Интеграция с 1С",
                "origin": "solution_option_inventory",
                "feasibility": "conditional",
                "rationale": "Зависит от доступа к контуру заказчика.",
                "blockers": ["Нет доступа к контуру 1С"],
                "prerequisites": ["Тестовый контур 1С"],
                "confidence": 0.6,
                "covered_by": "capability.integration",
                "matched_capability": "integration.erp_sync",
            },
            {
                "name": "Юридически значимое начисление",
                "origin": "normalized_request",
                "feasibility": "infeasible",
                "rationale": "Требует аттестованной биллинговой системы вне периметра PoV.",
                "blockers": ["Нет аттестованной биллинговой системы"],
                "prerequisites": ["Промышленный биллинг"],
                "confidence": 0.7,
            },
        ],
        "overall_feasibility": "mixed",
        "summary": "Базовое реализуемо, интеграция — при условии, начисление — вне рамок.",
        "confidence": 0.74,
    }


# --- схема -----------------------------------------------------------------


def test_schema_accepts_full_payload() -> None:
    validate_json_schema(_full_payload(), artifact_schema(ROLE))


def test_schema_accepts_minimal_payload() -> None:
    payload = {
        "capabilities": [
            {"name": "X", "feasibility": "feasible", "rationale": "ok"},
        ],
        "summary": "S",
    }
    validate_json_schema(payload, artifact_schema(ROLE))


def test_schema_rejects_payload_without_capabilities() -> None:
    payload = _full_payload()
    del payload["capabilities"]
    with pytest.raises(ValidationError):
        validate_json_schema(payload, artifact_schema(ROLE))


def test_schema_rejects_capability_without_feasibility() -> None:
    payload = _full_payload()
    del payload["capabilities"][0]["feasibility"]
    with pytest.raises(ValidationError):
        validate_json_schema(payload, artifact_schema(ROLE))


def test_schema_rejects_unknown_feasibility_verdict() -> None:
    payload = _full_payload()
    payload["capabilities"][0]["feasibility"] = "maybe"
    with pytest.raises(ValidationError):
        validate_json_schema(payload, artifact_schema(ROLE))


# --- рендер -----------------------------------------------------------------


def test_render_emits_row_per_capability() -> None:
    md = render_markdown(ROLE, _full_payload())
    assert "# Оценка реализуемости" in md
    for cap in _full_payload()["capabilities"]:
        assert cap["name"] in md
    # вердикты в человекочитаемом виде
    assert "реализуемо" in md
    assert "при условии" in md
    assert "не реализуемо" in md


def test_render_highlights_problem_parts() -> None:
    md = render_markdown(ROLE, _full_payload())
    assert "## Не реализуемо / под вопросом" in md
    # проблемные части попадают в детальный блок с обоснованием/блокерами
    assert "Зависит от доступа к контуру заказчика." in md
    assert "Нет аттестованной биллинговой системы" in md


def test_render_omits_problem_section_when_all_feasible() -> None:
    payload = {
        "capabilities": [
            {"name": "A", "feasibility": "feasible", "rationale": "ok", "blockers": [], "prerequisites": []},
        ],
        "summary": "Всё реализуемо.",
    }
    md = render_markdown(ROLE, payload)
    assert "Не реализуемо / под вопросом" not in md
    # пустые покрытие/блокеры/предпосылки схлопываются в прочерк
    assert "| 1 | A | реализуемо | — | — | — |" in md


def test_render_shows_coverage_column() -> None:
    md = render_markdown(ROLE, _full_payload())
    assert "Покрытие" in md
    # покрытая часть показывает агента и его способность
    assert "capability.backend / backend.data_ingestion" in md
    assert "capability.integration / integration.erp_sync" in md


def test_schema_accepts_coverage_fields() -> None:
    # covered_by/matched_capability должны проходить (additionalProperties: False)
    validate_json_schema(_full_payload(), artifact_schema(ROLE))
