"""Тесты рендеринга ТЗ (`requirements_spec`).

Фокус — на читаемости структуры:
* пользовательские сценарии выводятся честным нумерованным списком
  (каждый шаг с новой строки), а не «слипшимся» абзацем;
* рендерер устойчив к старым плоским строкам (legacy-данные);
* заголовки разделов — без декоративных эмодзи.
"""
from __future__ import annotations

from pov_generator.application.artifact_contracts import render_markdown

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
