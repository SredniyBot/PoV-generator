"""Тесты рендеринга архитектурных артефактов (Stage 1).

Покрывают `system_context_definition`: schema-валидация payload'а и
markdown-рендеринг с обязательным Mermaid-блоком.
"""
from __future__ import annotations

import pytest

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.common.errors import ValidationError


def _valid_system_context_payload() -> dict:
    return {
        "system_name": "Тестовая система",
        "system_purpose": "Поддерживать заданный бизнес-процесс.",
        "actors": [
            {"name": "Бизнес-инициатор", "kind": "user", "description": "Подаёт запрос"},
            {"name": "Оператор", "kind": "operator"},
        ],
        "external_systems": [
            {
                "name": "CRM",
                "role": "Источник клиентских данных",
                "interactions": ["Читает реестр клиентов"],
            },
        ],
        "mermaid_context_diagram": "flowchart LR\n    Initiator[Инициатор] --> System[Система]\n    System --> CRM[(CRM)]",
        "blocking_questions": [],
    }


def test_render_system_context_definition_emits_mermaid_block() -> None:
    md = render_markdown("system_context_definition", _valid_system_context_payload())
    assert "# Системный контекст" in md
    assert "## Акторы" in md
    assert "## Внешние системы" in md
    assert "## Контекстная диаграмма" in md
    assert "```mermaid" in md
    assert "flowchart LR" in md
    assert "Бизнес-инициатор" in md
    assert "CRM" in md


def test_render_includes_actor_description_when_present() -> None:
    md = render_markdown("system_context_definition", _valid_system_context_payload())
    assert "Подаёт запрос" in md


def test_render_omits_optional_sections_when_missing() -> None:
    md = render_markdown("system_context_definition", _valid_system_context_payload())
    assert "## Границы системы" not in md
    assert "## Допущения" not in md
    assert "## Блокирующие вопросы" not in md


def test_render_emits_optional_sections_when_provided() -> None:
    payload = _valid_system_context_payload()
    payload["system_boundaries"] = ["Не хранит ПДн дольше обработки"]
    payload["assumptions"] = ["LLM-провайдер доступен из контура"]
    payload["blocking_questions"] = ["Не определён владелец CRM"]
    md = render_markdown("system_context_definition", payload)
    assert "## Границы системы" in md
    assert "## Допущения" in md
    assert "## Блокирующие вопросы" in md
    assert "Не хранит ПДн дольше обработки" in md
    assert "Не определён владелец CRM" in md


def test_schema_accepts_valid_payload() -> None:
    schema = artifact_schema("system_context_definition")
    validate_json_schema(_valid_system_context_payload(), schema)


