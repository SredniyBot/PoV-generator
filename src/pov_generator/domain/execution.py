from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .decisions import DecisionInput

ExecutionProvider = Literal["stub", "openrouter", "claude_sdk", "claude_subscription"]
# v3.0: добавлен статус `paused_for_checkpoint` — задача дошла до этапа
# выявления решений, есть решения на уровне пользователя; workflow ждёт submit
# сессии в /api/projects/.../checkpoints/.../answer, после чего задача
# будет ретрайнута и подхватит финализированные решения.
ExecutionStatus = Literal["succeeded", "failed", "cancelled", "paused_for_checkpoint"]


@dataclass(frozen=True)
class ExecutionRequest:
    execution_run_id: str
    project_id: str
    task_id: str
    template_ref: str
    context_manifest_id: str
    provider: ExecutionProvider
    model: str
    actor: str
    complexity: str | None = None
    methodology_pack_ref: str | None = None


@dataclass(frozen=True)
class ExecutionOutput:
    artifact_id: str
    artifact_role: str
    kind: Literal["primary", "reasoning", "trace"] = "primary"


@dataclass(frozen=True)
class ExecutionTrace:
    trace_id: str
    trace_type: Literal["request", "response", "prompt_bundle", "error"]
    title: str
    content: str


@dataclass(frozen=True)
class ExecutionResult:
    execution_run_id: str
    status: ExecutionStatus
    outputs: tuple[ExecutionOutput, ...] = field(default_factory=tuple)
    trace_ids: tuple[str, ...] = field(default_factory=tuple)
    proposed_goal: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    # v3.1: DecisionInput от правил активной methodology_pack. In-memory
    # канал между execution_service и validation_service: validation
    # регистрирует их через CheckpointService.register_decision_inputs,
    # не пересчитывая правила. До v3.1 поле было `methodology_candidates`
    # типа ClarificationCandidate — переименовано в `methodology_decisions`.
    methodology_decisions: tuple[DecisionInput, ...] = field(default_factory=tuple)
    # v3.0: ID checkpoint-сессии, если задача приостановлена для участия
    # пользователя в решениях. Заполняется одновременно со
    # status="paused_for_checkpoint"; в остальных случаях — None.
    checkpoint_session_id: str | None = None
