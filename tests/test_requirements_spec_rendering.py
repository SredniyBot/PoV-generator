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
