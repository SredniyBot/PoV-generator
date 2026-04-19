from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from ..common.errors import NotFoundError
from ..common.serialization import json_dumps, json_loads, to_primitive, utc_now_iso
from ..domain.planning import AdmissionCheck, CandidateEvaluation, PlanningDecision
from ..domain.problem_state import ProblemEvent, ProblemPatch, ProblemState, apply_problem_patch
from ..domain.tasks import (
    TaskEvent,
    TaskRecipeProgress,
    TaskRecord,
    apply_task_command,
    recipe_progress_status_for_task,
)


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
            """
        )
