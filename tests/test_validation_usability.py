"""Тесты «usability-based» валидации (`_artifact_is_usable`).

Новый инвариант: задача `failed` означает «артефакт НЕПРИГОДЕН» (пустой или не
рендерится), а не «форма не совпала со строгой схемой». Строгое соответствие
схеме — НЕблокирующее замечание (см. validation_service / structured_output).
"""
from __future__ import annotations

from pov_generator.application.validation_service import _artifact_is_usable


def test_usable_when_has_content_and_renders() -> None:
    payload = {
        "title": "Техническое задание",
        "business_goal": "Сократить ручную сверку реестра.",
        "functional_requirements": ["Система импортирует реестр из почты."],
    }
    usable, reason = _artifact_is_usable("requirements_spec", payload)
    assert usable is True
    assert reason == ""


def test_usable_even_with_schema_drift() -> None:
    """Артефакт со структурным дрейфом (acceptance_criteria плоскими строками
    вместо объектов) ВСЁ РАВНО пригоден: рендерер толерантен. Строгая схема —
    отдельное (неблокирующее) замечание, не повод заваливать задачу."""
    payload = {
        "title": "ТЗ",
        "business_goal": "Цель.",
        # Намеренно «неправильная» форма для строгой схемы (строки вместо
        # объектов {criterion, verification_method}) — но рендерится.
        "acceptance_criteria": ["Релевантность не ниже 80%"],
    }
    usable, _ = _artifact_is_usable("requirements_spec", payload)
    assert usable is True


def test_unusable_when_empty() -> None:
    usable, reason = _artifact_is_usable("requirements_spec", {})
    assert usable is False
    assert "пуст" in reason.lower()


def test_unusable_when_only_meta() -> None:
    """Только метаданные (confidence/reasoning) — это не содержание."""
    usable, reason = _artifact_is_usable("requirements_spec", {"confidence": 0.5, "reasoning": "..."})
    assert usable is False
    assert "пуст" in reason.lower()


def test_unusable_when_not_object() -> None:
    usable, reason = _artifact_is_usable("requirements_spec", ["не", "объект"])
    assert usable is False
    assert reason
