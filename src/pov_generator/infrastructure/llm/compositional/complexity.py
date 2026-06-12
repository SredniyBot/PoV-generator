"""Метрика сложности JSON-схемы и порог проактивной декомпозиции.

Идея: чем глубже вложенность, чем больше обязательных полей, enum'ов и
вложенных массивов объектов — тем менее надёжен один strict-проход и тем
оправданнее сразу собирать структуру по частям. Простые схемы (низкий балл)
идут одним вызовом, как раньше.

Метрика — чистая функция; порог переопределяется ``POV_COMPOSITIONAL_THRESHOLD``
(0 → декомпозировать всегда; очень большое → практически никогда проактивно,
останется только реактивный путь).
"""

from __future__ import annotations

import os

from . import schema_utils as su
from .schema_utils import JSONSchema

# Веса признаков сложности. Подобраны так, чтобы плоские объекты из нескольких
# полей (problem_statement, goal_hypothesis) были НИЖЕ порога, а вложенные
# структуры с массивами объектов (карта решений, ТЗ) — выше.
_W_REQUIRED = 1          # каждое обязательное поле
_W_ENUM = 1             # каждый enum (модели тяжело держать строгий набор)
_W_NESTED_ARRAY_OBJ = 4  # массив объектов — самый дорогой для strict
_W_DEPTH = 2            # за каждый уровень вложенности глубже первого

# Порог намеренно консервативный: проактивно дробим только ЯВНО тяжёлые схемы
# (вложенный массив сложных объектов — карта решений, ТЗ), которые ненадёжно
# берутся одним проходом. Умеренные схемы (один массив простых объектов, плоские
# объекты) остаются одним проходом — быстро; для них работает РЕАКТИВНЫЙ путь
# (дробим только если проход реально не уложился). Так подход не замедляет то,
# что и так работает. Переопределяется POV_COMPOSITIONAL_THRESHOLD.
_DEFAULT_THRESHOLD = 20


def schema_complexity(schema: JSONSchema) -> int:
    """Целочисленный балл сложности схемы (0 для скаляра/пустого)."""
    return _score(schema, depth=0)


def _score(schema: object, *, depth: int) -> int:
    if not isinstance(schema, dict):
        return 0
    total = 0
    if "enum" in schema:
        total += _W_ENUM
    if su.is_object_schema(schema):
        total += depth * _W_DEPTH
        total += len(su.required_fields(schema)) * _W_REQUIRED
        for sub in su.object_properties(schema).values():
            total += _score(sub, depth=depth + 1)
    elif su.is_array_schema(schema):
        item = su.array_item_schema(schema)
        if su.is_object_schema(item or {}):
            total += _W_NESTED_ARRAY_OBJ
        if item is not None:
            total += _score(item, depth=depth + 1)
    return total


def decomposition_threshold() -> int:
    """Порог балла, выше которого включается проактивная декомпозиция."""
    raw = os.environ.get("POV_COMPOSITIONAL_THRESHOLD")
    if raw is None:
        return _DEFAULT_THRESHOLD
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD
    return value if value >= 0 else _DEFAULT_THRESHOLD


def should_decompose(schema: JSONSchema) -> bool:
    """Достаточно ли схема сложна, чтобы собирать её по частям проактивно."""
    return schema_complexity(schema) >= decomposition_threshold()
