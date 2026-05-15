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
    from tests.test_foundation import OBJECTIVE_REF, build_services

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

    # Этап 1.1: trace живёт в ArtifactMetadata primary артефакта,
    # не как отдельный артефакт.
    primary_output = next(o for o in bundle.result.outputs if o.kind == "primary")
    primary = runtime.load_artifact(workspace, primary_output.artifact_id)
    payload = primary.metadata.methodology_trace

    methodology = snapshot.resolve_methodology_pack(bundle.request.methodology_pack_ref)
    # Track 5: ожидаемый набор правил зависит от methodology_mode задачи.
    # Если задача-источник имеет mode=skip/minimal/validation, она получит
    # только подмножество стадий.
    task_template = snapshot.resolve_template(bundle.request.template_ref)
    methodology_mode = getattr(task_template, "methodology_mode", "full")
    expected_rules = {
        (stage.identifier, rule.identifier)
        for stage in methodology.stages_for(bundle.request.complexity, methodology_mode)
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


# --- CE11 draft preparation across providers --------------------------------


def _valid_draft_payload() -> dict[str, Any]:
    """Минимально валидный ответ LLM по схеме `_draft_schema()`."""
    return {
        "description": "Бизнес-контекст требует уточнения KPI пилота.",
        "answer_mode": "single",
        "options": [
            {
                "option_id": "monthly",
                "label": "Раз в месяц",
                "description": "Достаточная частота для PoV.",
                "effect_preview": "Проект продолжается с периодом обновления = месяц.",
                "confidence": 0.7,
            },
            {
                "option_id": "weekly",
                "label": "Раз в неделю",
                "description": "Если бизнес хочет более точечный мониторинг.",
                "effect_preview": "Проект продолжается с периодом обновления = неделя.",
                "confidence": 0.4,
            },
        ],
        "recommended_option_id": "monthly",
        "visibility": "architectural",
    }


def _build_clarification_service_with_runtime():
    from pov_generator.application.clarification_service import ClarificationService
    runtime = SqliteRuntime()
    return runtime, ClarificationService


@pytest.mark.parametrize(
    "provider_name, client_attr",
    [
        ("claude_subscription", "ClaudeSubscriptionClient"),
        ("claude_sdk", "ClaudeSdkClient"),
    ],
)
def test_clarification_draft_uses_selected_claude_provider(
    provider_name: str, client_attr: str
) -> None:
    """CE11 (LLM-подготовка уточнения) должна работать через claude-провайдеры,
    а не только через openrouter. Проверяем, что при `provider=<claude...>`
    сервис вызывает соответствующий client.from_env(...).chat_json(...) с
    переданными prompt'ами и нашей JSON-схемой."""
    from pov_generator.application import clarification_service as cs_module

    runtime, ServiceCls = _build_clarification_service_with_runtime()
    service = ServiceCls(runtime, provider=provider_name)
    candidate = service.candidate_from_question(
        project_id="proj-1",
        source_type="methodology_pack",
        source_id="process.lean_jtbd@1.0.0#decision.ambiguous_choice",
        question="Какой период обновления выбрать для пилота?",
        affected_task_ids=("task-1",),
        related_artifact_ids=(),
    )
    fallback = cs_module.ClarificationDraft(
        description="fallback description",
        answer_mode="single",
        options=(),
        recommended_option_id=None,
        visibility="architectural",
    )

    fake_instance = MagicMock()
    fake_instance.chat_json.return_value = _valid_draft_payload()

    with patch.object(cs_module, client_attr) as fake_client_class:
        fake_client_class.from_env.return_value = fake_instance
        draft = service._build_draft(candidate=candidate, context={}, fallback=fallback)

    fake_client_class.from_env.assert_called_once()
    fake_instance.chat_json.assert_called_once()
    kwargs = fake_instance.chat_json.call_args.kwargs
    assert kwargs["system_prompt"]
    assert kwargs["user_prompt"]
    assert kwargs["schema"]["type"] == "object"
    # Описание и options должны прийти из ответа LLM, а не из fallback.
    assert draft.description == _valid_draft_payload()["description"]
    assert {opt.option_id for opt in draft.options} == {"monthly", "weekly"}
    assert draft.recommended_option_id == "monthly"


def test_clarification_default_provider_follows_execution_provider(monkeypatch) -> None:
    """Если `POV_CLARIFICATION_PROVIDER` явно не задан, CE11 должен идти за
    `POV_EXECUTION_PROVIDER`. Это держит подготовку уточнений и исполнение
    задач на одной модельной семье (Q4 — claude_subscription основной)."""
    runtime, ServiceCls = _build_clarification_service_with_runtime()
    service = ServiceCls(runtime)

    monkeypatch.delenv("POV_CLARIFICATION_PROVIDER", raising=False)
    monkeypatch.delenv("POV_OPENROUTER_API_KEY", raising=False)

    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "claude_subscription")
    assert service._active_provider() == "claude_subscription"

    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "claude_sdk")
    assert service._active_provider() == "claude_sdk"

    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "stub")
    assert service._active_provider() == "stub"


def test_clarification_explicit_provider_overrides_execution_default(monkeypatch) -> None:
    """Явный `provider=` в конструкторе ClarificationService должен побеждать
    автоматический выбор по `POV_EXECUTION_PROVIDER`."""
    runtime, ServiceCls = _build_clarification_service_with_runtime()
    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "stub")
    monkeypatch.delenv("POV_CLARIFICATION_PROVIDER", raising=False)

    service = ServiceCls(runtime, provider="claude_subscription")
    assert service._active_provider() == "claude_subscription"
