from __future__ import annotations

import sqlite3
from pathlib import Path

from pov_generator.application.decision_context_builder import DecisionContextBuilder
from pov_generator.domain.artifacts import ContextBudget, ContextItem, ContextManifest
from pov_generator.domain.decisions import Decision, DecisionAlternative
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


def _decision(
    decision_id: str,
    *,
    project_id: str = "project-1",
    title: str = "Целевая аудитория",
    description: str = "Какой вариант нужно считать зафиксированным?",
    category: str = "",
    chosen_option_id: str = "b2b",
    status: str = "accepted_default",
    level: str = "business",
    source_task_id: str = "task-a",
    affected_artifact_ids: tuple[str, ...] = (),
    depends_on_decision_ids: tuple[str, ...] = (),
    answer_mode: str = "single",
    chosen_option_ids: tuple[str, ...] = (),
    user_free_text_answer: str | None = None,
    rationale: str = "Такой выбор следует из бизнес-запроса.",
    created_at: str = "",
    updated_at: str = "",
) -> Decision:
    return Decision(
        decision_id=decision_id,
        project_id=project_id,
        title=title,
        description=description,
        category=category,
        chosen_option_id=chosen_option_id,
        alternatives=(
            DecisionAlternative(
                option_id="b2b",
                label="Корпоративные клиенты",
                description="B2B сегмент",
            ),
            DecisionAlternative(
                option_id="retail",
                label="Розничные клиенты",
                description="B2C сегмент",
            ),
        ),
        rationale=rationale,
        level=level,  # type: ignore[arg-type]
        level_rationale="Тестовая классификация.",
        confidence=0.8,
        status=status,  # type: ignore[arg-type]
        source="pre_flight",
        source_task_id=source_task_id,
        affected_artifact_ids=affected_artifact_ids,
        depends_on_decision_ids=depends_on_decision_ids,
        answer_mode=answer_mode,  # type: ignore[arg-type]
        chosen_option_ids=chosen_option_ids,
        user_free_text_answer=user_free_text_answer,
        created_at=created_at,
        updated_at=updated_at,
    )


def _manifest_with_artifact(project_id: str, task_id: str, artifact_id: str) -> ContextManifest:
    item = ContextItem(
        item_id="ctx-1",
        item_type="artifact",
        source_ref=f"artifact:{artifact_id}",
        title="Upstream artifact",
        content="{}",
        token_estimate=1,
        required=True,
        priority=100,
    )
    return ContextManifest(
        manifest_id="manifest-1",
        project_id=project_id,
        task_id=task_id,
        template_ref="common.test@1.0.0",
        problem_state_version=1,
        budget=ContextBudget(max_input_tokens=1000, reserved_for_output=100, used_tokens=1),
        items=(item,),
    )


def test_builder_renders_closed_project_level_decisions_from_previous_task(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    runtime.upsert_decision(
        workspace,
        _decision("accepted-from-task-a", project_id=project_id, source_task_id="task-a"),
    )
    runtime.upsert_decision(
        workspace,
        _decision(
            "proposed-from-task-a",
            project_id=project_id,
            title="Черновое решение",
            status="proposed",
            source_task_id="task-a",
        ),
    )

    block = DecisionContextBuilder(runtime).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
    )

    assert block.decisions[0].decision_id == "accepted-from-task-a"
    assert "<decision_constraints>" in block.text
    assert "Целевая аудитория" in block.text
    assert "Корпоративные клиенты: B2B сегмент" in block.text
    assert "Такой выбор следует из бизнес-запроса." in block.text
    assert "задача task-a" in block.text
    assert "Черновое решение" not in block.text


def test_builder_treats_deferred_decision_as_applied_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    runtime.upsert_decision(
        workspace,
        _decision(
            "deferred-default",
            project_id=project_id,
            title="Граница MVP",
            status="deferred",
            source_task_id="task-a",
        ),
    )

    block = DecisionContextBuilder(runtime).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
    )

    assert [decision.decision_id for decision in block.decisions] == ["deferred-default"]
    assert "Граница MVP (отложено; применён дефолт)" in block.text


def test_builder_includes_detail_decisions_only_through_artifact_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    runtime.upsert_decision(
        workspace,
        _decision(
            "detail-linked",
            project_id=project_id,
            title="Форматы API",
            level="detail",
            source_task_id="task-a",
            affected_artifact_ids=("artifact-a",),
            answer_mode="multiple",
            chosen_option_id="",
            chosen_option_ids=("b2b", "retail"),
        ),
    )
    runtime.upsert_decision(
        workspace,
        _decision(
            "detail-unrelated",
            project_id=project_id,
            title="Локальный стиль именования",
            level="detail",
            source_task_id="task-a",
            affected_artifact_ids=("artifact-z",),
        ),
    )

    block = DecisionContextBuilder(runtime).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
        context_manifest=_manifest_with_artifact(project_id, "task-b", "artifact-a"),
    )

    assert [decision.decision_id for decision in block.decisions] == ["detail-linked"]
    assert "Форматы API" in block.text
    assert "Корпоративные клиенты: B2B сегмент; Розничные клиенты: B2C сегмент" in block.text
    assert "Локальный стиль именования" not in block.text


