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
from typing import Any

from pov_generator.application.context_service import ContextService
from pov_generator.application.execution_service import ExecutionService
from pov_generator.application.planning_service import PlanningService
from pov_generator.application.project_service import ProjectService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.registry import ObjectRef
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.llm import LLMProviderRegistry, LLMResult, LLMUsage
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]


class _RecordingLLMProvider:
    """Фейковый ``LLMProvider``, фиксирующий каждый chat_json-вызов и
    возвращающий минимально-валидный payload по схеме."""

    name = "claude_sdk"
    model = "fake-claude-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict) -> LLMResult:
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt, "schema": schema})
        return LLMResult(
            payload=_fabricate_payload_for_schema(schema),
            usage=LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15, source="actual"),
        )


class _RecordingLLMRegistry(LLMProviderRegistry):
    """Подменяет ``get(...)`` так, чтобы всегда возвращать один и тот же
    recording-провайдер. Все остальные операции — как у настоящего реестра."""

    def __init__(self) -> None:
        self.provider = _RecordingLLMProvider()

    def get(self, *, provider: str, model: str | None = None, complexity: str | None = None):
        return self.provider


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


def _build_recording_execution(tmp_path: Path, registry_root: Path):
    """Соберёт ExecutionService с recording-LLM и подготовит workspace.

    Возвращает (workspace, snapshot, task_id, exec_service, fake_registry).
    """
    registry_service = RegistryService(FilesystemRegistryLoader(registry_root))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    fake_registry = _RecordingLLMRegistry()
    execution_service = ExecutionService(runtime, context_service, llm_registry=fake_registry)

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
    return workspace, snapshot, decision.selected_task_id, execution_service, fake_registry, runtime


def test_per_stage_cot_calls_llm_once_per_stage_plus_primary(tmp_path: Path) -> None:
    """N стадий + 1 финальный вызов на primary = N+1 chat_json вызовов.
    Default lean_jtbd для standard complexity активирует goal_framing,
    jtbd_anchor, option_generation, decision = 4 стадии → 5 вызовов."""
    registry_root = _registry_with_per_stage_methodology(tmp_path)
    workspace, snapshot, task_id, execution_service, fake_registry, _runtime = (
        _build_recording_execution(tmp_path, registry_root)
    )

    execution_service.execute_task(workspace, snapshot, task_id, provider="claude_sdk")

    # 4 active stages for standard complexity + 1 primary = 5 calls
    calls = fake_registry.provider.calls
    assert len(calls) == 5, f"Ожидаем 5 вызовов (4 стадии + primary), получили {len(calls)}"


def test_per_stage_cot_accumulates_previous_stage_outputs_in_prompt(tmp_path: Path) -> None:
    """User prompt стадии N должен содержать stage_outputs стадий 1..N-1."""
    registry_root = _registry_with_per_stage_methodology(tmp_path)
    workspace, snapshot, task_id, execution_service, fake_registry, _runtime = (
        _build_recording_execution(tmp_path, registry_root)
    )

    execution_service.execute_task(workspace, snapshot, task_id, provider="claude_sdk")
    user_prompts = [call["user_prompt"] for call in fake_registry.provider.calls]

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
    workspace, snapshot, task_id, execution_service, _fake_registry, runtime = (
        _build_recording_execution(tmp_path, registry_root)
    )

    bundle = execution_service.execute_task(workspace, snapshot, task_id, provider="claude_sdk")

    # Этап 1.1: reasoning живёт в ArtifactMetadata primary артефакта,
    # не как отдельный артефакт.
    primary_output = next(o for o in bundle.result.outputs if o.kind == "primary")
    primary = runtime.load_artifact(workspace, primary_output.artifact_id)
    payload = primary.metadata.reasoning
    stage_ids = {block["stage_id"] for block in payload["stages"]}
    # При standard complexity активны 4 стадии.
    assert stage_ids == {"goal_framing", "jtbd_anchor", "option_generation", "decision"}


def test_single_call_mode_still_makes_one_llm_call(tmp_path: Path) -> None:
    """Не сломали ли single_call: без правки methodology_pack должно
    остаться поведение «один вызов»."""
    workspace, snapshot, task_id, execution_service, fake_registry, _runtime = (
        _build_recording_execution(tmp_path, REPO_ROOT / "templates")
    )
    execution_service.execute_task(workspace, snapshot, task_id, provider="claude_sdk")
    assert len(fake_registry.provider.calls) == 1


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
