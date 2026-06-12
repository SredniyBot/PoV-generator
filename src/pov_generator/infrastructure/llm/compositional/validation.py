"""Лёгкая проверка значения на соответствие JSON-схеме.

Нужна внутри ассемблера, чтобы решить «часть/целое уложилось в схему?» —
триггер реактивной сборки и точечного перезапроса узла. Сознательно минимальна
(type / required / enum / границы массива и рекурсия) и БЕЗ внешней зависимости
(``jsonschema`` в проекте не используется). Полная семантическая валидация
артефакта остаётся за слоем application (validation_service) — здесь только
быстрый структурный чек.
"""

from __future__ import annotations

from typing import Any

from . import schema_utils as su
from .schema_utils import JSONSchema


def matches_schema(value: Any, schema: JSONSchema) -> bool:
    """Структурно ли ``value`` соответствует ``schema`` (type/required/enum/границы).

    Терпимо к неизвестным конструкциям схемы (их не считаем нарушением).
    Лишние поля сверх ``properties`` НЕ считаем ошибкой — модель иногда добавляет
    пояснительные ключи, их потом снимет нормализация; важна достаточность, а не
    строгая замкнутость."""
    if not isinstance(schema, dict):
        return True
    if "enum" in schema:
        return value in schema["enum"]
    t = su.schema_type(schema)
    if t == "object":
        return _matches_object(value, schema)
    if t == "array":
        return _matches_array(value, schema)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    return True  # тип не определён — не блокируем


def _matches_object(value: Any, schema: JSONSchema) -> bool:
    if not isinstance(value, dict):
        return False
    for name in su.required_fields(schema):
        if name not in value:
            return False
    props = su.object_properties(schema)
    for name, sub in props.items():
        if name in value and not matches_schema(value[name], sub):
            return False
    return True


def _matches_array(value: Any, schema: JSONSchema) -> bool:
    if not isinstance(value, list):
        return False
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and len(value) > max_items:
        return False
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(value) < min_items:
        return False
    item = su.array_item_schema(schema)
    if item is None:
        return True
    return all(matches_schema(v, item) for v in value)
