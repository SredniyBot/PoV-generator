from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ArtifactFormat = Literal["json", "markdown", "text"]
ArtifactKind = Literal["primary", "derived"]
ContextItemType = Literal["problem_field", "artifact", "instruction"]


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    project_id: str
    artifact_role: str
    title: str
    description: str | None
    artifact_format: ArtifactFormat
    artifact_kind: ArtifactKind
    created_by_task_id: str | None
    parent_artifact_id: str | None
    metadata: dict[str, object]
    storage_path: str
    created_at: str


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    item_type: ContextItemType
    source_ref: str
    title: str
    content: str
    token_estimate: int
    required: bool
    priority: int


@dataclass(frozen=True)
class ContextBudget:
    max_input_tokens: int
    reserved_for_output: int
    used_tokens: int


@dataclass(frozen=True)
class ContextManifest:
    manifest_id: str
    project_id: str
    task_id: str
    template_ref: str
    problem_state_version: int
    budget: ContextBudget
    items: tuple[ContextItem, ...] = field(default_factory=tuple)
    excluded_items: tuple[str, ...] = field(default_factory=tuple)
    input_fingerprint: str = ""
    created_at: str = ""
