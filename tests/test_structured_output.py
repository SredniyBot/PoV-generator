"""Тесты слоя structured output (``infrastructure/llm/structured_output.py``).

Слой «провайдер сам гарантирует форму»: преобразование схем к strict-
подмножеству OpenAI, канонизация null'ов, очистка guidance-полей для
канала с лимитом размера (CLI-аргумент).
"""
from __future__ import annotations

from pov_generator.infrastructure.llm.structured_output import (
    strip_descriptions,
    strip_nulls,
    to_strict_schema,
)

_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "description": "Имя."},
        "tags": {"type": "array", "items": {"type": "string"}},
        "meta": {
            "type": "object",
            "required": ["kind"],
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string"},
                "note": {"type": "string"},
            },
        },
    },
}


def test_to_strict_schema_makes_all_properties_required_with_nullable_optionals() -> None:
    strict = to_strict_schema(_SCHEMA)
    assert strict is not None
    # Все properties в required (включая бывшие опциональными).
    assert strict["required"] == ["meta", "name", "tags"]
    assert strict["additionalProperties"] is False
    # Обязательное поле осталось как было (без nullable-обёртки).
    assert strict["properties"]["name"]["type"] == "string"
    # Опциональное → anyOf [исходный, null].
    tags = strict["properties"]["tags"]
    assert "anyOf" in tags
    assert {"type": "null"} in tags["anyOf"]
    # Вложенный объект преобразован рекурсивно.
    meta = strict["properties"]["meta"]["anyOf"][0]
    assert meta["required"] == ["kind", "note"]
    assert "anyOf" in meta["properties"]["note"]


def test_to_strict_schema_bails_on_open_objects() -> None:
    """«Unstructured» контракты (additionalProperties: true) в strict-режиме
    не выражаются — клиент должен деградировать к json_object/промпту."""
    assert to_strict_schema({"type": "object", "additionalProperties": True}) is None
    nested = {
        "type": "object",
        "properties": {"blob": {"type": "object", "additionalProperties": True}},
    }
    assert to_strict_schema(nested) is None


def test_to_strict_schema_requires_object_root_and_known_constructs() -> None:
    assert to_strict_schema({"type": "array", "items": {"type": "string"}}) is None
    # Неподдерживаемые конструкции (oneOf/$ref) — отказ, не молчаливая порча.
    assert to_strict_schema({"type": "object", "properties": {"x": {"oneOf": []}}}) is None


def test_to_strict_schema_preserves_existing_anyof() -> None:
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"anyOf": [{"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}, {"type": "string"}]},
            }
        },
    }
    strict = to_strict_schema(schema)
    assert strict is not None
    branches = strict["properties"]["items"]["items"]["anyOf"]
    assert {"type": "string"} in branches


def test_strip_nulls_drops_none_keys_and_items_recursively() -> None:
    payload = {
        "name": "X",
        "tags": None,
        "meta": {"kind": "k", "note": None},
        "list": ["a", None, {"b": None, "c": 1}],
    }
    assert strip_nulls(payload) == {
        "name": "X",
        "meta": {"kind": "k"},
        "list": ["a", {"c": 1}],
    }


def test_strip_descriptions_removes_guidance_keeps_structure() -> None:
    lean = strip_descriptions(_SCHEMA)
    assert "description" not in lean["properties"]["name"]
    # Структурные ограничения не тронуты.
    assert lean["required"] == ["name"]
    assert lean["properties"]["meta"]["additionalProperties"] is False
