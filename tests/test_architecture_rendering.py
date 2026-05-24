"""Тесты рендеринга архитектурных артефактов.

Покрывают `system_context_definition`, `component_decomposition`,
`interaction_view`: schema-валидация payload'а с **структурированными**
diagram-объектами (новый контракт; Python детерминированно собирает Mermaid
через `_build_flowchart` / `_build_sequence_diagram`).
"""
from __future__ import annotations

import pytest

from pov_generator.application.artifact_contracts import (
    _build_flowchart,
    _build_interaction_diagram,
    _build_sequence_diagram,
    _escape_mermaid_label,
    _sanitize_mermaid_id,
    artifact_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.common.errors import ValidationError

# --- system_context_definition --------------------------------------------


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
        "context_diagram": {
            "direction": "LR",
            "nodes": [
                {"id": "Initiator", "label": "Инициатор"},
                {"id": "System", "label": "Система"},
                {"id": "CRM", "label": "CRM", "shape": "cylinder"},
            ],
            "edges": [
                {"from": "Initiator", "to": "System"},
                {"from": "System", "to": "CRM"},
            ],
        },
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


def test_schema_rejects_payload_without_context_diagram() -> None:
    payload = _valid_system_context_payload()
    del payload["context_diagram"]
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


def test_schema_rejects_context_diagram_with_invalid_direction() -> None:
    payload = _valid_system_context_payload()
    payload["context_diagram"]["direction"] = "DIAGONAL"
    schema = artifact_schema("system_context_definition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_schema_rejects_context_diagram_node_with_invalid_shape() -> None:
    payload = _valid_system_context_payload()
    payload["context_diagram"]["nodes"][0]["shape"] = "triangle"
    schema = artifact_schema("system_context_definition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


# --- component_decomposition ----------------------------------------------


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
        "component_diagram": {
            "direction": "LR",
            "nodes": [
                {"id": "Gateway", "label": "API Gateway"},
                {"id": "Worker", "label": "Worker"},
            ],
            "edges": [
                {"from": "Gateway", "to": "Worker"},
            ],
        },
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
    assert "Gateway --> Worker" in md


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


def test_component_schema_rejects_payload_without_diagram() -> None:
    payload = _valid_component_decomposition_payload()
    del payload["component_diagram"]
    schema = artifact_schema("component_decomposition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_component_schema_rejects_component_without_responsibilities() -> None:
    payload = _valid_component_decomposition_payload()
    payload["components"][0].pop("responsibilities")
    schema = artifact_schema("component_decomposition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_component_schema_rejects_diagram_edge_with_unknown_kind() -> None:
    payload = _valid_component_decomposition_payload()
    payload["component_diagram"]["edges"][0]["kind"] = "wiggly"
    schema = artifact_schema("component_decomposition")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


# --- interaction_view -----------------------------------------------------


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
        "interaction_diagram": {
            "kind": "sequence",
            "participants": [
                {"id": "U", "label": "User"},
                {"id": "G", "label": "Gateway"},
                {"id": "W", "label": "Worker"},
            ],
            "messages": [
                {"from": "U", "to": "G", "label": "POST /request"},
                {"from": "G", "to": "W", "label": "forward"},
                {"from": "W", "to": "G", "label": "ack", "kind": "reply"},
            ],
        },
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
    assert "sequenceDiagram" in md
    assert "U->>G: POST /request" in md
    assert "W-->>G: ack" in md


def test_render_interaction_view_uses_flowchart_heading_when_kind_set() -> None:
    payload = _valid_interaction_view_payload()
    payload["interaction_diagram"] = {
        "kind": "flowchart",
        "direction": "TD",
        "nodes": [
            {"id": "Start", "label": "Start"},
            {"id": "End", "label": "End"},
        ],
        "edges": [{"from": "Start", "to": "End"}],
    }
    md = render_markdown("interaction_view", payload)
    assert "Диаграмма потока" in md
    assert "Sequence-диаграмма" not in md
    assert "flowchart TD" in md


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
    payload["interaction_diagram"]["kind"] = "uml"
    schema = artifact_schema("interaction_view")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


def test_interaction_view_schema_rejects_flow_without_steps() -> None:
    payload = _valid_interaction_view_payload()
    payload["flows"][0].pop("steps")
    schema = artifact_schema("interaction_view")
    with pytest.raises(ValidationError):
        validate_json_schema(payload, schema)


# --- Mermaid builders -----------------------------------------------------


def test_build_flowchart_emits_header_and_nodes_and_edges() -> None:
    mmd = _build_flowchart(
        {
            "direction": "TD",
            "nodes": [
                {"id": "A", "label": "Alpha"},
                {"id": "B", "label": "Beta", "shape": "cylinder"},
            ],
            "edges": [{"from": "A", "to": "B", "label": "calls"}],
        }
    )
    lines = mmd.splitlines()
    assert lines[0] == "flowchart TD"
    assert '    A["Alpha"]' in lines
    assert '    B[("Beta")]' in lines
    assert "    A -->|calls| B" in lines


def test_build_flowchart_quotes_special_chars_via_escaper() -> None:
    """`&`, `<`, `>` теперь безопасны — мы всегда заворачиваем в `"..."`."""
    mmd = _build_flowchart(
        {
            "direction": "LR",
            "nodes": [
                {"id": "ASR", "label": "ASR & Diarization Module"},
                {"id": "Log", "label": "Log <Metrics> Collector"},
            ],
            "edges": [{"from": "ASR", "to": "Log"}],
        }
    )
    assert '"ASR & Diarization Module"' in mmd
    assert '"Log <Metrics> Collector"' in mmd


def test_build_flowchart_escapes_double_quote_in_label() -> None:
    mmd = _build_flowchart(
        {
            "direction": "LR",
            "nodes": [{"id": "A", "label": 'Quoted "value" here'}],
            "edges": [],
        }
    )
    assert 'Quoted #quot;value#quot; here' in mmd


def test_build_flowchart_collapses_newlines_in_labels() -> None:
    mmd = _build_flowchart(
        {
            "direction": "LR",
            "nodes": [{"id": "A", "label": "Line one\nLine two"}],
            "edges": [],
        }
    )
    assert "Line one Line two" in mmd
    assert "Line one\nLine two" not in mmd.splitlines()[1]


def test_build_flowchart_defaults_to_lr_when_direction_invalid() -> None:
    mmd = _build_flowchart({"direction": "garbage", "nodes": [], "edges": []})
    assert mmd.startswith("flowchart LR")


def test_build_flowchart_sanitizes_non_ascii_ids() -> None:
    """Если LLM зачем-то отдал id с кириллицей/пробелами — мы нормализуем."""
    mmd = _build_flowchart(
        {
            "direction": "LR",
            "nodes": [{"id": "Сервис 1", "label": "Сервис"}],
            "edges": [{"from": "Сервис 1", "to": "Сервис 1"}],
        }
    )
    # Сама id целиком превращается в `_`, попадает в fallback или N-префикс.
    assert "Сервис" not in mmd.splitlines()[1].split('"')[0]


def test_build_sequence_diagram_renders_participants_and_messages() -> None:
    mmd = _build_sequence_diagram(
        {
            "kind": "sequence",
            "participants": [
                {"id": "U", "label": "User"},
                {"id": "G"},  # без label — допустимо
            ],
            "messages": [
                {"from": "U", "to": "G", "label": "ping"},
                {"from": "G", "to": "U", "label": "pong", "kind": "reply"},
            ],
        }
    )
    assert mmd.startswith("sequenceDiagram")
    assert '    participant U as "User"' in mmd
    assert "    participant G" in mmd
    assert "    U->>G: ping" in mmd
    assert "    G-->>U: pong" in mmd


def test_build_sequence_diagram_supports_async_kinds() -> None:
    mmd = _build_sequence_diagram(
        {
            "kind": "sequence",
            "participants": [{"id": "A"}, {"id": "B"}],
            "messages": [
                {"from": "A", "to": "B", "label": "fire-and-forget", "kind": "async_request"},
                {"from": "B", "to": "A", "label": "later", "kind": "async_reply"},
            ],
        }
    )
    assert "A-)B: fire-and-forget" in mmd
    assert "B--)A: later" in mmd


def test_build_interaction_diagram_dispatches_on_kind() -> None:
    seq = _build_interaction_diagram(
        {"kind": "sequence", "participants": [{"id": "A"}], "messages": []}
    )
    assert seq.startswith("sequenceDiagram")
    flow = _build_interaction_diagram(
        {"kind": "flowchart", "direction": "LR", "nodes": [], "edges": []}
    )
    assert flow.startswith("flowchart LR")


def test_build_flowchart_returns_empty_string_for_none() -> None:
    assert _build_flowchart(None) == ""
    assert _build_sequence_diagram(None) == ""
    assert _build_interaction_diagram(None) == ""


def test_sanitize_mermaid_id_strips_non_alnum_and_handles_digits() -> None:
    assert _sanitize_mermaid_id("Hello World") == "Hello_World"
    assert _sanitize_mermaid_id("3rd Party") == "N3rd_Party"
    assert _sanitize_mermaid_id("Сервис") == "node"  # фоллбек — без ASCII букв
    assert _sanitize_mermaid_id("") == "node"
    assert _sanitize_mermaid_id(None, fallback="X") == "X"


def test_escape_mermaid_label_handles_quotes_and_newlines() -> None:
    assert _escape_mermaid_label('say "hi"') == "say #quot;hi#quot;"
    assert _escape_mermaid_label("multi\nline") == "multi line"
    assert _escape_mermaid_label(None) == ""
