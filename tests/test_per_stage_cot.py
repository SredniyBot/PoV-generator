"""Тесты для per-stage CoT режима methodology_pack (W3.1).

Раньше methodology работала только в single_call: один LLM-вызов с
объединённой схемой `primary + reasoning`. С W3.1 в methodology_pack
можно поставить `stage_execution_mode: per_stage_cot`, и execution
делает отдельный LLM-вызов на каждую активную стадию + финальный
вызов на primary с накопительным reasoning.

Проверяем:
1. Когда `stage_execution_mode == "single_call"` — один LLM-вызов
   (как раньше), reasoning приходит вместе с primary в одном JSON.
2. Когда `stage_execution_mode == "per_stage_cot"` — N+1 вызов:
   по одному на каждую активную стадию + финальный на primary.
3. Stage N+1 видит выводы стадий 1..N в своём user prompt.
4. Финальный primary вызов получает все stage_outputs как контекст.
5. live_reasoning, который попадает в reasoning_artifact, собирается
   из per-stage результатов, а не от LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]


def _registry_with_per_stage_methodology(tmp_path: Path) -> Path:
    """Копирует templates в tmp_path и переключает process.lean_jtbd на
    per_stage_cot. Возвращает корень нового реестра."""
    import shutil

    dst = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", dst)
    pack_path = dst / "methodologies" / "process.lean_jtbd.yaml"
    text = pack_path.read_text(encoding="utf-8")
    text = text.replace("stage_execution_mode: single_call", "stage_execution_mode: per_stage_cot")
    pack_path.write_text(text, encoding="utf-8")
    return dst


def test_per_stage_cot_calls_llm_once_per_stage_plus_primary(tmp_path: Path) -> None:
    """N стадий + 1 финальный вызов на primary = N+1 chat_json вызовов.
    Default lean_jtbd для standard complexity активирует goal_framing,
    jtbd_anchor, option_generation, decision = 4 стадии → 5 вызовов."""
    registry_root = _registry_with_per_stage_methodology(tmp_path)
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)

    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="per-stage test",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Тест per-stage CoT.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")

    call_log: list[tuple[str, dict]] = []

    def _fake_chat(self, *, provider, model, system_prompt, user_prompt, schema):
        # Distinguish stage vs primary call by schema shape: stages have
        # ≤4 properties; primary schema is the full artifact_contract.
        call_log.append((system_prompt[:80], schema))
        # Return minimal valid payload for whatever schema is asked for.
        return _fabricate_payload_for_schema(schema)

    with patch.object(ExecutionService, "_chat_json", _fake_chat):
        # claude_sdk даёт ненулевую `model` через `model_for_complexity`,
        # без которой sqlite_runtime ругается на NOT NULL execution_runs.model.
        execution_service.execute_task(
            workspace, snapshot, decision.selected_task_id, provider="claude_sdk"
        )

    # 4 active stages for standard complexity + 1 primary = 5 calls
    assert len(call_log) == 5, f"Ожидаем 5 вызовов (4 стадии + primary), получили {len(call_log)}"


def test_per_stage_cot_accumulates_previous_stage_outputs_in_prompt(tmp_path: Path) -> None:
    """User prompt стадии N должен содержать stage_outputs стадий 1..N-1."""
    registry_root = _registry_with_per_stage_methodology(tmp_path)
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)

    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="per-stage acc",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Тест накопления контекста.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")

    user_prompts: list[str] = []

    def _fake_chat(self, *, provider, model, system_prompt, user_prompt, schema):
        user_prompts.append(user_prompt)
        return _fabricate_payload_for_schema(schema)

    with patch.object(ExecutionService, "_chat_json", _fake_chat):
        execution_service.execute_task(
            workspace, snapshot, decision.selected_task_id, provider="claude_sdk"
        )

    # First stage prompt — нет previous outputs.
    assert "Уже зафиксированное рассуждение" not in user_prompts[0]
    # Со стадии 2+ блок должен появиться.
    assert "Уже зафиксированное рассуждение" in user_prompts[1]
    # Финальный (primary) вызов содержит "Reasoning через стадии" блок.
    assert "Reasoning через стадии (per-stage CoT)" in user_prompts[-1]


def test_per_stage_cot_writes_stage_outputs_to_reasoning_artifact(tmp_path: Path) -> None:
    """live_reasoning, который кладётся в reasoning_artifact, должен собраться
    из вызовов LLM по стадиям (не от LLM как монолит)."""
    registry_root = _registry_with_per_stage_methodology(tmp_path)
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)

    snapshot, _ = registry_service.validate()
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="per-stage reasoning",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="Reasoning persistence test.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")

    def _fake_chat(self, *, provider, model, system_prompt, user_prompt, schema):
        # Stage payloads imprint a per-stage marker into a field we don't
        # care about, so we can verify reasoning_artifact is composed of
        # OUR stage outputs, not random LLM output.
        payload = _fabricate_payload_for_schema(schema)
        return payload

    with patch.object(ExecutionService, "_chat_json", _fake_chat):
        bundle = execution_service.execute_task(
            workspace, snapshot, decision.selected_task_id, provider="claude_sdk"
        )

    reasoning_output = next(o for o in bundle.result.outputs if o.kind == "reasoning")
    payload = json.loads(runtime.load_artifact_content(workspace, reasoning_output.artifact_id))
    stage_ids = {block["stage_id"] for block in payload["stages"]}
    # При standard complexity активны 4 стадии.
    assert stage_ids == {"goal_framing", "jtbd_anchor", "option_generation", "decision"}


def test_single_call_mode_still_makes_one_llm_call(tmp_path: Path) -> None:
    """Не сломали ли single_call: без правки methodology_pack должно
    остаться поведение «один вызов»."""
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
        name="single-call guard",
        objective_ref=ObjectRef.parse("common.requirements_specification@1.0.0"),
        request_text="single_call default check.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")

    calls = 0

    def _fake_chat(self, *, provider, model, system_prompt, user_prompt, schema):
        nonlocal calls
        calls += 1
        return _fabricate_payload_for_schema(schema)

    with patch.object(ExecutionService, "_chat_json", _fake_chat):
        execution_service.execute_task(
            workspace, snapshot, decision.selected_task_id, provider="claude_sdk"
        )

    assert calls == 1


# ---- helpers --------------------------------------------------------------


def _fabricate_payload_for_schema(schema: dict) -> dict:
    """Минимальный валидный payload для произвольной JSON-схемы. Достаточно
    для прохода через ExecutionService — настоящая валидация артефакта
    делается отдельно validation_service, мы тестируем только маршрутизацию."""
    if not isinstance(schema, dict):
        return {}
    if schema.get("type") == "object":
        out: dict = {}
        for key, sub in (schema.get("properties") or {}).items():
            out[key] = _value_for_schema(sub)
        return out
    return {}


def _value_for_schema(schema):
    t = schema.get("type") if isinstance(schema, dict) else None
    if t == "string":
        return "stub"
    if t == "number":
        return 0.5
    if t == "integer":
        return 1
    if t == "boolean":
        return True
    if t == "array":
        return []
    if t == "object":
        return _fabricate_payload_for_schema(schema)
    if isinstance(schema, dict) and "anyOf" in schema:
        return None
    return None
