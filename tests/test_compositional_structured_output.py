"""Compositional structured output: декомпозиция → сборка по частям.

Проверяем всю подсистему ``infrastructure/llm/compositional`` на чистых данных и
фейк-провайдере (без сети): метрику сложности, декомпозер на РЕАЛЬНОЙ схеме
решений, сборку целого по частям и оба режима декоратора (проактивный по
сложности и реактивный при несоответствии одного прохода).
"""

from __future__ import annotations

from typing import Any

from pov_generator.application.decision_identification_service import _build_identification_schema
from pov_generator.infrastructure.llm.compositional import (
    CompositionalLLMProvider,
    SchemaTreeDecomposer,
    schema_complexity,
    should_decompose,
)
from pov_generator.infrastructure.llm.compositional.complexity import decomposition_threshold
from pov_generator.infrastructure.llm.compositional.plan import ArrayPlan, LeafPlan, ObjectPlan
from pov_generator.infrastructure.llm.compositional.validation import matches_schema
from pov_generator.infrastructure.llm.protocol import LLMResult, LLMUsage

# --- общий фейк-провайдер ----------------------------------------------------


def _fill(schema: dict[str, Any]) -> Any:
    """Минимальное ВАЛИДНОЕ по схеме значение (для фейк-провайдера)."""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        return {name: _fill(sub) for name, sub in (schema.get("properties") or {}).items()}
    if t == "array":
        item = schema.get("items") or {}
        count = max(int(schema.get("minItems", 0)), 2)  # стабильно ≥2 элемента
        return [_fill(item) for _ in range(count)]
    if t in ("number", "integer"):
        return 1
    if t == "boolean":
        return True
    return "x"


class _FakeProvider:
    """Отдаёт валидный по запрошенной схеме payload; считает вызовы."""

    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any]) -> LLMResult:
        self.calls.append(schema)
        return LLMResult(payload=_fill(schema), usage=LLMUsage(2, 3, 5, "actual"))


_SIMPLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


# --- метрика сложности -------------------------------------------------------


def test_complexity_decision_above_simple_below_threshold() -> None:
    threshold = decomposition_threshold()
    decision = _build_identification_schema()
    assert schema_complexity(decision) >= threshold, "схема решений должна быть выше порога"
    assert schema_complexity(_SIMPLE_SCHEMA) < threshold, "плоская схема должна быть ниже порога"
    assert should_decompose(decision) is True
    assert should_decompose(_SIMPLE_SCHEMA) is False


# --- декомпозер --------------------------------------------------------------


def test_decompose_simple_schema_is_leaf() -> None:
    plan = SchemaTreeDecomposer().decompose(_SIMPLE_SCHEMA)
    assert isinstance(plan, LeafPlan)


def test_decompose_decision_splits_array() -> None:
    """Корень → объект; decisions → массив «каркас→наполнение»; элемент-решение —
    отдельный фрагмент. Это и есть устранение одного гигантского strict-вызова."""
    plan = SchemaTreeDecomposer().decompose(_build_identification_schema())
    assert isinstance(plan, ObjectPlan)
    assert [name for name, _ in plan.structural] == ["decisions"]
    decisions_plan = plan.structural[0][1]
    assert isinstance(decisions_plan, ArrayPlan)
    # Каркас несёт «опознавательное» ядро элемента (включая title).
    outline_item = decisions_plan.outline_schema["properties"]["items"]["items"]
    assert "title" in outline_item["properties"]


# --- сборка end-to-end (фейк-провайдер) --------------------------------------


def test_assemble_decision_produces_valid_whole() -> None:
    from pov_generator.infrastructure.llm.compositional.assembler import StructuredAssembler

    schema = _build_identification_schema()
    fake = _FakeProvider()
    assembler = StructuredAssembler(fake, decomposer=SchemaTreeDecomposer())
    value, usages = assembler.assemble(schema, base_system="sys", base_user="task")

    # Целое валидно по ИСХОДНОЙ схеме — контракт собран, не «съехал».
    assert matches_schema(value, schema)
    assert isinstance(value["decisions"], list) and value["decisions"]
    # Каждое решение содержит вложенный массив альтернатив (собран в фрагменте).
    assert all("alternatives" in d for d in value["decisions"])
    # Массив развёрнут «каркас → наполнение», каждое решение собрано из фрагментов
    # (поля решения батчатся) — вызовов как минимум по одному на решение + каркас.
    assert len(fake.calls) >= 1 + len(value["decisions"])
    # usage агрегируется по всем фрагментам.
    assert len(usages) == len(fake.calls)


# --- декоратор: оба режима ---------------------------------------------------


def test_provider_simple_schema_single_pass() -> None:
    """Простая схема: один проход, без декомпозиции (как раньше)."""
    fake = _FakeProvider()
    provider = CompositionalLLMProvider(fake)
    result = provider.chat_json(system_prompt="s", user_prompt="u", schema=_SIMPLE_SCHEMA)
    assert matches_schema(result.payload, _SIMPLE_SCHEMA)
    assert len(fake.calls) == 1  # ровно один вызов


def test_provider_complex_schema_proactive_assembly() -> None:
    """Сложная схема: проактивная сборка по частям (> 1 вызова), валидный итог."""
    schema = _build_identification_schema()
    fake = _FakeProvider()
    provider = CompositionalLLMProvider(fake)
    result = provider.chat_json(system_prompt="s", user_prompt="u", schema=schema)
    assert matches_schema(result.payload, schema)
    assert len(fake.calls) > 1
    assert result.usage is not None and result.usage.output_tokens > 0
    # Запуски агрегируются: call_count = число фактических вызовов (фрагментов).
    assert result.usage.call_count == len(fake.calls)


