"""AST-эвалюатор `if`-выражений в правилах methodology_pack (W1.1 / BACKLOG #7).

Раньше `_eval_rule` распознавал три hardcoded имени правила (`empty_goal`,
`ambiguous_choice`, `low_overall_confidence`). Любая новая методология
требовала правки Python — нарушение принципа «доменная расширяемость» из
spec/00. Теперь YAML-выражение парсится через `ast.parse(mode="eval")`,
проходит whitelist и резолвится по `stage_outputs`.

Поддерживаемая грамматика:

- литералы: числа, строки, `null` / `true` / `false` (как имена), а также
  Python-аналоги `None` / `True` / `False`;
- доступ к выходам стадий: `<stage_id>.<field>` — точка между точкой
  стадии и полем; bare-имя `<field>` ищется сначала в текущей стадии,
  потом во всех стадиях;
- проекция по списку: `path.field` на `list[dict]` возвращает список
  значений `field`. Опциональный «маркер» `[*]` (как в spec/02 § правила
  методологии) транслируется в эту же неявную проекцию (`path[*].field`
  трактуется как `path.field`);
- сравнения: `==`, `!=`, `<`, `<=`, `>`, `>=`;
- логика: `and`, `or`, `not`;
- арифметика: `+`, `-`, `*`, `/`, унарный `-`;
- функции whitelist: `len`, `max`, `min`, `sum`, `count` (синоним `len`),
  `second` (второй по убыванию), `is_null`.

Все остальные узлы AST приводят к «правило не сработало» (без падения).
Это сознательно: правило, которое нельзя надёжно посчитать, не должно
блокировать пайплайн — оно молча игнорируется и возвращает `False`.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Callable


_STAR_PROJECTION_RE = re.compile(r"\[\*\]")
_SPECIAL_LITERALS: dict[str, Any] = {"null": None, "true": True, "false": False}


class _RuleEvalError(Exception):
    """Внутренний сигнал «эту ветку нельзя посчитать». Перехватывается на
    верхнем уровне и приводит к `evaluate_rule -> False`."""


def evaluate_rule(
    expression: str | None,
    *,
    current_stage_outputs: dict[str, Any],
    all_stage_outputs: dict[str, dict[str, Any]],
) -> bool:
    """Возвращает True, если выражение сработало.

    Любая ошибка парсинга / неподдерживаемого узла / отсутствующего имени
    приводит к `False` (правило молча не срабатывает). Это нужно, чтобы
    неполный reasoning (нет одной из стадий) не валил workflow.
    """
    if not expression:
        return False
    cleaned = _STAR_PROJECTION_RE.sub("", expression).strip()
    if not cleaned:
        return False
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError:
        return False
    evaluator = _Evaluator(current_stage_outputs, all_stage_outputs)
    try:
        result = evaluator.visit(tree.body)
    except _RuleEvalError:
        return False
    except Exception:
        # Любая непредвиденная ошибка интерпретируется как «не сработало»,
        # но никогда не пробрасывается наружу: правила не должны падать.
        return False
    try:
        return bool(result)
    except Exception:
        return False


class _Evaluator:
    def __init__(
        self,
        current: dict[str, Any],
        all_outputs: dict[str, dict[str, Any]],
    ) -> None:
        self._current = current
        self._all = all_outputs
        self._functions: dict[str, Callable[..., Any]] = {
            "len": _safe_len,
            "count": _safe_len,
            "max": _safe_max,
            "min": _safe_min,
            "sum": _safe_sum,
            "second": _safe_second,
            "is_null": _is_null,
        }

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"_visit_{type(node).__name__}", None)
        if method is None:
            raise _RuleEvalError(f"unsupported node {type(node).__name__}")
        return method(node)

    def _visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _visit_Name(self, node: ast.Name) -> Any:
        if node.id in _SPECIAL_LITERALS:
            return _SPECIAL_LITERALS[node.id]
        if node.id in self._current:
            return self._current[node.id]
        if node.id in self._all:
            return self._all[node.id]
        raise _RuleEvalError(f"unknown name {node.id}")

    def _visit_Attribute(self, node: ast.Attribute) -> Any:
        obj = self.visit(node.value)
        return _attr_or_project(obj, node.attr)

    def _visit_Subscript(self, node: ast.Subscript) -> Any:
        container = self.visit(node.value)
        index = self.visit(node.slice)
        try:
            return container[index]
        except (TypeError, KeyError, IndexError) as exc:
            raise _RuleEvalError(f"bad subscript: {exc}") from exc

    def _visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise _RuleEvalError("call must be a plain name")
        fn = self._functions.get(node.func.id)
        if fn is None:
            raise _RuleEvalError(f"unknown function {node.func.id}")
        if node.keywords:
            raise _RuleEvalError("keyword arguments are not supported")
        args = [self.visit(arg) for arg in node.args]
        return fn(*args)

    def _visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise _RuleEvalError(f"unsupported unary op {type(node.op).__name__}")

    def _visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        raise _RuleEvalError(f"unsupported binary op {type(node.op).__name__}")

    def _visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            if not _apply_compare(op, left, right):
                return False
            left = right
        return True

    def _visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not self.visit(value):
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for value in node.values:
                if self.visit(value):
                    return True
            return False
        raise _RuleEvalError(f"unsupported bool op {type(node.op).__name__}")

    def _visit_List(self, node: ast.List) -> Any:
        return [self.visit(item) for item in node.elts]

    def _visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(item) for item in node.elts)


def _apply_compare(op: ast.AST, left: Any, right: Any) -> bool:
    try:
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
    except TypeError as exc:
        raise _RuleEvalError(f"comparison type error: {exc}") from exc
    raise _RuleEvalError(f"unsupported comparison op {type(op).__name__}")


def _attr_or_project(obj: Any, attr: str) -> Any:
    """Доступ `.attr` к dict ИЛИ list[dict]. На list — неявная проекция."""
    if isinstance(obj, dict):
        if attr not in obj:
            raise _RuleEvalError(f"missing field {attr}")
        return obj[attr]
    if isinstance(obj, list):
        result = []
        for item in obj:
            if isinstance(item, dict) and attr in item:
                result.append(item[attr])
        return result
    raise _RuleEvalError(f"cannot access .{attr} on {type(obj).__name__}")


# --- whitelisted functions --------------------------------------------------


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except TypeError as exc:
        raise _RuleEvalError(f"len() unsupported: {exc}") from exc


def _safe_max(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        try:
            return max(_filter_numeric(value))
        except ValueError as exc:
            raise _RuleEvalError(str(exc)) from exc
    raise _RuleEvalError("max() needs a non-empty list")


def _safe_min(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        try:
            return min(_filter_numeric(value))
        except ValueError as exc:
            raise _RuleEvalError(str(exc)) from exc
    raise _RuleEvalError("min() needs a non-empty list")


def _safe_sum(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return sum(_filter_numeric(value))
    raise _RuleEvalError("sum() needs a list")


def _safe_second(value: Any) -> Any:
    """Второй по убыванию элемент списка чисел. Если элементов меньше двух —
    правило не срабатывает (raise → evaluate_rule вернёт False)."""
    if isinstance(value, (list, tuple)):
        numbers = sorted(_filter_numeric(value), reverse=True)
        if len(numbers) >= 2:
            return numbers[1]
    raise _RuleEvalError("second() needs at least two numeric items")


def _is_null(value: Any) -> bool:
    return value is None


def _filter_numeric(values: Any) -> list[float]:
    result: list[float] = []
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result.append(float(value))
    if not result:
        raise _RuleEvalError("no numeric values")
    return result
