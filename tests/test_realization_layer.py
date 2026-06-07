"""Ф8b — цикл «спека → реализация»: harness-узлы реализуют компоненты.

Проверяем:
* stub-harness отдаёт бандл реализации компонента (фикстура-каталог);
* схема realization_index принимает фикстуру, рендер даёт таблицу компонентов;
* реестр валиден и резолвит objective/композит/веер/harness-лист реализации;
* harness-лист — executor=harness, bundle-выход, с гейтами готовности.
"""

from __future__ import annotations

import json
from pathlib import Path

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    render_markdown,
    validate_json_schema,
)
from pov_generator.application.registry_service import RegistryService
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.harness import ExpectedArtifact, HarnessRunSpec
from pov_generator.infrastructure.harness.providers.stub import StubHarnessProvider

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "templates" / "stub_fixtures"


def test_stub_harness_returns_component_implementation_bundle() -> None:
    provider = StubHarnessProvider()
    spec = HarnessRunSpec(
        brief="реализуй компонент",
        expected_artifacts=(
            ExpectedArtifact(role="component_implementation", fmt="files"),
        ),
    )
    result = provider.run(spec)
    assert result.status == "completed"
    art = result.artifacts[0]
    assert art.files is not None
    assert "src/main.py" in art.files
    assert "README.md" in art.files
    assert b"main" in art.files["src/main.py"]


def test_realization_index_schema_and_render() -> None:
    payload = json.loads((FIXTURES / "realization_index.json").read_text(encoding="utf-8"))
    validate_json_schema(payload, artifact_schema("realization_index"))

    md = render_markdown("realization_index", payload)
    assert "Сводка реализации" in md
    assert "Компоненты" in md
    assert "Приём заявок" in md


def test_realize_objective_graph_resolves() -> None:
    snapshot, report = RegistryService(
        FilesystemRegistryLoader(REPO_ROOT / "templates")
    ).validate()
    assert report.is_valid

    from pov_generator.domain.registry import ObjectRef

    objective = snapshot.resolve_objective(ObjectRef.parse("implementation.realize@1.0.0"))
    assert objective.root_task_ref.as_string() == "implementation.realize_system@1.0.0"

    root = snapshot.resolve_template("implementation.realize_system@1.0.0")
    assert root.template_type == "composite"

    impl = snapshot.resolve_template("implementation.component_implementation@1.0.0")
    assert impl.executor == "harness"
    assert impl.harness_output == "bundle"
    assert len(impl.harness_gates) >= 1