def test_builder_uses_explicit_category_in_compact_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    runtime.upsert_decision(
        workspace,
        _decision(
            "with-category",
            project_id=project_id,
            title="Выбор СУБД",
            category="tech_stack",
        ),
    )

    block = DecisionContextBuilder(runtime).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
    )

    assert "Категория: tech_stack" in block.text
    assert "[tech_stack]" not in block.text


def test_builder_falls_back_to_legacy_category_prefix(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    runtime.upsert_decision(
        workspace,
        _decision(
            "legacy-category",
            project_id=project_id,
            title="Граница MVP",
            category="scope",
        ),
    )
    with sqlite3.connect(workspace / runtime.DB_FILENAME) as connection:
        connection.execute(
            "update decisions set category = '', description = ? where decision_id = ?",
            ("[scope] Что входит в первую поставку", "legacy-category"),
        )
        connection.commit()

    block = DecisionContextBuilder(runtime).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
    )

    assert "Категория: scope" in block.text
    assert "[scope]" not in block.text


def test_builder_deduplicates_by_normalized_signature(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    runtime.upsert_decision(
        workspace,
        _decision(
            "decision-a",
            project_id=project_id,
            title="Выбор СУБД",
            category="tech_stack",
        ),
    )
    runtime.upsert_decision(
        workspace,
        _decision(
            "decision-b",
            project_id=project_id,
            title="  выбор   субд! ",
            category="tech_stack",
        ),
    )

    block = DecisionContextBuilder(runtime).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
    )

    assert [decision.decision_id for decision in block.decisions] == ["decision-a"]


def test_builder_prioritizes_task_and_artifact_decisions_over_large_global_ledger(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    for index in range(8):
        runtime.upsert_decision(
            workspace,
            _decision(
                f"global-{index}",
                project_id=project_id,
                title=f"Глобальное бизнес-решение {index}",
                level="business",
                source_task_id=f"task-global-{index}",
                created_at=f"2026-05-20T00:00:0{index}Z",
            ),
        )
    runtime.upsert_decision(
        workspace,
        _decision(
            "artifact-linked",
            project_id=project_id,
            title="Формат входного артефакта",
            level="detail",
            source_task_id="task-a",
            affected_artifact_ids=("artifact-a",),
            created_at="2026-05-19T00:00:00Z",
        ),
    )
    runtime.upsert_decision(
        workspace,
        _decision(
            "current-task",
            project_id=project_id,
            title="Локальное решение текущей задачи",
            level="detail",
            source_task_id="task-b",
            created_at="2026-05-18T00:00:00Z",
        ),
    )

    block = DecisionContextBuilder(runtime, max_decisions=3).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
        context_manifest=_manifest_with_artifact(project_id, "task-b", "artifact-a"),
    )

    assert [decision.decision_id for decision in block.decisions[:2]] == [
        "current-task",
        "artifact-linked",
    ]
    assert len(block.decisions) == 3


def test_builder_includes_closed_dependencies_of_relevant_decisions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    runtime.upsert_decision(
        workspace,
        _decision(
            "dependency-detail",
            project_id=project_id,
            title="Внутренний формат событий",
            level="detail",
            source_task_id="task-a",
            created_at="2026-05-20T00:00:00Z",
        ),
    )
    runtime.upsert_decision(
        workspace,
        _decision(
            "current-dependent",
            project_id=project_id,
            title="Использовать событийную интеграцию",
            level="architecture",
            source_task_id="task-b",
            depends_on_decision_ids=("dependency-detail",),
            created_at="2026-05-21T00:00:00Z",
        ),
    )

    block = DecisionContextBuilder(runtime).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
    )

    assert [decision.decision_id for decision in block.decisions] == [
        "current-dependent",
        "dependency-detail",
    ]


def test_builder_respects_token_cap_for_large_ledger(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = SqliteRuntime()
    project_id = "project-1"
    long_rationale = " ".join(["обоснование"] * 80)
    for index in range(10):
        runtime.upsert_decision(
            workspace,
            _decision(
                f"decision-{index}",
                project_id=project_id,
                title=f"Решение {index}",
                level="business",
                source_task_id=f"task-{index}",
                rationale=long_rationale,
                created_at=f"2026-05-20T00:00:0{index}Z",
            ),
        )

    block = DecisionContextBuilder(
        runtime,
        max_decisions=10,
        max_tokens=360,
    ).build_generation_constraints(
        workspace=workspace,
        project_id=project_id,
        task_id="task-b",
    )

    assert 1 <= len(block.decisions) < 10
    assert block.token_estimate <= 360
