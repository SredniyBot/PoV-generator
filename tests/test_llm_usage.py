"""Тесты фичи 2 — учёт потраченных токенов.

Покрывает: контракт ``chat_json`` с usage по всем провайдерам (openrouter,
claude_sdk, claude_subscription) и stub-пути; запись на каждый LLM-вызов;
агрегацию до задачи (retry, per_stage_cot); честный n/a без выдуманных чисел.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

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
OBJECTIVE_REF = "common.requirements_specification@1.0.0"


# --- usage от провайдеров (контракт chat_json) -------------------------------


def test_openrouter_client_returns_actual_usage() -> None:
    from pov_generator.infrastructure import openrouter_client as mod

    api_response = {
        "choices": [{"message": {"content": json.dumps({"summary": "ok"})}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(api_response).encode("utf-8")

    client = mod.OpenRouterClient(mod.OpenRouterConfig(api_key="k", model="x/y"))
    with patch.object(mod.request, "urlopen", return_value=_FakeResponse()):
        result = client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert result.payload == {"summary": "ok"}
    assert result.usage is not None
    assert result.usage.source == "actual"
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 30
    assert result.usage.total_tokens == 150


def test_claude_sdk_client_returns_actual_usage() -> None:
    from pov_generator.infrastructure import claude_sdk_client as mod

    tool_block = SimpleNamespace(type="tool_use", input={"summary": "ok"}, name="produce_artifact")
    usage = SimpleNamespace(
        input_tokens=200, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=10
    )
    response = SimpleNamespace(content=[tool_block], usage=usage)

    fake_anthropic = MagicMock()
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response
    fake_anthropic.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        client = mod.ClaudeSdkClient(mod.ClaudeSdkConfig(api_key="k", model="claude-sonnet-4-6"))
        result = client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert result.payload == {"summary": "ok"}
    assert result.usage is not None
    assert result.usage.source == "actual"
    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 50
    assert result.usage.cache_tokens == 10


def _subscription_query(text: str, *, result_usage: dict | None):
    async def _query(prompt: str, options: Any):  # noqa: ARG001
        yield SimpleNamespace(content=[SimpleNamespace(text=text)])
        if result_usage is not None:
            # ResultMessage в конце стрима: без content, но с usage/total_cost_usd.
            yield SimpleNamespace(content=None, usage=result_usage, total_cost_usd=0.0021)

    return _query


def test_claude_subscription_reads_usage_from_result_message() -> None:
    from pov_generator.infrastructure import claude_subscription_client as mod

    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)
    fake_sdk.query = _subscription_query(
        '{"declared_goal": "alpha"}',
        result_usage={"input_tokens": 300, "output_tokens": 80},
    )

    with patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}):
        client = mod.ClaudeSubscriptionClient(mod.ClaudeSubscriptionConfig(model="m", max_turns=1))
        client._sdk = fake_sdk
        result = client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert result.payload == {"declared_goal": "alpha"}
    assert result.usage is not None
    assert result.usage.source == "actual"
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 80
    assert result.usage.cost_usd == 0.0021


def test_claude_subscription_falls_back_to_estimate_without_result_message() -> None:
    from pov_generator.infrastructure import claude_subscription_client as mod

    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)
    fake_sdk.query = _subscription_query('{"declared_goal": "alpha"}', result_usage=None)

    with patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}):
        client = mod.ClaudeSubscriptionClient(mod.ClaudeSubscriptionConfig(model="m", max_turns=1))
        client._sdk = fake_sdk
        result = client.chat_json(system_prompt="s", user_prompt="u", schema={"type": "object"})

    assert result.payload == {"declared_goal": "alpha"}
    assert result.usage is not None
    assert result.usage.source == "estimated"
    assert result.usage.total_tokens > 0


def test_estimated_usage_helper() -> None:
    usage = LLMUsage.estimated(input_text="x" * 40, output_text="y" * 20)
    assert usage.source == "estimated"
    assert usage.input_tokens == 10  # 40 // 4
    assert usage.output_tokens == 5  # 20 // 4
    assert usage.total_tokens == 15


# --- запись и агрегация на исполнении задачи ---------------------------------


def _bootstrap(tmp_path: Path):
    registry_service = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workspace = tmp_path / "case"
    bootstrap = project_service.init_project(
        workspace=workspace,
        name="usage test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Нужна CRM-интеграция.",
        domain_packs=(),
    )
    PlanningService(runtime).expand_graph(workspace, snapshot)
    return workspace, bootstrap.manifest.project_id, runtime, registry_service


def _first_simple_leaf(runtime, snapshot, workspace):
    return next(
        t
        for t in runtime.list_tasks(workspace)
        if t.template_type == "leaf" and not snapshot.resolve_template(t.template_ref).inputs.required_artifact_roles
    )


def test_stub_execution_records_estimated_usage(tmp_path: Path) -> None:
    workspace, _project_id, runtime, registry_service = _bootstrap(tmp_path)
    snapshot, _ = registry_service.validate()
    execution = ExecutionService(runtime, ContextService(runtime))
    leaf = _first_simple_leaf(runtime, snapshot, workspace)

    execution.execute_task(workspace, snapshot, leaf.task_id, provider="stub")

    rows = runtime.list_llm_usage(workspace, task_id=leaf.task_id)
    assert len(rows) == 1
    assert rows[0].source == "estimated"
    assert rows[0].total_tokens > 0
    assert rows[0].execution_run_id

    aggregate = runtime.llm_usage_by_task(workspace)[leaf.task_id]
    assert aggregate.call_count == 1
    assert aggregate.has_estimated is True
    assert aggregate.total_tokens == rows[0].total_tokens


def test_retry_aggregates_usage_across_attempts(tmp_path: Path) -> None:
    workspace, _project_id, runtime, registry_service = _bootstrap(tmp_path)
    snapshot, _ = registry_service.validate()
    execution = ExecutionService(runtime, ContextService(runtime))
    leaf = _first_simple_leaf(runtime, snapshot, workspace)

    # Два исполнения той же задачи (как retry) → две строки usage, суммируются.
    execution.execute_task(workspace, snapshot, leaf.task_id, provider="stub")
    execution.execute_task(workspace, snapshot, leaf.task_id, provider="stub")

    rows = runtime.list_llm_usage(workspace, task_id=leaf.task_id)
    assert len(rows) == 2
    aggregate = runtime.llm_usage_by_task(workspace)[leaf.task_id]
    assert aggregate.call_count == 2
    assert aggregate.total_tokens == sum(r.total_tokens for r in rows)


def test_project_aggregate_and_na_when_empty(tmp_path: Path) -> None:
    workspace, _project_id, runtime, registry_service = _bootstrap(tmp_path)
    # Без исполнений — агрегат по проекту None (n/a, без выдуманных чисел).
    assert runtime.llm_usage_for_project(workspace) is None

    snapshot, _ = registry_service.validate()
    execution = ExecutionService(runtime, ContextService(runtime))
    leaf = _first_simple_leaf(runtime, snapshot, workspace)
    execution.execute_task(workspace, snapshot, leaf.task_id, provider="stub")

    project_aggregate = runtime.llm_usage_for_project(workspace)
    assert project_aggregate is not None
    assert project_aggregate.call_count >= 1


class _RecordingProvider:
    name = "claude_sdk"
    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict) -> LLMResult:
        self.calls += 1
        payload = _fabricate_payload(schema)
        return LLMResult(
            payload=payload,
            usage=LLMUsage(input_tokens=100, output_tokens=20, total_tokens=120, source="actual"),
        )


def _fabricate_payload(schema: dict) -> dict:
    """Минимально-валидный payload по JSON-схеме (рекурсивно)."""
    if schema.get("type") == "object":
        result: dict[str, Any] = {}
        for name, prop in (schema.get("properties") or {}).items():
            result[name] = _fabricate_payload(prop)
        return result
    if schema.get("type") == "array":
        return []
    if schema.get("type") == "number" or schema.get("type") == "integer":
        return 0
    if schema.get("type") == "boolean":
        return False
    return "x"


def test_per_stage_cot_records_usage_per_call(tmp_path: Path) -> None:
    """per_stage_cot пишет usage по вызову на каждую стадию + финальный."""
    import shutil

    dst = tmp_path / "templates"
    shutil.copytree(REPO_ROOT / "templates", dst)
    pack_path = dst / "methodologies" / "process.lean_jtbd.yaml"
    text = pack_path.read_text(encoding="utf-8")
    text = text.replace("stage_execution_mode: single_call", "stage_execution_mode: per_stage_cot")
    pack_path.write_text(text, encoding="utf-8")

    registry_service = RegistryService(FilesystemRegistryLoader(dst))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="cot usage",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Запрос.",
        domain_packs=(),
    )
    PlanningService(runtime).expand_graph(workspace, snapshot)

    provider = _RecordingProvider()

    class _Registry(LLMProviderRegistry):
        def __init__(self) -> None:
            self.provider = provider

        def get(self, *, provider: str, model: str | None = None, complexity: str | None = None):
            return self.provider

    execution = ExecutionService(runtime, ContextService(runtime), llm_registry=_Registry())
    leaf = _first_simple_leaf(runtime, snapshot, workspace)
    execution.execute_task(workspace, snapshot, leaf.task_id, provider="claude_sdk")

    rows = runtime.list_llm_usage(workspace, task_id=leaf.task_id)
    # По вызову на каждую стадию + один финальный на primary.
    assert len(rows) == provider.calls
    assert provider.calls >= 2  # минимум 1 стадия + финальный
    staged = [r for r in rows if r.stage is not None]
    assert staged, "стадийные вызовы должны иметь метку stage"
    assert all(r.source == "actual" for r in rows)