def test_schema_rejects_payload_without_mermaid_field() -> None:
    payload = _valid_system_context_payload()
    del payload["mermaid_context_diagram"]
    schema = artifact_schema("system_context_definition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_schema_rejects_payload_without_blocking_questions() -> None:
    payload = _valid_system_context_payload()
    del payload["blocking_questions"]
    schema = artifact_schema("system_context_definition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_schema_rejects_actor_missing_kind() -> None:
    payload = _valid_system_context_payload()
    payload["actors"][0].pop("kind")
    schema = artifact_schema("system_context_definition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_schema_rejects_external_system_missing_role() -> None:
    payload = _valid_system_context_payload()
    payload["external_systems"][0].pop("role")
    schema = artifact_schema("system_context_definition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


# --- component_decomposition (Stage 3) -------------------------------------


def _valid_component_decomposition_payload() -> dict:
    return {
        "components": [
            {
                "name": "API Gateway",
                "responsibilities": "Принимает запросы и маршрутизирует.",
                "owns_data": ["Ничего не хранит дольше запроса"],
                "dependencies": ["Auth"],
            },
            {
                "name": "Worker",
                "responsibilities": "Бизнес-логика.",
            },
        ],
        "mermaid_component_diagram": "flowchart LR\n    Gateway --> Worker",
        "blocking_questions": [],
    }


def test_render_component_decomposition_emits_mermaid_and_sections() -> None:
    md = render_markdown("component_decomposition", _valid_component_decomposition_payload())
    assert "# Декомпозиция на компоненты" in md
    assert "## Компоненты" in md
    assert "### API Gateway" in md
    assert "### Worker" in md
    assert "**Владеет данными:**" in md
    assert "**Зависимости:**" in md
    assert "```mermaid" in md
    assert "flowchart LR" in md


def test_render_component_omits_optional_sections_when_missing() -> None:
    md = render_markdown("component_decomposition", _valid_component_decomposition_payload())
    assert "## Сквозные аспекты" not in md
    assert "## Открытые вопросы дизайна" not in md
    assert "## Блокирующие вопросы" not in md


def test_render_component_emits_optional_sections_when_provided() -> None:
    payload = _valid_component_decomposition_payload()
    payload["summary"] = "Слоистая декомпозиция."
    payload["cross_cutting_concerns"] = ["Логирование с trace-id"]
    payload["open_design_questions"] = ["Sync vs async?"]
    md = render_markdown("component_decomposition", payload)
    assert "Слоистая декомпозиция." in md
    assert "## Сквозные аспекты" in md
    assert "Логирование с trace-id" in md
    assert "## Открытые вопросы дизайна" in md


def test_component_schema_rejects_payload_without_mermaid() -> None:
    payload = _valid_component_decomposition_payload()
    del payload["mermaid_component_diagram"]
    schema = artifact_schema("component_decomposition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_component_schema_rejects_component_without_responsibilities() -> None:
    payload = _valid_component_decomposition_payload()
    payload["components"][0].pop("responsibilities")
    schema = artifact_schema("component_decomposition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


# --- interaction_view (Stage 3) --------------------------------------------


def _valid_interaction_view_payload() -> dict:
    return {
        "flows": [
            {
                "name": "Базовый поток",
                "trigger": "POST /request",
                "participants": ["User", "Gateway", "Worker"],
                "steps": [
                    "User шлёт запрос",
                    "Gateway валидирует и форвардит",
                    "Worker обрабатывает",
                ],
            },
        ],
        "mermaid_sequence_diagram": "sequenceDiagram\n    User->>Gateway: POST\n    Gateway->>Worker: forward",
        "blocking_questions": [],
    }


def test_render_interaction_view_emits_mermaid_and_steps() -> None:
    md = render_markdown("interaction_view", _valid_interaction_view_payload())
    assert "# Потоки взаимодействия" in md
    assert "## Сценарии" in md
    assert "### Базовый поток" in md
    assert "**Триггер:**" in md
    assert "**Участники:** User, Gateway, Worker" in md
    assert "1. User шлёт запрос" in md
    assert "Sequence-диаграмма" in md
    assert "```mermaid" in md


def test_render_interaction_view_uses_flowchart_heading_when_kind_set() -> None:
    payload = _valid_interaction_view_payload()
    payload["diagram_kind"] = "flowchart"
    md = render_markdown("interaction_view", payload)
    assert "Диаграмма потока" in md
    assert "Sequence-диаграмма" not in md


def test_render_interaction_view_emits_data_contracts_table_when_provided() -> None:
    payload = _valid_interaction_view_payload()
    payload["data_contracts"] = [
        {"from": "User", "to": "Gateway", "payload": "JSON", "format": "JSON"},
    ]
    payload["failure_modes"] = ["Auth недоступен — 503"]
    md = render_markdown("interaction_view", payload)
    assert "## Контракты данных" in md
    assert "| User | Gateway | JSON | JSON |" in md
    assert "## Режимы сбоев" in md
    assert "Auth недоступен — 503" in md


def test_interaction_view_schema_rejects_diagram_kind_outside_enum() -> None:
    payload = _valid_interaction_view_payload()
    payload["diagram_kind"] = "uml"
    schema = artifact_schema("interaction_view")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_interaction_view_schema_rejects_flow_without_steps() -> None:
    payload = _valid_interaction_view_payload()
    payload["flows"][0].pop("steps")
    schema = artifact_schema("interaction_view")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)
