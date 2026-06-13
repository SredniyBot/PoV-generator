"""Декомпозиция JSON-схемы в :class:`FieldPlan` (как собирать по частям).

Стратегия (см. :class:`SchemaTreeDecomposer`):

* Объект разлагается, ТОЛЬКО если у него есть хотя бы одно «структурно сложное»
  поле (вложенный объект/массив, который сам разложился). Иначе — :class:`LeafPlan`
  (один вызов). Простые поля объекта собираются в одну скаляр-группу и
  генерируются ПОСЛЕ сложных — чтобы скаляр, ссылающийся на список, видел его.
* Массив объектов разлагается на «каркас → наполнение», но ТОЛЬКО если его
  элемент сам сложен (есть что декомпозировать внутри). Массив простых элементов
  отдаётся одним :class:`LeafPlan` — дробить незачем.

Декомпозер — чистая функция над схемой (без I/O, без LLM). За интерфейсом
:class:`DecompositionStrategy` — можно подменить стратегию, не трогая ассемблер
(Open/Closed).
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from . import schema_utils as su
from .complexity import schema_complexity
from .plan import ArrayPlan, FieldPlan, JSONSchema, LeafPlan, ObjectPlan

# Сколько полей элемента берём в «ядро» каркаса массива (опознавательные:
# обычно title/category). Каркас должен быть лёгким — детали добьёт наполнение.
_MAX_OUTLINE_CORE_FIELDS = 3
# Токены имён, выдающие «опознавательное» поле (годится в каркас). Сравнение
# по токенам snake_case (а не подстроке), чтобы напр. ``proposed_option_id`` НЕ
# попало из-за «id» — оно зависимое, не идентификатор.
_IDENTITY_TOKENS = frozenset({"title", "name", "label", "category", "type", "kind"})
# Подсказки «это длинный текст» — такие поля в лёгкий каркас не берём.
_LONG_TEXT_TOKENS = frozenset(
    {"description", "rationale", "detail", "details", "summary", "text", "body",
     "content", "explanation", "justification", "notes"}
)


def _name_tokens(name: str) -> set[str]:
    return set(name.lower().split("_"))
# Дефолтный потолок числа элементов массива, если в схеме не задан maxItems.
_DEFAULT_ARRAY_MAX = 8
# Бюджет сложности на ОДИН батч простых полей объекта. Плоский объект с многими
# полями режем на батчи в пределах бюджета. Компромисс: крупнее батч → меньше
# вызовов (важно для лимитов подписки), но ближе к «линии шторма» strict. 10 ≈
# объект из ~8 простых полей. Переопределяется POV_COMPOSITIONAL_BATCH_BUDGET.
_DEFAULT_SCALAR_GROUP_BUDGET = 10


def _scalar_group_budget() -> int:
    raw = os.environ.get("POV_COMPOSITIONAL_BATCH_BUDGET")
    if raw is None:
        return _DEFAULT_SCALAR_GROUP_BUDGET
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SCALAR_GROUP_BUDGET
    return value if value > 0 else _DEFAULT_SCALAR_GROUP_BUDGET


@runtime_checkable
class DecompositionStrategy(Protocol):
    """Контракт стратегии декомпозиции: схема → план сборки."""

    def decompose(self, schema: JSONSchema) -> FieldPlan: ...


class SchemaTreeDecomposer:
    """Декомпозиция спуском по дереву схемы (см. модульный docstring)."""

    def decompose(self, schema: JSONSchema) -> FieldPlan:
        if su.is_object_schema(schema):
            return self._object(schema)
        if su.array_of_objects(schema):
            return self._array(schema)
        return LeafPlan(schema)

    # --- object -------------------------------------------------------------

    def _object(self, schema: JSONSchema) -> FieldPlan:
        structural: list[tuple[str, FieldPlan]] = []
        simple_fields: dict[str, JSONSchema] = {}
        for name, sub in su.object_properties(schema).items():
            sub_plan = self.decompose(sub)
            if isinstance(sub_plan, LeafPlan):
                # Простое поле (скаляр / массив скаляров / простой объект) —
                # пойдёт в батчи простых полей.
                simple_fields[name] = sub
            else:
                structural.append((name, sub_plan))
        groups = self._batch_fields(schema, simple_fields)
        # Нечего дробить: нет структурных полей И простые влезают в один батч —
        # отдаём весь объект одним проходом (strict его возьмёт).
        if not structural and len(groups) <= 1:
            return LeafPlan(schema)
        return ObjectPlan(schema=schema, structural=tuple(structural), scalar_groups=tuple(groups))

    def _batch_fields(
        self, parent: JSONSchema, fields: dict[str, JSONSchema]
    ) -> list[JSONSchema]:
        """Разбить простые поля объекта на батчи в пределах бюджета сложности.

        Жадно копим поля, пока суммарная сложность батча не превысит бюджет —
        тогда начинаем новый. Так каждый батч остаётся достаточно простым, чтобы
        strict взял его одним ходом. Поле, само превышающее бюджет, занимает
        отдельный батч."""
        budget = _scalar_group_budget()
        groups: list[JSONSchema] = []
        current: dict[str, JSONSchema] = {}
        current_cx = 0
        for name, sub in fields.items():
            field_cx = schema_complexity(sub) + 1  # +1 за само поле
            if current and current_cx + field_cx > budget:
                groups.append(self._subset_schema(parent, current))
                current, current_cx = {}, 0
            current[name] = sub
            current_cx += field_cx
        if current:
            groups.append(self._subset_schema(parent, current))
        return groups

    # --- array of objects ---------------------------------------------------

    def _array(self, schema: JSONSchema) -> FieldPlan:
        item = su.array_item_schema(schema)
        assert item is not None  # гарантировано array_of_objects
        if not self._item_worth_expanding(item):
            # Тривиальный элемент (пара скаляров: {label,description},
            # {term,definition}) — дробить массив незачем, модель отдаёт такой
            # список за один проход.
            return LeafPlan(schema)
        # Многопольный/вложенный элемент: разворачиваем «каркас → наполнение»,
        # фрагмент на элемент (иначе весь массив таких объектов тяжёл для strict).
        return ArrayPlan(
            schema=schema,
            outline_schema=self._outline_schema(schema, item),
            item_plan=self.decompose(item),
        )

    @staticmethod
    def _item_worth_expanding(item: JSONSchema) -> bool:
        """Стоит ли разворачивать массив таких элементов по фрагменту на элемент.

        Тривиальная пара скаляров (≤2 скаляр-поля: {label,description},
        {term,definition}) — нет (массив берётся одним ходом). Многопольный
        объект (>2 поля) ИЛИ объект с вложенной структурой — да (иначе весь
        массив тяжёл для strict-coercion)."""
        props = su.object_properties(item)
        if len(props) > 2:
            return True
        return any(not su.is_scalar_schema(sub) for sub in props.values())

    # --- helpers ------------------------------------------------------------

    def _subset_schema(self, parent: JSONSchema, fields: dict[str, JSONSchema]) -> JSONSchema:
        """Под-схема ``object`` из подмножества полей родителя.

        Сохраняем ``required`` только для включённых полей и их описания
        (guidance для модели). ``additionalProperties: False`` — строгая форма."""
        keep_required = [f for f in su.required_fields(parent) if f in fields]
        out: JSONSchema = {
            "type": "object",
            "additionalProperties": False,
            "properties": dict(fields),
        }
        if keep_required:
            out["required"] = keep_required
        return out

    def _outline_schema(self, array_schema: JSONSchema, item: JSONSchema) -> JSONSchema:
        """Под-схема ответа-каркаса массива: ``{items: [<ядро элемента>]}``."""
        core_names = self._core_fields(item)
        item_props = su.object_properties(item)
        core_schema: JSONSchema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {n: item_props[n] for n in core_names},
        }
        if core_names:
            core_schema["required"] = list(core_names)
        items_schema: JSONSchema = {"type": "array", "items": core_schema}
        max_items = array_schema.get("maxItems", _DEFAULT_ARRAY_MAX)
        if isinstance(max_items, int):
            items_schema["maxItems"] = max_items
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {"items": items_schema},
        }

    def _core_fields(self, item: JSONSchema) -> tuple[str, ...]:
        """«Опознавательные» поля элемента для каркаса (по которым модель решает
        «сколько и какие элементы»).

        Приоритет: явные идентификаторы (title/category/…), затем — короткие
        required-скаляры (без длинных текстов и зависимых полей). В каркас НЕ
        берём description/rationale (тяжёлые) и зависимые поля вроде
        ``proposed_option_id`` (их добьёт наполнение, видя уже собранное).
        Минимум одно поле — иначе каркас бессмыслен."""
        props = su.object_properties(item)
        scalars = [n for n, s in props.items() if su.is_scalar_schema(s)]
        required = set(su.required_fields(item))

        identity = [n for n in scalars if _name_tokens(n) & _IDENTITY_TOKENS]
        if identity:
            # Идентификаторы, которые ещё и обязательны, — в первую очередь.
            ordered = [n for n in identity if n in required] or identity
            return tuple(ordered[:_MAX_OUTLINE_CORE_FIELDS])

        short_required = [
            n for n in scalars if n in required and not (_name_tokens(n) & _LONG_TEXT_TOKENS)
        ]
        fallback = short_required or [n for n in scalars if n in required] or scalars
        return tuple(fallback[:1])
