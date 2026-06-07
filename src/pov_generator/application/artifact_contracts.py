from __future__ import annotations

import re
from typing import Any

from ..common.errors import ValidationError

JSONSchema = dict[str, Any]


_FLOWCHART_DIRECTIONS = {"LR", "RL", "TD", "TB", "BT"}
_FLOWCHART_SHAPE_BRACKETS: dict[str, tuple[str, str]] = {
    "rect": ("[", "]"),
    "round": ("(", ")"),
    "stadium": ("([", "])"),
    "subroutine": ("[[", "]]"),
    "cylinder": ("[(", ")]"),
    "circle": ("((", "))"),
    "hexagon": ("{{", "}}"),
    "rhombus": ("{", "}"),
}
_FLOWCHART_EDGE_ARROWS: dict[str, str] = {
    "solid": "-->",
    "dotted": "-.->",
    "thick": "==>",
}
_SEQUENCE_MESSAGE_ARROWS: dict[str, str] = {
    "request": "->>",
    "reply": "-->>",
    "async_request": "-)",
    "async_reply": "--)",
}
_VALID_MERMAID_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# --- Общий механизм диаграмм -------------------------------------------------
# Любой артефакт может нести блок ``diagrams`` — список диаграмм с семантическим
# типом и подписью. Тип влияет на подпись по умолчанию; форма рендера (flowchart
# или sequence) определяется самим ``spec``. Это снимает «диаграммы только в
# фиксированных слотах»: новые артефакты (component_model, deployment_map)
# несут произвольное число диаграмм единообразно.
_DIAGRAM_TYPES = (
    "context",      # системный контекст
    "components",   # компоненты и связи
    "internal",     # внутреннее устройство компонента
    "sequence",     # последовательность сообщений
    "flow",         # поток процесса
    "deployment",   # развёртывание
    "data",         # поток данных
)
_DIAGRAM_TYPE_HEADINGS: dict[str, str] = {
    "context": "Контекстная диаграмма",
    "components": "Диаграмма компонентов",
    "internal": "Внутреннее устройство",
    "sequence": "Диаграмма последовательности",
    "flow": "Диаграмма потока",
    "deployment": "Диаграмма развёртывания",
    "data": "Поток данных",
}


def _sanitize_mermaid_id(raw: Any, fallback: str = "node") -> str:
    """Return a Mermaid-safe identifier.

    LLMs sometimes emit ids with spaces, Cyrillic, or punctuation. We keep only
    ASCII alnum / underscore; non-matching chars become underscores; we prepend
    ``N`` if the id starts with a digit; empty strings fall back to ``fallback``.
    """
    if not isinstance(raw, str):
        raw = str(raw) if raw is not None else ""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_")
    if not cleaned:
        return fallback
    if cleaned[0].isdigit():
        cleaned = "N" + cleaned
    return cleaned


def _escape_mermaid_label(raw: Any) -> str:
    """Escape characters that break Mermaid label parsing.

    We always wrap labels in double quotes inside shape brackets, so ``&<>``
    are safe; the one thing we must escape is the double-quote itself.
    Newlines collapse to spaces — Mermaid does support ``<br/>`` but the
    rendered look is worse than a single line.
    """
    if raw is None:
        return ""
    text = str(raw)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.replace('"', "#quot;")
    return text.strip()


def _build_flowchart(diagram: dict[str, Any] | None) -> str:
    if not isinstance(diagram, dict):
        return ""
    direction = diagram.get("direction")
    if not isinstance(direction, str) or direction.upper() not in _FLOWCHART_DIRECTIONS:
        direction = "LR"
    else:
        direction = direction.upper()
    lines = [f"flowchart {direction}"]

    seen_ids: dict[str, str] = {}

    def _resolve_id(raw: Any, fallback: str) -> str:
        key = raw if isinstance(raw, str) else str(raw or "")
        if key in seen_ids:
            return seen_ids[key]
        nid = _sanitize_mermaid_id(raw, fallback=fallback)
        # Avoid id collisions between distinct raw labels.
        candidate = nid
        suffix = 2
        while candidate in seen_ids.values() and seen_ids.get(key) != candidate:
            candidate = f"{nid}_{suffix}"
            suffix += 1
        seen_ids[key] = candidate
        return candidate

    for index, node in enumerate(diagram.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        nid = _resolve_id(node.get("id"), fallback=f"N{index + 1}")
        label = _escape_mermaid_label(node.get("label") or node.get("id") or nid)
        shape = node.get("shape") if isinstance(node.get("shape"), str) else "rect"
        open_b, close_b = _FLOWCHART_SHAPE_BRACKETS.get(shape, ("[", "]"))
        lines.append(f'    {nid}{open_b}"{label}"{close_b}')

    for edge in diagram.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src = _resolve_id(edge.get("from"), fallback="A")
        dst = _resolve_id(edge.get("to"), fallback="B")
        kind = edge.get("kind") if isinstance(edge.get("kind"), str) else "solid"
        arrow = _FLOWCHART_EDGE_ARROWS.get(kind, "-->")
        label = edge.get("label")
        if isinstance(label, str) and label.strip():
            escaped = _escape_mermaid_label(label)
            # Кавычки вокруг подписи ребра ОБЯЗАТЕЛЬНЫ: без них спецсимволы в
            # тексте (в первую очередь «(», а также «|») ломают парсер mermaid —
            # он токенизирует «(» как начало узла (ошибка «got 'PS'»). Узлы уже
            # закавычены; теперь и рёбра — единообразно и безопасно.
            lines.append(f'    {src} {arrow}|"{escaped}"| {dst}')
        else:
            lines.append(f"    {src} {arrow} {dst}")

    return "\n".join(lines)


def _build_sequence_diagram(diagram: dict[str, Any] | None) -> str:
    if not isinstance(diagram, dict):
        return ""
    lines = ["sequenceDiagram"]

    seen_ids: dict[str, str] = {}

    def _resolve_id(raw: Any, fallback: str) -> str:
        key = raw if isinstance(raw, str) else str(raw or "")
        if key in seen_ids:
            return seen_ids[key]
        nid = _sanitize_mermaid_id(raw, fallback=fallback)
        candidate = nid
        suffix = 2
        while candidate in seen_ids.values() and seen_ids.get(key) != candidate:
            candidate = f"{nid}_{suffix}"
            suffix += 1
        seen_ids[key] = candidate
        return candidate

    for index, participant in enumerate(diagram.get("participants") or []):
        if not isinstance(participant, dict):
            continue
        pid = _resolve_id(participant.get("id"), fallback=f"P{index + 1}")
        label = participant.get("label")
        if isinstance(label, str) and label.strip():
            lines.append(f'    participant {pid} as "{_escape_mermaid_label(label)}"')
        else:
            lines.append(f"    participant {pid}")

    for message in diagram.get("messages") or []:
        if not isinstance(message, dict):
            continue
        src = _resolve_id(message.get("from"), fallback="A")
        dst = _resolve_id(message.get("to"), fallback="B")
        kind = message.get("kind") if isinstance(message.get("kind"), str) else "request"
        arrow = _SEQUENCE_MESSAGE_ARROWS.get(kind, "->>")
        label = _escape_mermaid_label(message.get("label"))
        lines.append(f"    {src}{arrow}{dst}: {label}")

    return "\n".join(lines)


def _build_interaction_diagram(diagram: dict[str, Any] | None) -> str:
    if not isinstance(diagram, dict):
        return ""
    kind = diagram.get("kind")
    if kind == "flowchart":
        return _build_flowchart(diagram)
    return _build_sequence_diagram(diagram)


def _build_diagram(spec: dict[str, Any] | None) -> str:
    """Собрать Mermaid из произвольного diagram-spec (для общего механизма).

    Форма выбирается так: явный ``kind`` главенствует; иначе выводим из формы —
    наличие ``messages`` / ``participants`` означает sequence, иначе flowchart.
    Это устойчивее ``_build_interaction_diagram`` (который по умолчанию даёт
    sequence) для flowchart-спеков без явного ``kind``.
    """
    if not isinstance(spec, dict):
        return ""
    kind = spec.get("kind")
    if kind == "flowchart":
        return _build_flowchart(spec)
    if kind == "sequence":
        return _build_sequence_diagram(spec)
    if spec.get("messages") or spec.get("participants"):
        return _build_sequence_diagram(spec)
    return _build_flowchart(spec)


def _render_mermaid_block(mmd: str, heading: str) -> list[str]:
    """Единый рендер одного Mermaid-блока: жирная подпись + fence.

    Пустой ``mmd`` → пустой список (диаграмму не рисуем). Один источник правды
    для формата, на который опираются и фиксированные слоты, и общий механизм.
    """
    if not mmd:
        return []
    return [f"\n**{heading}:**", "```mermaid", mmd, "```"]


def _render_diagrams(diagrams: Any) -> list[str]:
    """Отрендерить блок ``diagrams`` любого артефакта.

    Каждая диаграмма: ``type`` (семантика → подпись по умолчанию), опц.
    ``caption`` (перекрывает подпись), ``spec`` (структура). Пустые/битые
    элементы тихо пропускаются — рендер никогда не падает на кривых данных.
    """
    if not isinstance(diagrams, list):
        return []
    out: list[str] = []
    for block in diagrams:
        if not isinstance(block, dict):
            continue
        mmd = _build_diagram(block.get("spec"))
        if not mmd:
            continue
        caption = block.get("caption")
        if not (isinstance(caption, str) and caption.strip()):
            caption = _DIAGRAM_TYPE_HEADINGS.get(block.get("type"), "Диаграмма")
        out.extend(_render_mermaid_block(mmd, caption.strip()))
    return out


def _pack_enabled(domain_pack_refs: tuple[str, ...], pack_prefix: str) -> bool:
    return any(ref.startswith(f"{pack_prefix}@") for ref in domain_pack_refs)


def _string_array_schema() -> JSONSchema:
    return {"type": "array", "items": {"type": "string"}}


def _flowchart_diagram_schema() -> JSONSchema:
    """Структурированное представление flowchart-диаграммы.

    Никаких сырых Mermaid-строк: модель отдаёт списки узлов/рёбер, Python
    детерминированно собирает Mermaid через ``_build_flowchart``.
    """
    return {
        "type": "object",
        "required": ["direction", "nodes", "edges"],
        "additionalProperties": False,
        "properties": {
            "direction": {
                "type": "string",
                "enum": sorted(_FLOWCHART_DIRECTIONS),
            },
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "label"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "shape": {
                            "type": "string",
                            "enum": sorted(_FLOWCHART_SHAPE_BRACKETS.keys()),
                        },
                    },
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["from", "to"],
                    "additionalProperties": False,
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "label": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": sorted(_FLOWCHART_EDGE_ARROWS.keys()),
                        },
                    },
                },
            },
        },
    }


def _interaction_diagram_schema() -> JSONSchema:
    """Объединённая схема для interaction_view: sequence- или flowchart-диаграмма.

    Один tag-discriminator ``kind`` ∈ {sequence, flowchart}. Списки участников
    / сообщений нужны для sequence; nodes/edges/direction — для flowchart.
    Все остальные поля optional, чтобы LLM не путалась.
    """
    return {
        "type": "object",
        "required": ["kind"],
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["sequence", "flowchart"]},
            "direction": {
                "type": "string",
                "enum": sorted(_FLOWCHART_DIRECTIONS),
            },
            "participants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                    },
                },
            },
            "messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["from", "to", "label"],
                    "additionalProperties": False,
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "label": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": sorted(_SEQUENCE_MESSAGE_ARROWS.keys()),
                        },
                    },
                },
            },
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "label"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "shape": {
                            "type": "string",
                            "enum": sorted(_FLOWCHART_SHAPE_BRACKETS.keys()),
                        },
                    },
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["from", "to"],
                    "additionalProperties": False,
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "label": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": sorted(_FLOWCHART_EDGE_ARROWS.keys()),
                        },
                    },
                },
            },
        },
    }


def _diagram_block_schema() -> JSONSchema:
    """Схема одного элемента блока ``diagrams``.

    ``spec`` переиспользует объединённую схему (sequence | flowchart) — она же
    у interaction_view, чтобы модель не учила два разных формата диаграмм.
    ``of`` — id компонента, к которому относится диаграмма (для type=internal).
    """
    return {
        "type": "object",
        "required": ["type", "spec"],
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": list(_DIAGRAM_TYPES)},
            "caption": {"type": "string"},
            "of": {"type": "string"},
            "spec": _interaction_diagram_schema(),
        },
    }


def _diagrams_array_schema() -> JSONSchema:
    """Схема блока ``diagrams`` — массив диаграмм, который может нести артефакт."""
    return {"type": "array", "items": _diagram_block_schema()}


def _analysis_meta_properties() -> JSONSchema:
    """Поля, которые есть у любого аналитического артефакта.

    ``confidence`` — мера уверенности модели в результате (0..1). После
    рефакторинга это поле — **метаданные артефакта**, а не часть его
    бизнес-содержимого. LLM по-прежнему может вернуть его в payload (мы
    оставляем optional в схеме для обратной совместимости и потому что
    модель так привыкла), но execution_service вытащит значение из
    payload и положит в ``ArtifactMetadata.overall_confidence`` —
    единственное канонично место для уверенности.

    ``blocking_questions`` удалён как legacy: открытые вопросы теперь живут в
    реестре решений (Decision ledger), отдельного поля документа нет.
    """
    return {
        "confidence": {"type": "number"},
    }


def _analysis_object(required: list[str], properties: JSONSchema) -> JSONSchema:
    """Сборка схемы для аналитического артефакта.

    ``confidence`` остаётся в ``properties`` (optional), но НЕ
    включается в ``required``. См. ``_analysis_meta_properties``.
    """
    merged = dict(properties)
    merged.update(_analysis_meta_properties())
    return {
        "type": "object",
        "required": list(required),
        "additionalProperties": False,
        "properties": merged,
    }


def _build_spec_schema() -> JSONSchema:
    """Схема спеки сборки агента (Слой 2) — общая для всех *_build_spec ролей."""
    return _analysis_object(
        ["components", "summary"],
        {
            "assigned_parts": _string_array_schema(),
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "purpose": {"type": "string"},
                        "tech": _string_array_schema(),
                        "interfaces": _string_array_schema(),
                        "data": _string_array_schema(),
                        "dependencies": _string_array_schema(),
                        "test_approach": {"type": "string"},
                    },
                },
            },
            "out_of_scope": _string_array_schema(),
            "open_questions": _string_array_schema(),
            "summary": {"type": "string"},
        },
    )


