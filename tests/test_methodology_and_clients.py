"""Тесты для methodology rule evaluator и Claude провайдеров.

Покрывают Phase 3 (rule firing → ClarificationCandidate) и Phase 6
(claude_sdk / claude_subscription адаптеры) — see Задача #9 в BACKLOG.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pov_generator.application.clarification_service import ClarificationService
from pov_generator.application.methodology_rules import evaluate_methodology_rules
from pov_generator.application.registry_service import RegistryService
from pov_generator.application.validation_service import ValidationService
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.sqlite_runtime import SqliteRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]
METHODOLOGY_REF = "process.lean_jtbd@1.0.0"


def _build_service() -> tuple[ValidationService, Any]:
    snapshot, report = RegistryService(
        FilesystemRegistryLoader(REPO_ROOT / "templates")
    ).validate()
    assert report.is_valid
    runtime = SqliteRuntime()
    service = ValidationService(runtime, ClarificationService(runtime))
    return service, snapshot


def test_methodology_rule_emits_clarification_candidate() -> None:
    service, snapshot = _build_service()
    methodology = snapshot.resolve_methodology_pack(METHODOLOGY_REF)
    reasoning = {
        "stages": [
            {"stage_id": "goal_framing", "outputs": {"declared_goal": None}},
            {"stage_id": "decision", "outputs": {"chosen_option_id": None}},
        ]
    }

    candidates = service._evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-1",
    )

    assert len(candidates) >= 1
    empty_goal = next(
        (c for c in candidates if c.source_id.endswith("goal_framing.empty_goal")),
        None,
    )
    assert empty_goal is not None
    assert empty_goal.source_type == "methodology_pack"
    assert empty_goal.source_id == f"{METHODOLOGY_REF}#goal_framing.empty_goal"
    assert empty_goal.severity == "high"
    assert empty_goal.blocking_scope == "task"
    assert empty_goal.affected_task_ids == ("task-1",)


def test_evaluate_methodology_rules_returns_outcomes_for_every_rule() -> None:
    """Pure-функция должна возвращать запись `RuleOutcome` для каждого правила
    каждой активной стадии — независимо от того, сработало правило или нет.
    Это гарантирует, что methodology_trace может показать «что проверялось»."""
    _, snapshot = _build_service()
    methodology = snapshot.resolve_methodology_pack(METHODOLOGY_REF)
    reasoning = {
        "stages": [
            {"stage_id": "goal_framing", "outputs": {"declared_goal": "Цель есть."}},
            {
                "stage_id": "option_generation",
                "outputs": {
                    "options": [
                        {"label": "A", "confidence": 0.9},
                        {"label": "B", "confidence": 0.3},
                    ]
                },
            },
            {"stage_id": "decision", "outputs": {"chosen_option_id": "A"}},
        ]
    }

    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-1",
    )

    rule_keys = {(o.stage_id, o.rule_id) for o in evaluation.rule_outcomes}
    assert ("goal_framing", "empty_goal") in rule_keys
    assert ("decision", "ambiguous_choice") in rule_keys
    assert ("decision", "low_overall_confidence") in rule_keys
    # Никаких правил не должно сработать на этом reasoning'е.
    assert all(not o.fired for o in evaluation.rule_outcomes)
    assert evaluation.candidates == ()
    # stage_outputs должен быть нормализован к dict[stage_id, dict].
    assert evaluation.stage_outputs["goal_framing"]["declared_goal"] == "Цель есть."


def test_evaluate_methodology_rules_links_candidate_to_outcome() -> None:
    """Когда правило сработало — `RuleOutcome.candidate_id` должен ссылаться
    на тот же `ClarificationCandidate`, что лежит в `evaluation.candidates`.
    Это связка для UI L4 («какое правило породило этот вопрос»)."""
    _, snapshot = _build_service()
    methodology = snapshot.resolve_methodology_pack(METHODOLOGY_REF)
    reasoning = {
        "stages": [
            {"stage_id": "goal_framing", "outputs": {"declared_goal": None}},
        ]
    }

    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-1",
    )

    fired = [o for o in evaluation.rule_outcomes if o.fired]
    assert len(fired) == 1
    assert fired[0].rule_id == "empty_goal"
    assert fired[0].candidate_id is not None
    assert fired[0].candidate_id in {c.candidate_id for c in evaluation.candidates}


def test_methodology_trace_artifact_records_real_rule_outcomes(tmp_path: Path) -> None:
    """e2e: после стаб-исполнения leaf-задачи `methodology_trace` artifact должен
    содержать `rules_evaluated` для всех правил активных стадий и непустой
    `stage_outputs`. Это закрывает багу, когда trace писался с `fired: False`
    placeholder'ами и пустыми candidates_emitted."""
    from pov_generator.application.context_service import ContextService
    from pov_generator.application.execution_service import ExecutionService
    from pov_generator.domain.registry import ObjectRef
    from tests.test_foundation import REPO_ROOT, build_services, OBJECTIVE_REF

    registry_service, runtime, project_service, planning_service = build_services()
    snapshot, report = registry_service.validate()
    assert report.is_valid
    workspace = tmp_path / "case"
    project_service.init_project(
        workspace=workspace,
        name="Trace test",
        objective_ref=ObjectRef.parse(OBJECTIVE_REF),
        request_text="Тест трасы методологии.",
        domain_packs=(),
    )
    planning_service.expand_graph(workspace, snapshot)
    decision = planning_service.plan(workspace, snapshot, mode="dry-run")
    assert decision.selected_task_id is not None

    bundle = ExecutionService(runtime, ContextService(runtime)).execute_task(
        workspace, snapshot, decision.selected_task_id, provider="stub"
    )

    trace_output = next(o for o in bundle.result.outputs if o.kind == "trace")
    raw = runtime.load_artifact_content(workspace, trace_output.artifact_id)
    payload = json.loads(raw)

    methodology = snapshot.resolve_methodology_pack(bundle.request.methodology_pack_ref)
    expected_rules = {
        (stage.identifier, rule.identifier)
        for stage in methodology.stages_for_complexity(bundle.request.complexity)
        for rule in stage.rules
    }
    actual_rules = {(item["stage_id"], item["rule_id"]) for item in payload["rules_evaluated"]}
    assert actual_rules == expected_rules, "trace должен содержать запись по каждому правилу методологии"
    # На stub reasoning'е (declared_goal заполнен, options пустые) ни одно правило
    # сработать не должно — но запись о проверке должна присутствовать.
    assert all("fired" in item for item in payload["rules_evaluated"])
    assert payload["candidates_emitted"] == []
    assert payload["stage_outputs"], "stage_outputs не должен быть пустым словарём"


