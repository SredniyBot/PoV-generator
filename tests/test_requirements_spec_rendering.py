"""Тесты рендеринга ТЗ (`requirements_spec`).

Фокус — на читаемости структуры:
* пользовательские сценарии выводятся честным нумерованным списком
  (каждый шаг с новой строки), а не «слипшимся» абзацем;
* рендерер устойчив к старым плоским строкам (legacy-данные);
* заголовки разделов — без декоративных эмодзи.
"""
from __future__ import annotations

import pytest

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    collect_schema_errors,
    normalize_to_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.common.errors import ValidationError

_EMOJI_SAMPLES = ("🎯", "👥", "📋", "🔌", "🛡", "🖥", "🤖", "🔒", "🏗", "🔐", "✅", "⛔", "🗓", "🧭", "💡", "⚠", "❓", "📖")


def _spec_with_scenarios() -> dict:
    return {
        "title": "Техническое задание",
        "business_goal": "Сократить ручную сверку реестра вагонов.",
        "user_stories": [
            {
                "actor": "Оформитель вагонов",
                "goal": "подтвердить корректность строки реестра за секунды вместо минут",
                "steps": [
                    "Получает email-уведомление о новой обработанной строке",
                    "Открывает посуточный реестр на сетевой шаре",
                    "Сверяет номера с исходным письмом",
                    "Проставляет флаг «подтверждено»",
                ],
            },
            # Legacy-форма: плоская строка должна по-прежнему рендериться.
            "Свободный сценарий без структуры шагов",
        ],
    }


def test_user_scenarios_render_as_numbered_list() -> None:
    md = render_markdown("requirements_spec", _spec_with_scenarios())

    # Подзаголовок сценария с ролью и номером.
    assert "### Сценарий 1. Оформитель вагонов" in md
    # Цель отдельной строкой.
    assert "**Цель.** подтвердить корректность строки реестра за секунды вместо минут" in md
    # Каждый шаг — отдельный пункт нумерованного списка (на своей строке).
    assert "\n1. Получает email-уведомление о новой обработанной строке" in md
    assert "\n2. Открывает посуточный реестр на сетевой шаре" in md
    assert "\n3. Сверяет номера с исходным письмом" in md
    assert "\n4. Проставляет флаг «подтверждено»" in md


def test_user_scenarios_tolerate_plain_string_items() -> None:
    md = render_markdown("requirements_spec", _spec_with_scenarios())
    assert "- Свободный сценарий без структуры шагов" in md


def test_requirements_spec_headers_have_no_emoji() -> None:
    md = render_markdown("requirements_spec", _spec_with_scenarios())
    assert "## Контекст и цель" in md
    assert "### Бизнес-цель" in md
    for emoji in _EMOJI_SAMPLES:
        assert emoji not in md, f"в документе остался эмодзи {emoji!r}"


def test_acceptance_criteria_require_method_and_render_it() -> None:
    """П.4: критерий приёмки — объект с ОБЯЗАТЕЛЬНЫМ методом проверки, чтобы
    «≥80%» не оставалось без «как меряется». Рендер несёт метод; legacy-строки
    всё ещё рендерятся."""
    schema = artifact_schema("requirements_spec", ())["properties"]["acceptance_criteria"]
    crit = [
        {
            "criterion": "Релевантность подсказки не ниже 80%",
            "verification_method": "Оценка аналитиком качества на выборке из ≥100 диалогов",
            "fit_criterion": "доля «релевантно» ≥ 80%",
        }
    ]
    validate_json_schema(crit, schema)  # объект с методом — валиден
    with pytest.raises(ValidationError):  # без метода — отклоняется (гарантия полноты)
        validate_json_schema([{"criterion": "≥80%"}], schema)

    md = render_markdown("requirements_spec", {"title": "ТЗ", "acceptance_criteria": crit})
    assert "Релевантность подсказки не ниже 80%" in md
    assert "Как проверяется:" in md
    # Устойчивость к старым плоским строкам.
    md_legacy = render_markdown("requirements_spec", {"title": "ТЗ", "acceptance_criteria": ["Старый критерий"]})
    assert "Старый критерий" in md_legacy


def _user_stories_schema() -> dict:
    return artifact_schema("requirements_spec", ())["properties"]["user_stories"]


def test_user_stories_validation_accepts_object_and_string_forms() -> None:
    """Регресс инцидента: гигантский артефакт ТЗ (45-76K токенов, 18 минут на
    попытку) детерминированно падал на `$.user_stories[0]: ожидался объект`,
    когда модель отдавала сценарии плоскими строками — и так на каждой попытке.
    Схема теперь, как и рендерер, принимает обе формы."""
    schema = _user_stories_schema()
    # Структурная форма — основная (даёт нумерованные шаги при рендере).
    validate_json_schema(
        [{"actor": "HR-аналитик", "goal": "видеть прогноз оттока", "steps": ["Открыть дашборд", "Выбрать отдел"]}],
        schema,
    )
    # Плоские строки — раньше валили весь артефакт, теперь допустимы.
    validate_json_schema(["Как аналитик, я хочу видеть прогноз оттока"], schema)
    # Смешанный массив (частый ответ модели) — тоже ок.
    validate_json_schema(
        [{"actor": "Менеджер", "goal": "согласовать выгрузку"}, "Свободный сценарий без шагов"],
        schema,
    )


def test_user_stories_validation_still_rejects_malformed_items() -> None:
    """anyOf не превращает поле в «что угодно»: объект с чужими ключами или без
    обязательного goal, а также не-строка/не-объект — по-прежнему отклоняются."""
    schema = _user_stories_schema()
    with pytest.raises(ValidationError):
        validate_json_schema([{"actor": "X", "goal": "Y", "посторонний": 1}], schema)
    with pytest.raises(ValidationError):
        validate_json_schema([{"actor": "X"}], schema)  # нет обязательного goal
    with pytest.raises(ValidationError):
        validate_json_schema([123], schema)  # не объект и не строка