def artifact_schema(artifact_role: str, domain_pack_refs: tuple[str, ...] = ()) -> JSONSchema:
    frontend_enabled = _pack_enabled(domain_pack_refs, "frontend.web_workspace") or _pack_enabled(domain_pack_refs, "frontend.web_app_requirements")
    ml_enabled = _pack_enabled(domain_pack_refs, "ml.predictive_analytics") or _pack_enabled(domain_pack_refs, "ml.predictive_analytics_pov_requirements")
    security_enabled = _pack_enabled(domain_pack_refs, "security.enterprise_compliance") or _pack_enabled(domain_pack_refs, "security.enterprise_compliance_requirements")
    integration_enabled = _pack_enabled(domain_pack_refs, "integration.enterprise_integration") or _pack_enabled(domain_pack_refs, "integration.enterprise_delivery_requirements")

    requirements_spec_properties: JSONSchema = {
        "title": {"type": "string"},
        "business_goal": {"type": "string"},
        "success_criteria": _string_array_schema(),
        "actors": _string_array_schema(),
        # Сценарий — структурный объект (роль + цель + упорядоченные шаги),
        # а НЕ плоская строка. Это позволяет рендерить честный нумерованный
        # список (каждый шаг с новой строки) вместо «слипшегося» абзаца с
        # инлайновыми «1) 2) 3)». Единое правило с потоками interaction_view:
        # упорядоченная процедура — это массив шагов, не нумерация в прозе.
        "user_stories": {
            "type": "array",
            "description": (
                "Пользовательские сценарии. Для каждой ключевой роли — её цель "
                "и упорядоченные шаги взаимодействия с решением. Шаги задаются "
                "массивом steps (по одному действию на шаг), а НЕ нумерацией "
                "внутри одной строки."
            ),
            "items": {
                "type": "object",
                "required": ["actor", "goal"],
                "additionalProperties": False,
                "properties": {
                    "actor": {"type": "string", "description": "Роль или действующее лицо сценария."},
                    "goal": {"type": "string", "description": "Что роль хочет получить в этом сценарии."},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Упорядоченные шаги сценария, по одному действию на шаг.",
                    },
                },
            },
        },
        "functional_requirements": _string_array_schema(),
        "non_functional_requirements": _string_array_schema(),
        "assumptions": _string_array_schema(),
        "risks": _string_array_schema(),
        "alternatives_considered": _string_array_schema(),
        "acceptance_criteria": _string_array_schema(),
        "open_questions": _string_array_schema(),
        "executive_summary": {"type": "string"},
        "business_context": {"type": "string"},
        "target_outcomes": _string_array_schema(),
        "scope_in": _string_array_schema(),
        "scope_out": _string_array_schema(),
        "stakeholders": _string_array_schema(),
        "operating_model": _string_array_schema(),
        "data_requirements": _string_array_schema(),
        "integration_requirements": _string_array_schema(),
        "security_requirements": _string_array_schema(),
        "deployment_requirements": _string_array_schema(),
        "delivery_artifacts": _string_array_schema(),
        "phased_plan": _string_array_schema(),
        # Расширенные разделы — необязательные. Если в upstream активны
        # соответствующие задачи (glossary_drafting / deployment_topology /
        # project_risk_register / privacy_impact_assessment), то их вклад
        # подмешивается в финальное ТЗ как отдельные разделы. Если нет —
        # эти поля просто отсутствуют, и рендерер их опускает.
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["term", "definition"],
                "additionalProperties": False,
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                    "category": {"type": "string"},
                    "synonyms": _string_array_schema(),
                },
            },
        },
        "deployment_topology": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "environments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "scaling": {"type": "string"},
                        },
                    },
                },
                "network_zones": _string_array_schema(),
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "placement", "responsibilities"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "placement": {"type": "string"},
                            "technology": {"type": "string"},
                            "responsibilities": {"type": "string"},
                        },
                    },
                },
                "deployment_flow": {"type": "string"},
            },
        },
        "project_risks_detail": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "category", "description", "probability", "impact", "mitigation"],
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "probability": {"type": "string", "enum": ["low", "medium", "high"]},
                    "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                    "mitigation": {"type": "string"},
                    "trigger": {"type": "string"},
                    "owner": {"type": "string"},
                },
            },
        },
        "privacy_impact": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pii_categories": _string_array_schema(),
                "processing_purposes": _string_array_schema(),
                "lawful_basis": {"type": "string"},
                "data_minimization": _string_array_schema(),
                "controls": _string_array_schema(),
                "data_subject_rights": {"type": "string"},
                "cross_border_transfers": {"type": "string"},
                "retention_policy": {"type": "string"},
                "residual_risks": _string_array_schema(),
            },
        },
    }

    requirements_spec_required = [
        "title",
        "business_goal",
        "success_criteria",
        "actors",
        "user_stories",
        "functional_requirements",
        "non_functional_requirements",
        "assumptions",
        "risks",
        "alternatives_considered",
        "acceptance_criteria",
        "open_questions",
    ]

    if frontend_enabled:
        requirements_spec_properties["frontend_requirements"] = {
            "type": "object",
            "required": ["user_roles", "user_flows", "screens", "ux_constraints"],
            "additionalProperties": False,
            "properties": {
                "user_roles": _string_array_schema(),
                "user_flows": _string_array_schema(),
                "screens": _string_array_schema(),
                "analytics_views": _string_array_schema(),
                "decision_support_needs": _string_array_schema(),
                "ux_constraints": _string_array_schema(),
            },
        }
        requirements_spec_required.append("frontend_requirements")

    if ml_enabled:
        requirements_spec_properties["ml_requirements"] = {
            "type": "object",
            "required": [
                "prediction_target",
                "prediction_horizon",
                "prediction_unit",
                "data_sources",
                "model_outputs",
                "evaluation_metrics",
                "explainability_requirements",
            ],
            "additionalProperties": False,
            "properties": {
                "prediction_target": {"type": "string"},
                "prediction_horizon": {"type": "string"},
                "prediction_unit": {"type": "string"},
                "data_sources": _string_array_schema(),
                "model_outputs": _string_array_schema(),
                "evaluation_metrics": _string_array_schema(),
                "explainability_requirements": _string_array_schema(),
            },
        }
        requirements_spec_required.append("ml_requirements")

    if security_enabled:
        requirements_spec_properties["security_constraints_detail"] = {
            "type": "object",
            "required": [
                "deployment_constraints",
                "privacy_constraints",
                "access_control_constraints",
                "allowed_ai_usage",
                "mandatory_controls",
            ],
            "additionalProperties": False,
            "properties": {
                "deployment_constraints": _string_array_schema(),
                "privacy_constraints": _string_array_schema(),
                "access_control_constraints": _string_array_schema(),
                "allowed_ai_usage": _string_array_schema(),
                "mandatory_controls": _string_array_schema(),
                "compliance_risks": _string_array_schema(),
            },
        }
        requirements_spec_required.append("security_constraints_detail")

    if integration_enabled:
        requirements_spec_properties["integration_model"] = {
            "type": "object",
            "required": [
                "source_systems",
                "delivery_pattern",
                "refresh_model",
                "target_surfaces",
                "operating_roles",
            ],
            "additionalProperties": False,
            "properties": {
                "source_systems": _string_array_schema(),
                "delivery_pattern": _string_array_schema(),
                "refresh_model": {"type": "string"},
                "target_surfaces": _string_array_schema(),
                "operating_roles": _string_array_schema(),
                "dependency_risks": _string_array_schema(),
            },
        }
        requirements_spec_required.append("integration_model")

    schemas: dict[str, JSONSchema] = {
        "clarification_notes": {
            "type": "object",
            "required": ["clarified_goal", "success_criteria", "assumptions", "open_questions"],
            "additionalProperties": False,
            "properties": {
                "clarified_goal": {"type": "string"},
                "success_criteria": _string_array_schema(),
                "assumptions": _string_array_schema(),
                "open_questions": _string_array_schema(),
            },
        },
        "user_story_map": {
            "type": "object",
            "required": ["actors", "user_stories", "edge_cases"],
            "additionalProperties": False,
            "properties": {
                "actors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "needs"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "needs": _string_array_schema(),
                        },
                    },
                },
                "user_stories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["actor", "story", "value"],
                        "additionalProperties": False,
                        "properties": {
                            "actor": {"type": "string"},
                            "story": {"type": "string"},
                            "value": {"type": "string"},
                        },
                    },
                },
                "edge_cases": _string_array_schema(),
            },
        },
        "alternatives_analysis": {
            "type": "object",
            "required": ["alternatives", "recommended_option", "rationale"],
            "additionalProperties": False,
            "properties": {
                "alternatives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "description", "pros", "cons"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "pros": _string_array_schema(),
                            "cons": _string_array_schema(),
                        },
                    },
                },
                "recommended_option": {"type": "string"},
                "rationale": {"type": "string"},
            },
        },
        # ТЗ v2: «Разбор запроса» — слияние нормализации + фактов + пробелов в
        # один артефакт (explicit_facts и safe_assumptions добавлены сюда из
        # бывших request_fact_sheet / ambiguity_gap_report). Меньше шагов.
        "normalized_request": _analysis_object(
            [
                "request_summary",
                "business_problem",
                "requested_solution_elements",
                "explicit_constraints",
                "explicit_facts",
                "implicit_risks",
                "ambiguous_points",
                "safe_assumptions",
            ],
            {
                "request_summary": {"type": "string"},
                "business_problem": {"type": "string"},
                "requested_solution_elements": _string_array_schema(),
                "explicit_constraints": _string_array_schema(),
                "explicit_facts": _string_array_schema(),       # факты из запроса
                "implicit_risks": _string_array_schema(),
                "ambiguous_points": _string_array_schema(),      # неоднозначности/пробелы
                "safe_assumptions": _string_array_schema(),      # рабочие допущения
            },
        ),
        "goal_hypothesis": _analysis_object(
            [
                "hypothesized_goal",
                "expected_effects",
                "project_stage_hypothesis",
                "success_signals",
                "unresolved_goal_points",
            ],
            {
                "hypothesized_goal": {"type": "string"},
                "expected_effects": _string_array_schema(),
                "project_stage_hypothesis": {"type": "string"},
                "success_signals": _string_array_schema(),
                "unresolved_goal_points": _string_array_schema(),
            },
        ),
        "constraint_inventory": _analysis_object(
            [
                "explicit_constraints",
                "inferred_constraints",
                "stage_constraints",
                "environment_constraints",
                "dependency_constraints",
            ],
            {
                "explicit_constraints": _string_array_schema(),
                "inferred_constraints": _string_array_schema(),
                "stage_constraints": _string_array_schema(),
                "environment_constraints": _string_array_schema(),
                "dependency_constraints": _string_array_schema(),
            },
        ),
        "problem_statement": _analysis_object(
            ["problem", "affected", "job_to_be_done", "desired_outcome", "definition_of_solved"],
            {
                # Стержень ТЗ: формализованная ПРОБЛЕМА, а не решение.
                "problem": {"type": "string"},
                "affected": _string_array_schema(),  # кто страдает / носители боли
                "job_to_be_done": {
                    "type": "object",
                    "required": ["when", "want", "so_that"],
                    "additionalProperties": False,
                    "properties": {
                        "when": {"type": "string"},
                        "want": {"type": "string"},
                        "so_that": {"type": "string"},
                    },
                },
                "desired_outcome": _string_array_schema(),       # нужный исход (качественно)
                "definition_of_solved": _string_array_schema(),  # что значит «решено»
                "assumptions": _string_array_schema(),
            },
        ),
        "integration_data_map": _analysis_object(
            ["external_systems", "data_sources", "data_flows"],
            {
                "external_systems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "integration_mode": {"type": "string"},
                        },
                    },
                },
                "data_sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "description"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "owner": {"type": "string"},
                        },
                    },
                },
                "data_flows": _string_array_schema(),
                "integration_constraints": _string_array_schema(),
                "open_questions": _string_array_schema(),
            },
        ),
        "business_outcome_model": _analysis_object(
            [
                "primary_business_goal",
                "target_kpis",
                "success_metrics",
                "business_process_impacts",
                "expected_decisions",
                "value_hypotheses",
                "assumptions",
            ],
            {
                "primary_business_goal": {"type": "string"},
                "target_kpis": _string_array_schema(),
                "success_metrics": _string_array_schema(),
                "business_process_impacts": _string_array_schema(),
                "expected_decisions": _string_array_schema(),
                "value_hypotheses": _string_array_schema(),
                "assumptions": _string_array_schema(),
            },
        ),
        "scope_boundary_matrix": _analysis_object(
            [
                "in_scope",
                "out_of_scope",
                "pilot_boundaries",
                "future_phase_candidates",
                "mandatory_deliverables",
                "excluded_deliverables",
            ],
            {
                "in_scope": _string_array_schema(),
                "out_of_scope": _string_array_schema(),
                "pilot_boundaries": _string_array_schema(),
                "future_phase_candidates": _string_array_schema(),
                "mandatory_deliverables": _string_array_schema(),
                "excluded_deliverables": _string_array_schema(),
            },
        ),
        "stakeholder_map": _analysis_object(
            ["stakeholder_groups", "primary_users", "data_owners", "support_teams"],
            {
                "stakeholder_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "role", "influence", "expectations"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "influence": {"type": "string"},
                            "expectations": _string_array_schema(),
                        },
                    },
                },
                "primary_users": _string_array_schema(),
                "data_owners": _string_array_schema(),
                "support_teams": _string_array_schema(),
            },
        ),
        "decision_ownership_matrix": _analysis_object(
            ["decisions", "unowned_decisions", "approval_points"],
            {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "owner", "participants", "timing"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "owner": {"type": "string"},
                            "participants": _string_array_schema(),
                            "timing": {"type": "string"},
                        },
                    },
                },
                "unowned_decisions": _string_array_schema(),
                "approval_points": _string_array_schema(),
            },
        ),
        "operating_model_outline": _analysis_object(
            ["process_flow", "producer_roles", "consumer_roles", "support_roles", "handoff_risks"],
            {
                "process_flow": _string_array_schema(),
                "producer_roles": _string_array_schema(),
                "consumer_roles": _string_array_schema(),
                "support_roles": _string_array_schema(),
                "handoff_risks": _string_array_schema(),
            },
        ),
        "stakeholder_operating_model": _analysis_object(
            [
                "stakeholder_groups",
                "primary_users",
                "decision_owners",
                "operating_model",
                "adoption_constraints",
            ],
            {
                "stakeholder_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "role", "expectations", "responsibilities"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "expectations": _string_array_schema(),
                            "responsibilities": _string_array_schema(),
                        },
                    },
                },
                "primary_users": _string_array_schema(),
                "decision_owners": _string_array_schema(),
                "operating_model": _string_array_schema(),
                "adoption_constraints": _string_array_schema(),
            },
        ),
        "solution_option_inventory": _analysis_object(
            ["options", "comparison_axes"],
            {
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "summary", "boundary_fit", "enabling_conditions"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "summary": {"type": "string"},
                            "boundary_fit": {"type": "string"},
                            "enabling_conditions": _string_array_schema(),
                            "pros": _string_array_schema(),
                            "cons": _string_array_schema(),
                            "fit_score": {"type": "number"},
                        },
                    },
                },
                "comparison_axes": _string_array_schema(),
                # recommended_option и rationale переехали сюда из
                # solution_tradeoff_matrix: задача solution_option_inventory
                # явно выбирает один вариант (см. её prompt). Раньше поле
                # отсутствовало в схеме и рекомендация терялась в массиве.
                "recommended_option": {"type": "string"},
                "recommendation_rationale": {"type": "string"},
            },
        ),
        "solution_tradeoff_matrix": _analysis_object(
            ["options", "recommended_option", "recommendation_rationale", "deferred_decisions"],
            {
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "summary", "fit_for_pilot", "pros", "cons", "risks"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "summary": {"type": "string"},
                            "fit_for_pilot": {"type": "string"},
                            "pros": _string_array_schema(),
                            "cons": _string_array_schema(),
                            "risks": _string_array_schema(),
                        },
                    },
                },
                "recommended_option": {"type": "string"},
                "recommendation_rationale": {"type": "string"},
                "deferred_decisions": _string_array_schema(),
            },
        ),
        "delivery_scope_definition": _analysis_object(
            ["delivery_items", "excluded_items", "demo_expectations", "evidence_artifacts"],
            {
                "delivery_items": _string_array_schema(),
                "excluded_items": _string_array_schema(),
                "demo_expectations": _string_array_schema(),
                "evidence_artifacts": _string_array_schema(),
            },
        ),
        "acceptance_model_definition": _analysis_object(
            [
                "acceptance_criteria",
                "success_evidence",
                "required_customer_inputs",
                "formal_approvals",
                "rejection_conditions",
            ],
            {
                "acceptance_criteria": _string_array_schema(),
                "success_evidence": _string_array_schema(),
                "required_customer_inputs": _string_array_schema(),
                "formal_approvals": _string_array_schema(),
                "rejection_conditions": _string_array_schema(),
            },
        ),
        "delivery_acceptance_plan": _analysis_object(
            [
                "delivery_items",
                "acceptance_criteria",
                "success_evidence",
                "required_customer_inputs",
                "formal_approvals",
                "open_dependencies",
            ],
            {
                "delivery_items": _string_array_schema(),
                "acceptance_criteria": _string_array_schema(),
                "success_evidence": _string_array_schema(),
                "required_customer_inputs": _string_array_schema(),
                "formal_approvals": _string_array_schema(),
                "open_dependencies": _string_array_schema(),
            },
        ),
        "dependency_map": _analysis_object(
            [
                "critical_dependencies",
                "customer_inputs",
                "external_decisions",
                "access_dependencies",
                "stop_conditions",
            ],
            {
                "critical_dependencies": _string_array_schema(),
                "customer_inputs": _string_array_schema(),
                "external_decisions": _string_array_schema(),
                "access_dependencies": _string_array_schema(),
                "stop_conditions": _string_array_schema(),
            },
        ),
        "implementation_dependency_plan": _analysis_object(
            ["phases", "critical_dependencies", "project_risks"],
            {
                "phases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "objectives", "dependencies", "outputs"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "objectives": _string_array_schema(),
                            "dependencies": _string_array_schema(),
                            "outputs": _string_array_schema(),
                        },
                    },
                },
                "critical_dependencies": _string_array_schema(),
                "project_risks": _string_array_schema(),
            },
        ),
        # Phase 3+4 additions: glossary, risk register, deployment topology,
        # privacy DPIA. Все они опциональные дополнения к финальному ТЗ.
        "glossary_terms": _analysis_object(
            ["entries"],
            {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["term", "definition"],
                        "additionalProperties": False,
                        "properties": {
                            "term": {"type": "string"},
                            "definition": {"type": "string"},
                            "category": {"type": "string"},
                            "synonyms": _string_array_schema(),
                        },
                    },
                },
                "domain_scope": {"type": "string"},
            },
        ),
        "project_risk_register": _analysis_object(
            ["risks"],
            {
                "risks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["title", "category", "probability", "impact", "mitigation"],
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "category": {"type": "string"},
                            "description": {"type": "string"},
                            "probability": {"type": "string", "enum": ["low", "medium", "high"]},
                            "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                            "mitigation": {"type": "string"},
                            "trigger": {"type": "string"},
                            "owner": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        ),
        "feasibility_assessment": _analysis_object(
            ["capabilities", "summary"],
            {
                "capabilities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "feasibility", "rationale"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "origin": {"type": "string"},
                            "feasibility": {
                                "type": "string",
                                "enum": ["feasible", "conditional", "uncertain", "infeasible"],
                            },
                            "rationale": {"type": "string"},
                            "blockers": _string_array_schema(),
                            "prerequisites": _string_array_schema(),
                            "confidence": {"type": "number"},
                            "covered_by": {"type": "string"},
                            "matched_capability": {"type": "string"},
                        },
                    },
                },
                "overall_feasibility": {
                    "type": "string",
                    "enum": ["feasible", "mixed", "blocked"],
                },
                "summary": {"type": "string"},
            },
        ),
        "backend_build_spec": _build_spec_schema(),
        "ui_build_spec": _build_spec_schema(),
        "ml_build_spec": _build_spec_schema(),
        "data_build_spec": _build_spec_schema(),
        "integration_build_spec": _build_spec_schema(),
        # Ф6: спека сборки ОДНОГО компонента (веер по компонентам). Это и есть
        # самодостаточный вход для harness-задачи реализации компонента.
        "component_build_spec": _analysis_object(
            ["component", "purpose"],
            {
                "component": {"type": "string"},
                "purpose": {"type": "string"},
                "capability_owner": {"type": "string"},
                "tech": _string_array_schema(),
                "provided_interfaces": _string_array_schema(),
                "consumed_interfaces": _string_array_schema(),
                "data": _string_array_schema(),
                "build_steps": _string_array_schema(),
                "test_approach": {"type": "string"},
                "acceptance": _string_array_schema(),
                "dependencies": _string_array_schema(),
                "out_of_scope": _string_array_schema(),
                "open_questions": _string_array_schema(),
            },
        ),
        "build_plan": _analysis_object(
            ["title", "executive_summary", "summary"],
            {
                "title": {"type": "string"},
                "executive_summary": {"type": "string"},
                "routing": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "part": {"type": "string"},
                            "agent": {"type": "string"},
                            "capability": {"type": "string"},
                            "status": {"type": "string"},
                        },
                    },
                },
                "sequencing": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "phase": {"type": "string"},
                            "items": _string_array_schema(),
                        },
                    },
                },
                "per_agent": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "agent": {"type": "string"},
                            "components_count": {"type": "number"},
                            "highlights": _string_array_schema(),
                        },
                    },
                },
                "risks_and_gaps": _string_array_schema(),
                "summary": {"type": "string"},
            },
        ),
        "deployment_topology": _analysis_object(
            ["environments", "network_zones", "components"],
            {
                "environments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "scaling": {"type": "string"},
                        },
                    },
                },
                "network_zones": _string_array_schema(),
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "placement", "responsibilities"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "placement": {"type": "string"},
                            "responsibilities": {"type": "string"},
                            "technology": {"type": "string"},
                        },
                    },
                },
                "deployment_flow": {"type": "string"},
            },
        ),
        "privacy_impact_assessment": _analysis_object(
            ["pii_categories", "processing_purposes", "lawful_basis", "controls"],
            {
                "pii_categories": _string_array_schema(),
                "processing_purposes": _string_array_schema(),
                "lawful_basis": {"type": "string"},
                "data_minimization": _string_array_schema(),
                "controls": _string_array_schema(),
                "data_subject_rights": _string_array_schema(),
                "cross_border_transfers": {"type": "string"},
                "retention_policy": {"type": "string"},
                "residual_risks": _string_array_schema(),
            },
        ),
        "predictive_problem_definition": _analysis_object(
            [
                "prediction_target",
                "prediction_horizon",
                "prediction_unit",
                "label_definition",
                "business_actions",
                "model_outputs",
                "evaluation_metrics",
                "baseline_expectations",
                "explainability_requirements",
            ],
            {
                "prediction_target": {"type": "string"},
                "prediction_horizon": {"type": "string"},
                "prediction_unit": {"type": "string"},
                "label_definition": {"type": "string"},
                "business_actions": _string_array_schema(),
                "model_outputs": _string_array_schema(),
                "evaluation_metrics": _string_array_schema(),
                "baseline_expectations": _string_array_schema(),
                "explainability_requirements": _string_array_schema(),
            },
        ),
        "data_landscape_assessment": _analysis_object(
            [
                "source_systems",
                "required_entities",
                "key_features",
                "data_quality_risks",
                "data_gaps",
                "feasibility_assessment",
                "privacy_notes",
            ],
            {
                "source_systems": _string_array_schema(),
                "required_entities": _string_array_schema(),
                "key_features": _string_array_schema(),
                "data_quality_risks": _string_array_schema(),
                "data_gaps": _string_array_schema(),
                "feasibility_assessment": {"type": "string"},
                "privacy_notes": _string_array_schema(),
            },
        ),
        "security_compliance_constraints": _analysis_object(
            [
                "deployment_constraints",
                "privacy_constraints",
                "access_control_constraints",
                "integration_security_constraints",
                "allowed_ai_usage",
                "mandatory_controls",
                "compliance_risks",
            ],
            {
                "deployment_constraints": _string_array_schema(),
                "privacy_constraints": _string_array_schema(),
                "access_control_constraints": _string_array_schema(),
                "integration_security_constraints": _string_array_schema(),
                "allowed_ai_usage": _string_array_schema(),
                "mandatory_controls": _string_array_schema(),
                "compliance_risks": _string_array_schema(),
            },
        ),
        "integration_operating_model": _analysis_object(
            [
                "source_integrations",
                "target_integrations",
                "refresh_model",
                "data_delivery_pattern",
                "operating_roles",
                "support_model",
                "dependency_risks",
            ],
            {
                "source_integrations": _string_array_schema(),
                "target_integrations": _string_array_schema(),
                "refresh_model": {"type": "string"},
                "data_delivery_pattern": _string_array_schema(),
                "operating_roles": _string_array_schema(),
                "support_model": _string_array_schema(),
                "dependency_risks": _string_array_schema(),
            },
        ),
        "design_document": _analysis_object(
            ["title", "executive_summary"],
            {
                "title": {"type": "string"},
                "executive_summary": {"type": "string"},
                # Секции — passthrough из upstream-артефактов. Структура
                # этих объектов уже валидирована собственными контрактами
                # (system_context_definition / component_decomposition /
                # interaction_view / deployment_topology). Здесь принимаем
                # как opaque-payload и оставляем рендеру разобрать.
                "system_context": {"type": "object", "additionalProperties": True},
                "components": {"type": "object", "additionalProperties": True},
                "interactions": {"type": "object", "additionalProperties": True},
                "deployment": {"type": "object", "additionalProperties": True},
                "risks": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "non_functional_requirements": _string_array_schema(),
            },
        ),
        "component_decomposition": _analysis_object(
            ["components", "component_diagram"],
            {
                "summary": {"type": "string"},
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "responsibilities"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "responsibilities": {"type": "string"},
                            "owns_data": _string_array_schema(),
                            "dependencies": _string_array_schema(),
                        },
                    },
                },
                "component_diagram": _flowchart_diagram_schema(),
                "cross_cutting_concerns": _string_array_schema(),
            },
        ),
        "interaction_view": _analysis_object(
            ["flows", "interaction_diagram"],
            {
                "summary": {"type": "string"},
                "flows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "trigger", "participants", "steps"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "trigger": {"type": "string"},
                            "participants": _string_array_schema(),
                            "steps": _string_array_schema(),
                        },
                    },
                },
                "interaction_diagram": _interaction_diagram_schema(),
                "data_contracts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["from", "to", "payload"],
                        "additionalProperties": False,
                        "properties": {
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "payload": {"type": "string"},
                            "format": {"type": "string"},
                        },
                    },
                },
                "failure_modes": _string_array_schema(),
            },
        ),
        "system_context_definition": _analysis_object(
            ["system_name", "system_purpose", "actors", "external_systems", "context_diagram"],
            {
                "system_name": {"type": "string"},
                "system_purpose": {"type": "string"},
                "actors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "kind"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "external_systems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "role"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "interactions": _string_array_schema(),
                        },
                    },
                },
                "context_diagram": _flowchart_diagram_schema(),
                "system_boundaries": _string_array_schema(),
                "assumptions": _string_array_schema(),
            },
        ),
        "component_model": _analysis_object(
            ["components", "coverage"],
            {
                "summary": {"type": "string"},
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "name",
                            "type",
                            "layer",
                            "responsibility",
                            "justification",
                            "provided_interfaces",
                            "modules",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": [
                                    "service",
                                    "datastore",
                                    "queue",
                                    "ui",
                                    "scheduled",
                                    "external",
                                ],
                            },
                            "layer": {
                                "type": "string",
                                "enum": [
                                    "core",
                                    "application",
                                    "adapter",
                                    "infrastructure",
                                ],
                            },
                            "responsibility": {"type": "string"},
                            "justification": {"type": "string"},
                            "capability_owner": {"type": "string"},
                            "nfr": _string_array_schema(),
                            "provided_interfaces": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["name"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "name": {"type": "string"},
                                        "sync": {
                                            "type": "string",
                                            "enum": ["sync", "async"],
                                        },
                                        "criticality": {
                                            "type": "string",
                                            "enum": ["normal", "critical"],
                                        },
                                        "input": {"type": "string"},
                                        "output": {"type": "string"},
                                        "errors": _string_array_schema(),
                                        # Углубление для critical-швов: схема/инварианты/
                                        # идемпотентность/гарантии. Для normal — не нужно.
                                        "detail": {"type": "string"},
                                    },
                                },
                            },
                            "consumed_interfaces": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["component", "interface"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "component": {"type": "string"},
                                        "interface": {"type": "string"},
                                    },
                                },
                            },
                            "owned_data": _string_array_schema(),
                            "events": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "emits": _string_array_schema(),
                                    "consumes": _string_array_schema(),
                                },
                            },
                            "modules": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["id", "responsibility"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "id": {"type": "string"},
                                        "responsibility": {"type": "string"},
                                        "realizes": {"type": "string"},
                                    },
                                },
                            },
                            "internal_edges": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["from", "to"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "from": {"type": "string"},
                                        "to": {"type": "string"},
                                    },
                                },
                            },
                            "requisites": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["id", "kind", "title"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "id": {"type": "string"},
                                        "kind": {
                                            "type": "string",
                                            "enum": [
                                                "credential",
                                                "dataset",
                                                "file",
                                                "setting",
                                                "interface_format",
                                                "sample",
                                                "other",
                                            ],
                                        },
                                        "title": {"type": "string"},
                                        "needed_for": {"type": "string"},
                                        "blocking": {"type": "boolean"},
                                    },
                                },
                            },
                        },
                    },
                },
                "coverage": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "actors": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["actor", "components"],
                                "additionalProperties": False,
                                "properties": {
                                    "actor": {"type": "string"},
                                    "components": _string_array_schema(),
                                },
                            },
                        },
                        "external_systems": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["system", "components"],
                                "additionalProperties": False,
                                "properties": {
                                    "system": {"type": "string"},
                                    "components": _string_array_schema(),
                                },
                            },
                        },
                    },
                },
                "diagrams": _diagrams_array_schema(),
            },
        ),
        "deployment_map": _analysis_object(
            ["deployment_units"],
            {
                "summary": {"type": "string"},
                "deployment_units": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "name", "components", "justification"],
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "runtime": {"type": "string"},
                            "components": _string_array_schema(),
                            "justification": {"type": "string"},
                        },
                    },
                },
                "diagrams": _diagrams_array_schema(),
            },
        ),
        "ui_requirements_outline": _analysis_object(
            ["user_roles", "user_flows", "screens", "ux_constraints"],
            {
                "user_roles": _string_array_schema(),
                "user_flows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "steps"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "steps": _string_array_schema(),
                        },
                    },
                },
                "screens": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                    },
                },
                "analytics_views": _string_array_schema(),
                "decision_support_needs": _string_array_schema(),
                "ux_constraints": _string_array_schema(),
            },
        ),
        "requirements_spec": {
            "type": "object",
            "required": requirements_spec_required,
            "additionalProperties": False,
            "properties": requirements_spec_properties,
        },
        # Demo / fan-out templates — unstructured, accept any object
        "feature_list": {"type": "object", "additionalProperties": True},
        "feature_detail": {"type": "object", "additionalProperties": True},
        # Harness-демо (executor: harness) — неструктурный демонстрационный выход.
        "demo_output": {"type": "object", "additionalProperties": True},
        "demo_bundle": {"type": "object", "additionalProperties": True},
    }
    if artifact_role not in schemas:
        raise ValidationError(f"Неизвестный контракт артефакта: {artifact_role}")
    return schemas[artifact_role]