class _OnePassInvalidProvider(_FakeProvider):
    """Возвращает пустой объект на ПОЛНУЮ схему решений (один проход «не уложился»),
    но валидные фрагменты на под-схемы — чтобы проверить реактивную сборку."""

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any]) -> LLMResult:
        self.calls.append(schema)
        props = schema.get("properties") or {}
        if "decisions" in props:  # полная корневая схема
            return LLMResult(payload={}, usage=LLMUsage(2, 3, 5, "actual"))
        return LLMResult(payload=_fill(schema), usage=LLMUsage(2, 3, 5, "actual"))


def test_provider_reactive_assembly_when_single_pass_invalid(monkeypatch) -> None:
    """Один проход вернул невалидное по схеме → реактивно собираем по частям,
    сохраняя контракт (а не «съезжая» на сырой ответ)."""
    # Отключаем проактивный режим, чтобы проверить именно реактивную ветку.
    monkeypatch.setenv("POV_COMPOSITIONAL_THRESHOLD", "100000")
    schema = _build_identification_schema()
    fake = _OnePassInvalidProvider()
    provider = CompositionalLLMProvider(fake)
    result = provider.chat_json(system_prompt="s", user_prompt="u", schema=schema)
    assert matches_schema(result.payload, schema)  # собрано валидно несмотря на провал прохода
    assert len(fake.calls) > 1  # один проход + фрагменты сборки


def test_flat_object_is_split_into_field_batches() -> None:
    """Плоский объект с многими полями (как артефактные схемы) дробится на БАТЧИ
    простых полей — иначе strict штормит на нём целиком. Каждый батч простой."""
    from pov_generator.infrastructure.llm.compositional.plan import ObjectPlan

    # 20 полей-массивов-строк (типично для артефакта вроде requirements_spec) —
    # плоский, без вложенных объектов: раньше это был один тяжёлый лист. Полей
    # заведомо больше бюджета батча → дробится на несколько.
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [f"f{i}" for i in range(20)],
        "properties": {f"f{i}": {"type": "array", "items": {"type": "string"}} for i in range(20)},
    }
    plan = SchemaTreeDecomposer().decompose(schema)
    assert isinstance(plan, ObjectPlan)
    assert len(plan.scalar_groups) >= 2, "20 полей должны разбиться на несколько батчей"
    # Каждый батч проще целого (ограничен бюджетом) — не «монстр» из всех полей.
    whole = schema_complexity(schema)
    assert all(schema_complexity(g) < whole for g in plan.scalar_groups)
    # Сборка фейком даёт валидное целое.
    fake = _FakeProvider()
    provider = CompositionalLLMProvider(fake)
    result = provider.chat_json(system_prompt="s", user_prompt="u", schema=schema)
    assert matches_schema(result.payload, schema)
    assert len(fake.calls) == len(plan.scalar_groups)  # по вызову на батч


def test_array_of_objects_expansion_policy() -> None:
    """Разворачиваем массив, только если элемент ГЕНУИННО тяжёл: вложенная
    структура внутри ИЛИ >4 полей. Небольшие плоские объекты — одним вызовом
    (снимает лишние per-item вызовы, скаляры strict держит надёжно)."""
    from pov_generator.infrastructure.llm.compositional.plan import ArrayPlan, LeafPlan

    def arr(item):
        return {"type": "array", "items": item}

    def obj(n, nested=False):
        props = {f"f{i}": {"type": "string"} for i in range(n)}
        if nested:
            props["sub"] = {"type": "array", "items": {"type": "string"}}
        return {"type": "object", "required": list(props), "properties": props}

    dec = SchemaTreeDecomposer()
    # Вложенная структура внутри элемента → разворачиваем.
    assert isinstance(dec.decompose(arr(obj(2, nested=True))), ArrayPlan)
    # Много полей (>4) → разворачиваем.
    assert isinstance(dec.decompose(arr(obj(5))), ArrayPlan)
    # Небольшой плоский объект (≤4 скаляр-поля) → один вызов на весь массив.
    assert isinstance(dec.decompose(arr(obj(3))), LeafPlan)
    assert isinstance(dec.decompose(arr(obj(2))), LeafPlan)


def test_provider_plain_mode_skips_decomposition() -> None:
    """В plain-режиме (ambient) декоратор НЕ декомпозирует даже сложную схему —
    один проход (форму добивает нормализация выше по конвейеру)."""
    from pov_generator.common.llm_modes import plain_json_scope

    schema = _build_identification_schema()  # заведомо сложная (см. тест complexity)
    fake = _FakeProvider()
    provider = CompositionalLLMProvider(fake)
    with plain_json_scope():
        result = provider.chat_json(system_prompt="s", user_prompt="u", schema=schema)
    assert len(fake.calls) == 1  # один проход, без фрагментов сборки
    assert isinstance(result.payload, dict)


# --- лёгкая валидация --------------------------------------------------------


def test_matches_schema_basics() -> None:
    s = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"},
         "n": {"type": "integer"}, "e": {"enum": ["x", "y"]}}}
    assert matches_schema({"a": "hi"}, s)
    assert not matches_schema({"n": 1}, s)            # нет required a
    assert not matches_schema({"a": 1}, s)            # a не строка
    assert not matches_schema({"a": "h", "e": "z"}, s)  # e вне enum
    assert not matches_schema("not-object", s)
