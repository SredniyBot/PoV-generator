"""Тесты артефактного графа (Этап 1 roadmap).

Покрывает:
    * Структуру ``ArtifactRecord`` с встроенными :class:`ArtifactMetadata`
      и :class:`ArtifactRelations`.
    * Сериализацию/десериализацию метаинформации через runtime.
    * Графовые обходы: ``downstream_artifacts`` / ``upstream_artifacts`` /
      ``artifacts_using_position``.
    * Инвариант ``used_position_ids`` (Этап 1.4): идентификаторы положений,
      использованных при сборке контекста, попадают в метадату артефакта.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.domain.artifacts import (
    ArtifactMetadata,
    ArtifactRecord,
    ArtifactRelations,
)
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

# --- 1. Структура ArtifactRecord -------------------------------------------


def test_artifact_record_defaults_to_empty_metadata_and_relations() -> None:
    artifact = ArtifactRecord(
        artifact_id="art-1",
        project_id="proj-1",
        artifact_role="goal_hypothesis",
        title="t",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=None,
        storage_path="artifacts/art-1.json",
        created_at="2026-05-13T10:00:00+00:00",
    )
    assert artifact.relations.parent_artifact_id is None
    assert artifact.relations.input_artifact_ids == ()
    assert artifact.relations.child_artifact_ids == ()
    assert artifact.metadata.reasoning == {}
    assert artifact.metadata.methodology_trace == {}
    assert artifact.metadata.used_position_ids == ()


def test_artifact_metadata_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="overall_confidence"):
        ArtifactMetadata(overall_confidence=1.5)


# --- 2. Round-trip через SQLite runtime ------------------------------------


def _make_artifact(
    artifact_id: str,
    *,
    role: str = "goal_hypothesis",
    inputs: tuple[str, ...] = (),
    children: tuple[str, ...] = (),
    used_positions: tuple[str, ...] = (),
    reasoning: dict | None = None,
    trace: dict | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project_id="proj-graph",
        artifact_role=role,
        title=f"{role} for {artifact_id}",
        description=None,
        artifact_format="json",
        artifact_kind="primary",
        created_by_task_id=f"task-{artifact_id}",
        storage_path=f"artifacts/{artifact_id}.json",
        created_at="2026-05-13T10:00:00+00:00",
        relations=ArtifactRelations(
            input_artifact_ids=inputs,
            child_artifact_ids=children,
        ),
        metadata=ArtifactMetadata(
            template_ref=f"common.{role}@1.0.0",
            reasoning=reasoning or {},
            methodology_trace=trace or {},
            used_position_ids=used_positions,
        ),
    )


def test_metadata_and_relations_survive_persistence_round_trip(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    workspace = tmp_path / "case"

    artifact = _make_artifact(
        "art-1",
        used_positions=("project.goal", "fact.x"),
        reasoning={"stages": [{"stage_id": "goal_framing", "outputs": {}}]},
        trace={"stages_executed": ["goal_framing"]},
    )
    runtime.store_artifact(workspace, artifact=artifact, content="{}")

    loaded = runtime.load_artifact(workspace, "art-1")
    assert loaded.metadata.used_position_ids == ("project.goal", "fact.x")
    assert loaded.metadata.reasoning == {
        "stages": [{"stage_id": "goal_framing", "outputs": {}}]
    }
    assert loaded.metadata.methodology_trace == {"stages_executed": ["goal_framing"]}


# --- 3. Графовые обходы ------------------------------------------------------


def test_downstream_artifacts_finds_transitive_consumers(tmp_path: Path) -> None:
    """A → B → C: downstream(A) должен включать и B, и C."""
    runtime = SqliteRuntime()
    workspace = tmp_path / "case"

    a = _make_artifact("A")
    b = _make_artifact("B", inputs=("A",))
    c = _make_artifact("C", inputs=("B",))

    runtime.store_artifact(workspace, artifact=a, content="{}")
    runtime.store_artifact(workspace, artifact=b, content="{}")
    runtime.store_artifact(workspace, artifact=c, content="{}")

    downstream_ids = {item.artifact_id for item in runtime.downstream_artifacts(workspace, "A")}
    assert downstream_ids == {"B", "C"}

    # У C нет downstream'а.
    assert runtime.downstream_artifacts(workspace, "C") == []


def test_upstream_artifacts_traverses_input_chain(tmp_path: Path) -> None:
    """upstream(C) при A→B→C должен включать A и B."""
    runtime = SqliteRuntime()
    workspace = tmp_path / "case"

    a = _make_artifact("A")
    b = _make_artifact("B", inputs=("A",))
    c = _make_artifact("C", inputs=("B",))

    runtime.store_artifact(workspace, artifact=a, content="{}")
    runtime.store_artifact(workspace, artifact=b, content="{}")
    runtime.store_artifact(workspace, artifact=c, content="{}")

    upstream_ids = {item.artifact_id for item in runtime.upstream_artifacts(workspace, "C")}
    assert upstream_ids == {"A", "B"}


def test_downstream_with_diamond_dependency_does_not_duplicate(tmp_path: Path) -> None:
    """Diamond: A→B, A→C, B→D, C→D — downstream(A) включает B, C, D ровно по разу."""
    runtime = SqliteRuntime()
    workspace = tmp_path / "case"

    runtime.store_artifact(workspace, artifact=_make_artifact("A"), content="{}")
    runtime.store_artifact(workspace, artifact=_make_artifact("B", inputs=("A",)), content="{}")
    runtime.store_artifact(workspace, artifact=_make_artifact("C", inputs=("A",)), content="{}")
    runtime.store_artifact(
        workspace, artifact=_make_artifact("D", inputs=("B", "C")), content="{}"
    )

    downstream = runtime.downstream_artifacts(workspace, "A")
    ids = [item.artifact_id for item in downstream]
    assert sorted(ids) == ["B", "C", "D"]
    assert len(ids) == 3, "diamond не должен порождать дубликаты"


def test_artifacts_using_position_returns_only_active_users(tmp_path: Path) -> None:
    runtime = SqliteRuntime()
    workspace = tmp_path / "case"

    using_goal = _make_artifact("uses-goal", used_positions=("project.goal",))
    using_other = _make_artifact("uses-other", used_positions=("fact.x",))
    runtime.store_artifact(workspace, artifact=using_goal, content="{}")
    runtime.store_artifact(workspace, artifact=using_other, content="{}")

    result = runtime.artifacts_using_position(workspace, "project.goal")
    assert {item.artifact_id for item in result} == {"uses-goal"}

    # После пометки superseded — артефакт не должен возвращаться (это
    # старая версия, новые потомки её не «затронут» при оспаривании).
    runtime.mark_artifact_superseded(workspace, "uses-goal")
    assert runtime.artifacts_using_position(workspace, "project.goal") == []


# --- 3b. Снятие согласования при архивации/замене версии --------------------


def test_signoff_cleared_when_artifact_superseded(tmp_path: Path) -> None:
    """Замена версии снимает sign-off: аппрув относится к конкретной версии,
    а не к роли. Иначе устаревшая, но согласованная версия держала бы гейт."""
    runtime = SqliteRuntime()
    workspace = tmp_path / "case"
    runtime.store_artifact(workspace, artifact=_make_artifact("art-sup"), content="{}")
    runtime.mark_artifact_signed_off(
        workspace, "art-sup", signed_off=True, signed_off_at="2026-06-09T10:00:00+00:00"
    )
    assert runtime.load_artifact(workspace, "art-sup").signed_off is True

    runtime.mark_artifact_superseded(workspace, "art-sup")
    reloaded = runtime.load_artifact(workspace, "art-sup")
    assert reloaded.is_superseded is True
    assert reloaded.signed_off is False
    assert reloaded.signed_off_at is None


def test_signoff_cleared_when_artifact_rolled_back(tmp_path: Path) -> None:
    """Откат архивирует артефакт и снимает с него sign-off: согласованного
    документа в активном состоянии больше нет → гейт не должен числиться пройден."""
    runtime = SqliteRuntime()
    workspace = tmp_path / "case"
    # _make_artifact задаёт created_by_task_id = "task-<id>".
    runtime.store_artifact(workspace, artifact=_make_artifact("art-rb"), content="{}")
    runtime.mark_artifact_signed_off(
        workspace, "art-rb", signed_off=True, signed_off_at="2026-06-09T10:00:00+00:00"
    )

    archived = runtime.archive_artifacts_for_tasks(workspace, ("task-art-rb",), "rb-1")
    assert archived == ["art-rb"]

    # Из активных исключён; в архиве виден, но уже без аппрува.
    assert all(a.artifact_id != "art-rb" for a in runtime.list_artifacts(workspace))
    archived_rec = next(
        a
        for a in runtime.list_artifacts(workspace, include_rolled_back=True)
        if a.artifact_id == "art-rb"
    )
    assert archived_rec.rolled_back_by == "rb-1"
    assert archived_rec.signed_off is False
    assert archived_rec.signed_off_at is None


# --- 4. Интеграционный тест: used_position_ids проросло из контекста --------


def test_artifact_records_used_positions_after_execution(tmp_path: Path) -> None:
    """После execute_task primary артефакт должен иметь used_position_ids,
    отражающие положения, которые попали в контекст задачи (Этап 1.4)."""
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.application.planning_service import PlanningService
    from pov_generator.application.project_service import ProjectService
    from pov_generator.application.registry_service import RegistryService
    from pov_generator.domain.registry import ObjectRef
    from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

    REPO_ROOT = Path(__file__).resolve().parents[1]
    runtime = SqliteRuntime()
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)

    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="graph-test",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Тестовый бизнес-запрос для проверки used_position_ids.",
    )
    # Положение `project.business_request` создаётся в init_project как fact.
    # Оно должно попасть в used_position_ids первого primary артефакта.

    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")
    assert decision.selected_task_id is not None
    bundle = execution_service.execute_task(
        workspace, snapshot, decision.selected_task_id, provider="stub"
    )

    primary = runtime.load_artifact(workspace, bundle.result.outputs[0].artifact_id)
    assert "project.business_request" in primary.metadata.used_position_ids