def validate_json_schema(value: Any, schema: JSONSchema, path: str = "$") -> None:
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            raise ValidationError(f"{path}: ожидался объект")
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ValidationError(f"{path}: отсутствует обязательное поле '{key}'")
        properties = schema.get("properties", {})
        if not schema.get("additionalProperties", True):
            unknown_keys = set(value) - set(properties)
            if unknown_keys:
                raise ValidationError(f"{path}: неизвестные поля {sorted(unknown_keys)}")
        for key, property_schema in properties.items():
            if key in value:
                validate_json_schema(value[key], property_schema, f"{path}.{key}")
        return
    if schema_type == "array":
        if not isinstance(value, list):
            raise ValidationError(f"{path}: ожидался список")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, f"{path}[{index}]")
        return
    if schema_type == "string":
        if not isinstance(value, str):
            raise ValidationError(f"{path}: ожидалась строка")
        allowed = schema.get("enum")
        if allowed and value not in allowed:
            raise ValidationError(f"{path}: недопустимое значение '{value}'")
        return
    if schema_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValidationError(f"{path}: ожидалось число")
        return
    if schema_type == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"{path}: ожидалось логическое значение")
        return
    raise ValidationError(f"{path}: неподдерживаемый тип схемы '{schema_type}'")


def schema_instruction(role: str, domain_pack_refs: tuple[str, ...]) -> str:
    schema = artifact_schema(role, domain_pack_refs)
    return (
        "Верни строго JSON, соответствующий этой схеме.\n"
        "Если данных недостаточно для уверенного вывода, не выдумывай: оставь "
        "поле незаполненным или отметь его как assumption. При желании укажи "
        "`confidence` (0..1) — это поле необязательное и будет вынесено в "
        "метаданные артефакта, поэтому в самом содержании цифру повторять "
        "не нужно. Вопросы к пользователю формируются отдельно через "
        "выявление решений и реестр решений — не записывай их в артефакт.\n"
        f"Роль артефакта: {role}\n"
        f"Схема: {schema}"
    )


