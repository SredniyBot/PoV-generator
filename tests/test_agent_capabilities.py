"""Тесты реестра контрактов способностей агентов (kind: agent_capability).

Проверяем:
* контракты грузятся и реестр валиден; resolver работает;
* неизвестный capability id (вне taxonomy-вокабуляра) → ошибка валидации;
* висячий binds → предупреждение, но реестр остаётся валидным;
* парсер отклоняет неизвестные role/maturity.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from pov_generator.application.registry_service import RegistryService
from pov_generator.common.errors import ValidationError
from pov_generator.domain.registry import parse_agent_capability
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

REPO_ROOT = Path(__file__).resolve().parents[1]


def _validate(registry_root: Path):
    return RegistryService(FilesystemRegistryLoader(registry_root)).validate()


def _copy_templates(tmp_path: Path) -> Path:
    registry_root = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", registry_root)
    return registry_root


def test_agent_capabilities_load_and_validate() -> None:
    snapshot, report = _validate(REPO_ROOT / "templates")
    assert report.is_valid
    assert len(snapshot.agent_capabilities) >= 5
    backend = snapshot.resolve_agent_capability("agent.backend@1.0.0")
    assert backend.role == "backend"
    assert any(c.capability == "backend.rest_api" for c in backend.capabilities)
    # все capability id из контрактов покрыты taxonomy-вокабуляром
    for spec in snapshot.agent_capabilities.values():
        for cap in spec.capabilities:
            assert snapshot.has_vocabulary_entry("agent_capabilities", cap.capability)


def test_unknown_capability_id_rejected(tmp_path: Path) -> None:
    registry_root = _copy_templates(tmp_path)
    path = registry_root / "agents" / "backend.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["capabilities"][0]["capability"] = "backend.bogus"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    _, report = _validate(registry_root)
    assert not report.is_valid
    assert any("backend.bogus" in issue.message for issue in report.errors)


def test_dangling_binds_is_warning_not_error(tmp_path: Path) -> None:
    registry_root = _copy_templates(tmp_path)
    path = registry_root / "agents" / "ml.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["binds"] = "nope.x@1.0.0"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    _, report = _validate(registry_root)
    assert report.is_valid  # это предупреждение, а не ошибка
    assert any("nope.x@1.0.0" in issue.message for issue in report.warnings)


def test_parser_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        parse_agent_capability(
            {
                "id": "agent.x",
                "version": "1.0.0",
                "title": "X",
                "role": "wizard",
                "capabilities": [],
                "cannot_do": [],
            },
            Path("x.yaml"),
        )


def test_parser_rejects_unknown_maturity() -> None:
    with pytest.raises(ValidationError):
        parse_agent_capability(
            {
                "id": "agent.x",
                "version": "1.0.0",
                "title": "X",
                "role": "backend",
                "capabilities": [{"capability": "backend.rest_api", "maturity": "godlike"}],
                "cannot_do": [],
            },
            Path("x.yaml"),
        )
