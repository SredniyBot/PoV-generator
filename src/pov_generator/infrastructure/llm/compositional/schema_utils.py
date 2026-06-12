"""Чистые helper'ы инспекции JSON-схемы.

Общий низкоуровневый слой для :mod:`complexity` и :mod:`decomposer`: ответить на
вопросы «какой тип у узла», «какие свойства/required», «что в элементах массива»
— терпимо к неполным/нестандартным схемам (никогда не кидаем, возвращаем
разумный дефолт). Бизнес-логики здесь нет.
"""

from __future__ import annotations

from typing import Any

JSONSchema = dict[str, Any]

_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})


def schema_type(schema: Any) -> str | None:
    """Эффективный тип узла схемы.

    ``type`` может быть строкой или списком (напр. ``["string", "null"]``) —
    возвращаем первый не-``null`` тип. ``enum`` без ``type`` трактуем как
    скаляр (``string``). Возвращает ``None``, если тип не определить."""
    if not isinstance(schema, dict):
        return None
    raw = schema.get("type")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        for item in raw:
            if item != "null":
                return item
        return None
    if "enum" in schema:
        return "string"
    if "properties" in schema:
        return "object"
    return None


def is_object_schema(schema: Any) -> bool:
    return schema_type(schema) == "object" and isinstance(schema, dict)


def is_array_schema(schema: Any) -> bool:
    return schema_type(schema) == "array" and isinstance(schema, dict)


def is_scalar_schema(schema: Any) -> bool:
    """Лист-скаляр: примитивный тип или enum (его генерировать дробить незачем)."""
    if not isinstance(schema, dict):
        return False
    if "enum" in schema:
        return True
    return schema_type(schema) in _SCALAR_TYPES


def object_properties(schema: JSONSchema) -> dict[str, JSONSchema]:
    """Свойства объекта (``{}`` если их нет/нестандартны)."""
    props = schema.get("properties")
    if isinstance(props, dict):
        return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}


def required_fields(schema: JSONSchema) -> tuple[str, ...]:
    """Имена обязательных полей в исходном порядке (пустой кортеж, если нет)."""
    req = schema.get("required")
    if isinstance(req, list):
        return tuple(str(x) for x in req)
    return ()


def array_item_schema(schema: JSONSchema) -> JSONSchema | None:
    """Схема элемента массива (``items`` как объект; tuple-форму игнорируем)."""
    items = schema.get("items")
    return items if isinstance(items, dict) else None


def array_of_objects(schema: Any) -> bool:
    """Массив, элементы которого — объекты (кандидат на каркас→наполнение)."""
    return is_array_schema(schema) and is_object_schema(array_item_schema(schema) or {})