def _render_bulleted(lines: list[str], items: list[Any]) -> None:
    """Helper: добавить буллет-список с разнесением многострочных пунктов."""
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        # Если пункт многострочный — преобразуем переводы строк в
        # markdown-перенос (двойной пробел в конце строки), чтобы абзацы
        # внутри одного пункта рендерились нормально.
        text = text.replace("\n", "  \n  ")
        lines.append(f"- {text}")


def _render_user_scenarios(lines: list[str], scenarios: list[Any]) -> None:
    """Render user scenarios as readable blocks with a real numbered list.

    A scenario is an inherently structured object — an actor, a goal and an
    ordered list of steps. Modelling it as ``{actor, goal, steps[]}`` (instead
    of a flat string) lets us emit each step on its own line under a clear
    sub-heading, the same way ``interaction_view`` renders its flows. One
    consistent rule across the codebase: an ordered procedure is a list of
    steps, never inline-numbered prose.

    Resilience: a plain-string item (legacy artifact or an occasional flat
    answer from the model) still renders — as a single bullet — so older
    documents never break.
    """
    index = 0
    for raw in scenarios:
        if isinstance(raw, dict):
            index += 1
            actor = str(raw.get("actor") or "").strip()
            goal = str(raw.get("goal") or "").strip()
            steps = [str(step).strip() for step in (raw.get("steps") or []) if str(step).strip()]
            heading = f"### Сценарий {index}"
            if actor:
                heading += f". {actor}"
            lines.append(heading)
            lines.append("")
            if goal:
                lines.append(f"**Цель.** {goal}")
                lines.append("")
            for step_no, step in enumerate(steps, start=1):
                lines.append(f"{step_no}. {step}")
            if steps:
                lines.append("")
        else:
            text = str(raw).strip()
            if text:
                lines.append(f"- {text}")
                lines.append("")


def _intro_for(label: str, count: int) -> str:
    """Короткая вводная фраза перед буллетным списком, чтобы документ читался
    как текст с логикой, а не как набор отдельных списков. Цель — связность.
    """
    if count == 0:
        return ""
    if count == 1:
        return f"_Ниже фиксируется ключевой пункт по разделу «{label}»._"
    return f"_Ниже зафиксировано {count} пункт(ов) по разделу «{label}»._"