def _phased_plan_schema() -> dict:
    return artifact_schema("requirements_spec", ())["properties"]["phased_plan"]


def test_phased_plan_validation_accepts_object_and_string_forms() -> None:
    """Тот же класс инцидента на следующем поле: после починки user_stories
    прогон ТЗ падал уже на `$.phased_plan[0]: ожидалась строка` (модель отдала
    фазы объектами). Схема теперь принимает и объект (предпочтительно), и строку."""
    schema = _phased_plan_schema()
    validate_json_schema(
        [{"phase": "Фаза 0", "objective": "Согласовать выгрузку", "milestone": "Допуск ИБ", "depends_on": []}],
        schema,
    )
    validate_json_schema(["Фаза 0. Подготовка и согласования."], schema)
    # без сроков/дат — но если модель добавит чужой ключ, это всё ещё ошибка.
    with pytest.raises(ValidationError):
        validate_json_schema([{"phase": "Ф", "duration_weeks": 2}], schema)


def test_phased_plan_renders_structured_blocks_and_tolerates_strings() -> None:
    md = render_markdown(
        "requirements_spec",
        {
            "title": "ТЗ",
            "business_goal": "Цель",
            "phased_plan": [
                {"phase": "Фаза 0. Подготовка", "objective": "Согласовать выгрузку",
                 "milestone": "Получен допуск ИБ", "depends_on": ["Юр. заключение"]},
                "Свободная фаза без структуры",
            ],
        },
    )
    assert "### Фаза 0. Подготовка" in md
    assert "**Цель.** Согласовать выгрузку" in md
    assert "**Контрольная точка.** Получен допуск ИБ" in md
    assert "**Зависит от:** Юр. заключение" in md
    assert "- Свободная фаза без структуры" in md


def test_renderer_does_not_crash_on_malformed_domain_fields() -> None:
    """Защита рендерера: доменные поля, пришедшие НЕ объектом (напр. списком
    строк — это и валило прежний прогон крашем `frontend.get` на list), не должны
    ронять рендер. Поле просто пропускается, документ строится."""
    md = render_markdown(
        "requirements_spec",
        {
            "title": "ТЗ",
            "business_goal": "Цель",
            "frontend_requirements": ["роняло раньше"],
            "ml_requirements": ["и это"],
            "integration_model": ["и это"],
            "security_constraints_detail": ["и это"],
            "privacy_impact": ["и это"],
        },
    )
    assert "# ТЗ" in md or "ТЗ" in md  # документ построен, без исключения


# --- детерминированная нормализация формы под схему ------------------------

_OBJ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
}


def test_normalize_drops_unknown_keys_in_strict_object() -> None:
    """privacy_impact падал на лишних ключах — нормализация их выкидывает."""
    out = normalize_to_schema({"name": "X", "лишний": 1, "tags": ["a"]}, _OBJ_SCHEMA)
    assert out == {"name": "X", "tags": ["a"]}
    assert collect_schema_errors(out, _OBJ_SCHEMA) == []


def test_normalize_coerces_scalar_and_dict_into_array() -> None:
    """delivery_pattern ждали списком, модель давала строку/объект."""
    arr = {"type": "array", "items": {"type": "string"}}
    assert normalize_to_schema("одна строка", arr) == ["одна строка"]
    # dict → ["ключ: значение", …] (operating_model объектом вместо списка)
    assert normalize_to_schema({"роль": "оператор", "sla": "8x5"}, arr) == [
        "роль: оператор",
        "sla: 8x5",
    ]


def test_normalize_stringifies_dict_and_list_where_string_expected() -> None:
    """actors[i] ждали строкой, модель давала объекты."""
    s = {"type": "string"}
    assert normalize_to_schema(["a", "b"], s) == "a; b"
    assert normalize_to_schema({"k": "v", "k2": "v2"}, s) == "k: v; k2: v2"


def test_normalize_array_items_dict_to_string() -> None:
    arr = {"type": "array", "items": {"type": "string"}}
    out = normalize_to_schema([{"actor": "HR", "goal": "видеть прогноз"}], arr)
    assert out == ["actor: HR; goal: видеть прогноз"]
    assert collect_schema_errors(out, arr) == []


def test_normalize_leaves_unrepairable_for_self_repair() -> None:
    """Объект вместо списка строк и пропуск обязательного ключа детерминированно
    не чинятся — нормализация их НЕ ломает дальше, оставляет self-repair'у."""
    # объект ждут, пришёл список — оставляем как есть (потом self-repair)
    out = normalize_to_schema(["строка"], _OBJ_SCHEMA)
    assert out == ["строка"]
    assert collect_schema_errors(out, _OBJ_SCHEMA)  # всё ещё невалидно


def test_normalize_idempotent_on_valid_payload() -> None:
    """На валидном payload нормализация — no-op (важно: stub/валидные ответы
    не должны меняться)."""
    valid = {"name": "X", "tags": ["a", "b"], "note": "n"}
    assert normalize_to_schema(valid, _OBJ_SCHEMA) == valid


def test_collect_schema_errors_returns_all_not_just_first() -> None:
    bad = {"tags": "не список", "note": 5}  # нет name, tags не список, note не строка
    errs = collect_schema_errors(bad, _OBJ_SCHEMA)
    assert len(errs) == 3
    joined = " | ".join(errs)
    assert "name" in joined and "tags" in joined and "note" in joined
