"""Тесты Слоя 2: objective implementation + агентные executor'ы.

Проверяем:
* objective/задачи валидны; agent-leaf имеет executor=="agent" и
  резолвящийся capability_ref; цепочка архитектура → implementation объявлена;
* валидация: executor=agent без agent / с висячим capability_ref → невалидно;
* _render_capability_pledge несёт роль и cannot_do;
* схемы build_spec/build_plan принимают фикстуры; рендер плана показывает
  маршрутизацию часть→агент.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.application.execution_service import _render_capability_pledge
from pov_generator.application.registry_service import RegistryService
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "templates" / "stub_fixtures"


def _validate(root: Path):
    return RegistryService(FilesystemRegistryLoader(root)).validate()


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", root)
    return root


def test_implementation_objective_and_capability_executor() -> None:
    snapshot, report = _validate(REPO_ROOT / "templates")
    assert report.is_valid
    obj = snapshot.resolve_objective("implementation.build_plan@1.0.0")
    assert obj.root_task_ref.as_string() == "implementation.prepare_build_plan@1.0.0"
    leaf = snapshot.resolve_template("implementation.backend_build_spec@1.0.0")
    # Привязка к профилю умений ортогональна обычному executor=llm.
    assert leaf.executor == "llm"
    assert leaf.capability_ref is not None
    assert leaf.capability_ref.as_string() == "capability.backend@1.0.0"
    assert snapshot.resolve_capability_profile(leaf.capability_ref).role == "backend"


def test_chain_from_architecture_to_implementation() -> None:
    snapshot, _ = _validate(REPO_ROOT / "templates")
    arch = snapshot.resolve_objective("architecture.system_design@1.0.0")
    nexts = {ref.as_string() for ref in arch.compatible_next_objectives}
    # #1: план реализации стал первой подзадачей этапа «Реализация» —
    # после архитектуры идёт сразу realize (отдельного гейта плана нет).
    assert "implementation.realize@1.0.0" in nexts


def test_build_spec_without_capability_ref_is_valid(tmp_path: Path) -> None:
    # Привязка к профилю умений необязательна: задача без неё — обычный
    # executor=llm, и это не ошибка (старого правила «executor=agent требует
    # поле» больше нет).
    root = _copy(tmp_path)
    path = root / "tasks" / "implementation" / "backend_build_spec.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    del raw["capability"]
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _, report = _validate(root)
    assert report.is_valid


def test_dangling_capability_ref_rejected(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    path = root / "tasks" / "implementation" / "backend_build_spec.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["capability"] = "capability.nope@1.0.0"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _, report = _validate(root)
    assert not report.is_valid
    assert any("capability.nope@1.0.0" in issue.message for issue in report.errors)


def test_agent_pledge_includes_role_and_cannot_do() -> None:
    snapshot, _ = _validate(REPO_ROOT / "templates")
    spec = snapshot.resolve_capability_profile("capability.backend@1.0.0")
    pledge = _render_capability_pledge(spec)
    assert "capability.backend" in pledge
    assert "роль: backend" in pledge
    assert "НЕ делаешь" in pledge
    assert spec.cannot_do[0] in pledge


def test_build_spec_and_plan_schemas_accept_fixtures() -> None:
    for role in ("backend_build_spec", "integration_build_spec", "ui_build_spec", "build_plan"):
        payload = json.loads((FIXTURES / f"{role}.json").read_text(encoding="utf-8"))
        validate_json_schema(payload, artifact_schema(role))


def test_build_plan_render_shows_routing() -> None:
    payload = json.loads((FIXTURES / "build_plan.json").read_text(encoding="utf-8"))
    md = render_markdown("build_plan", payload)
    assert "Маршрутизация" in md
    assert "capability.backend" in md
    assert "capability.integration" in md


def test_build_spec_render_lists_components_and_out_of_scope() -> None:
    payload = json.loads((FIXTURES / "integration_build_spec.json").read_text(encoding="utf-8"))
    md = render_markdown("integration_build_spec", payload)
    assert "Коннектор обмена с 1С" in md
    assert "Вне зоны ответственности" in md
