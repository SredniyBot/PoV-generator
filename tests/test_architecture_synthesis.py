"""Тесты Stage 4: synthesis-задача `architecture.design_synthesis`
и финальный артефакт `architecture.design_document`.

Покрывают:
* render_markdown с полным payload — все 3 mermaid-блока на месте, табличные
  секции (deployment / risks) тоже;
* render опускает секции, для которых в payload пусто;
* compose-логика `_execute_stub` собирает design_document из upstream-артефактов,
  поданных через ContextManifest;
* compose устойчив к отсутствию опциональных upstream'ов (deployment / risks).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.common.errors import ValidationError
from pov_generator.domain.artifacts import ContextBudget, ContextItem, ContextManifest


def _full_design_payload() -> dict:
    return {
        "title": "Тестовая система",
        "executive_summary": "Сервис обработки запросов с асинхронным пайплайном.",
        "system_context": {
            "system_name": "Тестовая система",
            "system_purpose": "Обработка входных запросов.",
            "actors": [
                {"name": "User", "kind": "user", "description": "Подаёт запрос"},
            ],
            "external_systems": [
                {"name": "CRM", "role": "Справочник", "interactions": ["GET /clients"]},
            ],
            "mermaid_context_diagram": "flowchart LR\n    User --> System\n    System --> CRM",
        },
        "components": {
            "summary": "Слоистая декомпозиция.",
            "components": [
                {
                    "name": "Gateway",
                    "responsibilities": "Маршрутизация.",
                    "owns_data": ["—"],
                    "dependencies": ["Worker"],
                },
                {
                    "name": "Worker",
                    "responsibilities": "Бизнес-логика.",
                },
            ],
            "mermaid_component_diagram": "flowchart LR\n    Gateway --> Worker",
        },
        "interactions": {
            "summary": "Синхронный приём, асинхронная обработка.",
            "flows": [
                {
                    "name": "Подача запроса",
                    "trigger": "POST /request",
                    "participants": ["User", "Gateway", "Worker"],
                    "steps": ["User шлёт запрос", "Gateway форвардит", "Worker обрабатывает"],
                },
            ],
            "mermaid_sequence_diagram": "sequenceDiagram\n    User->>Gateway: POST\n    Gateway->>Worker: forward",
            "diagram_kind": "sequence",
        },
        "deployment": {
            "environments": [{"name": "pilot", "purpose": "Пилотный контур"}],
            "components": [
                {
                    "name": "Gateway",
                    "placement": "pilot",
                    "technology": "Python",
                    "responsibilities": "Маршрутизация",
                },
            ],
            "deployment_flow": "Docker → Compose → pilot.",
        },
        "risks": [
            {
                "title": "Качество данных",
                "category": "data",
                "probability": "medium",
                "impact": "high",
                "mitigation": "Ранний EDA.",
            },
        ],
        "non_functional_requirements": ["RPS до 100", "p95 < 500ms"],
        "open_questions": ["Sync vs async для CRM?"],
        "blocking_questions": [],
    }


# --- rendering -------------------------------------------------------------


def test_synthesis_renders_mermaid_blocks_for_all_three_diagrams() -> None:
    md = render_markdown("design_document", _full_design_payload())
    # три mermaid-блока — context / components / interactions
    assert md.count("```mermaid") == 3
    assert "flowchart LR\n    User --> System" in md
    assert "flowchart LR\n    Gateway --> Worker" in md
    assert "sequenceDiagram" in md


def test_synthesis_renders_all_top_level_sections() -> None:
    md = render_markdown("design_document", _full_design_payload())
    assert "# Тестовая система" in md
    assert "## Краткое резюме" in md
    assert "## Системный контекст" in md
    assert "## Компоненты" in md
    assert "## Потоки взаимодействия" in md
    assert "## Развёртывание" in md
    assert "## Риски" in md
    assert "## Нефункциональные требования" in md
    assert "## Открытые вопросы" in md


def test_synthesis_renders_risks_table() -> None:
    md = render_markdown("design_document", _full_design_payload())
    assert "| # | Риск | Категория | Вероятность | Влияние | Митигация |" in md
    assert "| 1 | Качество данных | data | medium | high | Ранний EDA. |" in md


def test_synthesis_renders_deployment_table() -> None:
    md = render_markdown("design_document", _full_design_payload())
    assert "| Компонент | Размещение | Технология | Зона ответственности |" in md
    assert "| Gateway | pilot | Python | Маршрутизация |" in md


def test_synthesis_omits_section_when_upstream_payload_missing() -> None:
    payload = {
        "title": "Только контекст",
        "executive_summary": "Минимальный документ.",
        "system_context": _full_design_payload()["system_context"],
        "blocking_questions": [],
    }
    md = render_markdown("design_document", payload)
    assert "## Системный контекст" in md
    assert "## Компоненты" not in md
    assert "## Потоки взаимодействия" not in md
    assert "## Развёртывание" not in md
    assert "## Риски" not in md
    assert "## Нефункциональные требования" not in md
    # ровно один mermaid (контекстный), остальные секции — не рендерятся
    assert md.count("```mermaid") == 1


def test_synthesis_omits_executive_summary_section_when_field_empty() -> None:
    payload = {
        "title": "Без summary",
        "executive_summary": "",
        "blocking_questions": [],
    }
    md = render_markdown("design_document", payload)
    assert "# Без summary" in md
    assert "## Краткое резюме" not in md


def test_synthesis_emits_blocking_questions_section_when_provided() -> None:
    payload = {
        "title": "С блокером",
        "executive_summary": "...",
        "blocking_questions": ["Не определён владелец CRM"],
    }
    md = render_markdown("design_document", payload)
    assert "## Блокирующие вопросы" in md
    assert "Не определён владелец CRM" in md


# --- schema ---------------------------------------------------------------


def test_schema_accepts_full_payload() -> None:
    schema = artifact_schema("design_document")
    validate_json_schema(_full_design_payload(), schema)


def test_schema_accepts_minimal_payload() -> None:
    schema = artifact_schema("design_document")
    payload = {
        "title": "X",
        "executive_summary": "Y",
        "blocking_questions": [],
    }
    validate_json_schema(payload, schema)


def test_schema_rejects_payload_without_title() -> None:
    schema = artifact_schema("design_document")
    payload = _full_design_payload()
    del payload["title"]
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_schema_rejects_payload_without_executive_summary() -> None:
    schema = artifact_schema("design_document")
    payload = _full_design_payload()
    del payload["executive_summary"]
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


# --- compose --------------------------------------------------------------


def _make_context_item(item_id: str, title: str, payload: dict) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        item_type="artifact",
        source_ref=f"artifact.architecture.{item_id}",
        title=title,
        content=json.dumps(payload, ensure_ascii=False),
        token_estimate=100,
        required=True,
        priority=100,
    )


def _make_context_manifest(items: tuple[ContextItem, ...]) -> ContextManifest:
    return ContextManifest(
        manifest_id="m-1",
        project_id="proj-1",
        task_id="task-1",
        template_ref="architecture.design_synthesis@1.0.0",
        problem_state_version=1,
        budget=ContextBudget(max_input_tokens=10000, reserved_for_output=2000, used_tokens=0),
        items=items,
    )


def test_compose_pulls_from_parsed_inputs() -> None:
    """Compose-ветка в _execute_stub собирает design_document из upstream-артефактов
    (system_context, components, interactions, deployment, risks)."""
    service = ExecutionService(SimpleNamespace(), ContextService(SimpleNamespace()))

    context_items = (
        _make_context_item(
            "context",
            title="Описать системный контекст",
            payload=_full_design_payload()["system_context"],
        ),
        _make_context_item(
            "components",
            title="Выделить компоненты системы",
            payload=_full_design_payload()["components"],
        ),
        _make_context_item(
            "flows",
            title="Описать потоки взаимодействия",
            payload=_full_design_payload()["interactions"],
        ),
        _make_context_item(
            "deployment",
            title="Описать топологию развёртывания",
            payload=_full_design_payload()["deployment"],
        ),
        _make_context_item(
            "risks",
            title="Собрать реестр рисков проекта",
            payload={"risks": _full_design_payload()["risks"]},
        ),
    )

    result = service._execute_stub(
        artifact_role="design_document",
        context_manifest=_make_context_manifest(context_items),
        business_request="Тестовый запрос.",
        goal=None,
        domain_pack_refs=(),
    )

    assert result["title"] == "Тестовая система"
    assert "Обработка входных запросов." in result["executive_summary"]
    assert "Декомпозирована на 2 ключевых компонента" in result["executive_summary"]
    assert "Описано 1 сценариев взаимодействия" in result["executive_summary"]
    assert result["system_context"]["mermaid_context_diagram"].startswith("flowchart LR")
    assert "blocking_questions" not in result["system_context"]
    assert "confidence" not in result["components"]
    assert result["risks"][0]["title"] == "Качество данных"
    # И — main acceptance: результат проходит схему.
    validate_json_schema(result, artifact_schema("design_document"))


def test_compose_works_without_optional_upstreams() -> None:
    """Compose должен отработать даже когда deployment/risks отсутствуют."""
    service = ExecutionService(SimpleNamespace(), ContextService(SimpleNamespace()))

    context_items = (
        _make_context_item(
            "context",
            title="Описать системный контекст",
            payload=_full_design_payload()["system_context"],
        ),
        _make_context_item(
            "components",
            title="Выделить компоненты системы",
            payload=_full_design_payload()["components"],
        ),
        _make_context_item(
            "flows",
            title="Описать потоки взаимодействия",
            payload=_full_design_payload()["interactions"],
        ),
    )

    result = service._execute_stub(
        artifact_role="design_document",
        context_manifest=_make_context_manifest(context_items),
        business_request="Тестовый запрос.",
        goal=None,
        domain_pack_refs=(),
    )

    assert result["title"] == "Тестовая система"
    assert "deployment" not in result
    assert "risks" not in result
    validate_json_schema(result, artifact_schema("design_document"))


def test_compose_falls_back_to_business_request_when_no_upstream() -> None:
    """Когда upstream-артефакты отсутствуют, compose выдаёт минимально валидный
    payload с executive_summary от business_request."""
    service = ExecutionService(SimpleNamespace(), ContextService(SimpleNamespace()))
    result = service._execute_stub(
        artifact_role="design_document",
        context_manifest=_make_context_manifest(()),
        business_request="Описать сервис.",
        goal=None,
        domain_pack_refs=(),
    )
    assert result["title"] == "Архитектурный документ"
    assert "Описать сервис." in result["executive_summary"]
    validate_json_schema(result, artifact_schema("design_document"))
