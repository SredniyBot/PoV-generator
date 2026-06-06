"""Реквизиты — извлечение требуемых от пользователя входных данных.

Проверяем изолированный разбор артефакта реализуемости
(:func:`_extract_requisites`): берём только предусловия, блокеры исключаем,
дедуплицируем, кривые/пустые поля пропускаем.
"""

from __future__ import annotations

from pov_generator.application.workspace_query_service import _extract_requisites


def test_extracts_prerequisites_with_needed_for() -> None:
    payload = {
        "capabilities": [
            {
                "name": "Онлайн-интеграция с 1С",
                "prerequisites": ["Доступ к тестовому контуру 1С", "Формат обмена"],
                "blockers": ["Нет подтверждённого доступа"],
            }
        ]
    }
    items = _extract_requisites(payload)
    titles = [i.title for i in items]
    assert "Доступ к тестовому контуру 1С" in titles
    assert "Формат обмена" in titles
    # Блокеры — это причины невыполнимости, не запрос данных.
    assert "Нет подтверждённого доступа" not in titles
    assert all(i.status == "requested" for i in items)
    assert items[0].needed_for == "Онлайн-интеграция с 1С"


def test_dedupes_same_prerequisite_across_capabilities() -> None:
    payload = {
        "capabilities": [
            {"name": "A", "prerequisites": ["Доступ к данным"]},
            {"name": "B", "prerequisites": ["доступ к данным"]},  # тот же, иной регистр
        ]
    }
    items = _extract_requisites(payload)
    assert len(items) == 1  # дубль схлопнут


def test_tolerates_malformed_payload() -> None:
    assert _extract_requisites({}) == ()
    assert _extract_requisites({"capabilities": "не список"}) == ()
    assert _extract_requisites({"capabilities": [None, 42, {}]}) == ()
    # Пустые/пробельные предусловия пропускаются.
    assert _extract_requisites({"capabilities": [{"prerequisites": ["", "  "]}]}) == ()


def test_missing_name_falls_back_to_project() -> None:
    items = _extract_requisites({"capabilities": [{"prerequisites": ["Значение тайм-аута"]}]})
    assert len(items) == 1
    assert items[0].needed_for == "проект"
