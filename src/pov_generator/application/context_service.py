from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import os
import uuid

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.artifacts import ContextBudget, ContextItem, ContextManifest
from ..domain.registry import RegistrySnapshot, compose_recipe
from ..infrastructure.sqlite_runtime import SqliteRuntime


def estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4)


@dataclass(frozen=True)
class ContextBuildResult:
    manifest: ContextManifest


class ContextService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def build_for_task(self, workspace: Path, snapshot: RegistrySnapshot, task_id: str) -> ContextBuildResult:
        manifest = self._runtime.load_manifest(workspace)
        state = self._runtime.load_problem_state(workspace)
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(f"{task.template_id}@{task.template_version}")
        composed_recipe = compose_recipe(snapshot, manifest.recipe_ref, tuple(sorted(state.enabled_domain_packs.keys())))
        current_step = next((step for step in composed_recipe.steps if step.identifier == task.recipe_step_id), None)
        if current_step is None:
            raise ConflictError(f"Шаг '{task.recipe_step_id}' отсутствует в composed recipe.")

        items: list[ContextItem] = []
        source_refs: list[str] = []

        for field_name in template.inputs.required_problem_fields:
            value = getattr(state, field_name, None)
            if value in (None, ""):
                raise ConflictError(f"Для задачи '{task.task_id}' отсутствует обязательное поле ProblemState '{field_name}'.")
            content = json_dumps(value) if isinstance(value, (dict, list, tuple)) else str(value)
            item = ContextItem(
                item_id=str(uuid.uuid4()),
                item_type="problem_field",
                source_ref=f"problem_state:{state.version}:{field_name}",
                title=f"ProblemState.{field_name}",
                content=content,
                token_estimate=estimate_tokens(content),
                required=True,
                priority=100,
            )
            items.append(item)
            source_refs.append(item.source_ref)

        produced_before_current = {
            artifact_role
            for previous_step in composed_recipe.steps
            if previous_step.order < current_step.order
            for artifact_role in previous_step.completion.artifact_roles
        }

        required_artifact_roles = template.inputs.required_artifact_roles
        optional_artifact_roles = tuple(
            role for role in template.inputs.optional_artifact_roles if role not in required_artifact_roles
        )

        if not required_artifact_roles and not optional_artifact_roles:
            required_artifact_roles = tuple(sorted(produced_before_current))

        for artifact_role in required_artifact_roles:
            artifact = self._runtime.latest_artifact_by_role(workspace, artifact_role)
            if artifact is None:
                raise ConflictError(
                    f"Для задачи '{task.task_id}' отсутствует обязательный входной артефакт роли '{artifact_role}'."
                )
            self._append_artifact_item(workspace, items, source_refs, artifact, required=True)

        instruction = ContextItem(
            item_id=str(uuid.uuid4()),
            item_type="instruction",
            source_ref=f"template:{template.ref.as_string()}",
            title="Локальная методология шага",
            content=template.framework_summary,
            token_estimate=estimate_tokens(template.framework_summary),
            required=True,
            priority=1000,
        )
        items.append(instruction)
        source_refs.append(instruction.source_ref)

        max_tokens = self._effective_max_tokens(template.context_policy.max_tokens)

        for artifact_role in optional_artifact_roles:
            artifact = self._runtime.latest_artifact_by_role(workspace, artifact_role)
            if artifact is None:
                continue
            candidate_item = self._make_artifact_item(workspace, artifact, required=False)
            if max_tokens is not None and (
                sum(item.token_estimate for item in items) + candidate_item.token_estimate > max_tokens
            ):
                continue
            items.append(candidate_item)
            source_refs.append(candidate_item.source_ref)

        used_tokens = sum(item.token_estimate for item in items)
        if max_tokens is not None and used_tokens > max_tokens:
            raise ConflictError(
                f"Контекст задачи '{task.task_id}' не помещается в budget: {used_tokens} > {max_tokens}."
            )

        fingerprint = sha256("|".join(sorted(source_refs)).encode("utf-8")).hexdigest()
        manifest_max_tokens = max_tokens if max_tokens is not None else 1_048_576
        context_manifest = ContextManifest(
            manifest_id=str(uuid.uuid4()),
            project_id=manifest.project_id,
            task_id=task.task_id,
            template_ref=template.ref.as_string(),
            problem_state_version=state.version,
            budget=ContextBudget(
                max_input_tokens=manifest_max_tokens,
                reserved_for_output=min(1200, manifest_max_tokens // 2),
                used_tokens=used_tokens,
            ),
            items=tuple(items),
            excluded_items=(),
            input_fingerprint=fingerprint,
            created_at=utc_now_iso(),
        )
        self._runtime.record_context_manifest(workspace, context_manifest)
        return ContextBuildResult(manifest=context_manifest)

    def _effective_max_tokens(self, template_max_tokens: int) -> int | None:
        raw_disable = os.environ.get("POV_DISABLE_TEMPLATE_CONTEXT_BUDGET", "").strip().lower()
        if raw_disable in {"1", "true", "yes", "on"}:
            return None

        raw_override = os.environ.get("POV_TEMPLATE_CONTEXT_MAX_TOKENS", "").strip()
        if raw_override:
            try:
                override = int(raw_override)
            except ValueError:
                override = template_max_tokens
            else:
                if override <= 0:
                    return None
                return override

        return template_max_tokens

    def _append_artifact_item(
        self,
        workspace: Path,
        items: list[ContextItem],
        source_refs: list[str],
        artifact,
        *,
        required: bool,
    ) -> None:
        item = self._make_artifact_item(workspace, artifact, required=required)
        items.append(item)
        source_refs.append(item.source_ref)

    def _make_artifact_item(self, workspace: Path, artifact, *, required: bool) -> ContextItem:
        content = self._runtime.load_artifact_content(workspace, artifact.artifact_id)
        return ContextItem(
            item_id=str(uuid.uuid4()),
            item_type="artifact",
            source_ref=f"artifact:{artifact.artifact_id}",
            title=artifact.title,
            content=content,
            token_estimate=estimate_tokens(content),
            required=required,
            priority=80,
        )
