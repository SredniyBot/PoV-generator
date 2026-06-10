"""Structured output: соблюдение JSON-схемы силами самого провайдера.

Контракт ``LLMProvider.chat_json`` обещает payload по схеме, и провайдер обязан
добиваться этого САМЫМ СИЛЬНЫМ механизмом, который поддерживает его API. Этот
модуль — единственное место, где описана матрица возможностей и живут общие
преобразования схем. Цепочка гарантий системы (каждый следующий слой — no-op,
если предыдущий отработал идеально):

1. ПРОВАЙДЕР (этот модуль + клиенты): нативный structured output.
   • ``openrouter``      — ``response_format: json_schema (strict)``. OpenAI-
     совместимый strict-режим принимает только подмножество JSON Schema (все
     properties в ``required``, ``additionalProperties: false`` на каждом
     объекте) — :func:`to_strict_schema` приводит схему к нему БЕЗ потери
     семантики: опциональные поля становятся nullable-обязательными, а ``null``
     после парсинга вычищается (:func:`strip_nulls`). Деградация при отказе
     модели/провайдера: json_schema → json_object → без response_format.
   • ``claude_sdk``      — forced tool use (``tool_choice={"type": "tool"}``,
     схема целиком в ``input_schema``) — родной механизм Anthropic API,
     преобразований не требует.
   • ``claude_subscription`` — ``--json-schema`` через Claude Agent SDK
     (``ClaudeAgentOptions.output_format``), результат в
     ``ResultMessage.structured_output``. Схема передаётся аргументом командной
     строки → перед передачей очищается от ``description`` (стирание only-
     guidance полей, :func:`strip_descriptions`) и ограничивается по размеру
     (лимит командной строки Windows ~32К символов). Деградация: CLI без
     поддержки флага / отказ API → прежний путь «схема текстом в промпте +
     извлечение JSON из ответа».
   • ``stub``            — детерминированные фикстуры, всегда валидны.

2. НОРМАЛИЗАЦИЯ (``application.artifact_contracts.normalize_to_schema``) —
   детерминированная починка механики формы без LLM.
3. SELF-REPAIR (``application.execution_service._repair_payload_to_schema``) —
   точечная LLM-починка только провалившихся полей.
4. ВАЛИДАЦИЯ (``application.validation_service``) — финальный страж контракта.

Деградации каждого слоя логируются — разработчик видит, какой механизм
фактически применился и почему.
"""

from __future__ import annotations

from typing import Any

# Поля JSON Schema, которые несут только guidance для модели и не влияют на
# валидацию. Вычищаются там, где размер схемы критичен (CLI-аргумент).
_GUIDANCE_KEYS = ("description",)


def to_strict_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Привести схему к strict-подмножеству OpenAI structured outputs.

    Возвращает преобразованную копию или ``None``, если схема принципиально
    не выражается в strict-режиме (есть ``additionalProperties: true`` —
    «unstructured» контракты — или неподдерживаемые конструкции): тогда клиент
    деградирует к ``json_object`` / промпт-режиму.

    Преобразование БЕЗ потери семантики:
    • каждый объект получает ``additionalProperties: false`` (сужает то, что
      модель может выдать, — итог остаётся валидным по исходной схеме);
    • опциональные properties попадают в ``required``, а их тип оборачивается
      в ``anyOf: [<исходный>, {"type": "null"}]`` — модель обязана выдать ключ,
      но может выдать ``null`` = «не заполнено». После парсинга ``null``
      вычищаются (:func:`strip_nulls`), и payload неотличим от «ключ опущен».
    """
    converted = _to_strict_node(schema)
    if converted is None:
        return None
    if converted.get("type") != "object":
        return None  # корень strict-режима обязан быть объектом
    return converted


def _to_strict_node(schema: dict[str, Any]) -> dict[str, Any] | None:
    any_of = schema.get("anyOf")
    if any_of is not None:
        branches = []
        for branch in any_of:
            converted = _to_strict_node(branch)
            if converted is None:
                return None
            branches.append(converted)
        out = {key: value for key, value in schema.items() if key != "anyOf"}
        out["anyOf"] = branches
        return out

    schema_type = schema.get("type")
    if schema_type == "object":
        if schema.get("additionalProperties", False) is True:
            return None  # открытый объект strict-режим не выражает
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        new_properties: dict[str, Any] = {}
        for key, prop_schema in properties.items():
            converted = _to_strict_node(prop_schema)
            if converted is None:
                return None
            if key not in required:
                # Опциональное поле → nullable-обязательное (см. docstring).
                converted = {"anyOf": [converted, {"type": "null"}]}
            new_properties[key] = converted
        out = dict(schema)
        out["properties"] = new_properties
        out["required"] = sorted(properties)
        out["additionalProperties"] = False
        return out

    if schema_type == "array":
        out = dict(schema)
        item_schema = schema.get("items")
        if item_schema is not None:
            converted = _to_strict_node(item_schema)
            if converted is None:
                return None
            out["items"] = converted
        return out

    if schema_type in ("string", "number", "boolean", "integer", "null"):
        return dict(schema)

    # oneOf / $ref / allOf и прочее мы не используем; встретили — не рискуем.
    return None


def strip_nulls(value: Any) -> Any:
    """Убрать ``null``-значения из payload: ключи-None и None-элементы списков.

    Семантика всей системы: «нет данных = ключ отсутствует» (валидатор
    отвергает ``null`` у типизированных полей). Strict-преобразование заставляет
    модель выдавать ``null`` вместо пропуска ключа — здесь это сводится обратно
    к канонической форме.
    """
    if isinstance(value, dict):
        return {key: strip_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [strip_nulls(item) for item in value if item is not None]
    return value


def strip_descriptions(schema: Any) -> Any:
    """Снять guidance-поля (``description``) со всех узлов схемы.

    Структурные ограничения (типы, required, additionalProperties, enum)
    не затрагиваются. Используется там, где схема передаётся через канал с
    лимитом размера (аргумент командной строки CLI): описания уже присутствуют
    в текстовой копии схемы в промпте, enforcement-слою нужна только структура.
    """
    if isinstance(schema, dict):
        return {
            key: strip_descriptions(value)
            for key, value in schema.items()
            if key not in _GUIDANCE_KEYS
        }
    if isinstance(schema, list):
        return [strip_descriptions(item) for item in schema]
    return schema
