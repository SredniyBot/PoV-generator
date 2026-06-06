"""Тесты Слоя 2: objective implementation + агентные executor'ы.

Проверяем:
* objective/задачи валидны; agent-leaf имеет executor=="agent" и
  резолвящийся agent_ref; цепочка архитектура → implementation объявлена;
* валидация: executor=agent без agent / с висячим agent_ref → невалидно;
* _render_agent_pledge несёт роль и cannot_do;
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
from pov_generator.application.execution_service import _render_agent_pledge
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


def test_implementation_objective_and_agent_executor() -> None:
    snapshot, report = _validate(REPO_ROOT / "templates")
    assert report.is_valid
    obj = snapshot.resolve_objective("implementation.build_plan@1.0.0")
    assert obj.root_task_ref.as_string() == "implementation.prepare_build_plan@1.0.0"
    leaf = snapshot.resolve_template("implementation.backend_build_spec@1.0.0")
    assert leaf.executor == "agent"
    assert leaf.agent_ref is not None
    assert leaf.agent_ref.as_string() == "agent.backend@1.0.0"
    assert snapshot.resolve_agent_capability(leaf.agent_ref).role == "backend"


def test_chain_from_architecture_to_implementation() -> None:
    snapshot, _ = _validate(REPO_ROOT / "templates")
    arch = snapshot.resolve_objective("architecture.system_design@1.0.0")
    nexts = {ref.as_string() for ref in arch.compatible_next_objectives}
    assert "implementation.build_plan@1.0.0" in nexts


def test_agent_executor_without_agent_ref_rejected(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    path = root / "tasks" / "implementation" / "backend_build_spec.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    del raw["agent"]
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _, report = _validate(root)
    assert not report.is_valid
    assert any("executor=agent" in issue.message for issue in report.errors)


def test_agent_executor_dangling_ref_rejected(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    path = root / "tasks" / "implementation" / "backend_build_spec.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["agent"] = "agent.nope@1.0.0"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _, report = _validate(root)
    assert not report.is_valid
    assert any("agent.nope@1.0.0" in issue.message for issue in report.errors)


def test_agent_pledge_includes_role_and_cannot_do() -> None:
    snapshot, _ = _validate(REPO_ROOT / "templates")
    spec = snapshot.resolve_agent_capability("agent.backend@1.0.0")
    pledge = _render_agent_pledge(spec)
    assert "agent.backend" in pledge
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
    assert "agent.backend" in md
    assert "agent.integration" in md


def test_build_spec_render_lists_components_and_out_of_scope() -> None:
    payload = json.loads((FIXTURES / "integration_build_spec.json").read_text(encoding="utf-8"))
    md = render_markdown("integration_build_spec", payload)
    assert "Коннектор обмена с 1С" in md
    assert "Вне зоны ответственности" in md
