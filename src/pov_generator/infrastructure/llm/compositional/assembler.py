"""Оркестратор сборки структуры по плану (:class:`StructuredAssembler`).

Рекурсивно исполняет :class:`FieldPlan`: на каждый узел делает простой
``chat_json`` по под-схеме фрагмента, валидирует фрагмент, при необходимости
точечно перезапрашивает ТОЛЬКО его, и детерминированно склеивает результат.
Возвращает значение по ИСХОДНОЙ схеме плюс список usage всех фрагментов (для
агрегирования вызывающим).

Согласованность между фрагментами обеспечивается:
* порядком (в :class:`ObjectPlan` структурные поля раньше скаляров — скаляр
  видит уже собранные списки в «скелете»);
* «скелетом целого» в промпте фрагмента (см. :mod:`prompt_builder`).

Текущая реализация — ПОСЛЕДОВАТЕЛЬНАЯ (корректность + работающая отмена через
ambient cancellation внутри ``chat_json``). Элементы массива независимы и в
принципе параллелизуемы — это отдельный шаг оптимизации (после замера
параллелизм-vs-prompt-кэш), сознательно не вносится сейчас.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..protocol import LLMProvider, LLMUsage
from ..structured_output import strip_nulls
from . import schema_utils as su
from .decomposer import DecompositionStrategy
from .plan import ArrayPlan, FieldPlan, JSONSchema, LeafPlan, ObjectPlan
from .prompt_builder import PLACEHOLDER, PartPromptBuilder
from .validation import matches_schema

_PLACEHOLDER_PENDING = "…"  # ещё не собранное поле в скелете (не текущий фокус)


def _part_retries() -> int:
    """Сколько раз точечно перезапросить фрагмент, не прошедший под-схему."""
    raw = os.environ.get("POV_COMPOSITIONAL_PART_RETRIES")
    if raw is None:
        return 1
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 1


@dataclass
class _Run:
    """Контекст одной сборки: общие промпты + накопитель usage."""

    base_system: str
    base_user: str
    usages: list[LLMUsage | None] = field(default_factory=list)


class StructuredAssembler:
    """Собирает значение по :class:`FieldPlan`, делая простые ``chat_json``."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        decomposer: DecompositionStrategy,
        prompt_builder: PartPromptBuilder | None = None,
    ) -> None:
        self._provider = provider
        self._decomposer = decomposer
        self._prompts = prompt_builder or PartPromptBuilder()

    def assemble(
        self, schema: JSONSchema, *, base_system: str, base_user: str
    ) -> tuple[Any, list[LLMUsage | None]]:
        """Собрать значение по ``schema`` целиком. Возвращает (value, usages)."""
        plan = self._decomposer.decompose(schema)
        run = _Run(base_system=base_system, base_user=base_user)
        value = self._build(plan, run, seed={}, label="итоговый объект", ancestor=None)
        return value, run.usages

    # --- рекурсивная сборка по типу узла ------------------------------------

    def _build(
        self, plan: FieldPlan, run: _Run, *, seed: dict[str, Any], label: str, ancestor: dict[str, Any] | None
    ) -> Any:
        if isinstance(plan, ObjectPlan):
            return self._build_object(plan, run, seed=seed, label=label, ancestor=ancestor)
        if isinstance(plan, ArrayPlan):
            return self._build_array(plan, run, label=label, ancestor=ancestor)
        if isinstance(plan, LeafPlan):
            return self._gen_value(
                plan.schema, run, skeleton={label: PLACEHOLDER}, label=label, seed=seed, ancestor=ancestor
            )
        raise TypeError(f"Неизвестный тип плана: {type(plan).__name__}")

    def _build_object(
        self, plan: ObjectPlan, run: _Run, *, seed: dict[str, Any], label: str, ancestor: dict[str, Any] | None
    ) -> dict[str, Any]:
        result: dict[str, Any] = dict(seed)  # ядро элемента (если есть) — стартовая часть
        # 1) структурные поля — каждое своим под-планом, в порядке. Под-полю
        #    отдаём снимок текущего объекта как родительский контекст.
        for name, sub_plan in plan.structural:
            child_ancestor = {**(ancestor or {}), **result}
            value = self._build(
                sub_plan, run, seed={}, label=f"{label} → «{name}»", ancestor=child_ancestor or None
            )
            result[name] = value
        # 2) простые поля — БАТЧАМИ, каждый отдельным вызовом ПОСЛЕ структурных
        #    (каждый батч видит уже собранное в скелете → согласованность).
        for index, group_schema in enumerate(plan.scalar_groups):
            focus = set(su.object_properties(group_schema).keys())
            skeleton = self._object_skeleton(plan.schema, result, focus=focus)
            group_label = (
                f"{label}: поля ({', '.join(sorted(focus))})"
                if len(plan.scalar_groups) > 1
                else f"{label}: основные поля"
            )
            part = self._gen_value(
                group_schema, run, skeleton=skeleton, label=group_label, seed={}, ancestor=ancestor
            )
            if isinstance(part, dict):
                result.update(part)
        return result

    def _build_array(
        self, plan: ArrayPlan, run: _Run, *, label: str, ancestor: dict[str, Any] | None
    ) -> list[Any]:
        # 1) каркас: сколько и какие элементы (только ядро).
        outline = self._gen_value(
            plan.outline_schema, run, skeleton={"items": [PLACEHOLDER]},
            label=f"{label}: каркас (сколько и какие)", seed={}, ancestor=ancestor,
        )
        cores = outline.get("items", []) if isinstance(outline, dict) else []
        # 2) наполнение каждого элемента его под-планом (ядро как seed; элементы
        #    наследуют родительский контекст массива).
        items: list[Any] = []
        for index, core in enumerate(cores):
            seed = core if isinstance(core, dict) else {}
            item = self._build(
                plan.item_plan, run, seed=seed, label=f"{label}[{index}]", ancestor=ancestor
            )
            items.append(item)
        return items

    # --- один вызов модели на фрагмент --------------------------------------

    def _gen_value(
        self, schema: JSONSchema, run: _Run, *, skeleton: Any, label: str,
        seed: dict[str, Any], ancestor: dict[str, Any] | None,
    ) -> Any:
        """Сгенерировать значение по под-схеме одним вызовом (+ точечный re-ask).

        Не-объектные под-схемы оборачиваем в ``{value: <schema>}`` — контракт
        ``LLMProvider.chat_json`` отдаёт JSON-объект, голый скаляр/массив так не
        получить."""
        if su.is_object_schema(schema):
            # Для объекта с ядром (seed) показываем уже известные поля и просим
            # достроить остальные — модель не тратит ход на повтор ядра.
            if seed:
                focus = {n for n in su.object_properties(schema) if n not in seed}
                skeleton = self._object_skeleton(schema, seed, focus=focus)
            obj = self._gen_object(schema, run, skeleton=skeleton, label=label, ancestor=ancestor)
            if seed:
                # Ядро (выбранное на каркасе) — авторитетно для идентичности
                # элемента: оно ПЕРЕБИВАЕТ возможные отклонения наполнения.
                return {**obj, **seed}
            return obj
        wrapper: JSONSchema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": schema},
        }
        wrapped = self._gen_object(wrapper, run, skeleton=skeleton, label=label, ancestor=ancestor)
        return wrapped.get("value")

    def _gen_object(
        self, schema: JSONSchema, run: _Run, *, skeleton: Any, label: str, ancestor: dict[str, Any] | None
    ) -> dict[str, Any]:
        system, user = self._prompts.build(
            base_system=run.base_system, base_user=run.base_user, focus_label=label,
            skeleton=skeleton, ancestor=ancestor,
        )
        payload: dict[str, Any] = {}
        for _ in range(_part_retries() + 1):
            result = self._provider.chat_json(system_prompt=system, user_prompt=user, schema=schema)
            run.usages.append(result.usage)
            payload = strip_nulls(result.payload) if isinstance(result.payload, dict) else {}
            if matches_schema(payload, schema):
                return payload
        # Последняя попытка как есть: финальную валидность целого проверит
        # вызывающий (validation_service); здесь — лучшее усилие по фрагменту.
        return payload

    # --- скелет целого ------------------------------------------------------

    def _object_skeleton(self, schema: JSONSchema, collected: dict[str, Any], *, focus: set[str]) -> dict[str, Any]:
        """Карта объекта для промпта: собранное → значения, фокус → плейсхолдер,
        прочее ещё не собранное → многоточие."""
        skeleton: dict[str, Any] = {}
        for name in su.object_properties(schema):
            if name in collected:
                skeleton[name] = collected[name]
            elif name in focus:
                skeleton[name] = PLACEHOLDER
            else:
                skeleton[name] = _PLACEHOLDER_PENDING
        return skeleton
