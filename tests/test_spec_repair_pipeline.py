"""P0-регресс: грязный payload ТЗ (requirements_spec) ДОЛЖЕН становиться валидным.

Это доказательство того, ради чего делались normalize_to_schema + self-repair:
огромный артефакт ТЗ под строгими вложенными схемами модель в один проход не
выдаёт идеально, и раньше единичный промах формы валил весь артефакт. Здесь мы
берём РЕАЛЬНУЮ схему requirements_spec (со всеми доменными паками), портим её
теми же способами, что были в инциденте, и проверяем, что цепочка
``normalize_to_schema`` → ``ExecutionService._repair_payload_to_schema`` приводит
её к нулю ошибок валидации.

Покрывает дыру, на которую указал ревью: вся машинерия чинилки раньше не была
покрыта ни одним тестом (runner гоняет stub с заведомо валидным payload, а
fake-LLM в per_stage-тестах тоже отдаёт валидное — чинилка работала вхолостую).
"""
from __future__ import annotations

import copy
from typing import Any

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    collect_schema_errors,
    normalize_to_schema,
)
from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.infrastructure.llm import LLMResult, LLMUsage
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

_DOMAIN_PACKS = (
    "frontend.web_workspace@1.0.0",
    "integration.enterprise_integration@1.0.0",
    "ml.predictive_analytics@1.0.0",
    "security.enterprise_compliance@1.0.0",
)


# --- схемо-зависимый генератор ВАЛИДНОГО значения --------------------------


def _valid_for_schema(schema: Any) -> Any:
    """Минимальное значение, проходящее validate_json_schema для данной схемы."""
    if not isinstance(schema, dict):
        return "значение"
    if "anyOf" in schema and schema["anyOf"]:
        return _valid_for_schema(schema["anyOf"][0])
    if schema.get("enum"):
        return schema["enum"][0]
    schema_type = schema.get("type")
    if schema_type == "object":
        return {key: _valid_for_schema(sub) for key, sub in (schema.get("properties") or {}).items()}
    if schema_type == "array":
        item = schema.get("items")
        return [_valid_for_schema(item)] if item is not None else []
    if schema_type == "number":
        return 1.0
    if schema_type == "integer":
        return 1
    if schema_type == "boolean":
        return True
    if schema_type == "null":
        return None
    return "значение"


def _valid_spec_payload() -> dict[str, Any]:
    schema = artifact_schema("requirements_spec", _DOMAIN_PACKS)
    payload = _valid_for_schema(schema)
    # sanity: чистый базовый payload действительно валиден
    assert collect_schema_errors(payload, schema) == []
    return payload


# --- фейковые LLM-провайдеры для self-repair -------------------------------


class _FixingLLM:
    """Чинит запрошенный фрагмент: возвращает валидное значение по sub-схеме.
    Имитирует модель, которая в repair-вызове переложила содержание в форму."""

    name = "claude_sdk"
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict) -> LLMResult:
        self.calls += 1
        return LLMResult(payload=_valid_for_schema(schema), usage=LLMUsage(1, 1, 2, "actual"))


class _UselessLLM:
    """Возвращает фрагмент, который НЕ уменьшает число ошибок (пустые объекты)."""

    name = "claude_sdk"
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict) -> LLMResult:
        self.calls += 1
        empty = {key: {} for key in (schema.get("properties") or {})}
        return LLMResult(payload=empty, usage=None)


class _RaisingLLM:
    name = "claude_sdk"
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict) -> LLMResult:
        self.calls += 1
        raise RuntimeError("repair LLM упал")


def _exec_service() -> ExecutionService:
    runtime = SqliteRuntime()
    return ExecutionService(runtime, ContextService(runtime))


# --- P0: грязный ТЗ → валидный --------------------------------------------


def test_malformed_spec_becomes_valid_via_normalize_then_repair() -> None:
    """Смесь дефектов: часть чинит детерминированная нормализация, остальное —
    точечный self-repair. На выходе — НОЛЬ ошибок по реальной схеме ТЗ."""
    schema = artifact_schema("requirements_spec", _DOMAIN_PACKS)
    payload = _valid_spec_payload()

    # --- дефекты, чинимые нормализацией (механика формы) ---
    payload["actors"] = [{"role": "Оператор"}, {"role": "Аналитик"}]  # объекты вместо строк
    payload["operating_model"] = {"кто": "эксплуатирует решение"}  # объект вместо списка
    payload["privacy_impact"] = {  # лишние ключи (другой вокабуляр)
        **payload["privacy_impact"],
        "data_categories": ["ПДн"],
        "residency": "РФ",
    }
    payload["integration_model"]["delivery_pattern"] = "разовая выгрузка"  # строка вместо списка

    # --- дефекты, требующие self-repair (нет обязательных ключей / не тот тип) ---
    payload["ml_requirements"] = {}  # пустой объект: нет обязательных ключей
    payload["frontend_requirements"] = ["роль1", "роль2"]  # список вместо объекта

    assert collect_schema_errors(payload, schema), "payload должен быть невалиден до починки"

    normalized = normalize_to_schema(payload, schema)
    llm = _FixingLLM()
    repaired, usages = _exec_service()._repair_payload_to_schema(
        llm=llm,
        payload=normalized,
        primary_schema=schema,
        artifact_role="requirements_spec",
    )

    assert collect_schema_errors(repaired, schema) == [], "после normalize+repair ошибок быть не должно"
    assert llm.calls >= 1, "self-repair обязан был вызваться (остаточные дефекты были)"
    assert usages, "usage repair-вызовов должен учитываться"


