from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from ..common.errors import NotFoundError
from ..common.serialization import json_dumps, json_loads, to_primitive, utc_now_iso
from ..domain.artifacts import ArtifactRecord, ContextBudget, ContextItem, ContextManifest
from ..domain.execution import ExecutionRequest, ExecutionResult, ExecutionTrace
from ..domain.planning import AdmissionCheck, CandidateEvaluation, PlanningDecision
from ..domain.problem_state import ProblemEvent, ProblemPatch, ProblemState, apply_problem_patch
from ..domain.tasks import (
    TaskEvent,
    TaskRecipeProgress,
    TaskRecord,
    apply_task_command,
    recipe_progress_status_for_task,
)
from ..domain.validation import EscalationTicket, ValidationFinding, ValidationRun


@dataclass(frozen=True)
class ProjectManifest:
    project_id: str
    name: str
    recipe_ref: str
    created_at: str


def _problem_state_to_dict(state: ProblemState) -> dict[str, object]:
    return to_primitive(state)


def _problem_state_from_dict(payload: dict) -> ProblemState:
    from ..domain.problem_state import (
        EnabledDomainPack,
        FactRecord,
        GapRecord,
        ReadinessRecord,
        RecipeCompositionRecord,
    )

    return ProblemState(
        project_id=payload["project_id"],
        recipe_ref=payload["recipe_ref"],
        business_request=payload["business_request"],
        goal=payload.get("goal"),
        known_facts={key: FactRecord(**value) for key, value in payload.get("known_facts", {}).items()},
        active_gaps={key: GapRecord(**value) for key, value in payload.get("active_gaps", {}).items()},
        readiness={key: ReadinessRecord(**value) for key, value in payload.get("readiness", {}).items()},
        enabled_domain_packs={
            key: EnabledDomainPack(**value) for key, value in payload.get("enabled_domain_packs", {}).items()
        },
        recipe_composition=RecipeCompositionRecord(**payload["recipe_composition"])
        if payload.get("recipe_composition")
        else None,
        version=int(payload.get("version", 0)),
        updated_at=payload.get("updated_at", utc_now_iso()),
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        project_id=row["project_id"],
        template_id=row["template_id"],
        template_version=row["template_version"],
        template_type=row["template_type"],
        template_role=row["template_role"],
        recipe_id=row["recipe_id"],
        recipe_version=row["recipe_version"],
        recipe_step_id=row["recipe_step_id"],
        task_family_key=row["task_family_key"],
        status=row["status"],
        attempt=row["attempt"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        project_id=row["project_id"],
        artifact_role=row["artifact_role"],
        title=row["title"],
        description=row["description"],
        artifact_format=row["artifact_format"],
        artifact_kind=row["artifact_kind"],
        created_by_task_id=row["created_by_task_id"],
        parent_artifact_id=row["parent_artifact_id"],
        metadata=json_loads(row["metadata_json"]),
        storage_path=row["storage_path"],
        created_at=row["created_at"],
    )


def _context_item_from_row(row: sqlite3.Row) -> ContextItem:
    return ContextItem(
        item_id=row["item_id"],
        item_type=row["item_type"],
        source_ref=row["source_ref"],
        title=row["title"],
        content=row["content"],
        token_estimate=row["token_estimate"],
        required=bool(row["required"]),
        priority=row["priority"],
    )


class SqliteRuntime:
    DB_FILENAME = "runtime.db"
    MANIFEST_FILENAME = "project.json"

    def create_workspace(
        self,
        workspace: Path,
        manifest: ProjectManifest,
        initial_state: ProblemState,
        bootstrap_event: ProblemEvent,
    ) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        manifest_path = workspace / self.MANIFEST_FILENAME
        manifest_path.write_text(json_dumps(manifest), encoding="utf-8")
        with self._connect(workspace) as connection:
            self._ensure_schema(connection)
            connection.execute(
                """
                insert into problem_snapshots(project_id, state_json, version, updated_at)
                values (?, ?, ?, ?)
                """,
                (manifest.project_id, json_dumps(_problem_state_to_dict(initial_state)), initial_state.version, initial_state.updated_at),
            )
            connection.execute(
                """
                insert into problem_events(project_id, version, patch_type, payload_json, actor, reason, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.project_id,
                    bootstrap_event.version,
                    bootstrap_event.patch_type,
                    json_dumps(bootstrap_event.payload),
                    bootstrap_event.actor,
                    bootstrap_event.reason,
                    bootstrap_event.created_at,
                ),
            )
            connection.commit()

    def load_manifest(self, workspace: Path) -> ProjectManifest:
        manifest_path = workspace / self.MANIFEST_FILENAME
        if not manifest_path.exists():
            raise NotFoundError(f"Workspace manifest not found: {manifest_path}")
        raw = json_loads(manifest_path.read_text(encoding="utf-8"))
        return ProjectManifest(**raw)

    def load_problem_state(self, workspace: Path) -> ProblemState:
        with self._connect(workspace) as connection:
            row = connection.execute("select state_json from problem_snapshots limit 1").fetchone()
            if row is None:
                raise NotFoundError("Problem snapshot not found.")
            return _problem_state_from_dict(json_loads(row["state_json"]))

    def list_problem_events(self, workspace: Path) -> list[ProblemEvent]:
        with self._connect(workspace) as connection:
            rows = connection.execute(
                """
                select version, patch_type, payload_json, actor, reason, created_at
                from problem_events
                order by version
                """
            ).fetchall()
        return [
            ProblemEvent(
                version=row["version"],
                patch_type=row["patch_type"],
                payload=json_loads(row["payload_json"]),
                actor=row["actor"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def apply_problem_patch(self, workspace: Path, patch: ProblemPatch, actor: str, reason: str) -> ProblemState:
        state = self.load_problem_state(workspace)
        next_state = apply_problem_patch(state, patch)
        payload = to_primitive(patch)
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update problem_snapshots set state_json = ?, version = ?, updated_at = ?
                where project_id = ?
                """,
                (
                    json_dumps(_problem_state_to_dict(next_state)),
                    next_state.version,
                    next_state.updated_at,
                    next_state.project_id,
                ),
            )
            connection.execute(
                """
                insert into problem_events(project_id, version, patch_type, payload_json, actor, reason, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_state.project_id,
                    next_state.version,
                    type(patch).__name__,
                    json_dumps(payload),
                    actor,
                    reason,
                    next_state.updated_at,
                ),
            )
            connection.commit()
        return next_state

    def create_task(self, workspace: Path, task: TaskRecord) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into tasks(
                  task_id, project_id, template_id, template_version, template_type, template_role,
                  recipe_id, recipe_version, recipe_step_id, task_family_key, status, attempt, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.project_id,
                    task.template_id,
                    task.template_version,
                    task.template_type,
                    task.template_role,
                    task.recipe_id,
                    task.recipe_version,
                    task.recipe_step_id,
                    task.task_family_key,
                    task.status,
                    task.attempt,
                    task.created_at,
                    task.updated_at,
                ),
            )
            connection.execute(
                """
                insert into task_events(task_id, project_id, event_type, from_status, to_status, payload_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.project_id,
                    "created",
                    None,
                    task.status,
                    json_dumps({"recipe_step_id": task.recipe_step_id}),
                    task.created_at,
                ),
            )
            self._upsert_recipe_progress(connection, task)
            connection.commit()

    def list_tasks(self, workspace: Path) -> list[TaskRecord]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from tasks order by created_at, task_id").fetchall()
        return [_task_from_row(row) for row in rows]

    def get_task(self, workspace: Path, task_id: str) -> TaskRecord:
        with self._connect(workspace) as connection:
            row = connection.execute("select * from tasks where task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return _task_from_row(row)

    def transition_task(self, workspace: Path, task_id: str, command: str) -> TaskRecord:
        task = self.get_task(workspace, task_id)
        next_task = apply_task_command(task, command)
        event = TaskEvent(
            task_id=task.task_id,
            event_type=command,
            from_status=task.status,
            to_status=next_task.status,
            payload={},
            created_at=next_task.updated_at,
        )
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update tasks set status = ?, attempt = ?, updated_at = ?
                where task_id = ?
                """,
                (next_task.status, next_task.attempt, next_task.updated_at, next_task.task_id),
            )
            connection.execute(
                """
                insert into task_events(task_id, project_id, event_type, from_status, to_status, payload_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_task.task_id,
                    next_task.project_id,
                    event.event_type,
                    event.from_status,
                    event.to_status,
                    json_dumps(event.payload),
                    event.created_at,
                ),
            )
            self._upsert_recipe_progress(connection, next_task)
            connection.commit()
        return next_task

    def list_task_events(self, workspace: Path, task_id: str | None = None) -> list[TaskEvent]:
        query = """
            select task_id, event_type, from_status, to_status, payload_json, created_at
            from task_events
        """
        params: tuple[object, ...] = ()
        if task_id is not None:
            query += " where task_id = ?"
            params = (task_id,)
        query += " order by created_at, id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            TaskEvent(
                task_id=row["task_id"],
                event_type=row["event_type"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                payload=json_loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def list_recipe_progress(self, workspace: Path, recipe_ref: str | None = None) -> list[TaskRecipeProgress]:
        query = """
            select project_id, recipe_id, recipe_version, recipe_step_id, status, task_id, updated_at, note
            from task_recipe_progress
        """
        params: tuple[object, ...] = ()
        if recipe_ref is not None:
            recipe_id, recipe_version = recipe_ref.rsplit("@", 1)
            query += " where recipe_id = ? and recipe_version = ?"
            params = (recipe_id, recipe_version)
        query += " order by recipe_step_id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            TaskRecipeProgress(
                project_id=row["project_id"],
                recipe_id=row["recipe_id"],
                recipe_version=row["recipe_version"],
                recipe_step_id=row["recipe_step_id"],
                status=row["status"],
                task_id=row["task_id"],
                updated_at=row["updated_at"],
                note=row["note"],
            )
            for row in rows
        ]

    def record_planning_decision(self, workspace: Path, decision: PlanningDecision) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into planning_decisions(project_id, created_at, decision_json)
                values (?, ?, ?)
                """,
                (decision.project_id, decision.created_at or utc_now_iso(), json_dumps(to_primitive(decision))),
            )
            connection.commit()

    def list_planning_decisions(self, workspace: Path) -> list[PlanningDecision]:
        with self._connect(workspace) as connection:
            rows = connection.execute(
                "select decision_json from planning_decisions order by id"
            ).fetchall()
        decisions = []
        for row in rows:
            payload = json_loads(row["decision_json"])
            candidates = []
            for candidate_raw in payload.get("candidates", []):
                checks = tuple(AdmissionCheck(**check_raw) for check_raw in candidate_raw.get("checks", []))
                candidates.append(
                    CandidateEvaluation(
                        recipe_step_id=candidate_raw["recipe_step_id"],
                        step_title=candidate_raw["step_title"],
                        template_ref=candidate_raw["template_ref"],
                        step_source_kind=candidate_raw["step_source_kind"],
                        step_source_ref=candidate_raw["step_source_ref"],
                        admissible=candidate_raw["admissible"],
                        score=candidate_raw["score"],
                        duplicate=candidate_raw["duplicate"],
                        checks=checks,
                        reasons=tuple(candidate_raw.get("reasons", [])),
                    )
                )
            decisions.append(
                PlanningDecision(
                    project_id=payload["project_id"],
                    recipe_ref=payload["recipe_ref"],
                    domain_pack_refs=tuple(payload.get("domain_pack_refs", [])),
                    recipe_fragment_refs=tuple(payload.get("recipe_fragment_refs", [])),
                    mode=payload["mode"],
                    outcome=payload["outcome"],
                    selected_step_id=payload.get("selected_step_id"),
                    selected_template_ref=payload.get("selected_template_ref"),
                    created_task_id=payload.get("created_task_id"),
                    candidates=tuple(candidates),
                    reasons=tuple(payload.get("reasons", [])),
                    created_at=payload.get("created_at", ""),
                )
            )
        return decisions

    def store_artifact(
        self,
        workspace: Path,
        *,
        artifact: ArtifactRecord,
        content: str,
    ) -> ArtifactRecord:
        artifact_path = workspace / artifact.storage_path
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(content, encoding="utf-8")
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into artifacts(
                  artifact_id, project_id, artifact_role, title, description, artifact_format, artifact_kind,
                  created_by_task_id, parent_artifact_id, metadata_json, storage_path, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.project_id,
                    artifact.artifact_role,
                    artifact.title,
                    artifact.description,
                    artifact.artifact_format,
                    artifact.artifact_kind,
                    artifact.created_by_task_id,
                    artifact.parent_artifact_id,
                    json_dumps(artifact.metadata),
                    artifact.storage_path,
                    artifact.created_at,
                ),
            )
            connection.commit()
        return artifact

    def load_artifact(self, workspace: Path, artifact_id: str) -> ArtifactRecord:
        with self._connect(workspace) as connection:
            row = connection.execute("select * from artifacts where artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Artifact not found: {artifact_id}")
        return _artifact_from_row(row)

    def load_artifact_content(self, workspace: Path, artifact_id: str) -> str:
        artifact = self.load_artifact(workspace, artifact_id)
        artifact_path = workspace / artifact.storage_path
        if not artifact_path.exists():
            raise NotFoundError(f"Artifact content not found: {artifact.storage_path}")
        return artifact_path.read_text(encoding="utf-8")

    def list_artifacts(self, workspace: Path, artifact_role: str | None = None) -> list[ArtifactRecord]:
        query = "select * from artifacts"
        params: tuple[object, ...] = ()
        if artifact_role is not None:
            query += " where artifact_role = ?"
            params = (artifact_role,)
        query += " order by created_at, artifact_id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def latest_artifact_by_role(self, workspace: Path, artifact_role: str) -> ArtifactRecord | None:
        with self._connect(workspace) as connection:
            row = connection.execute(
                """
                select * from artifacts
                where artifact_role = ?
                order by created_at desc, artifact_id desc
                limit 1
                """,
                (artifact_role,),
            ).fetchone()
        return None if row is None else _artifact_from_row(row)

    def record_context_manifest(self, workspace: Path, manifest: ContextManifest) -> ContextManifest:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into context_manifests(
                  manifest_id, project_id, task_id, template_ref, problem_state_version, budget_json,
                  excluded_items_json, input_fingerprint, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id,
                    manifest.project_id,
                    manifest.task_id,
                    manifest.template_ref,
                    manifest.problem_state_version,
                    json_dumps(manifest.budget),
                    json_dumps(list(manifest.excluded_items)),
                    manifest.input_fingerprint,
                    manifest.created_at,
                ),
            )
            for item in manifest.items:
                connection.execute(
                    """
                    insert into context_manifest_items(
                      item_id, manifest_id, item_type, source_ref, title, content,
                      token_estimate, required, priority
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.item_id,
                        manifest.manifest_id,
                        item.item_type,
                        item.source_ref,
                        item.title,
                        item.content,
                        item.token_estimate,
                        int(item.required),
                        item.priority,
                    ),
                )
            connection.commit()
        return manifest

    def load_context_manifest(self, workspace: Path, manifest_id: str) -> ContextManifest:
        with self._connect(workspace) as connection:
            manifest_row = connection.execute(
                "select * from context_manifests where manifest_id = ?",
                (manifest_id,),
            ).fetchone()
            if manifest_row is None:
                raise NotFoundError(f"Context manifest not found: {manifest_id}")
            item_rows = connection.execute(
                """
                select * from context_manifest_items
                where manifest_id = ?
                order by required desc, priority desc, item_id
                """,
                (manifest_id,),
            ).fetchall()
        return ContextManifest(
            manifest_id=manifest_row["manifest_id"],
            project_id=manifest_row["project_id"],
            task_id=manifest_row["task_id"],
            template_ref=manifest_row["template_ref"],
            problem_state_version=manifest_row["problem_state_version"],
            budget=ContextBudget(**json_loads(manifest_row["budget_json"])),
            items=tuple(_context_item_from_row(row) for row in item_rows),
            excluded_items=tuple(json_loads(manifest_row["excluded_items_json"])),
            input_fingerprint=manifest_row["input_fingerprint"],
            created_at=manifest_row["created_at"],
        )

    def list_context_manifests(self, workspace: Path) -> list[ContextManifest]:
        with self._connect(workspace) as connection:
            manifest_rows = connection.execute(
                """
                select manifest_id, project_id, task_id, template_ref, problem_state_version, budget_json,
                       excluded_items_json, input_fingerprint, created_at
                from context_manifests
                order by created_at, manifest_id
                """
            ).fetchall()
            item_rows = connection.execute(
                """
                select * from context_manifest_items
                order by manifest_id, required desc, priority desc, item_id
                """
            ).fetchall()
        items_by_manifest: dict[str, list[ContextItem]] = {}
        for row in item_rows:
            items_by_manifest.setdefault(row["manifest_id"], []).append(_context_item_from_row(row))
        manifests: list[ContextManifest] = []
        for manifest_row in manifest_rows:
            manifests.append(
                ContextManifest(
                    manifest_id=manifest_row["manifest_id"],
                    project_id=manifest_row["project_id"],
                    task_id=manifest_row["task_id"],
                    template_ref=manifest_row["template_ref"],
                    problem_state_version=manifest_row["problem_state_version"],
                    budget=ContextBudget(**json_loads(manifest_row["budget_json"])),
                    items=tuple(items_by_manifest.get(manifest_row["manifest_id"], [])),
                    excluded_items=tuple(json_loads(manifest_row["excluded_items_json"])),
                    input_fingerprint=manifest_row["input_fingerprint"],
                    created_at=manifest_row["created_at"],
                )
            )
        return manifests

    def record_execution_run(
        self,
        workspace: Path,
        *,
        request: ExecutionRequest,
        result: ExecutionResult,
        traces: tuple[ExecutionTrace, ...],
    ) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into execution_runs(
                  execution_run_id, project_id, task_id, template_ref, provider, model, context_manifest_id,
                  actor, status, output_artifact_ids_json, trace_ids_json, failure_code, failure_message, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.execution_run_id,
                    request.project_id,
                    request.task_id,
                    request.template_ref,
                    request.provider,
                    request.model,
                    request.context_manifest_id,
                    request.actor,
                    result.status,
                    json_dumps([output.artifact_id for output in result.outputs]),
                    json_dumps(list(result.trace_ids)),
                    result.failure_code,
                    result.failure_message,
                    utc_now_iso(),
                ),
            )
            for trace in traces:
                connection.execute(
                    """
                    insert into execution_traces(trace_id, execution_run_id, trace_type, title, content, created_at)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace.trace_id,
                        request.execution_run_id,
                        trace.trace_type,
                        trace.title,
                        trace.content,
                        utc_now_iso(),
                    ),
                )
            connection.commit()

    def list_execution_runs(self, workspace: Path) -> list[dict[str, object]]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from execution_runs order by created_at, execution_run_id").fetchall()
        return [dict(row) for row in rows]

    def list_execution_traces(self, workspace: Path, execution_run_id: str | None = None) -> list[dict[str, object]]:
        query = "select * from execution_traces"
        params: tuple[object, ...] = ()
        if execution_run_id is not None:
            query += " where execution_run_id = ?"
            params = (execution_run_id,)
        query += " order by created_at, trace_id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_validation_run(self, workspace: Path, run: ValidationRun) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into validation_runs(
                  validation_run_id, project_id, task_id, execution_run_id, status, findings_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.validation_run_id,
                    run.project_id,
                    run.task_id,
                    run.execution_run_id,
                    run.status,
                    json_dumps(run.findings),
                    run.created_at,
                ),
            )
            connection.commit()

    def list_validation_runs(self, workspace: Path) -> list[ValidationRun]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from validation_runs order by created_at, validation_run_id").fetchall()
        runs: list[ValidationRun] = []
        for row in rows:
            findings = tuple(ValidationFinding(**finding) for finding in json_loads(row["findings_json"]))
            runs.append(
                ValidationRun(
                    validation_run_id=row["validation_run_id"],
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    execution_run_id=row["execution_run_id"],
                    status=row["status"],
                    findings=findings,
                    created_at=row["created_at"],
                )
            )
        return runs

    def record_escalation_ticket(self, workspace: Path, ticket: EscalationTicket) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into escalation_tickets(
                  escalation_ticket_id, project_id, task_id, reason_code, severity, blocking, summary,
                  details_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.escalation_ticket_id,
                    ticket.project_id,
                    ticket.task_id,
                    ticket.reason_code,
                    ticket.severity,
                    int(ticket.blocking),
                    ticket.summary,
                    json_dumps(ticket.details),
                    ticket.created_at,
                ),
            )
            connection.commit()

    def list_escalations(self, workspace: Path) -> list[EscalationTicket]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from escalation_tickets order by created_at, escalation_ticket_id").fetchall()
        return [
            EscalationTicket(
                escalation_ticket_id=row["escalation_ticket_id"],
                project_id=row["project_id"],
                task_id=row["task_id"],
                reason_code=row["reason_code"],
                severity=row["severity"],
                blocking=bool(row["blocking"]),
                summary=row["summary"],
                details=json_loads(row["details_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _upsert_recipe_progress(self, connection: sqlite3.Connection, task: TaskRecord) -> None:
        connection.execute(
            """
            insert into task_recipe_progress(
              project_id, recipe_id, recipe_version, recipe_step_id, status, task_id, updated_at, note
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(project_id, recipe_id, recipe_version, recipe_step_id)
            do update set
              status = excluded.status,
              task_id = excluded.task_id,
              updated_at = excluded.updated_at,
              note = excluded.note
            """,
            (
                task.project_id,
                task.recipe_id,
                task.recipe_version,
                task.recipe_step_id,
                recipe_progress_status_for_task(task.status),
                task.task_id,
                task.updated_at,
                f"derived from task status {task.status}",
            ),
        )

    @contextmanager
    def _connect(self, workspace: Path):
        workspace.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(workspace / self.DB_FILENAME)
        connection.row_factory = sqlite3.Row
        try:
            self._ensure_schema(connection)
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            create table if not exists problem_snapshots (
              project_id text primary key,
              state_json text not null,
              version integer not null,
              updated_at text not null
            );

            create table if not exists problem_events (
              id integer primary key autoincrement,
              project_id text not null,
              version integer not null,
              patch_type text not null,
              payload_json text not null,
              actor text not null,
              reason text not null,
              created_at text not null
            );

            create table if not exists tasks (
              task_id text primary key,
              project_id text not null,
              template_id text not null,
              template_version text not null,
              template_type text not null,
              template_role text not null,
              recipe_id text not null,
              recipe_version text not null,
              recipe_step_id text not null,
              task_family_key text not null,
              status text not null,
              attempt integer not null,
              created_at text not null,
              updated_at text not null
            );

            create table if not exists task_events (
              id integer primary key autoincrement,
              task_id text not null,
              project_id text not null,
              event_type text not null,
              from_status text,
              to_status text,
              payload_json text not null,
              created_at text not null
            );

            create table if not exists task_recipe_progress (
              project_id text not null,
              recipe_id text not null,
              recipe_version text not null,
              recipe_step_id text not null,
              status text not null,
              task_id text,
              updated_at text not null,
              note text,
              primary key(project_id, recipe_id, recipe_version, recipe_step_id)
            );

            create table if not exists planning_decisions (
              id integer primary key autoincrement,
              project_id text not null,
              created_at text not null,
              decision_json text not null
            );

            create table if not exists artifacts (
              artifact_id text primary key,
              project_id text not null,
              artifact_role text not null,
              title text not null,
              description text,
              artifact_format text not null,
              artifact_kind text not null,
              created_by_task_id text,
              parent_artifact_id text,
              metadata_json text not null,
              storage_path text not null,
              created_at text not null
            );

            create table if not exists context_manifests (
              manifest_id text primary key,
              project_id text not null,
              task_id text not null,
              template_ref text not null,
              problem_state_version integer not null,
              budget_json text not null,
              excluded_items_json text not null,
              input_fingerprint text not null,
              created_at text not null
            );

            create table if not exists context_manifest_items (
              item_id text primary key,
              manifest_id text not null,
              item_type text not null,
              source_ref text not null,
              title text not null,
              content text not null,
              token_estimate integer not null,
              required integer not null,
              priority integer not null
            );

            create table if not exists execution_runs (
              execution_run_id text primary key,
              project_id text not null,
              task_id text not null,
              template_ref text not null,
              provider text not null,
              model text not null,
              context_manifest_id text not null,
              actor text not null,
              status text not null,
              output_artifact_ids_json text not null,
              trace_ids_json text not null,
              failure_code text,
              failure_message text,
              created_at text not null
            );

            create table if not exists execution_traces (
              trace_id text primary key,
              execution_run_id text not null,
              trace_type text not null,
              title text not null,
              content text not null,
              created_at text not null
            );

            create table if not exists validation_runs (
              validation_run_id text primary key,
              project_id text not null,
              task_id text not null,
              execution_run_id text not null,
              status text not null,
              findings_json text not null,
              created_at text not null
            );

            create table if not exists escalation_tickets (
              escalation_ticket_id text primary key,
              project_id text not null,
              task_id text,
              reason_code text not null,
              severity text not null,
              blocking integer not null,
              summary text not null,
              details_json text not null,
              created_at text not null
            );
            """
        )
