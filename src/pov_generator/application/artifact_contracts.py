from __future__ import annotations

from typing import Any

from ..common.errors import ValidationError


JSONSchema = dict[str, Any]


def artifact_schema(artifact_role: str, domain_pack_refs: tuple[str, ...] = ()) -> JSONSchema:
    frontend_enabled = "frontend.web_app_requirements@1.0.0" in domain_pack_refs
    requirements_spec_properties: JSONSchema = {
        "title": {"type": "string"},
        "business_goal": {"type": "string"},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "actors": {"type": "array", "items": {"type": "string"}},
        "user_stories": {"type": "array", "items": {"type": "string"}},
        "functional_requirements": {"type": "array", "items": {"type": "string"}},
        "non_functional_requirements": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "alternatives_considered": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    }
    if frontend_enabled:
        requirements_spec_properties["frontend_requirements"] = {
            "type": "object",
            "required": ["user_roles", "user_flows", "screens", "ux_constraints"],
            "additionalProperties": False,
            "properties": {
                "user_roles": {"type": "array", "items": {"type": "string"}},
                "user_flows": {"type": "array", "items": {"type": "string"}},
                "screens": {"type": "array", "items": {"type": "string"}},
                "ux_constraints": {"type": "array", "items": {"type": "string"}},
            },
        }
    schemas: dict[str, JSONSchema] = {
        "clarification_notes": {
            "type": "object",
            "required": ["clarified_goal", "success_criteria", "assumptions", "open_questions"],
            "additionalProperties": False,
            "properties": {
                "clarified_goal": {"type": "string"},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
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
                            "needs": {"type": "array", "items": {"type": "string"}},
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
                "edge_cases": {"type": "array", "items": {"type": "string"}},
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
                            "pros": {"type": "array", "items": {"type": "string"}},
                            "cons": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "recommended_option": {"type": "string"},
                "rationale": {"type": "string"},
            },
        },
        "ui_requirements_outline": {
            "type": "object",
            "required": ["user_roles", "user_flows", "screens", "ux_constraints"],
            "additionalProperties": False,
            "properties": {
                "user_roles": {"type": "array", "items": {"type": "string"}},
                "user_flows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "steps"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "steps": {"type": "array", "items": {"type": "string"}},
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
                "ux_constraints": {"type": "array", "items": {"type": "string"}},
            },
        },
        "requirements_spec": {
            "type": "object",
            "required": [
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
            + (["frontend_requirements"] if frontend_enabled else []),
            "additionalProperties": False,
            "properties": requirements_spec_properties,
        },
        "review_report": {
            "type": "object",
            "required": ["overall_status", "summary", "strengths", "issues", "recommendations"],
            "additionalProperties": False,
            "properties": {
                "overall_status": {"type": "string", "enum": ["passed", "needs_changes"]},
                "summary": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["severity", "message"],
                        "additionalProperties": False,
                        "properties": {
                            "severity": {"type": "string", "enum": ["info", "warning", "error", "critical"]},
                            "message": {"type": "string"},
                        },
                    },
                },
                "recommendations": {"type": "array", "items": {"type": "string"}},
            },
        },
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
    raise ValidationError(f"{path}: неподдерживаемый тип схемы '{schema_type}'")


def schema_instruction(role: str, domain_pack_refs: tuple[str, ...]) -> str:
    schema = artifact_schema(role, domain_pack_refs)
    return (
        "Верни строго JSON, соответствующий этой схеме.\n"
        f"Роль артефакта: {role}\n"
        f"Схема: {schema}"
    )


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
        lines = ["# Анализ альтернатив", f"## Рекомендованный вариант\n{payload['recommended_option']}", "## Обоснование", payload["rationale"], "## Варианты"]
        for item in payload["alternatives"]:
            lines.append(f"### {item['name']}")
            lines.append(item["description"])
            lines.append("Плюсы:")
            lines.extend(f"- {entry}" for entry in item["pros"])
            lines.append("Минусы:")
            lines.extend(f"- {entry}" for entry in item["cons"])
        return "\n".join(lines)
    if artifact_role == "ui_requirements_outline":
        lines = ["# Контур UI-требований", "## Роли пользователей"]
        lines.extend(f"- {item}" for item in payload["user_roles"])
        lines.append("\n## Пользовательские потоки")
        for flow in payload["user_flows"]:
            lines.append(f"### {flow['name']}")
            lines.extend(f"- {step}" for step in flow["steps"])
        lines.append("\n## Экраны")
        for screen in payload["screens"]:
            lines.append(f"- {screen['name']}: {screen['purpose']}")
        lines.append("\n## UX-ограничения")
        lines.extend(f"- {item}" for item in payload["ux_constraints"])
        return "\n".join(lines)
    if artifact_role == "requirements_spec":
        lines = [
            f"# {payload['title']}",
            f"## Бизнес-цель\n{payload['business_goal']}",
            "## Критерии успеха",
            *[f"- {item}" for item in payload["success_criteria"]],
            "\n## Роли",
            *[f"- {item}" for item in payload["actors"]],
            "\n## User story",
            *[f"- {item}" for item in payload["user_stories"]],
            "\n## Функциональные требования",
            *[f"- {item}" for item in payload["functional_requirements"]],
            "\n## Нефункциональные требования",
            *[f"- {item}" for item in payload["non_functional_requirements"]],
            "\n## Допущения",
            *[f"- {item}" for item in payload["assumptions"]],
            "\n## Риски",
            *[f"- {item}" for item in payload["risks"]],
            "\n## Рассмотренные альтернативы",
            *[f"- {item}" for item in payload["alternatives_considered"]],
            "\n## Критерии приёмки",
            *[f"- {item}" for item in payload["acceptance_criteria"]],
            "\n## Открытые вопросы",
            *[f"- {item}" for item in payload["open_questions"]],
        ]
        frontend = payload.get("frontend_requirements")
        if frontend:
            lines.extend(
                [
                    "\n## Требования к фронтенду",
                    "### Пользовательские роли",
                    *[f"- {item}" for item in frontend["user_roles"]],
                    "\n### Пользовательские потоки",
                    *[f"- {item}" for item in frontend["user_flows"]],
                    "\n### Экраны",
                    *[f"- {item}" for item in frontend["screens"]],
                    "\n### UX-ограничения",
                    *[f"- {item}" for item in frontend["ux_constraints"]],
                ]
            )
        return "\n".join(lines)
    if artifact_role == "review_report":
        lines = [
            "# Отчёт ревью",
            f"## Статус\n{payload['overall_status']}",
            f"## Резюме\n{payload['summary']}",
            "## Сильные стороны",
            *[f"- {item}" for item in payload["strengths"]],
            "\n## Замечания",
        ]
        for issue in payload["issues"]:
            lines.append(f"- [{issue['severity']}] {issue['message']}")
        lines.append("\n## Рекомендации")
        lines.extend(f"- {item}" for item in payload["recommendations"])
        return "\n".join(lines)
    raise ValidationError(f"Неизвестный рендерер артефакта: {artifact_role}")
