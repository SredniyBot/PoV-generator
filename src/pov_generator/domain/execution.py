from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ExecutionProvider = Literal["stub", "openrouter"]
ExecutionStatus = Literal["succeeded", "failed", "cancelled"]


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


@dataclass(frozen=True)
class ExecutionOutput:
    artifact_id: str
    artifact_role: str


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
