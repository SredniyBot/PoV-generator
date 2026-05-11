from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from ..common.errors import NotFoundError
from ..common.serialization import json_dumps, json_loads, to_primitive, utc_now_iso
from ..domain.artifacts import ArtifactRecord, ContextBudget, ContextItem, ContextManifest
from ..domain.clarifications import ClarificationCandidate, ClarificationOption, ClarificationRequest
from ..domain.execution import ExecutionRequest, ExecutionResult, ExecutionTrace
from ..domain.planning import AdmissionCheck, CandidateEvaluation, PlanningDecision
from ..domain.problem_state import (
    ActiveDomainPackRecord,
    ActiveMethodologyPackRecord,
    DomainSignalRecord,
    FactRecord,
    GapRecord,
    ProblemEvent,
    ProblemPatch,
    ProblemState,
    ReadinessRecord,
    apply_problem_patch,
)
from ..domain.tasks import TaskEvent, TaskRecord, apply_task_command
from ..domain.validation import EscalationTicket, ValidationFinding, ValidationRun


@dataclass(frozen=True)
class ProjectManifest:
    project_id: str
    name: str
    objective_ref: str
    created_at: str


def _problem_state_to_dict(state: ProblemState) -> dict[str, object]:
    return to_primitive(state)


def _problem_state_from_dict(payload: dict) -> ProblemState:
    return ProblemState(
        project_id=payload["project_id"],
        objective_ref=payload["objective_ref"],
        root_task_id=payload.get("root_task_id"),
        business_request=payload["business_request"],
        goal=payload.get("goal"),
        known_facts={key: FactRecord(**value) for key, value in payload.get("known_facts", {}).items()},
        assumptions={key: FactRecord(**value) for key, value in payload.get("assumptions", {}).items()},
        constraints={key: FactRecord(**value) for key, value in payload.get("constraints", {}).items()},
        risks={key: FactRecord(**value) for key, value in payload.get("risks", {}).items()},
        active_gaps={key: GapRecord(**value) for key, value in payload.get("active_gaps", {}).items()},
        decisions={key: FactRecord(**value) for key, value in payload.get("decisions", {}).items()},
        readiness={key: ReadinessRecord(**value) for key, value in payload.get("readiness", {}).items()},
        domain_signals={key: DomainSignalRecord(**value) for key, value in payload.get("domain_signals", {}).items()},
        active_domain_packs={
            key: ActiveDomainPackRecord(**value) for key, value in payload.get("active_domain_packs", {}).items()
        },
        active_methodology_packs={
            key: ActiveMethodologyPackRecord(**value)
            for key, value in payload.get("active_methodology_packs", {}).items()
        },
        clarification_mode=payload.get("clarification_mode", "balanced"),
        version=int(payload.get("version", 0)),
        updated_at=payload.get("updated_at", utc_now_iso()),
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        project_id=row["project_id"],
        objective_ref=row["objective_ref"],
        parent_task_id=row["parent_task_id"],
        template_ref=row["template_ref"],
        template_type=row["template_type"],
        title=row["title"],
        status=row["status"],
        origin_kind=row["origin_kind"],
        origin_ref=row["origin_ref"],
        stable_key=row["stable_key"],
        depth=row["depth"],
        slot_id=row["slot_id"],
        attempt=row["attempt"],
        error_message=row["error_message"],
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


def _option_from_dict(payload: dict[str, object]) -> ClarificationOption:
    raw_confidence = payload.get("confidence")
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool) else None
    return ClarificationOption(
        option_id=str(payload.get("option_id", "")),
        label=str(payload.get("label", "")),
        description=str(payload.get("description", "")),
        effect_preview=str(payload.get("effect_preview", "")),
        confidence=confidence,
    )


def _fallback_clarification_description(payload: dict[str, object]) -> str:
    question = str(payload.get("question", "")).strip()
    rationale = str(payload.get("rationale", "")).strip()
    impact = str(payload.get("impact", "")).strip()
    parts = [
        rationale or "Система обнаружила неопределенность, которую нельзя надежно закрыть из текущего контекста.",
        f"Необходимо ответить на вопрос: {question}" if question else "",
        impact or "Ответ будет использован при дальнейшей детализации требований и планировании следующих задач.",
    ]
    return " ".join(part for part in parts if part)


