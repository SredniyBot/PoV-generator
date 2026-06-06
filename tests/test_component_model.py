"""Модель компонентов (Ф2 переработки архитектуры).

Проверяем три вещи:
1. stub-фикстура валидна по Python-схеме и НЕ даёт замечаний целостности
   (это эталон «хорошей» модели);
2. детерминированная проверка целостности ловит реальные дефекты
   (висячая ссылка, нереализованный интерфейс, нарушение правила зависимостей,
   цикл, раздувание количества) — и все находки неблокирующие;
3. рендер модели и карты развёртывания не падает и даёт ключевые секции.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    check_component_model_consistency,
    render_markdown,
    validate_json_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "templates" / "stub_fixtures" / "component_model.json"


def _good_model() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- 1. Фикстура — эталон ----------------------------------------------------


def test_fixture_matches_schema() -> None:
    validate_json_schema(_good_model(), artifact_schema("component_model"))


def test_fixture_has_no_consistency_issues() -> None:
    assert check_component_model_consistency(_good_model()) == []


# --- 2. Проверка ловит дефекты ----------------------------------------------


def test_detects_dangling_consumed_component() -> None:
    model = _good_model()
    model["components"][0]["consumed_interfaces"] = [
        {"component": "ghost", "interface": "x"}
    ]
    issues = check_component_model_consistency(model)
    assert any("ghost" in i for i in issues)


def test_detects_unrealized_provided_interface() -> None:
    model = _good_model()
    # убираем realizes у модуля, реализующего submitRequest
    for module in model["components"][0]["modules"]:
        module.pop("realizes", None)
    issues = check_component_model_consistency(model)
    assert any("submitRequest" in i for i in issues)


def test_detects_core_depends_outward() -> None:
    model = _good_model()
    # ingest делаем ядром, он потребляет store (infrastructure) — нарушение
    model["components"][0]["layer"] = "core"
    issues = check_component_model_consistency(model)
    assert any("ядро" in i for i in issues)


def test_service_to_datastore_is_not_flagged() -> None:
    # application → infrastructure (обычный сервис → хранилище) НЕ должно ругаться
    assert check_component_model_consistency(_good_model()) == []


def test_detects_dependency_cycle() -> None:
    model = _good_model()
    # store начинает зависеть от ingest → цикл ingest↔store
    model["components"][1]["consumed_interfaces"] = [
        {"component": "ingest", "interface": "submitRequest"}
    ]
    issues = check_component_model_consistency(model)
    assert any("цикл" in i.lower() for i in issues)


def test_detects_coverage_dangling_component() -> None:
    model = _good_model()
    model["coverage"]["actors"] = [{"actor": "manager", "components": ["nope"]}]
    issues = check_component_model_consistency(model)
    assert any("nope" in i for i in issues)


def test_detects_budget_overflow() -> None:
    model = _good_model()
    template = copy.deepcopy(model["components"][1])
    extra = []
    for n in range(11):
        clone = copy.deepcopy(template)
        clone["id"] = f"store{n}"
        extra.append(clone)
    model["components"] = [model["components"][0], *extra]
    issues = check_component_model_consistency(model)
    assert any("бюджет" in i.lower() for i in issues)


# --- 3. Рендер ---------------------------------------------------------------


def test_render_component_model_smoke() -> None:
    md = render_markdown("component_model", _good_model())
    assert "# Модель компонентов" in md
    assert "Приём заявок" in md
    assert "Предоставляет" in md
    assert "Внутреннее устройство" in md
    assert "## Покрытие" in md
    assert "```mermaid" in md  # диаграмма из блока diagrams


def test_render_deployment_map_smoke() -> None:
    payload = json.loads(
        (REPO_ROOT / "templates" / "stub_fixtures" / "deployment_map.json").read_text(
            encoding="utf-8"
        )
    )
    validate_json_schema(payload, artifact_schema("deployment_map"))
    md = render_markdown("deployment_map", payload)
    assert "# Карта развёртывания" in md
    assert "Основное приложение" in md