def test_mechanical_defects_fixed_by_normalize_alone_no_llm() -> None:
    """Если все дефекты механические — нормализация чинит их БЕЗ LLM (0 вызовов)."""
    schema = artifact_schema("requirements_spec", _DOMAIN_PACKS)
    payload = _valid_spec_payload()
    payload["actors"] = [{"role": "Оператор"}]  # объект вместо строки
    payload["operating_model"] = {"кто": "эксплуатирует"}  # объект вместо списка
    payload["integration_model"]["delivery_pattern"] = "разовая выгрузка"  # строка вместо списка

    normalized = normalize_to_schema(payload, schema)
    llm = _FixingLLM()
    repaired, usages = _exec_service()._repair_payload_to_schema(
        llm=llm,
        payload=normalized,
        primary_schema=schema,
        artifact_role="requirements_spec",
    )

    assert collect_schema_errors(repaired, schema) == []
    assert llm.calls == 0, "нормализация всё починила — LLM трогать не должны"
    assert usages == []


# --- юнит-поведение _repair_payload_to_schema ------------------------------

_SMALL_SCHEMA = {
    "type": "object",
    "required": ["a", "b"],
    "additionalProperties": False,
    "properties": {
        "a": {"type": "object", "required": ["x"], "additionalProperties": False,
              "properties": {"x": {"type": "string"}}},
        "b": {"type": "string"},
        "c": {"type": "string"},
    },
}


def test_repair_merges_only_failing_fields_and_keeps_rest() -> None:
    payload = {"a": {}, "b": "ок", "c": "не трогать"}  # падает только 'a'
    llm = _FixingLLM()
    repaired, _ = _exec_service()._repair_payload_to_schema(
        llm=llm, payload=payload, primary_schema=_SMALL_SCHEMA, artifact_role="t"
    )
    assert collect_schema_errors(repaired, _SMALL_SCHEMA) == []
    assert repaired["b"] == "ок" and repaired["c"] == "не трогать"  # нетронуты
    assert llm.calls == 1


def test_repair_rolls_back_when_fix_does_not_help() -> None:
    """Если фрагмент от LLM не уменьшил число ошибок — payload не меняется,
    цикл прекращается (не зацикливаемся, не делаем хуже)."""
    payload = {"a": {}, "b": "ок"}
    llm = _UselessLLM()
    repaired, _ = _exec_service()._repair_payload_to_schema(
        llm=llm, payload=payload, primary_schema=_SMALL_SCHEMA, artifact_role="t"
    )
    assert repaired == payload  # без изменений
    assert llm.calls == 1  # одна попытка, дальше не крутим


def test_repair_swallows_llm_error_without_failing_task() -> None:
    payload = {"a": {}, "b": "ок"}
    llm = _RaisingLLM()
    repaired, usages = _exec_service()._repair_payload_to_schema(
        llm=llm, payload=payload, primary_schema=_SMALL_SCHEMA, artifact_role="t"
    )
    assert repaired == payload  # вернулся исходный, без исключения
    assert usages == []


def test_repair_is_noop_on_valid_payload() -> None:
    payload = {"a": {"x": "y"}, "b": "ок"}
    llm = _FixingLLM()
    repaired, usages = _exec_service()._repair_payload_to_schema(
        llm=llm, payload=payload, primary_schema=_SMALL_SCHEMA, artifact_role="t"
    )
    assert repaired == payload
    assert llm.calls == 0 and usages == []


def test_repair_respects_max_iters() -> None:
    """Частичная починка за раз → не больше max_iters вызовов."""

    class _PartialLLM:
        name = "claude_sdk"
        model = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def chat_json(self, *, system_prompt, user_prompt, schema) -> LLMResult:
            self.calls += 1
            # Чинит только 'a', 'd' оставляет битым → ошибки уменьшаются, но не до нуля.
            props = schema.get("properties") or {}
            out = {}
            if "a" in props:
                out["a"] = {"x": "ok"}
            if "d" in props:
                out["d"] = {}  # остаётся невалидным
            return LLMResult(payload=out, usage=None)

    schema = copy.deepcopy(_SMALL_SCHEMA)
    schema["required"] = ["a", "b", "d"]
    schema["properties"]["d"] = {"type": "object", "required": ["z"],
                                  "additionalProperties": False, "properties": {"z": {"type": "string"}}}
    payload = {"a": {}, "b": "ок", "d": {}}
    llm = _PartialLLM()
    _repaired, _ = _exec_service()._repair_payload_to_schema(
        llm=llm, payload=payload, primary_schema=schema, artifact_role="t", max_iters=2
    )
    assert llm.calls <= 2