def _candidate_from_row(row: sqlite3.Row) -> ClarificationCandidate:
    payload = json_loads(row["payload_json"])
    return ClarificationCandidate(
        candidate_id=row["candidate_id"],
        project_id=row["project_id"],
        source_type=payload["source_type"],
        source_id=payload["source_id"],
        need=payload["need"],
        question=payload["question"],
        description=payload.get("description") or _fallback_clarification_description(payload),
        rationale=payload["rationale"],
        impact=payload["impact"],
        severity=payload["severity"],
        confidence_without_user=float(payload["confidence_without_user"]),
        min_participation_mode=payload.get("min_participation_mode", "balanced"),
        default_assumption=payload.get("default_assumption"),
        recommended_answer=payload.get("recommended_answer"),
        answer_mode=payload["answer_mode"],
        options=tuple(_option_from_dict(item) for item in payload.get("options", [])),
        affected_task_ids=tuple(payload.get("affected_task_ids", [])),
        related_artifact_ids=tuple(payload.get("related_artifact_ids", [])),
        blocking_scope=payload.get("blocking_scope", "task"),
        decision_owner_role=payload.get("decision_owner_role", "business"),
        created_at=row["created_at"],
    )


def _request_from_row(row: sqlite3.Row) -> ClarificationRequest:
    # decision_owner_role — поле, добавленное в W1.2 миграцией; sqlite3.Row
    # не поддерживает .get(), поэтому идём через keys() с дефолтом "business"
    # для существующих записей.
    decision_owner_role = (
        row["decision_owner_role"]
        if "decision_owner_role" in row.keys()
        else "business"
    )
    return ClarificationRequest(
        request_id=row["request_id"],
        project_id=row["project_id"],
        status=row["status"],
        priority=row["priority"],
        title=row["title"],
        question=row["question"],
        description=row["description"] or _fallback_clarification_description(
            {"question": row["question"], "rationale": row["reason"], "impact": row["impact"]}
        ),
        reason=row["reason"],
        impact=row["impact"],
        answer_mode=row["answer_mode"],
        options=tuple(_option_from_dict(item) for item in json_loads(row["options_json"])),
        recommended_option_id=row["recommended_option_id"],
        min_participation_mode=row["min_participation_mode"],
        default_assumption=row["default_assumption"],
        affected_task_ids=tuple(json_loads(row["affected_task_ids_json"])),
        related_artifact_ids=tuple(json_loads(row["related_artifact_ids_json"])),
        blocking_scope=row["blocking_scope"],
        decision_owner_role=decision_owner_role or "business",
        source_type=row["source_type"],
        source_id=row["source_id"],
        created_from_candidate_ids=tuple(json_loads(row["created_from_candidate_ids_json"])),
        selected_option_ids=tuple(json_loads(row["selected_option_ids_json"])),
        free_text=row["free_text"],
        resolution_summary=row["resolution_summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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
        (workspace / self.MANIFEST_FILENAME).write_text(json_dumps(manifest), encoding="utf-8")
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into problem_snapshots(project_id, state_json, version, updated_at)
                values (?, ?, ?, ?)
                """,
                (
                    manifest.project_id,
                    json_dumps(_problem_state_to_dict(initial_state)),
                    initial_state.version,
                    initial_state.updated_at,
                ),
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
        return ProjectManifest(**json_loads(manifest_path.read_text(encoding="utf-8")))

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
                    json_dumps(to_primitive(patch)),
                    actor,
                    reason,
                    next_state.updated_at,
                ),
            )
            connection.commit()
        return next_state

    def create_task(self, workspace: Path, task: TaskRecord) -> TaskRecord:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into tasks(
                  task_id, project_id, objective_ref, parent_task_id, template_ref, template_type, title, status,
                  origin_kind, origin_ref, stable_key, depth, slot_id, attempt, error_message, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.project_id,
                    task.objective_ref,
                    task.parent_task_id,
                    task.template_ref,
                    task.template_type,
                    task.title,
                    task.status,
                    task.origin_kind,
                    task.origin_ref,
                    task.stable_key,
                    task.depth,
                    task.slot_id,
                    task.attempt,
                    task.error_message,
                    task.created_at,
                    task.updated_at,
                ),
            )
            self._insert_task_event(connection, task, "task_created", None, task.status, {"stable_key": task.stable_key})
            connection.commit()
        return task

    def list_tasks(self, workspace: Path) -> list[TaskRecord]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from tasks order by depth, created_at, task_id").fetchall()
        return [_task_from_row(row) for row in rows]

    def get_task(self, workspace: Path, task_id: str) -> TaskRecord:
        with self._connect(workspace) as connection:
            row = connection.execute("select * from tasks where task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return _task_from_row(row)

    def find_task_by_stable_key(self, workspace: Path, stable_key: str) -> TaskRecord | None:
        with self._connect(workspace) as connection:
            row = connection.execute("select * from tasks where stable_key = ?", (stable_key,)).fetchone()
        return None if row is None else _task_from_row(row)

    def transition_task(
        self,
        workspace: Path,
        task_id: str,
        command: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> TaskRecord:
        task = self.get_task(workspace, task_id)
        error_message = None
        if payload:
            raw_error = payload.get("error_message") or payload.get("reason")
            error_message = str(raw_error) if raw_error else None
        next_task = apply_task_command(task, command, error_message=error_message)
        if next_task == task:
            return task
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update tasks set status = ?, attempt = ?, error_message = ?, updated_at = ?
                where task_id = ?
                """,
                (next_task.status, next_task.attempt, next_task.error_message, next_task.updated_at, next_task.task_id),
            )
            self._insert_task_event(connection, next_task, command, task.status, next_task.status, payload or {})
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
            rows = connection.execute("select decision_json from planning_decisions order by id").fetchall()
        decisions = []
        for row in rows:
            payload = json_loads(row["decision_json"])
            candidates = []
            for candidate_raw in payload.get("candidates", []):
                checks = tuple(AdmissionCheck(**check_raw) for check_raw in candidate_raw.get("checks", []))
                candidates.append(
                    CandidateEvaluation(
                        task_id=candidate_raw["task_id"],
                        task_key=candidate_raw["task_key"],
                        title=candidate_raw["title"],
                        template_ref=candidate_raw["template_ref"],
                        admissible=candidate_raw["admissible"],
                        score=candidate_raw["score"],
                        checks=checks,
                        reasons=tuple(candidate_raw.get("reasons", [])),
                    )
                )
            decisions.append(
                PlanningDecision(
                    decision_id=payload["decision_id"],
                    project_id=payload["project_id"],
                    objective_ref=payload["objective_ref"],
                    mode=payload["mode"],
                    outcome=payload["outcome"],
                    selected_task_id=payload.get("selected_task_id"),
                    selected_task_key=payload.get("selected_task_key"),
                    selected_template_ref=payload.get("selected_template_ref"),
                    admitted_task_ids=tuple(payload.get("admitted_task_ids", [])),
                    blocked_task_summaries=tuple(payload.get("blocked_task_summaries", [])),
                    ranking_strategy=payload.get("ranking_strategy", "deterministic"),
                    candidates=tuple(candidates),
                    reasons=tuple(payload.get("reasons", [])),
                    created_at=payload.get("created_at", ""),
                )
            )
        return decisions

    def store_artifact(self, workspace: Path, *, artifact: ArtifactRecord, content: str) -> ArtifactRecord:
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
            manifest_row = connection.execute("select * from context_manifests where manifest_id = ?", (manifest_id,)).fetchone()
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
            manifest_rows = connection.execute("select * from context_manifests order by created_at, manifest_id").fetchall()
            item_rows = connection.execute("select * from context_manifest_items order by manifest_id, required desc, priority desc, item_id").fetchall()
        items_by_manifest: dict[str, list[ContextItem]] = {}
        for row in item_rows:
            items_by_manifest.setdefault(row["manifest_id"], []).append(_context_item_from_row(row))
        return [
            ContextManifest(
                manifest_id=row["manifest_id"],
                project_id=row["project_id"],
                task_id=row["task_id"],
                template_ref=row["template_ref"],
                problem_state_version=row["problem_state_version"],
                budget=ContextBudget(**json_loads(row["budget_json"])),
                items=tuple(items_by_manifest.get(row["manifest_id"], [])),
                excluded_items=tuple(json_loads(row["excluded_items_json"])),
                input_fingerprint=row["input_fingerprint"],
                created_at=row["created_at"],
            )
            for row in manifest_rows
        ]

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
                    (trace.trace_id, request.execution_run_id, trace.trace_type, trace.title, trace.content, utc_now_iso()),
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
                  validation_run_id, project_id, task_id, execution_run_id, status, findings_json,
                  clarification_candidate_ids_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.validation_run_id,
                    run.project_id,
                    run.task_id,
                    run.execution_run_id,
                    run.status,
                    json_dumps(run.findings),
                    json_dumps(run.clarification_candidate_ids),
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
                    clarification_candidate_ids=tuple(json_loads(row["clarification_candidate_ids_json"] or "[]")),
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

    def record_clarification_candidate(self, workspace: Path, candidate: ClarificationCandidate) -> ClarificationCandidate:
        created_at = candidate.created_at or utc_now_iso()
        payload = to_primitive(candidate)
        payload["created_at"] = created_at
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert or ignore into clarification_candidates(
                  candidate_id, project_id, source_type, source_id, payload_json, created_at
                )
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.project_id,
                    candidate.source_type,
                    candidate.source_id,
                    json_dumps(payload),
                    created_at,
                ),
            )
            connection.commit()
        return candidate if candidate.created_at else replace(candidate, created_at=created_at)

    def list_clarification_candidates(self, workspace: Path) -> list[ClarificationCandidate]:
        with self._connect(workspace) as connection:
            rows = connection.execute(
                "select * from clarification_candidates order by created_at, candidate_id"
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def find_clarification_by_source(
        self,
        workspace: Path,
        *,
        source_type: str,
        source_id: str,
        question: str,
    ) -> ClarificationRequest | None:
        with self._connect(workspace) as connection:
            row = connection.execute(
                """
                select * from clarification_requests
                where source_type = ? and source_id = ? and question = ?
                  and status in ('open', 'answered', 'assumed', 'deferred')
                order by created_at desc
                limit 1
                """,
                (source_type, source_id, question),
            ).fetchone()
        return None if row is None else _request_from_row(row)

    def create_clarification_request(self, workspace: Path, request: ClarificationRequest) -> ClarificationRequest:
        now = request.created_at or utc_now_iso()
        updated_at = request.updated_at or now
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into clarification_requests(
                  request_id, project_id, status, priority, title, question, description, reason, impact,
                  answer_mode, options_json, recommended_option_id, min_participation_mode, default_assumption,
                  affected_task_ids_json, related_artifact_ids_json, blocking_scope, decision_owner_role,
                  source_type, source_id, created_from_candidate_ids_json,
                  selected_option_ids_json, free_text, resolution_summary, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.project_id,
                    request.status,
                    request.priority,
                    request.title,
                    request.question,
                    request.description,
                    request.reason,
                    request.impact,
                    request.answer_mode,
                    json_dumps(request.options),
                    request.recommended_option_id,
                    request.min_participation_mode,
                    request.default_assumption,
                    json_dumps(request.affected_task_ids),
                    json_dumps(request.related_artifact_ids),
                    request.blocking_scope,
                    request.decision_owner_role,
                    request.source_type,
                    request.source_id,
                    json_dumps(request.created_from_candidate_ids),
                    json_dumps(request.selected_option_ids),
                    request.free_text,
                    request.resolution_summary,
                    now,
                    updated_at,
                ),
            )
            connection.commit()
        return self.get_clarification_request(workspace, request.request_id)

    def list_clarification_requests(
        self,
        workspace: Path,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> list[ClarificationRequest]:
        query = "select * from clarification_requests"
        params: tuple[object, ...] = ()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" where status in ({placeholders})"
            params = statuses
        query += " order by created_at, request_id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [_request_from_row(row) for row in rows]

    def get_clarification_request(self, workspace: Path, request_id: str) -> ClarificationRequest:
        with self._connect(workspace) as connection:
            row = connection.execute(
                "select * from clarification_requests where request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Clarification request not found: {request_id}")
        return _request_from_row(row)

    def answer_clarification_request(
        self,
        workspace: Path,
        request_id: str,
        *,
        selected_option_ids: tuple[str, ...],
        free_text: str | None,
        resolution_summary: str,
    ) -> ClarificationRequest:
        now = utc_now_iso()
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update clarification_requests
                set status = 'answered',
                    selected_option_ids_json = ?,
                    free_text = ?,
                    resolution_summary = ?,
                    updated_at = ?
                where request_id = ?
                """,
                (json_dumps(selected_option_ids), free_text, resolution_summary, now, request_id),
            )
            connection.commit()
        return self.get_clarification_request(workspace, request_id)

    def accept_clarification_assumption(self, workspace: Path, request_id: str, *, resolution_summary: str) -> ClarificationRequest:
        now = utc_now_iso()
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update clarification_requests
                set status = 'assumed',
                    resolution_summary = ?,
                    updated_at = ?
                where request_id = ?
                """,
                (resolution_summary, now, request_id),
            )
            connection.commit()
        return self.get_clarification_request(workspace, request_id)

    def _insert_task_event(
        self,
        connection: sqlite3.Connection,
        task: TaskRecord,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            """
            insert into task_events(task_id, project_id, event_type, from_status, to_status, payload_json, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (task.task_id, task.project_id, event_type, from_status, to_status, json_dumps(payload), utc_now_iso()),
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
              objective_ref text not null,
              parent_task_id text,
              template_ref text not null,
              template_type text not null,
              title text not null,
              status text not null,
              origin_kind text not null,
              origin_ref text not null,
              stable_key text not null unique,
              depth integer not null,
              slot_id text,
              attempt integer not null,
              error_message text,
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
              clarification_candidate_ids_json text not null default '[]',
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

            create table if not exists clarification_candidates (
              candidate_id text primary key,
              project_id text not null,
              source_type text not null,
              source_id text not null,
              payload_json text not null,
              created_at text not null
            );

            create table if not exists clarification_requests (
              request_id text primary key,
              project_id text not null,
              status text not null,
              priority text not null,
              title text not null,
              question text not null,
              description text not null default '',
              reason text not null,
              impact text not null,
              answer_mode text not null,
              options_json text not null,
              recommended_option_id text,
              min_participation_mode text not null default 'balanced',
              default_assumption text,
              affected_task_ids_json text not null,
              related_artifact_ids_json text not null,
              blocking_scope text not null,
              source_type text not null,
              source_id text not null,
              created_from_candidate_ids_json text not null,
              selected_option_ids_json text not null,
              free_text text,
              resolution_summary text,
              created_at text not null,
              updated_at text not null
            );
            """
        )
        self._ensure_column(
            connection,
            "validation_runs",
            "clarification_candidate_ids_json",
            "text not null default '[]'",
        )
        self._ensure_column(
            connection,
            "clarification_requests",
            "description",
            "text not null default ''",
        )
        self._ensure_column(
            connection,
            "clarification_requests",
            "min_participation_mode",
            "text not null default 'balanced'",
        )
        self._ensure_column(
            connection,
            "clarification_requests",
            "decision_owner_role",
            "text not null default 'business'",
        )

    def _ensure_column(self, connection: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        columns = {row["name"] for row in connection.execute(f"pragma table_info({table_name})").fetchall()}
        if column_name not in columns:
            connection.execute(f"alter table {table_name} add column {column_name} {ddl}")