def test_methodology_rule_ambiguous_choice_with_cross_stage_options() -> None:
    service, snapshot = _build_service()
    methodology = snapshot.resolve_methodology_pack(METHODOLOGY_REF)
    reasoning = {
        "stages": [
            {"stage_id": "goal_framing", "outputs": {"declared_goal": "Цель есть."}},
            {
                "stage_id": "option_generation",
                "outputs": {
                    "options": [
                        {"label": "A", "confidence": 0.5},
                        {"label": "B", "confidence": 0.45},
                    ]
                },
            },
            {"stage_id": "decision", "outputs": {"chosen_option_id": None}},
        ]
    }

    candidates = service._evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-2",
    )

    ambiguous = next(
        (c for c in candidates if c.source_id.endswith("decision.ambiguous_choice")),
        None,
    )
    assert ambiguous is not None
    assert ambiguous.source_type == "methodology_pack"
    assert ambiguous.severity == "high"
    # low_overall_confidence не должен сработать: max=0.5 не строго < 0.5.
    assert not any(
        c.source_id.endswith("decision.low_overall_confidence") for c in candidates
    )


# --- ClaudeSdkClient -------------------------------------------------------


def _fake_anthropic_response(payload: dict[str, Any]) -> Any:
    tool_use_block = SimpleNamespace(type="tool_use", input=payload, name="produce_artifact")
    return SimpleNamespace(content=[tool_use_block])


def test_claude_sdk_client_builds_tool_use_request() -> None:
    from pov_generator.infrastructure import claude_sdk_client as mod

    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    expected_payload = {"summary": "ok"}

    fake_anthropic_module = MagicMock()
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(expected_payload)
    fake_anthropic_module.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        client = mod.ClaudeSdkClient(
            mod.ClaudeSdkConfig(api_key="dummy", model="claude-sonnet-4-6", max_tokens=1024)
        )
        result = client.chat_json(
            system_prompt="sys",
            user_prompt="user",
            schema=schema,
            tool_name="produce_artifact",
        )

    assert result == expected_payload
    fake_anthropic_module.Anthropic.assert_called_once_with(api_key="dummy")
    fake_client.messages.create.assert_called_once()
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["system"] == "sys"
    assert kwargs["max_tokens"] == 1024
    assert kwargs["messages"] == [{"role": "user", "content": "user"}]
    tools = kwargs["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "produce_artifact"
    assert tools[0]["input_schema"] == schema
    assert kwargs["tool_choice"] == {"type": "tool", "name": "produce_artifact"}


# --- ClaudeSubscriptionClient ---------------------------------------------


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


def _make_async_query(response_text: str):
    async def _query(prompt: str, options: Any):  # noqa: ARG001 — signature must match SDK
        yield _FakeMessage(response_text)

    return _query


@pytest.mark.parametrize(
    "response_text, expected",
    [
        ("```json\n{\"declared_goal\": \"alpha\"}\n```", {"declared_goal": "alpha"}),
        ("{\"declared_goal\": \"alpha\"}", {"declared_goal": "alpha"}),
    ],
    ids=["fenced_markdown", "raw_json"],
)
def test_claude_subscription_client_extracts_json_from_text_response(
    response_text: str, expected: dict[str, Any]
) -> None:
    from pov_generator.infrastructure import claude_subscription_client as mod

    fake_sdk = MagicMock()
    fake_sdk.ClaudeAgentOptions = lambda **kw: SimpleNamespace(**kw)
    fake_sdk.query = _make_async_query(response_text)

    with patch.dict("sys.modules", {"claude_agent_sdk": fake_sdk}):
        client = mod.ClaudeSubscriptionClient(
            mod.ClaudeSubscriptionConfig(model="claude-sonnet-4-6", max_turns=1)
        )
        client._sdk = fake_sdk  # гарантируем, что инстанс использует наш мок
        result = client.chat_json(
            system_prompt="sys",
            user_prompt="user",
            schema={"type": "object"},
        )

    assert result == expected
