"""Реквизиты — извлечение требуемых от пользователя входных данных.

Проверяем изолированный разбор артефакта реализуемости
(:func:`_extract_requisites`): берём только предусловия, блокеры исключаем,
дедуплицируем, кривые/пустые поля пропускаем.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.project_service import ProjectService
from pov_generator.application.workspace_query_service import _extract_gaps, _extract_requisites
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


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


# --- Зоны роста (пробелы в умениях) -----------------------------------------


def test_gaps_are_items_without_covered_by() -> None:
    payload = {
        "capabilities": [
            {"name": "CRUD-сервис", "covered_by": "capability.backend@1.0.0"},  # закрыто
            {"name": "Распознавание речи", "rationale": "нет такого умения"},   # пробел
        ]
    }
    gaps = _extract_gaps(payload)
    titles = [g.title for g in gaps]
    assert titles == ["Распознавание речи"]
    assert gaps[0].reason == "нет такого умения"


def test_gap_reason_falls_back_to_first_blocker() -> None:
    payload = {"capabilities": [{"name": "X", "blockers": ["слишком высокая точность"]}]}
    gaps = _extract_gaps(payload)
    assert gaps[0].reason == "слишком высокая точность"


def test_gaps_tolerate_malformed_payload() -> None:
    assert _extract_gaps({}) == ()
    assert _extract_gaps({"capabilities": [None, 7, {"covered_by": "x"}]}) == ()
    assert _extract_gaps({"capabilities": [{"name": ""}]}) == ()


# --- Ф2: модель реквизита (ключ-предусловие + consumer_ref) ------------------


def test_realizability_key_is_namespaced_and_consumer_empty() -> None:
    items = _extract_requisites(
        {"capabilities": [{"name": "Интеграция", "prerequisites": ["Доступ к 1С"]}]}
    )
    assert items[0].key == "realizability:Доступ к 1С"
    # У реализуемости потребитель ещё не известен — раннее предупреждение.
    assert items[0].consumer_ref == ""


def test_component_model_requisite_carries_consumer_ref() -> None:
    from pov_generator.application.workspace_query_service import (
        _extract_component_model_requisites,
    )

    items = _extract_component_model_requisites(
        {
            "components": [
                {
                    "id": "ingest",
                    "name": "Приём данных",
                    "requisites": [
                        {"id": "crm_creds", "title": "Доступ к CRM", "kind": "credential",
                         "blocking": True}
                    ],
                }
            ]
        }
    )
    assert items[0].key == "architecture:ingest:crm_creds"
    assert items[0].consumer_ref == "ingest"
    assert items[0].kind == "credential"
    assert items[0].blocking is True


# --- Ф2: предоставление реквизитов (round-trip структурного стора) -----------


def test_requisite_provision_round_trip(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    workspace = tmp_path / "ws"
    ProjectService(runtime).init_project(
        workspace=workspace,
        name="T",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="req",
        domain_packs=(),
    )
    assert runtime.list_requisite_provisions(workspace) == {}

    # Legacy-вызов (только note) → mode='reference', value пуст.
    runtime.mark_requisite_provided(workspace, requisite_key="Доступ к 1С", note="выдан")
    provisions = runtime.list_requisite_provisions(workspace)
    assert set(provisions) == {"Доступ к 1С"}
    assert provisions["Доступ к 1С"]["note"] == "выдан"
    assert provisions["Доступ к 1С"]["mode"] == "reference"
    assert provisions["Доступ к 1С"]["value"] == ""

    # idempotent + смена на структурный payload (mode=value).
    runtime.mark_requisite_provided(
        workspace, requisite_key="Доступ к 1С", note="", mode="value", value="42"
    )
    provisions = runtime.list_requisite_provisions(workspace)
    assert provisions["Доступ к 1С"]["mode"] == "value"
    assert provisions["Доступ к 1С"]["value"] == "42"