def _render_requirements_spec(payload: dict[str, Any]) -> str:
    """Рендерит ТЗ как связный документ, а не как набор плоских списков.

    Принципы:
    • Документ начинается с краткого резюме (Executive Summary) — главное
      сообщение читателю.
    • Каждый раздел имеет НАРРАТИВНУЮ ВСТАВКУ перед буллет-списком,
      чтобы читатель понимал, ЗАЧЕМ этот раздел и что в нём искать.
    • Парные блоки (что входит / что не входит) даются рядом с явным
      контрастом.
    • Риски — таблицей, а не списком: даёт сразу видимую структуру.
    • Доменные расширения (frontend / ml / security / integration) идут в
      собственных подразделах с горизонтальной чертой-разделителем.
    • Заголовки — без декоративных пиктограмм: навигацию даёт структура и
      оглавление, а не эмодзи (это согласуется с запретом эмодзи для модели).
    """
    lines: list[str] = []

    title = (payload.get("title") or "Техническое задание").strip()
    lines.append(f"# {title}")
    lines.append("")

    # ----- Введение / резюме --------------------------------------------------
    summary = (payload.get("executive_summary") or "").strip()
    if summary:
        # Резюме идёт «цитатой» — визуально выделяется в начале документа.
        lines.append("> " + summary.replace("\n", "\n> "))
        lines.append("")

    # Контекст и цель — единый верхний раздел: постановка проблемы, цель и
    # целевые результаты вместе, без дробления на три почти одинаковых блока.
    business_context = (payload.get("business_context") or "").strip()
    business_goal = (payload.get("business_goal") or "").strip()
    target_outcomes = payload.get("target_outcomes") or []
    if business_context or business_goal or target_outcomes:
        lines.append("## Контекст и цель")
        lines.append("")
        if business_context:
            lines.append(business_context)
            lines.append("")
        if business_goal:
            lines.append("### Бизнес-цель")
            lines.append("")
            lines.append(business_goal)
            lines.append("")
        if target_outcomes:
            lines.append("### Целевые результаты")
            lines.append("")
            lines.append(_intro_for("целевые результаты", len(target_outcomes)))
            lines.append("")
            _render_bulleted(lines, target_outcomes)
            lines.append("")

    # ----- Границы проекта ----------------------------------------------------
    scope_in = payload.get("scope_in") or []
    scope_out = payload.get("scope_out") or []
    if scope_in or scope_out:
        lines.append("---")
        lines.append("")
        lines.append("## Границы текущего этапа")
        lines.append("")
        lines.append(
            "Раздел фиксирует, какие результаты PoV команда обязуется поставить, "
            "и какие пункты сознательно вынесены за рамки этапа. Это защита от "
            "scope creep и явная точка для последующих обсуждений."
        )
        lines.append("")
        if scope_in:
            lines.append("**Входит в этап**")
            lines.append("")
            _render_bulleted(lines, scope_in)
            lines.append("")
        if scope_out:
            lines.append("**Не входит в этап (отнесено к следующим этапам)**")
            lines.append("")
            _render_bulleted(lines, scope_out)
            lines.append("")

    # ----- Стейкхолдеры -------------------------------------------------------
    stakeholders = payload.get("stakeholders") or payload.get("actors") or []
    if stakeholders:
        lines.append("## Стейкхолдеры и роли")
        lines.append("")
        lines.append(
            "Здесь перечислены ключевые роли, влияющие на проект: владелец, "
            "согласующие, ответственные за данные и инфраструктуру, конечные "
            "пользователи."
        )
        lines.append("")
        _render_bulleted(lines, stakeholders)
        lines.append("")

    operating_model = payload.get("operating_model") or []
    if operating_model:
        lines.append("### Операционная модель")
        lines.append("")
        _render_bulleted(lines, operating_model)
        lines.append("")

    # ----- Пользовательские сценарии -----------------------------------------
    user_stories = payload.get("user_stories") or []
    if user_stories:
        lines.append("---")
        lines.append("")
        lines.append("## Пользовательские сценарии")
        lines.append("")
        lines.append(
            "Сценарии описывают типовые пути использования решения "
            "конкретными ролями. Они служат основой для проектирования "
            "интерфейса и приёмки."
        )
        lines.append("")
        _render_user_scenarios(lines, user_stories)
        lines.append("")

    # ----- Требования ---------------------------------------------------------
    data_reqs = payload.get("data_requirements") or []
    func_reqs = payload.get("functional_requirements") or []
    non_func_reqs = payload.get("non_functional_requirements") or []
    integration_reqs = payload.get("integration_requirements") or []
    security_reqs = payload.get("security_requirements") or []
    deployment_reqs = payload.get("deployment_requirements") or []

    if any([data_reqs, func_reqs, non_func_reqs, integration_reqs, security_reqs, deployment_reqs]):
        lines.append("---")
        lines.append("")
        lines.append("## Требования к решению")
        lines.append("")
        lines.append(
            "Требования сгруппированы по типу — от функциональных до "
            "инфраструктурных. Каждый пункт должен быть проверяем на "
            "финальной приёмке."
        )
        lines.append("")

        if func_reqs:
            lines.append("### Функциональные требования")
            lines.append("")
            _render_bulleted(lines, func_reqs)
            lines.append("")

        if non_func_reqs:
            lines.append("### Нефункциональные требования")
            lines.append("")
            _render_bulleted(lines, non_func_reqs)
            lines.append("")

        if data_reqs:
            lines.append("### Требования к данным")
            lines.append("")
            _render_bulleted(lines, data_reqs)
            lines.append("")

        if integration_reqs:
            lines.append("### Интеграционные требования")
            lines.append("")
            _render_bulleted(lines, integration_reqs)
            lines.append("")

        if security_reqs:
            lines.append("### Требования ИБ и комплаенса")
            lines.append("")
            _render_bulleted(lines, security_reqs)
            lines.append("")

        if deployment_reqs:
            lines.append("### Требования к развёртыванию")
            lines.append("")
            _render_bulleted(lines, deployment_reqs)
            lines.append("")

    # ----- Доменные расширения ------------------------------------------------
    frontend = payload.get("frontend_requirements")
    if frontend:
        lines.append("---")
        lines.append("")
        lines.append("## Требования к интерфейсу")
        lines.append("")
        lines.append(
            "Этот блок описывает пользовательскую часть решения: кто будет "
            "работать с системой, какие сценарии он проходит, какие экраны и "
            "представления видит."
        )
        lines.append("")
        if frontend.get("user_roles"):
            lines.append("### Пользовательские роли")
            lines.append("")
            _render_bulleted(lines, frontend["user_roles"])
            lines.append("")
        if frontend.get("user_flows"):
            lines.append("### Пользовательские потоки")
            lines.append("")
            _render_bulleted(lines, frontend["user_flows"])
            lines.append("")
        if frontend.get("screens"):
            lines.append("### Экраны")
            lines.append("")
            _render_bulleted(lines, frontend["screens"])
            lines.append("")
        if frontend.get("analytics_views"):
            lines.append("### Аналитические представления")
            lines.append("")
            _render_bulleted(lines, frontend["analytics_views"])
            lines.append("")
        if frontend.get("decision_support_needs"):
            lines.append("### Поддержка принятия решений")
            lines.append("")
            _render_bulleted(lines, frontend["decision_support_needs"])
            lines.append("")
        if frontend.get("ux_constraints"):
            lines.append("### UX-ограничения и принципы")
            lines.append("")
            _render_bulleted(lines, frontend["ux_constraints"])
            lines.append("")

    ml_requirements = payload.get("ml_requirements")
    if ml_requirements:
        lines.append("---")
        lines.append("")
        lines.append("## ML-задача и данные")
        lines.append("")
        prediction_target = (ml_requirements.get("prediction_target") or "").strip()
        prediction_horizon = (ml_requirements.get("prediction_horizon") or "").strip()
        prediction_unit = (ml_requirements.get("prediction_unit") or "").strip()
        if prediction_target:
            lines.append(f"**Цель предсказания.** {prediction_target}")
            lines.append("")
        if prediction_horizon:
            lines.append(f"**Горизонт прогноза.** {prediction_horizon}")
            lines.append("")
        if prediction_unit:
            lines.append(f"**Единица предсказания.** {prediction_unit}")
            lines.append("")
        if ml_requirements.get("data_sources"):
            lines.append("### Источники данных")
            lines.append("")
            _render_bulleted(lines, ml_requirements["data_sources"])
            lines.append("")
        if ml_requirements.get("model_outputs"):
            lines.append("### Выходы модели")
            lines.append("")
            _render_bulleted(lines, ml_requirements["model_outputs"])
            lines.append("")
        if ml_requirements.get("evaluation_metrics"):
            lines.append("### Метрики качества")
            lines.append("")
            _render_bulleted(lines, ml_requirements["evaluation_metrics"])
            lines.append("")
        if ml_requirements.get("explainability_requirements"):
            lines.append("### Требования к интерпретируемости")
            lines.append("")
            _render_bulleted(lines, ml_requirements["explainability_requirements"])
            lines.append("")

    security_detail = payload.get("security_constraints_detail")
    if security_detail:
        lines.append("---")
        lines.append("")
        lines.append("## Детальные ограничения ИБ и комплаенса")
        lines.append("")
        lines.append(
            "Раздел раскрывает требования к контуру решения: где живут данные, "
            "как защищены, как контролируется доступ, что допустимо в работе с ИИ."
        )
        lines.append("")
        if security_detail.get("deployment_constraints"):
            lines.append("### Контур и развёртывание")
            lines.append("")
            _render_bulleted(lines, security_detail["deployment_constraints"])
            lines.append("")
        if security_detail.get("privacy_constraints"):
            lines.append("### Приватность данных")
            lines.append("")
            _render_bulleted(lines, security_detail["privacy_constraints"])
            lines.append("")
        if security_detail.get("access_control_constraints"):
            lines.append("### Контроль доступа")
            lines.append("")
            _render_bulleted(lines, security_detail["access_control_constraints"])
            lines.append("")
        if security_detail.get("allowed_ai_usage"):
            lines.append("### Допустимое использование ИИ")
            lines.append("")
            _render_bulleted(lines, security_detail["allowed_ai_usage"])
            lines.append("")
        if security_detail.get("mandatory_controls"):
            lines.append("### Обязательные меры контроля")
            lines.append("")
            _render_bulleted(lines, security_detail["mandatory_controls"])
            lines.append("")

    integration_model = payload.get("integration_model")
    if integration_model:
        lines.append("---")
        lines.append("")
        lines.append("## Интеграционная модель")
        lines.append("")
        lines.append(
            "Описание того, откуда поступают данные, в каком виде, как часто, "
            "куда уходит результат, и кто обеспечивает эксплуатацию связей."
        )
        lines.append("")
        if integration_model.get("source_systems"):
            lines.append("### Системы-источники")
            lines.append("")
            _render_bulleted(lines, integration_model["source_systems"])
            lines.append("")
        if integration_model.get("delivery_pattern"):
            lines.append("### Способ доставки данных")
            lines.append("")
            _render_bulleted(lines, integration_model["delivery_pattern"])
            lines.append("")
        refresh = (integration_model.get("refresh_model") or "").strip()
        if refresh:
            lines.append(f"**Модель обновления.** {refresh}")
            lines.append("")
        if integration_model.get("target_surfaces"):
            lines.append("### Точки потребления результата")
            lines.append("")
            _render_bulleted(lines, integration_model["target_surfaces"])
            lines.append("")
        if integration_model.get("operating_roles"):
            lines.append("### Операционные роли")
            lines.append("")
            _render_bulleted(lines, integration_model["operating_roles"])
            lines.append("")

    # ----- Топология развёртывания -------------------------------------------
    deployment_topology = payload.get("deployment_topology") or {}
    if deployment_topology:
        lines.append("---")
        lines.append("")
        lines.append("## Топология развёртывания")
        lines.append("")
        lines.append(
            "Раздел описывает, как и где физически разворачивается решение: "
            "среды, сетевые контуры, где живут ключевые компоненты, как "
            "выглядит цикл поставки и обновлений."
        )
        lines.append("")
        environments = deployment_topology.get("environments") or []
        if environments:
            lines.append("### Среды")
            lines.append("")
            lines.append("| Среда | Назначение | Масштаб |")
            lines.append("|---|---|---|")
            for env in environments:
                if not isinstance(env, dict):
                    continue
                name = (env.get("name") or "").strip() or "—"
                purpose = (env.get("purpose") or "").strip() or "—"
                scaling = (env.get("scaling") or "").strip() or "—"
                lines.append(f"| {name} | {purpose} | {scaling} |")
            lines.append("")
        network_zones = deployment_topology.get("network_zones") or []
        if network_zones:
            lines.append("### Сетевые контуры")
            lines.append("")
            _render_bulleted(lines, network_zones)
            lines.append("")
        components = deployment_topology.get("components") or []
        if components:
            lines.append("### Размещение компонентов")
            lines.append("")
            lines.append("| Компонент | Размещение | Технология | Назначение |")
            lines.append("|---|---|---|---|")
            for comp in components:
                if not isinstance(comp, dict):
                    continue
                name = (comp.get("name") or "").strip() or "—"
                placement = (comp.get("placement") or "").strip() or "—"
                technology = (comp.get("technology") or "").strip() or "—"
                resp = (comp.get("responsibilities") or "").strip() or "—"
                lines.append(f"| {name} | {placement} | {technology} | {resp} |")
            lines.append("")
        deployment_flow = (deployment_topology.get("deployment_flow") or "").strip()
        if deployment_flow:
            lines.append("### Цикл поставки и обновлений")
            lines.append("")
            lines.append(deployment_flow)
            lines.append("")

    # ----- DPIA / Оценка воздействия на персональные данные ------------------
    privacy_impact = payload.get("privacy_impact") or {}
    if privacy_impact:
        lines.append("---")
        lines.append("")
        lines.append("## Оценка воздействия на персональные данные (DPIA)")
        lines.append("")
        lines.append(
            "Формальный раздел для DPO/ИБ заказчика: какие категории ПДн "
            "обрабатываются, на каком основании, какими мерами защищены и "
            "какие остаточные риски остаются."
        )
        lines.append("")
        pii_categories = privacy_impact.get("pii_categories") or []
        if pii_categories:
            lines.append("**Категории ПДн.**")
            lines.append("")
            _render_bulleted(lines, pii_categories)
            lines.append("")
        else:
            lines.append(
                "В рамках PoV персональные данные не обрабатываются "
                "(используются обезличенные или синтетические данные)."
            )
            lines.append("")
        processing_purposes = privacy_impact.get("processing_purposes") or []
        if processing_purposes:
            lines.append("**Цели обработки.**")
            lines.append("")
            _render_bulleted(lines, processing_purposes)
            lines.append("")
        lawful_basis = (privacy_impact.get("lawful_basis") or "").strip()
        if lawful_basis:
            lines.append(f"**Правовое основание.** {lawful_basis}")
            lines.append("")
        data_min = privacy_impact.get("data_minimization") or []
        if data_min:
            lines.append("**Минимизация данных.**")
            lines.append("")
            _render_bulleted(lines, data_min)
            lines.append("")
        controls = privacy_impact.get("controls") or []
        if controls:
            lines.append("**Меры контроля.**")
            lines.append("")
            _render_bulleted(lines, controls)
            lines.append("")
        dsr = (privacy_impact.get("data_subject_rights") or "").strip()
        if dsr:
            lines.append(f"**Права субъектов ПДн.** {dsr}")
            lines.append("")
        cross_border = (privacy_impact.get("cross_border_transfers") or "").strip()
        if cross_border:
            lines.append(f"**Трансграничная передача.** {cross_border}")
            lines.append("")
        retention = (privacy_impact.get("retention_policy") or "").strip()
        if retention:
            lines.append(f"**Хранение и ретенция.** {retention}")
            lines.append("")
        residual = privacy_impact.get("residual_risks") or []
        if residual:
            lines.append("**Остаточные риски.**")
            lines.append("")
            _render_bulleted(lines, residual)
            lines.append("")

    # ----- Результаты и критерии приёмки -------------------------------------
    delivery_artifacts = payload.get("delivery_artifacts") or []
    acceptance = payload.get("acceptance_criteria") or []
    success = payload.get("success_criteria") or []
    if delivery_artifacts or acceptance or success:
        lines.append("---")
        lines.append("")
        lines.append("## Результаты и приёмка")
        lines.append("")
        lines.append(
            "Конкретные результаты этапа и измеримые критерии, по которым "
            "результат принимается заказчиком."
        )
        lines.append("")
        if delivery_artifacts:
            lines.append("### Поставляемые результаты")
            lines.append("")
            _render_bulleted(lines, delivery_artifacts)
            lines.append("")
        if success:
            lines.append("### Критерии успеха")
            lines.append("")
            _render_bulleted(lines, success)
            lines.append("")
        if acceptance:
            lines.append("### Критерии приёмки")
            lines.append("")
            _render_bulleted(lines, acceptance)
            lines.append("")

    # ----- План этапов --------------------------------------------------------
    phased = payload.get("phased_plan") or []
    if phased:
        lines.append("---")
        lines.append("")
        lines.append("## Этапы реализации")
        lines.append("")
        lines.append(
            "Крупная декомпозиция работы по фазам. Конкретные сроки и "
            "состав работ внутри фаз уточняются на этапе подготовки контракта."
        )
        lines.append("")
        _render_bulleted(lines, phased)
        lines.append("")

    # ----- Альтернативы, допущения, риски, открытые вопросы ------------------
    alternatives = payload.get("alternatives_considered") or []
    assumptions = payload.get("assumptions") or []
    risks = payload.get("risks") or []
    open_questions = payload.get("open_questions") or []

    if alternatives:
        lines.append("## Рассмотренные альтернативы")
        lines.append("")
        lines.append(
            "Альтернативные варианты архитектуры, между которыми сделан "
            "осознанный выбор. Зафиксированы здесь для прозрачности и для "
            "повторного обсуждения, если контекст изменится."
        )
        lines.append("")
        _render_bulleted(lines, alternatives)
        lines.append("")

    if assumptions:
        lines.append("## Допущения")
        lines.append("")
        lines.append(
            "Рабочие предположения, на которых строится решение. При "
            "несоответствии реальности — пересмотр соответствующих разделов."
        )
        lines.append("")
        _render_bulleted(lines, assumptions)
        lines.append("")

    risks_detail = payload.get("project_risks_detail") or []
    if risks_detail:
        lines.append("## Реестр рисков")
        lines.append("")
        lines.append(
            "Структурированный реестр известных рисков PoV с оценкой "
            "вероятности и влияния, митигацией, триггером эскалации и "
            "владельцем. Пополняется по ходу реализации."
        )
        lines.append("")
        lines.append("| # | Риск | Категория | Вероятность | Влияние | Митигация | Триггер | Владелец |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for idx, risk in enumerate(risks_detail, start=1):
            if not isinstance(risk, dict):
                continue
            title_risk = (risk.get("title") or "").strip().replace("|", "\\|") or "—"
            category = (risk.get("category") or "").strip().replace("|", "\\|") or "—"
            prob = (risk.get("probability") or "").strip() or "—"
            impact = (risk.get("impact") or "").strip() or "—"
            mitigation = (risk.get("mitigation") or "").strip().replace("|", "\\|") or "—"
            trigger = (risk.get("trigger") or "").strip().replace("|", "\\|") or "—"
            owner = (risk.get("owner") or "").strip().replace("|", "\\|") or "—"
            lines.append(
                f"| {idx} | {title_risk} | {category} | {prob} | {impact} | "
                f"{mitigation} | {trigger} | {owner} |"
            )
        lines.append("")
        # Дополнительно — короткие описания, чтобы таблица не оставалась без
        # контекста, если описание риска длиннее одной строки.
        descriptions = [
            (r.get("title", "").strip(), (r.get("description") or "").strip())
            for r in risks_detail
            if isinstance(r, dict) and (r.get("description") or "").strip()
        ]
        if descriptions:
            lines.append("**Расшифровки рисков.**")
            lines.append("")
            for title_risk, desc in descriptions:
                if not title_risk:
                    continue
                lines.append(f"- **{title_risk}.** {desc}")
            lines.append("")
    elif risks:
        lines.append("## Риски")
        lines.append("")
        lines.append(
            "Известные риски проекта с указанием митигации. Список не "
            "исчерпывающий и пополняется по ходу реализации."
        )
        lines.append("")
        _render_bulleted(lines, risks)
        lines.append("")

    if open_questions:
        lines.append("## Открытые вопросы")
        lines.append("")
        lines.append(
            "Вопросы, которые остаются на согласование с заказчиком. Их "
            "наличие — нормальная часть PoV-документации: они отражают "
            "осознанные точки уточнения, а не пробелы в проработке."
        )
        lines.append("")
        _render_bulleted(lines, open_questions)
        lines.append("")

    # ----- Глоссарий (приложение) --------------------------------------------
    glossary = payload.get("glossary") or []
    if glossary:
        lines.append("---")
        lines.append("")
        lines.append("## Глоссарий")
        lines.append("")
        lines.append(
            "Словарь ключевых терминов, ролей, систем и метрик, упомянутых "
            "в документе, — для общего понимания заказчиком и подрядчиком."
        )
        lines.append("")
        for entry in glossary:
            if not isinstance(entry, dict):
                continue
            term = (entry.get("term") or "").strip()
            definition = (entry.get("definition") or "").strip()
            if not term or not definition:
                continue
            category = (entry.get("category") or "").strip()
            synonyms = entry.get("synonyms") or []
            suffix = ""
            if synonyms:
                suffix = f" _(синонимы: {', '.join(str(s).strip() for s in synonyms if str(s).strip())})_"
            tag = f" _[{category}]_" if category else ""
            lines.append(f"- **{term}**{tag} — {definition}{suffix}")
        lines.append("")

    # Финальная пустая строка для аккуратности.
    if lines and lines[-1] != "":
        lines.append("")
    return "\n".join(lines)


def render_markdown(artifact_role: str, payload: dict[str, Any]) -> str:
    if artifact_role == "clarification_notes":
        sections = [
            "# Уточнение бизнес-цели",
            f"## Уточнённая цель\n{payload['clarified_goal']}",
            "## Критерии успеха\n" + "\n".join(f"- {item}" for item in payload["success_criteria"]),
            "## Допущения\n" + "\n".join(f"- {item}" for item in payload["assumptions"]),
            "## Открытые вопросы\n" + "\n".join(f"- {item}" for item in payload["open_questions"]),
        ]
        return "\n\n".join(sections)

    if artifact_role == "user_story_map":
        lines = ["# Карта user story", "## Роли"]
        lines.extend(f"- {actor['name']}: {', '.join(actor['needs'])}" for actor in payload["actors"])
        lines.append("\n## User story")
        lines.extend(
            f"- Как {item['actor']}, я хочу {item['story']}, чтобы {item['value']}"
            for item in payload["user_stories"]
        )
        lines.append("\n## Граничные случаи")
        lines.extend(f"- {item}" for item in payload["edge_cases"])
        return "\n".join(lines)

    if artifact_role == "alternatives_analysis":
        lines = [
            "# Анализ альтернатив",
            f"## Рекомендованный вариант\n{payload['recommended_option']}",
            "## Обоснование",
            payload["rationale"],
            "## Варианты",
        ]
        for item in payload["alternatives"]:
            lines.append(f"### {item['name']}")
            lines.append(item["description"])
            lines.append("Плюсы:")
            lines.extend(f"- {entry}" for entry in item["pros"])
            lines.append("Минусы:")
            lines.extend(f"- {entry}" for entry in item["cons"])
        return "\n".join(lines)

    if artifact_role == "normalized_request":
        return "\n".join(
            [
                "# Разбор запроса",
                f"## Краткое резюме\n{payload['request_summary']}",
                f"## Бизнес-проблема\n{payload['business_problem']}",
                "## Запрошенные элементы решения",
                *[f"- {item}" for item in payload["requested_solution_elements"]],
                "\n## Факты из запроса",
                *[f"- {item}" for item in payload["explicit_facts"]],
                "\n## Явные ограничения",
                *[f"- {item}" for item in payload["explicit_constraints"]],
                "\n## Неявные риски",
                *[f"- {item}" for item in payload["implicit_risks"]],
                "\n## Неоднозначности и пробелы",
                *[f"- {item}" for item in payload["ambiguous_points"]],
                "\n## Рабочие допущения",
                *[f"- {item}" for item in payload["safe_assumptions"]],
            ]
        )

    if artifact_role == "goal_hypothesis":
        return "\n".join(
            [
                "# Гипотеза цели проекта",
                f"## Рабочая формулировка цели\n{payload['hypothesized_goal']}",
                "\n## Ожидаемые эффекты",
                *[f"- {item}" for item in payload["expected_effects"]],
                f"\n## Гипотеза о стадии проекта\n{payload['project_stage_hypothesis']}",
                "\n## Сигналы успеха",
                *[f"- {item}" for item in payload["success_signals"]],
                "\n## Непрояснённые части цели",
                *[f"- {item}" for item in payload["unresolved_goal_points"]],
            ]
        )

    if artifact_role == "constraint_inventory":
        return "\n".join(
            [
                "# Инвентаризация ограничений",
                "## Явные ограничения",
                *[f"- {item}" for item in payload["explicit_constraints"]],
                "\n## Подразумеваемые ограничения",
                *[f"- {item}" for item in payload["inferred_constraints"]],
                "\n## Ограничения текущего этапа",
                *[f"- {item}" for item in payload["stage_constraints"]],
                "\n## Ограничения среды и контура",
                *[f"- {item}" for item in payload["environment_constraints"]],
                "\n## Зависимости и внешние условия",
                *[f"- {item}" for item in payload["dependency_constraints"]],
            ]
        )

    if artifact_role == "business_outcome_model":
        return "\n".join(
            [
                "# Модель бизнес-результата",
                f"## Основная цель\n{payload['primary_business_goal']}",
                "## KPI",
                *[f"- {item}" for item in payload["target_kpis"]],
                "\n## Метрики успеха",
                *[f"- {item}" for item in payload["success_metrics"]],
                "\n## Влияние на процессы",
                *[f"- {item}" for item in payload["business_process_impacts"]],
                "\n## Какие решения должен поддержать результат",
                *[f"- {item}" for item in payload["expected_decisions"]],
                "\n## Гипотезы ценности",
                *[f"- {item}" for item in payload["value_hypotheses"]],
                "\n## Допущения",
                *[f"- {item}" for item in payload["assumptions"]],
            ]
        )

    if artifact_role == "scope_boundary_matrix":
        return "\n".join(
            [
                "# Границы и рамка этапа",
                "## Входит в текущий этап",
                *[f"- {item}" for item in payload["in_scope"]],
                "\n## Не входит в текущий этап",
                *[f"- {item}" for item in payload["out_of_scope"]],
                "\n## Границы пилота",
                *[f"- {item}" for item in payload["pilot_boundaries"]],
                "\n## Кандидаты на следующие фазы",
                *[f"- {item}" for item in payload["future_phase_candidates"]],
                "\n## Обязательные результаты этапа",
                *[f"- {item}" for item in payload["mandatory_deliverables"]],
                "\n## Исключённые результаты этапа",
                *[f"- {item}" for item in payload["excluded_deliverables"]],
            ]
        )

    if artifact_role == "stakeholder_map":
        lines = ["# Карта стейкхолдеров", "## Группы стейкхолдеров"]
        for item in payload["stakeholder_groups"]:
            lines.append(f"### {item['name']}")
            lines.append(f"Роль: {item['role']}")
            lines.append(f"Влияние: {item['influence']}")
            lines.append("Ожидания:")
            lines.extend(f"- {entry}" for entry in item["expectations"])
        lines.extend(
            [
                "\n## Основные пользователи",
                *[f"- {item}" for item in payload["primary_users"]],
                "\n## Владельцы данных",
                *[f"- {item}" for item in payload["data_owners"]],
                "\n## Поддерживающие команды",
                *[f"- {item}" for item in payload["support_teams"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "decision_ownership_matrix":
        lines = ["# Владельцы решений и согласований", "## Решения"]
        for item in payload["decisions"]:
            lines.append(f"### {item['name']}")
            lines.append(f"Владелец: {item['owner']}")
            lines.append("Участники:")
            lines.extend(f"- {entry}" for entry in item["participants"])
            lines.append(f"Когда принимается: {item['timing']}")
        lines.extend(
            [
                "\n## Решения без владельца",
                *[f"- {item}" for item in payload["unowned_decisions"]],
                "\n## Точки согласования",
                *[f"- {item}" for item in payload["approval_points"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "operating_model_outline":
        return "\n".join(
            [
                "# Черновой контур операционной модели",
                "## Ход процесса",
                *[f"- {item}" for item in payload["process_flow"]],
                "\n## Роли-поставщики",
                *[f"- {item}" for item in payload["producer_roles"]],
                "\n## Роли-получатели результата",
                *[f"- {item}" for item in payload["consumer_roles"]],
                "\n## Роли поддержки",
                *[f"- {item}" for item in payload["support_roles"]],
                "\n## Риски передачи ответственности",
                *[f"- {item}" for item in payload["handoff_risks"]],
            ]
        )

    if artifact_role == "stakeholder_operating_model":
        lines = ["# Стейкхолдеры и операционная модель", "## Группы стейкхолдеров"]
        for item in payload["stakeholder_groups"]:
            lines.append(f"### {item['name']}")
            lines.append(f"Роль: {item['role']}")
            lines.append("Ожидания:")
            lines.extend(f"- {entry}" for entry in item["expectations"])
            lines.append("Ответственность:")
            lines.extend(f"- {entry}" for entry in item["responsibilities"])
        lines.extend(
            [
                "\n## Основные пользователи",
                *[f"- {item}" for item in payload["primary_users"]],
                "\n## Владельцы решений",
                *[f"- {item}" for item in payload["decision_owners"]],
                "\n## Операционная модель",
                *[f"- {item}" for item in payload["operating_model"]],
                "\n## Ограничения внедрения",
                *[f"- {item}" for item in payload["adoption_constraints"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "solution_option_inventory":
        lines = ["# Набор вариантов решения", "## Варианты"]
        for item in payload["options"]:
            lines.append(f"### {item['name']}")
            lines.append(item["summary"])
            lines.append(f"Соответствие рамке этапа: {item['boundary_fit']}")
            lines.append("Условия применимости:")
            lines.extend(f"- {entry}" for entry in item["enabling_conditions"])
        lines.extend(
            [
                "\n## Оси сравнения",
                *[f"- {item}" for item in payload["comparison_axes"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "solution_tradeoff_matrix":
        lines = ["# Матрица вариантов решения", "## Варианты"]
        for item in payload["options"]:
            lines.append(f"### {item['name']}")
            lines.append(item["summary"])
            lines.append(f"Пригодность для этапа: {item['fit_for_pilot']}")
            lines.append("Плюсы:")
            lines.extend(f"- {entry}" for entry in item["pros"])
            lines.append("Минусы:")
            lines.extend(f"- {entry}" for entry in item["cons"])
            lines.append("Риски:")
            lines.extend(f"- {entry}" for entry in item["risks"])
        lines.extend(
            [
                f"\n## Рекомендуемый вариант\n{payload['recommended_option']}",
                "\n## Обоснование",
                payload["recommendation_rationale"],
                "\n## Отложенные решения",
                *[f"- {item}" for item in payload["deferred_decisions"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "delivery_scope_definition":
        return "\n".join(
            [
                "# Состав поставки этапа",
                "## Что должно быть поставлено",
                *[f"- {item}" for item in payload["delivery_items"]],
                "\n## Что не обещается на этом этапе",
                *[f"- {item}" for item in payload["excluded_items"]],
                "\n## Ожидания к демонстрации результата",
                *[f"- {item}" for item in payload["demo_expectations"]],
                "\n## Артефакты-доказательства",
                *[f"- {item}" for item in payload["evidence_artifacts"]],
            ]
        )

    if artifact_role == "acceptance_model_definition":
        return "\n".join(
            [
                "# Модель приемки этапа",
                "## Критерии приемки",
                *[f"- {item}" for item in payload["acceptance_criteria"]],
                "\n## Подтверждающие доказательства",
                *[f"- {item}" for item in payload["success_evidence"]],
                "\n## Входы от заказчика",
                *[f"- {item}" for item in payload["required_customer_inputs"]],
                "\n## Формальные согласования",
                *[f"- {item}" for item in payload["formal_approvals"]],
                "\n## Основания для отклонения результата",
                *[f"- {item}" for item in payload["rejection_conditions"]],
            ]
        )

    if artifact_role == "delivery_acceptance_plan":
        return "\n".join(
            [
                "# Модель поставки и приемки",
                "## Результаты этапа",
                *[f"- {item}" for item in payload["delivery_items"]],
                "\n## Критерии приемки",
                *[f"- {item}" for item in payload["acceptance_criteria"]],
                "\n## Подтверждающие доказательства",
                *[f"- {item}" for item in payload["success_evidence"]],
                "\n## Входы от заказчика",
                *[f"- {item}" for item in payload["required_customer_inputs"]],
                "\n## Формальные согласования",
                *[f"- {item}" for item in payload["formal_approvals"]],
                "\n## Открытые зависимости",
                *[f"- {item}" for item in payload["open_dependencies"]],
            ]
        )

    if artifact_role == "dependency_map":
        return "\n".join(
            [
                "# Критические зависимости и входы",
                "## Критические зависимости",
                *[f"- {item}" for item in payload["critical_dependencies"]],
                "\n## Входы от заказчика",
                *[f"- {item}" for item in payload["customer_inputs"]],
                "\n## Внешние решения",
                *[f"- {item}" for item in payload["external_decisions"]],
                "\n## Доступы и разрешения",
                *[f"- {item}" for item in payload["access_dependencies"]],
                "\n## Условия остановки этапа",
                *[f"- {item}" for item in payload["stop_conditions"]],
            ]
        )

    if artifact_role == "implementation_dependency_plan":
        lines = ["# План реализации и зависимости", "## Фазы"]
        for phase in payload["phases"]:
            lines.append(f"### {phase['name']}")
            lines.append("Цели:")
            lines.extend(f"- {item}" for item in phase["objectives"])
            lines.append("Зависимости:")
            lines.extend(f"- {item}" for item in phase["dependencies"])
            lines.append("Выходы:")
            lines.extend(f"- {item}" for item in phase["outputs"])
        lines.extend(
            [
                "\n## Критические зависимости",
                *[f"- {item}" for item in payload["critical_dependencies"]],
                "\n## Риски проекта",
                *[f"- {item}" for item in payload["project_risks"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "predictive_problem_definition":
        return "\n".join(
            [
                "# Определение предиктивной задачи",
                f"## Цель предсказания\n{payload['prediction_target']}",
                f"## Горизонт прогноза\n{payload['prediction_horizon']}",
                f"## Единица предсказания\n{payload['prediction_unit']}",
                f"## Определение события\n{payload['label_definition']}",
                "\n## Бизнес-действия по результату",
                *[f"- {item}" for item in payload["business_actions"]],
                "\n## Выходы модели",
                *[f"- {item}" for item in payload["model_outputs"]],
                "\n## Метрики оценки",
                *[f"- {item}" for item in payload["evaluation_metrics"]],
                "\n## Базовые ожидания",
                *[f"- {item}" for item in payload["baseline_expectations"]],
                "\n## Требования к интерпретируемости",
                *[f"- {item}" for item in payload["explainability_requirements"]],
            ]
        )

    if artifact_role == "data_landscape_assessment":
        return "\n".join(
            [
                "# Оценка данных и реализуемости",
                "## Источники",
                *[f"- {item}" for item in payload["source_systems"]],
                "\n## Сущности",
                *[f"- {item}" for item in payload["required_entities"]],
                "\n## Ключевые признаки",
                *[f"- {item}" for item in payload["key_features"]],
                "\n## Риски качества данных",
                *[f"- {item}" for item in payload["data_quality_risks"]],
                "\n## Пробелы в данных",
                *[f"- {item}" for item in payload["data_gaps"]],
                f"\n## Оценка реализуемости\n{payload['feasibility_assessment']}",
                "\n## Замечания по приватности данных",
                *[f"- {item}" for item in payload["privacy_notes"]],
            ]
        )

    if artifact_role == "security_compliance_constraints":
        return "\n".join(
            [
                "# Ограничения ИБ и комплаенса",
                "## Ограничения развертывания",
                *[f"- {item}" for item in payload["deployment_constraints"]],
                "\n## Ограничения по приватности данных",
                *[f"- {item}" for item in payload["privacy_constraints"]],
                "\n## Ограничения контроля доступа",
                *[f"- {item}" for item in payload["access_control_constraints"]],
                "\n## Ограничения безопасности интеграций",
                *[f"- {item}" for item in payload["integration_security_constraints"]],
                "\n## Допустимое использование ИИ",
                *[f"- {item}" for item in payload["allowed_ai_usage"]],
                "\n## Обязательные меры контроля",
                *[f"- {item}" for item in payload["mandatory_controls"]],
                "\n## Комплаенс-риски",
                *[f"- {item}" for item in payload["compliance_risks"]],
            ]
        )

    if artifact_role == "integration_operating_model":
        return "\n".join(
            [
                "# Интеграционная и операционная модель",
                "## Источники и входящие интеграции",
                *[f"- {item}" for item in payload["source_integrations"]],
                "\n## Целевые точки потребления",
                *[f"- {item}" for item in payload["target_integrations"]],
                f"\n## Модель обновления\n{payload['refresh_model']}",
                "\n## Способ доставки данных",
                *[f"- {item}" for item in payload["data_delivery_pattern"]],
                "\n## Операционные роли",
                *[f"- {item}" for item in payload["operating_roles"]],
                "\n## Модель поддержки",
                *[f"- {item}" for item in payload["support_model"]],
                "\n## Риски зависимостей",
                *[f"- {item}" for item in payload["dependency_risks"]],
            ]
        )

    if artifact_role == "ui_requirements_outline":
        lines = ["# Контур UI/BI-требований", "## Роли пользователей"]
        lines.extend(f"- {item}" for item in payload["user_roles"])
        lines.append("\n## Пользовательские потоки")
        for flow in payload["user_flows"]:
            lines.append(f"### {flow['name']}")
            lines.extend(f"- {step}" for step in flow["steps"])
        lines.append("\n## Экраны")
        for screen in payload["screens"]:
            lines.append(f"- {screen['name']}: {screen['purpose']}")
        if payload.get("analytics_views"):
            lines.append("\n## Аналитические представления")
            lines.extend(f"- {item}" for item in payload["analytics_views"])
        if payload.get("decision_support_needs"):
            lines.append("\n## Сценарии поддержки решений")
            lines.extend(f"- {item}" for item in payload["decision_support_needs"])
        lines.extend(
            [
                "\n## UX-ограничения",
                *[f"- {item}" for item in payload["ux_constraints"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "requirements_spec":
        return _render_requirements_spec(payload)

    if artifact_role == "glossary_terms":
        entries = payload.get("entries") or []
        lines = ["# Глоссарий"]
        if payload.get("domain_scope"):
            lines.append(payload["domain_scope"])
            lines.append("")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            term = str(entry.get("term") or "").strip()
            definition = str(entry.get("definition") or "").strip()
            if not term:
                continue
            line = f"**{term}** — {definition}"
            if entry.get("category"):
                line += f" _({entry['category']})_"
            lines.append(line)
            syns = entry.get("synonyms") or []
            if syns:
                lines.append(f"  Синонимы: {', '.join(syns)}")
        return "\n\n".join(lines)

    if artifact_role == "project_risk_register":
        risks = payload.get("risks") or []
        lines = ["# Реестр рисков проекта"]
        if payload.get("summary"):
            lines.append(payload["summary"])
        lines.append("\n| # | Риск | Категория | Вероятность | Влияние | Митигация |")
        lines.append("|---|------|-----------|-------------|---------|-----------|")
        for i, r in enumerate(risks, 1):
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "—")
            cat = str(r.get("category") or "—")
            prob = str(r.get("probability") or "—")
            imp = str(r.get("impact") or "—")
            mit = str(r.get("mitigation") or "—").replace("\n", " ")
            lines.append(f"| {i} | {title} | {cat} | {prob} | {imp} | {mit} |")
        # Детали рисков с описанием/триггером/владельцем
        details = []
        for i, r in enumerate(risks, 1):
            if not isinstance(r, dict):
                continue
            description = (r.get("description") or "").strip()
            trigger = (r.get("trigger") or "").strip()
            owner = (r.get("owner") or "").strip()
            if any([description, trigger, owner]):
                block = [f"### Риск {i}: {r.get('title', '—')}"]
                if description:
                    block.append(description)
                if trigger:
                    block.append(f"**Триггер останова:** {trigger}")
                if owner:
                    block.append(f"**Ответственный:** {owner}")
                details.append("\n".join(block))
        if details:
            lines.append("\n## Детали критичных рисков\n")
            lines.append("\n\n".join(details))
        return "\n".join(lines)

    if artifact_role == "deployment_topology":
        lines = ["# Топология развёртывания"]
        envs = payload.get("environments") or []
        if envs:
            lines.append("\n## Среды")
            for env in envs:
                if not isinstance(env, dict):
                    continue
                name = env.get("name", "—")
                purpose = env.get("purpose", "")
                scaling = env.get("scaling", "")
                line = f"**{name}** — {purpose}"
                if scaling:
                    line += f" _(масштабирование: {scaling})_"
                lines.append(line)
        zones = payload.get("network_zones") or []
        if zones:
            lines.append("\n## Сетевые зоны")
            lines.extend(f"- {z}" for z in zones)
        components = payload.get("components") or []
        if components:
            lines.append("\n## Размещение компонентов")
            lines.append("\n| Компонент | Среда / зона | Технология | Зона ответственности |")
            lines.append("|-----------|--------------|------------|-----------------------|")
            for c in components:
                if not isinstance(c, dict):
                    continue
                name = c.get("name", "—")
                placement = c.get("placement", "—")
                tech = c.get("technology", "—")
                resp = c.get("responsibilities", "—")
                lines.append(f"| {name} | {placement} | {tech} | {resp} |")
        if payload.get("deployment_flow"):
            lines.append("\n## Процесс развёртывания")
            lines.append(payload["deployment_flow"])
        return "\n".join(lines)

    if artifact_role == "feasibility_assessment":
        caps = payload.get("capabilities") or []
        verdict_label = {
            "feasible": "реализуемо",
            "conditional": "при условии",
            "uncertain": "под вопросом",
            "infeasible": "не реализуемо",
        }
        lines = ["# Оценка реализуемости"]
        overall_label = {
            "feasible": "всё реализуемо",
            "mixed": "частично — есть условные / неопределённые части",
            "blocked": "есть нереализуемые части",
        }
        if payload.get("overall_feasibility"):
            ov = str(payload["overall_feasibility"])
            lines.append(f"**Сводный вердикт:** {overall_label.get(ov, ov)}")
        if payload.get("summary"):
            lines.append(payload["summary"])
        if caps:
            lines.append("\n| # | Часть проекта | Вердикт | Покрытие | Блокеры | Предпосылки |")
            lines.append("|---|---------------|---------|----------|---------|-------------|")
            for i, c in enumerate(caps, 1):
                if not isinstance(c, dict):
                    continue
                name = str(c.get("name") or "—")
                fv = str(c.get("feasibility") or "")
                verdict = verdict_label.get(fv, fv or "—")
                cov = c.get("covered_by")
                coverage = f"{cov} / {c.get('matched_capability') or '—'}" if cov else "—"
                blockers = "; ".join(c.get("blockers") or []).replace("\n", " ") or "—"
                prereq = "; ".join(c.get("prerequisites") or []).replace("\n", " ") or "—"
                lines.append(f"| {i} | {name} | {verdict} | {coverage} | {blockers} | {prereq} |")
        problem = [
            c
            for c in caps
            if isinstance(c, dict)
            and str(c.get("feasibility")) in {"conditional", "uncertain", "infeasible"}
        ]
        if problem:
            lines.append("\n## Не реализуемо / под вопросом\n")
            blocks = []
            for c in problem:
                fv = str(c.get("feasibility") or "")
                head = f"### {verdict_label.get(fv, fv)}: {c.get('name', '—')}"
                block = [head]
                if c.get("rationale"):
                    block.append(str(c["rationale"]))
                if c.get("covered_by"):
                    block.append(f"**Покрытие:** {c['covered_by']} ({c.get('matched_capability') or '—'})")
                if c.get("blockers"):
                    block.append("**Блокеры:** " + "; ".join(c["blockers"]))
                if c.get("prerequisites"):
                    block.append("**Нужно для реализации:** " + "; ".join(c["prerequisites"]))
                blocks.append("\n".join(block))
            lines.append("\n\n".join(blocks))
        return "\n".join(lines)

    if artifact_role == "component_build_spec":
        return _render_component_build_spec(payload)

    if artifact_role.endswith("_build_spec"):
        comps = payload.get("components") or []
        lines = [f"# Спека сборки ({artifact_role.removesuffix('_build_spec')})"]
        if payload.get("summary"):
            lines.append(payload["summary"])
        assigned = payload.get("assigned_parts") or []
        if assigned:
            lines.append("\n**Назначенные части:** " + "; ".join(assigned))
        for comp in comps:
            if not isinstance(comp, dict):
                continue
            lines.append(f"\n## {comp.get('name', '—')}")
            if comp.get("purpose"):
                lines.append(comp["purpose"])
            for label, key in (
                ("Технологии", "tech"),
                ("Интерфейсы", "interfaces"),
                ("Данные", "data"),
                ("Зависимости", "dependencies"),
            ):
                vals = comp.get(key) or []
                if vals:
                    lines.append(f"- **{label}:** " + "; ".join(vals))
            if comp.get("test_approach"):
                lines.append(f"- **Тесты:** {comp['test_approach']}")
        oos = payload.get("out_of_scope") or []
        if oos:
            lines.append("\n## Вне зоны ответственности\n")
            lines.extend(f"- {item}" for item in oos)
        oq = payload.get("open_questions") or []
        if oq:
            lines.append("\n## Открытые вопросы\n")
            lines.extend(f"- {item}" for item in oq)
        return "\n".join(lines)

    if artifact_role == "build_plan":
        lines = [f"# {payload.get('title') or 'План реализации'}"]
        if payload.get("executive_summary"):
            lines.append(payload["executive_summary"])
        routing = payload.get("routing") or []
        if routing:
            lines.append("\n## Маршрутизация: часть → агент")
            lines.append("\n| Часть | Агент | Способность | Статус |")
            lines.append("|-------|-------|-------------|--------|")
            for route in routing:
                if not isinstance(route, dict):
                    continue
                lines.append(
                    f"| {route.get('part', '—')} | {route.get('agent', '—')} | "
                    f"{route.get('capability', '—')} | {route.get('status', '—')} |"
                )
        seq = payload.get("sequencing") or []
        if seq:
            lines.append("\n## Очерёдность работ")
            for phase in seq:
                if not isinstance(phase, dict):
                    continue
                lines.append(f"\n### {phase.get('phase', '—')}")
                lines.extend(f"- {item}" for item in (phase.get("items") or []))
        risks = payload.get("risks_and_gaps") or []
        if risks:
            lines.append("\n## Риски и пробелы\n")
            lines.extend(f"- {item}" for item in risks)
        if payload.get("summary"):
            lines.append("\n" + payload["summary"])
        return "\n".join(lines)

    if artifact_role == "privacy_impact_assessment":
        lines = ["# Оценка воздействия на персональные данные (DPIA)"]
        sections = [
            ("Категории ПДн", payload.get("pii_categories")),
            ("Цели обработки", payload.get("processing_purposes")),
            ("Минимизация данных", payload.get("data_minimization")),
            ("Меры контроля", payload.get("controls")),
            ("Права субъектов ПДн", payload.get("data_subject_rights")),
            ("Остаточные риски", payload.get("residual_risks")),
        ]
        if payload.get("lawful_basis"):
            lines.append(f"\n**Правовое основание обработки:** {payload['lawful_basis']}")
        for title, items in sections:
            if items:
                lines.append(f"\n## {title}")
                lines.extend(f"- {item}" for item in items)
        if payload.get("cross_border_transfers"):
            lines.append("\n## Трансграничная передача данных")
            lines.append(payload["cross_border_transfers"])
        if payload.get("retention_policy"):
            lines.append("\n## Хранение данных")
            lines.append(payload["retention_policy"])
        return "\n".join(lines)

    if artifact_role == "system_context_definition":
        return _render_system_context_definition(payload)

    if artifact_role == "component_decomposition":
        return _render_component_decomposition(payload)

    if artifact_role == "component_model":
        return _render_component_model(payload)

    if artifact_role == "deployment_map":
        return _render_deployment_map(payload)

    if artifact_role == "interaction_view":
        return _render_interaction_view(payload)

    if artifact_role == "design_document":
        return _render_design_document(payload)

    if artifact_role == "problem_statement":
        jtbd = payload.get("job_to_be_done") or {}
        lines = ["# Постановка проблемы", "", payload.get("problem", "")]
        if payload.get("affected"):
            lines += ["", "## Кого затрагивает", *[f"- {x}" for x in payload["affected"]]]
        if jtbd:
            lines += [
                "", "## Работа пользователя (JTBD)",
                f"- Когда: {jtbd.get('when', '—')}",
                f"- Хочет: {jtbd.get('want', '—')}",
                f"- Чтобы: {jtbd.get('so_that', '—')}",
            ]
        if payload.get("desired_outcome"):
            lines += ["", "## Нужный исход", *[f"- {x}" for x in payload["desired_outcome"]]]
        if payload.get("definition_of_solved"):
            lines += [
                "", "## Когда считаем проблему решённой",
                *[f"- {x}" for x in payload["definition_of_solved"]],
            ]
        if payload.get("assumptions"):
            lines += ["", "## Допущения", *[f"- {x}" for x in payload["assumptions"]]]
        return "\n".join(lines)

    if artifact_role == "integration_data_map":
        lines = ["# Интеграции и данные"]
        systems = payload.get("external_systems") or []
        if systems:
            lines.append("\n## Внешние системы")
            for s in systems:
                if not isinstance(s, dict):
                    continue
                mode = f" — {s['integration_mode']}" if s.get("integration_mode") else ""
                lines.append(f"- **{s.get('name', '—')}**: {s.get('purpose', '')}{mode}")
        sources = payload.get("data_sources") or []
        if sources:
            lines.append("\n## Источники данных")
            for d in sources:
                if not isinstance(d, dict):
                    continue
                owner = f" _(владелец: {d['owner']})_" if d.get("owner") else ""
                lines.append(f"- **{d.get('name', '—')}**: {d.get('description', '')}{owner}")
        if payload.get("data_flows"):
            lines += ["\n## Потоки данных", *[f"- {x}" for x in payload["data_flows"]]]
        if payload.get("integration_constraints"):
            lines += ["\n## Ограничения интеграции", *[f"- {x}" for x in payload["integration_constraints"]]]
        if payload.get("open_questions"):
            lines += ["\n## Открытые вопросы", *[f"- {x}" for x in payload["open_questions"]]]
        return "\n".join(lines)

    # Generic fallback for demo / unstructured artifacts
    import json as _json
    return f"# {artifact_role}\n\n```json\n{_json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n"


def _render_design_document(payload: dict[str, Any]) -> str:
    title = payload.get("title", "Архитектурный документ")
    lines = [f"# {title}"]
    summary = payload.get("executive_summary")
    if summary:
        lines.append("\n## Краткое резюме")
        lines.append(summary)

    sc = payload.get("system_context") or {}
    if sc:
        lines.append("\n## Системный контекст")
        if sc.get("system_purpose"):
            lines.append(sc["system_purpose"])
        actors = sc.get("actors") or []
        if actors:
            lines.append("\n**Акторы:**")
            for actor in actors:
                if not isinstance(actor, dict):
                    continue
                kind = actor.get("kind", "—")
                description = actor.get("description")
                line = f"- **{actor.get('name', '—')}** _({kind})_"
                if description:
                    line += f" — {description}"
                lines.append(line)
        ext = sc.get("external_systems") or []
        if ext:
            lines.append("\n**Внешние системы:**")
            for system in ext:
                if not isinstance(system, dict):
                    continue
                lines.append(f"- **{system.get('name', '—')}** — {system.get('role', '—')}")
                for interaction in system.get("interactions") or []:
                    lines.append(f"  - {interaction}")
        lines.extend(
            _render_mermaid_block(
                _build_flowchart(sc.get("context_diagram")), "Контекстная диаграмма"
            )
        )

    comp = payload.get("components") or {}
    if comp:
        if _is_component_model_shape(comp):
            # Новая модель компонентов (Ф4): переиспользуем её рендер; убираем
            # ведущий H1, чтобы секции (## Компоненты / ## Покрытие) встали в
            # документ как обычные разделы.
            body = _render_component_model(comp)
            lines.append(body.split("\n", 1)[1] if body.startswith("# ") else body)
        else:
            # Старая форма component_decomposition (закреплённые проекты).
            lines.append("\n## Компоненты")
            if comp.get("summary"):
                lines.append(comp["summary"])
            items = comp.get("components") or comp.get("items") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                lines.append(f"\n### {item.get('name', '—')}")
                if item.get("responsibilities"):
                    lines.append(item["responsibilities"])
                owns = item.get("owns_data") or []
                if owns:
                    lines.append("**Владеет данными:**")
                    lines.extend(f"- {entry}" for entry in owns)
                deps = item.get("dependencies") or []
                if deps:
                    lines.append("**Зависимости:**")
                    lines.extend(f"- {entry}" for entry in deps)
            lines.extend(
                _render_mermaid_block(
                    _build_flowchart(comp.get("component_diagram")), "Диаграмма компонентов"
                )
            )

    interactions = payload.get("interactions") or {}
    if interactions:
        lines.append("\n## Потоки взаимодействия")
        if interactions.get("summary"):
            lines.append(interactions["summary"])
        flows = interactions.get("flows") or []
        for flow in flows:
            if not isinstance(flow, dict):
                continue
            lines.append(f"\n### {flow.get('name', '—')}")
            if flow.get("trigger"):
                lines.append(f"**Триггер:** {flow['trigger']}")
            participants = flow.get("participants") or []
            if participants:
                lines.append(f"**Участники:** {', '.join(participants)}")
            steps = flow.get("steps") or []
            if steps:
                lines.append("**Шаги:**")
                for i, step in enumerate(steps, 1):
                    lines.append(f"{i}. {step}")
        interaction_diagram = interactions.get("interaction_diagram") or {}
        kind = interaction_diagram.get("kind", "sequence")
        heading = "Sequence-диаграмма" if kind == "sequence" else "Диаграмма потока"
        lines.extend(
            _render_mermaid_block(_build_interaction_diagram(interaction_diagram), heading)
        )

    deployment = payload.get("deployment") or {}
    if deployment:
        if "deployment_units" in deployment:
            # Новая карта развёртывания (Ф4): переиспользуем её рендер.
            body = _render_deployment_map(deployment)
            lines.append(body.split("\n", 1)[1] if body.startswith("# ") else body)
        else:
            # Старая форма deployment_topology (закреплённые проекты).
            lines.append("\n## Развёртывание")
            envs = deployment.get("environments") or []
            if envs:
                lines.append("\n**Среды:**")
                for env in envs:
                    if not isinstance(env, dict):
                        continue
                    name = env.get("name", "—")
                    purpose = env.get("purpose", "")
                    lines.append(f"- **{name}** — {purpose}")
            components_dep = deployment.get("components") or []
            if components_dep:
                lines.append("\n| Компонент | Размещение | Технология | Зона ответственности |")
                lines.append("|-----------|------------|------------|----------------------|")
                for c in components_dep:
                    if not isinstance(c, dict):
                        continue
                    lines.append(
                        f"| {c.get('name', '—')} | {c.get('placement', '—')} "
                        f"| {c.get('technology', '—')} | {c.get('responsibilities', '—')} |"
                    )
            if deployment.get("deployment_flow"):
                lines.append("\n**Процесс развёртывания:**")
                lines.append(deployment["deployment_flow"])

    risks = payload.get("risks") or []
    if risks:
        lines.append("\n## Риски")
        lines.append("\n| # | Риск | Категория | Вероятность | Влияние | Митигация |")
        lines.append("|---|------|-----------|-------------|---------|-----------|")
        for i, r in enumerate(risks, 1):
            if not isinstance(r, dict):
                continue
            lines.append(
                f"| {i} | {r.get('title', '—')} | {r.get('category', '—')} "
                f"| {r.get('probability', '—')} | {r.get('impact', '—')} "
                f"| {str(r.get('mitigation', '—')).replace(chr(10), ' ')} |"
            )

    nfrs = payload.get("non_functional_requirements") or []
    if nfrs:
        lines.append("\n## Нефункциональные требования")
        lines.extend(f"- {item}" for item in nfrs)

    lines.extend(_render_diagrams(payload.get("diagrams")))
    return "\n".join(lines)


def _render_component_decomposition(payload: dict[str, Any]) -> str:
    lines = ["# Декомпозиция на компоненты"]
    summary = payload.get("summary")
    if summary:
        lines.append(f"\n{summary}")
    lines.append("\n## Компоненты")
    for component in payload["components"]:
        lines.append(f"### {component['name']}")
        lines.append(component["responsibilities"])
        owns = component.get("owns_data") or []
        if owns:
            lines.append("**Владеет данными:**")
            lines.extend(f"- {item}" for item in owns)
        deps = component.get("dependencies") or []
        if deps:
            lines.append("**Зависимости:**")
            lines.extend(f"- {item}" for item in deps)
        lines.append("")
    lines.append("## Диаграмма компонентов")
    lines.append("```mermaid")
    lines.append(_build_flowchart(payload["component_diagram"]))
    lines.append("```")
    cross = payload.get("cross_cutting_concerns") or []
    if cross:
        lines.append("\n## Сквозные аспекты")
        lines.extend(f"- {item}" for item in cross)
    lines.extend(_render_diagrams(payload.get("diagrams")))
    return "\n".join(lines)


def _render_interaction_view(payload: dict[str, Any]) -> str:
    lines = ["# Потоки взаимодействия"]
    summary = payload.get("summary")
    if summary:
        lines.append(f"\n{summary}")
    lines.append("\n## Сценарии")
    for flow in payload["flows"]:
        lines.append(f"### {flow['name']}")
        lines.append(f"**Триггер:** {flow['trigger']}")
        lines.append(f"**Участники:** {', '.join(flow['participants'])}")
        lines.append("**Шаги:**")
        for i, step in enumerate(flow["steps"], 1):
            lines.append(f"{i}. {step}")
        lines.append("")
    diagram = payload["interaction_diagram"]
    kind = diagram.get("kind", "sequence")
    heading = "Sequence-диаграмма" if kind == "sequence" else "Диаграмма потока"
    lines.append(f"## {heading}")
    lines.append("```mermaid")
    lines.append(_build_interaction_diagram(diagram))
    lines.append("```")
    contracts = payload.get("data_contracts") or []
    if contracts:
        lines.append("\n## Контракты данных")
        lines.append("\n| От | К | Полезная нагрузка | Формат |")
        lines.append("|----|---|--------------------|--------|")
        for contract in contracts:
            fmt = contract.get("format", "—")
            lines.append(f"| {contract['from']} | {contract['to']} | {contract['payload']} | {fmt} |")
    failures = payload.get("failure_modes") or []
    if failures:
        lines.append("\n## Режимы сбоев")
        lines.extend(f"- {item}" for item in failures)
    lines.extend(_render_diagrams(payload.get("diagrams")))
    return "\n".join(lines)


def _render_system_context_definition(payload: dict[str, Any]) -> str:
    lines = ["# Системный контекст"]
    lines.append(f"\n**Система:** {payload['system_name']}")
    lines.append(f"\n**Назначение:** {payload['system_purpose']}")
    lines.append("\n## Акторы")
    for actor in payload["actors"]:
        kind = actor.get("kind", "—")
        description = actor.get("description")
        line = f"- **{actor['name']}** _({kind})_"
        if description:
            line += f" — {description}"
        lines.append(line)
    lines.append("\n## Внешние системы")
    for system in payload["external_systems"]:
        lines.append(f"- **{system['name']}** — {system['role']}")
        for interaction in system.get("interactions") or []:
            lines.append(f"  - {interaction}")
    boundaries = payload.get("system_boundaries") or []
    if boundaries:
        lines.append("\n## Границы системы")
        lines.extend(f"- {item}" for item in boundaries)
    assumptions = payload.get("assumptions") or []
    if assumptions:
        lines.append("\n## Допущения")
        lines.extend(f"- {item}" for item in assumptions)
    lines.append("\n## Контекстная диаграмма")
    lines.append("```mermaid")
    lines.append(_build_flowchart(payload["context_diagram"]))
    lines.append("```")
    lines.extend(_render_diagrams(payload.get("diagrams")))
    return "\n".join(lines)


# Слой компонента в порядке «изнутри наружу» — для правила зависимостей чистой
# архитектуры: зависеть можно только внутрь (на меньший или равный индекс).
_COMPONENT_LAYER_ORDER: dict[str, int] = {
    "core": 0,
    "application": 1,
    "adapter": 2,
    "infrastructure": 3,
}
_COMPONENT_TYPE_LABELS: dict[str, str] = {
    "service": "сервис",
    "datastore": "хранилище",
    "queue": "очередь",
    "ui": "интерфейс",
    "scheduled": "по расписанию",
    "external": "внешняя",
}
_REQUISITE_KIND_LABELS: dict[str, str] = {
    "credential": "доступ/креды",
    "dataset": "набор данных",
    "file": "файл/таблица",
    "setting": "настройка",
    "interface_format": "формат интерфейса",
    "sample": "образец",
    "other": "прочее",
}
# Мягкий бюджет на количество компонентов: больше — проверка предупреждает
# (минимизируем; каждый сверх — с обоснованием). Не блокирует.
_COMPONENT_SOFT_BUDGET = 9


def _is_component_model_shape(section: Any) -> bool:
    """Отличить новую модель компонентов от старой component_decomposition.

    Новая несёт `coverage` и/или компоненты со слоем/контрактом/модулями.
    Используется рендером design_document для выбора ветки (совместимость с
    закреплёнными прошлыми проектами).
    """
    if not isinstance(section, dict):
        return False
    if "coverage" in section:
        return True
    for component in section.get("components") or []:
        if isinstance(component, dict) and (
            "modules" in component
            or "provided_interfaces" in component
            or "layer" in component
        ):
            return True
    return False


def _render_component_model(payload: dict[str, Any]) -> str:
    lines = ["# Модель компонентов"]
    summary = payload.get("summary")
    if summary:
        lines.append(f"\n{summary}")
    lines.append("\n## Компоненты")
    for component in payload.get("components") or []:
        if not isinstance(component, dict):
            continue
        cid = component.get("id", "—")
        ctype = _COMPONENT_TYPE_LABELS.get(component.get("type"), component.get("type", "—"))
        layer = component.get("layer", "—")
        lines.append(f"\n### {component.get('name', '—')} `{cid}`")
        lines.append(f"_{ctype}, слой: {layer}_")
        if component.get("responsibility"):
            lines.append(f"\n{component['responsibility']}")
        if component.get("justification"):
            lines.append(f"\n**Обоснование:** {component['justification']}")
        if component.get("capability_owner"):
            lines.append(f"**Строит:** {component['capability_owner']}")
        nfr = component.get("nfr") or []
        if nfr:
            lines.append("\n**Нефункциональные требования:**")
            lines.extend(f"- {item}" for item in nfr)
        provided = component.get("provided_interfaces") or []
        if provided:
            lines.append("\n**Предоставляет:**")
            for iface in provided:
                if not isinstance(iface, dict):
                    continue
                meta = [m for m in (iface.get("sync"), iface.get("criticality")) if m]
                suffix = f" _({', '.join(meta)})_" if meta else ""
                io = ""
                if iface.get("input") or iface.get("output"):
                    io = f": {iface.get('input', '—')} → {iface.get('output', '—')}"
                lines.append(f"- **{iface.get('name', '—')}**{suffix}{io}")
                errors = iface.get("errors") or []
                if errors:
                    lines.append(f"  - ошибки: {', '.join(errors)}")
                if iface.get("detail"):
                    lines.append(f"  - {iface['detail']}")
        consumed = component.get("consumed_interfaces") or []
        if consumed:
            lines.append("\n**Потребляет:**")
            for dep in consumed:
                if isinstance(dep, dict):
                    lines.append(f"- {dep.get('component', '—')}.{dep.get('interface', '—')}")
        owned = component.get("owned_data") or []
        if owned:
            lines.append("\n**Владеет данными:**")
            lines.extend(f"- {item}" for item in owned)
        events = component.get("events") or {}
        emits = events.get("emits") or []
        consumes = events.get("consumes") or []
        if emits or consumes:
            lines.append("\n**События:**")
            if emits:
                lines.append(f"- публикует: {', '.join(emits)}")
            if consumes:
                lines.append(f"- потребляет: {', '.join(consumes)}")
        modules = component.get("modules") or []
        if modules:
            lines.append("\n**Внутреннее устройство:**")
            for module in modules:
                if not isinstance(module, dict):
                    continue
                realizes = f" → реализует `{module['realizes']}`" if module.get("realizes") else ""
                lines.append(
                    f"- `{module.get('id', '—')}` — {module.get('responsibility', '—')}{realizes}"
                )
            edges = component.get("internal_edges") or []
            for edge in edges:
                if isinstance(edge, dict):
                    lines.append(f"  - {edge.get('from', '—')} → {edge.get('to', '—')}")
        requisites = component.get("requisites") or []
        if requisites:
            lines.append("\n**Реквизиты (нужно от пользователя):**")
            for req in requisites:
                if not isinstance(req, dict):
                    continue
                kind = _REQUISITE_KIND_LABELS.get(req.get("kind"), req.get("kind", "—"))
                mark = " — блокирует переход" if req.get("blocking") else ""
                needed = f" (для: {req['needed_for']})" if req.get("needed_for") else ""
                lines.append(f"- {req.get('title', '—')} _({kind})_{needed}{mark}")

    coverage = payload.get("coverage") or {}
    actors = coverage.get("actors") or []
    systems = coverage.get("external_systems") or []
    if actors or systems:
        lines.append("\n## Покрытие")
        for entry in actors:
            if isinstance(entry, dict):
                comps = ", ".join(entry.get("components") or []) or "—"
                lines.append(f"- актор **{entry.get('actor', '—')}** → {comps}")
        for entry in systems:
            if isinstance(entry, dict):
                comps = ", ".join(entry.get("components") or []) or "—"
                lines.append(f"- внешняя **{entry.get('system', '—')}** → {comps}")

    lines.extend(_render_diagrams(payload.get("diagrams")))
    return "\n".join(lines)


def _render_component_build_spec(payload: dict[str, Any]) -> str:
    lines = [f"# Спека сборки: {payload.get('component', '—')}"]
    if payload.get("purpose"):
        lines.append(f"\n{payload['purpose']}")
    if payload.get("capability_owner"):
        lines.append(f"**Строит:** {payload['capability_owner']}")
    for label, key in (
        ("Технологии", "tech"),
        ("Предоставляет", "provided_interfaces"),
        ("Потребляет", "consumed_interfaces"),
        ("Данные", "data"),
        ("Зависимости", "dependencies"),
    ):
        vals = payload.get(key) or []
        if vals:
            lines.append(f"\n**{label}:**")
            lines.extend(f"- {item}" for item in vals)
    steps = payload.get("build_steps") or []
    if steps:
        lines.append("\n**Шаги сборки:**")
        for i, step in enumerate(steps, 1):
            lines.append(f"{i}. {step}")
    if payload.get("test_approach"):
        lines.append(f"\n**Тесты:** {payload['test_approach']}")
    for label, key in (
        ("Критерии приёмки", "acceptance"),
        ("Вне рамок", "out_of_scope"),
        ("Открытые вопросы", "open_questions"),
    ):
        vals = payload.get(key) or []
        if vals:
            lines.append(f"\n**{label}:**")
            lines.extend(f"- {item}" for item in vals)
    return "\n".join(lines)


def _render_deployment_map(payload: dict[str, Any]) -> str:
    lines = ["# Карта развёртывания"]
    summary = payload.get("summary")
    if summary:
        lines.append(f"\n{summary}")
    units = payload.get("deployment_units") or []
    if units:
        lines.append("\n## Единицы развёртывания")
        for unit in units:
            if not isinstance(unit, dict):
                continue
            runtime = f" _({unit['runtime']})_" if unit.get("runtime") else ""
            lines.append(f"\n### {unit.get('name', '—')} `{unit.get('id', '—')}`{runtime}")
            comps = unit.get("components") or []
            if comps:
                lines.append(f"**Компоненты:** {', '.join(comps)}")
            if unit.get("justification"):
                lines.append(f"**Обоснование:** {unit['justification']}")
    lines.extend(_render_diagrams(payload.get("diagrams")))
    return "\n".join(lines)


def check_component_model_consistency(payload: dict[str, Any]) -> list[str]:
    """Детерминированная проверка целостности модели компонентов (без LLM).

    Возвращает список человекочитаемых замечаний. ВСЕ они неблокирующие —
    вызывающая сторона оформляет их как предупреждения / зоны роста, а не как
    отказ. Цель — поймать то, что не ловит схема: висячие ссылки, нереализованные
    интерфейсы, нарушение правила зависимостей, циклы, раздувание количества.
    Разбор защитный: кривые элементы тихо пропускаются.
    """
    issues: list[str] = []
    components = [c for c in (payload.get("components") or []) if isinstance(c, dict)]
    ids = [str(c.get("id")) for c in components if c.get("id")]
    id_set = set(ids)

    # 1. Уникальность id компонентов.
    seen: set[str] = set()
    for cid in ids:
        if cid in seen:
            issues.append(f"Дублирующийся id компонента: '{cid}'.")
        seen.add(cid)

    if len(components) > _COMPONENT_SOFT_BUDGET:
        issues.append(
            f"Компонентов {len(components)} — больше мягкого бюджета "
            f"({_COMPONENT_SOFT_BUDGET}); проверьте, обоснован ли каждый."
        )

    for component in components:
        cid = str(component.get("id") or "?")
        # 2. Потребляемые интерфейсы ссылаются на существующий компонент.
        for dep in component.get("consumed_interfaces") or []:
            if not isinstance(dep, dict):
                continue
            target = str(dep.get("component") or "")
            if target and target not in id_set:
                issues.append(
                    f"Компонент '{cid}' потребляет интерфейс несуществующего "
                    f"компонента '{target}'."
                )
            else:
                # 6. Правило зависимостей чистой архитектуры: ядро не зависит
                # наружу. Флагируем только этот канонический случай (а не любой
                # service→datastore), чтобы не шуметь.
                provider = next((c for c in components if str(c.get("id")) == target), None)
                if provider is not None and component.get("layer") == "core":
                    pi = _COMPONENT_LAYER_ORDER.get(provider.get("layer"))
                    if pi is not None and pi > 0:
                        issues.append(
                            f"Нарушение правила зависимостей: ядро '{cid}' зависит "
                            f"от '{target}' (слой {provider.get('layer')}) — "
                            f"ядро не должно зависеть наружу."
                        )
        # 3. Каждый предоставляемый интерфейс реализован каким-то модулем.
        realized = {
            str(m.get("realizes"))
            for m in (component.get("modules") or [])
            if isinstance(m, dict) and m.get("realizes")
        }
        module_ids = {
            str(m.get("id"))
            for m in (component.get("modules") or [])
            if isinstance(m, dict) and m.get("id")
        }
        for iface in component.get("provided_interfaces") or []:
            if not isinstance(iface, dict):
                continue
            name = str(iface.get("name") or "")
            if name and name not in realized:
                issues.append(
                    f"Компонент '{cid}': интерфейс '{name}' не закреплён ни за "
                    f"одним внутренним модулем (realizes)."
                )
        # 4. Внутренние рёбра ссылаются на объявленные модули.
        for edge in component.get("internal_edges") or []:
            if not isinstance(edge, dict):
                continue
            for end in ("from", "to"):
                ref = str(edge.get(end) or "")
                if ref and ref not in module_ids:
                    issues.append(
                        f"Компонент '{cid}': внутреннее ребро ссылается на "
                        f"несуществующий модуль '{ref}'."
                    )

    # 5. Покрытие ссылается на существующие компоненты.
    coverage = payload.get("coverage") or {}
    for entry in coverage.get("actors") or []:
        if isinstance(entry, dict):
            for comp in entry.get("components") or []:
                if str(comp) not in id_set:
                    issues.append(
                        f"Покрытие актора '{entry.get('actor')}' ссылается на "
                        f"несуществующий компонент '{comp}'."
                    )
    for entry in coverage.get("external_systems") or []:
        if isinstance(entry, dict):
            for comp in entry.get("components") or []:
                if str(comp) not in id_set:
                    issues.append(
                        f"Покрытие внешней системы '{entry.get('system')}' "
                        f"ссылается на несуществующий компонент '{comp}'."
                    )

    # 7. Цикл в графе зависимостей компонентов.
    if _has_dependency_cycle(components):
        issues.append("В графе зависимостей компонентов есть цикл.")

    return issues


def _has_dependency_cycle(components: list[dict[str, Any]]) -> bool:
    """Поиск цикла в ориентированном графе зависимостей (DFS с тремя цветами)."""
    graph: dict[str, list[str]] = {}
    for component in components:
        cid = str(component.get("id") or "")
        if not cid:
            continue
        targets = []
        for dep in component.get("consumed_interfaces") or []:
            if isinstance(dep, dict) and dep.get("component"):
                targets.append(str(dep["component"]))
        graph[cid] = targets

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(graph, WHITE)

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in graph.get(node, ()):
            if nxt not in color:
                continue
            if color[nxt] == GRAY:
                return True
            if color[nxt] == WHITE and visit(nxt):
                return True
        color[node] = BLACK
        return False

    return any(color[node] == WHITE and visit(node) for node in graph)
