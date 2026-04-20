from __future__ import annotations

import argparse
from pathlib import Path
import sys

from ..application.context_service import ContextService
from ..application.domain_pack_selection_service import DomainPackSelectionService
from ..application.execution_service import ExecutionService
from ..application.planning_service import PlanningService
from ..application.project_service import ProjectService
from ..application.registry_service import RegistryService
from ..application.validation_service import ValidationService
from ..application.workflow_service import WorkflowService
from ..common.env import load_repo_env
from ..common.errors import PovGeneratorError
from ..common.serialization import json_dumps, to_primitive
from ..domain.registry import ObjectRef
from ..infrastructure.filesystem_registry import FilesystemRegistryLoader
from ..infrastructure.sqlite_runtime import SqliteRuntime


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[3]
    load_repo_env(repo_root)
    registry_service = RegistryService(FilesystemRegistryLoader(repo_root / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)
    context_service = ContextService(runtime)
    execution_service = ExecutionService(runtime, context_service)
    validation_service = ValidationService(runtime)
    workflow_service = WorkflowService(runtime, planning_service, execution_service, validation_service)
    domain_pack_selection_service = DomainPackSelectionService()

    try:
        _dispatch(
            args,
            registry_service=registry_service,
            project_service=project_service,
            planning_service=planning_service,
            context_service=context_service,
            execution_service=execution_service,
            validation_service=validation_service,
            workflow_service=workflow_service,
            domain_pack_selection_service=domain_pack_selection_service,
            runtime=runtime,
        )
    except PovGeneratorError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _dispatch(
    args,
    *,
    registry_service: RegistryService,
    project_service: ProjectService,
    planning_service: PlanningService,
    context_service: ContextService,
    execution_service: ExecutionService,
    validation_service: ValidationService,
    workflow_service: WorkflowService,
    domain_pack_selection_service: DomainPackSelectionService,
    runtime: SqliteRuntime,
) -> None:
    if args.entity == "registry":
        if args.action == "validate":
            snapshot, report = registry_service.validate()
            payload = {
                "valid": report.is_valid,
                "summary": to_primitive(registry_service.summary(snapshot)),
                "errors": to_primitive(report.errors),
                "warnings": to_primitive(report.warnings),
            }
            print(json_dumps(payload))
            return
        snapshot = registry_service.load()
        if args.action == "show-template":
            print(json_dumps(snapshot.resolve_template(args.template)))
            return
        if args.action == "show-recipe":
            print(json_dumps(snapshot.resolve_recipe(args.recipe)))
            return
        if args.action == "show-fragment":
            print(json_dumps(snapshot.resolve_recipe_fragment(args.fragment)))
            return
        if args.action == "show-domain-pack":
            print(json_dumps(snapshot.resolve_domain_pack(args.domain_pack)))
            return

    if args.entity == "project":
        if args.action == "init":
            snapshot, report = registry_service.validate()
            if not report.is_valid:
                raise PovGeneratorError("Registry невалиден. Сначала выполните 'povgen registry validate'.")
            request_text = args.request_text or Path(args.request_file).read_text(encoding="utf-8")
            recipe_ref = ObjectRef.parse(args.recipe)
            if args.domain_pack:
                enabled_pack_refs = tuple(sorted(set(args.domain_pack)))
                selection_payload = {
                    "mode": "manual",
                    "provider": None,
                    "model": None,
                    "selected_pack_refs": enabled_pack_refs,
                    "rationale": "Использован явный ручной выбор domain pack.",
                    "confidence": 1.0,
                }
            else:
                selection = domain_pack_selection_service.select_for_request(
                    snapshot,
                    recipe_ref=recipe_ref.as_string(),
                    request_text=request_text.strip(),
                    provider=args.selection_provider,
                    model=args.selection_model,
                )
                enabled_pack_refs = selection.selected_pack_refs
                selection_payload = {
                    "mode": "auto",
                    "provider": selection.provider,
                    "model": selection.model,
                    "selected_pack_refs": enabled_pack_refs,
                    "rationale": selection.rationale,
                    "confidence": selection.confidence,
                }
            bootstrap_recipe = planning_service.build_recipe_bootstrap(
                snapshot,
                recipe_ref.as_string(),
                enabled_domain_pack_refs=enabled_pack_refs,
            )
            bootstrap = project_service.init_project(
                workspace=Path(args.workspace),
                name=args.name,
                recipe_ref=recipe_ref,
                request_text=request_text,
                bootstrap_recipe=bootstrap_recipe,
            )
            project_service.add_fact(
                Path(args.workspace),
                fact_id="domain_pack_selection",
                statement=(
                    "Автоматический selector domain pack выбрал: "
                    f"{', '.join(enabled_pack_refs) if enabled_pack_refs else 'ничего'}. "
                    f"Обоснование: {selection_payload['rationale']}"
                    if selection_payload["mode"] == "auto"
                    else "Использован явный ручной выбор domain pack."
                ),
                source="domain_pack_selector" if selection_payload["mode"] == "auto" else "manual_override",
            )
            print(
                json_dumps(
                    {
                        "manifest": bootstrap.manifest,
                        "state": project_service.load_problem_state(Path(args.workspace)),
                        "domain_pack_selection": selection_payload,
                    }
                )
            )
            return
        if args.action == "show":
            print(json_dumps(project_service.load_manifest(Path(args.workspace))))
            return

    if args.entity == "problem":
        workspace = Path(args.workspace)
        if args.action == "show":
            print(json_dumps(project_service.load_problem_state(workspace)))
            return
        if args.action == "history":
            print(json_dumps(project_service.problem_history(workspace)))
            return
        if args.action == "goal-set":
            print(json_dumps(project_service.set_goal(workspace, args.text)))
            return
        if args.action == "gap-open":
            print(
                json_dumps(
                    project_service.add_gap(
                        workspace,
                        gap_id=args.gap_id,
                        title=args.title,
                        description=args.description,
                        severity=args.severity,
                        blocking=args.blocking,
                    )
                )
            )
            return
        if args.action == "gap-close":
            print(json_dumps(project_service.close_gap(workspace, args.gap_id)))
            return
        if args.action == "readiness-set":
            print(
                json_dumps(
                    project_service.set_readiness(
                        workspace,
                        dimension=args.dimension,
                        status=args.status,
                        blocking=args.blocking,
                        confidence=args.confidence,
                    )
                )
            )
            return
        if args.action == "fact-add":
            print(json_dumps(project_service.add_fact(workspace, args.fact_id, args.statement, args.source)))
            return
        if args.action == "domain-pack-enable":
            snapshot, report = registry_service.validate()
            if not report.is_valid:
                raise PovGeneratorError("Registry невалиден. Сначала выполните 'povgen registry validate'.")
            pack = snapshot.resolve_domain_pack(args.domain_pack)
            print(json_dumps(project_service.enable_domain_pack(workspace, pack)))
            return
        if args.action == "composition-show":
            state = project_service.load_problem_state(workspace)
            print(json_dumps(state.recipe_composition))
            return

    if args.entity == "plan":
        workspace = Path(args.workspace)
        snapshot, report = registry_service.validate()
        if not report.is_valid:
            raise PovGeneratorError("Registry невалиден. Сначала выполните 'povgen registry validate'.")
        if args.action == "dry-run":
            print(json_dumps(planning_service.plan(workspace, snapshot, mode="dry-run")))
            return
        if args.action == "apply":
            print(json_dumps(planning_service.plan(workspace, snapshot, mode="apply")))
            return
        if args.action == "history":
            print(json_dumps(planning_service.planning_history(workspace)))
            return
        if args.action == "show-composed-recipe":
            print(json_dumps(planning_service.current_composed_recipe(workspace, snapshot)))
            return

    if args.entity == "tasks":
        workspace = Path(args.workspace)
        if args.action == "list":
            print(json_dumps(planning_service.list_tasks(workspace)))
            return
        if args.action == "events":
            print(json_dumps(planning_service.list_task_events(workspace, task_id=args.task_id)))
            return
        if args.action == "transition":
            print(json_dumps(planning_service.transition_task(workspace, args.task_id, args.command)))
            return
        if args.action == "recipe-progress":
            manifest = project_service.load_manifest(workspace)
            print(json_dumps(planning_service.list_recipe_progress(workspace, manifest.recipe_ref)))
            return

    if args.entity == "artifacts":
        workspace = Path(args.workspace)
        if args.action == "list":
            print(json_dumps(runtime.list_artifacts(workspace, artifact_role=args.role)))
            return
        if args.action == "show":
            artifact = runtime.load_artifact(workspace, args.artifact_id)
            payload = {"record": artifact, "content": runtime.load_artifact_content(workspace, args.artifact_id)}
            print(json_dumps(payload))
            return

    if args.entity == "context":
        workspace = Path(args.workspace)
        snapshot, report = registry_service.validate()
        if not report.is_valid:
            raise PovGeneratorError("Registry невалиден. Сначала выполните 'povgen registry validate'.")
        if args.action == "build":
            print(json_dumps(context_service.build_for_task(workspace, snapshot, args.task_id).manifest))
            return

    if args.entity == "execute":
        workspace = Path(args.workspace)
        snapshot, report = registry_service.validate()
        if not report.is_valid:
            raise PovGeneratorError("Registry невалиден. Сначала выполните 'povgen registry validate'.")
        if args.action == "task":
            print(
                json_dumps(
                    execution_service.execute_task(
                        workspace,
                        snapshot,
                        args.task_id,
                        provider=args.provider,
                        model=args.model,
                    )
                )
            )
            return
        if args.action == "runs":
            print(json_dumps(runtime.list_execution_runs(workspace)))
            return
        if args.action == "traces":
            print(json_dumps(runtime.list_execution_traces(workspace, execution_run_id=args.execution_run_id)))
            return

    if args.entity == "validation":
        workspace = Path(args.workspace)
        if args.action == "runs":
            print(json_dumps(runtime.list_validation_runs(workspace)))
            return
        if args.action == "escalations":
            print(json_dumps(runtime.list_escalations(workspace)))
            return

    if args.entity == "workflow":
        workspace = Path(args.workspace)
        snapshot, report = registry_service.validate()
        if not report.is_valid:
            raise PovGeneratorError("Registry невалиден. Сначала выполните 'povgen registry validate'.")
        if args.action == "run-next":
            print(
                json_dumps(
                    workflow_service.run_next(
                        workspace,
                        snapshot,
                        provider=args.provider,
                        model=args.model,
                    )
                )
            )
            return
        if args.action == "run-until-blocked":
            print(
                json_dumps(
                    workflow_service.run_until_blocked(
                        workspace,
                        snapshot,
                        provider=args.provider,
                        model=args.model,
                        max_steps=args.max_steps,
                    )
                )
            )
            return

    raise PovGeneratorError("Неподдерживаемая команда.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="povgen")
    subparsers = parser.add_subparsers(dest="entity", required=True)

    registry = subparsers.add_parser("registry")
    registry_subparsers = registry.add_subparsers(dest="action", required=True)
    registry_subparsers.add_parser("validate")
    show_template = registry_subparsers.add_parser("show-template")
    show_template.add_argument("--template", required=True)
    show_recipe = registry_subparsers.add_parser("show-recipe")
    show_recipe.add_argument("--recipe", required=True)
    show_fragment = registry_subparsers.add_parser("show-fragment")
    show_fragment.add_argument("--fragment", required=True)
    show_domain_pack = registry_subparsers.add_parser("show-domain-pack")
    show_domain_pack.add_argument("--domain-pack", required=True)

    project = subparsers.add_parser("project")
    project_subparsers = project.add_subparsers(dest="action", required=True)
    project_init = project_subparsers.add_parser("init")
    project_init.add_argument("--workspace", required=True)
    project_init.add_argument("--name", required=True)
    project_init.add_argument("--recipe", required=True)
    project_init.add_argument("--domain-pack", action="append", default=[])
    project_init.add_argument("--selection-provider")
    project_init.add_argument("--selection-model")
    request_group = project_init.add_mutually_exclusive_group(required=True)
    request_group.add_argument("--request-text")
    request_group.add_argument("--request-file")
    project_show = project_subparsers.add_parser("show")
    project_show.add_argument("--workspace", required=True)

    problem = subparsers.add_parser("problem")
    problem_subparsers = problem.add_subparsers(dest="action", required=True)
    for action in ("show", "history", "composition-show"):
        command = problem_subparsers.add_parser(action)
        command.add_argument("--workspace", required=True)
    goal_set = problem_subparsers.add_parser("goal-set")
    goal_set.add_argument("--workspace", required=True)
    goal_set.add_argument("--text", required=True)
    gap_open = problem_subparsers.add_parser("gap-open")
    gap_open.add_argument("--workspace", required=True)
    gap_open.add_argument("--gap-id", required=True)
    gap_open.add_argument("--title", required=True)
    gap_open.add_argument("--description", required=True)
    gap_open.add_argument("--severity", default="medium")
    gap_open.add_argument("--blocking", action=argparse.BooleanOptionalAction, default=True)
    gap_close = problem_subparsers.add_parser("gap-close")
    gap_close.add_argument("--workspace", required=True)
    gap_close.add_argument("--gap-id", required=True)
    readiness_set = problem_subparsers.add_parser("readiness-set")
    readiness_set.add_argument("--workspace", required=True)
    readiness_set.add_argument("--dimension", required=True)
    readiness_set.add_argument("--status", required=True)
    readiness_set.add_argument("--blocking", action=argparse.BooleanOptionalAction, default=True)
    readiness_set.add_argument("--confidence", type=float, default=1.0)
    fact_add = problem_subparsers.add_parser("fact-add")
    fact_add.add_argument("--workspace", required=True)
    fact_add.add_argument("--fact-id", required=True)
    fact_add.add_argument("--statement", required=True)
    fact_add.add_argument("--source", required=True)
    enable_pack = problem_subparsers.add_parser("domain-pack-enable")
    enable_pack.add_argument("--workspace", required=True)
    enable_pack.add_argument("--domain-pack", required=True)

    plan = subparsers.add_parser("plan")
    plan_subparsers = plan.add_subparsers(dest="action", required=True)
    for action in ("dry-run", "apply", "history", "show-composed-recipe"):
        command = plan_subparsers.add_parser(action)
        command.add_argument("--workspace", required=True)

    tasks = subparsers.add_parser("tasks")
    tasks_subparsers = tasks.add_subparsers(dest="action", required=True)
    task_list = tasks_subparsers.add_parser("list")
    task_list.add_argument("--workspace", required=True)
    task_events = tasks_subparsers.add_parser("events")
    task_events.add_argument("--workspace", required=True)
    task_events.add_argument("--task-id")
    task_transition = tasks_subparsers.add_parser("transition")
    task_transition.add_argument("--workspace", required=True)
    task_transition.add_argument("--task-id", required=True)
    task_transition.add_argument("--command", required=True)
    recipe_progress = tasks_subparsers.add_parser("recipe-progress")
    recipe_progress.add_argument("--workspace", required=True)

    artifacts = subparsers.add_parser("artifacts")
    artifacts_subparsers = artifacts.add_subparsers(dest="action", required=True)
    artifact_list = artifacts_subparsers.add_parser("list")
    artifact_list.add_argument("--workspace", required=True)
    artifact_list.add_argument("--role")
    artifact_show = artifacts_subparsers.add_parser("show")
    artifact_show.add_argument("--workspace", required=True)
    artifact_show.add_argument("--artifact-id", required=True)

    context = subparsers.add_parser("context")
    context_subparsers = context.add_subparsers(dest="action", required=True)
    context_build = context_subparsers.add_parser("build")
    context_build.add_argument("--workspace", required=True)
    context_build.add_argument("--task-id", required=True)

    execute = subparsers.add_parser("execute")
    execute_subparsers = execute.add_subparsers(dest="action", required=True)
    execute_task = execute_subparsers.add_parser("task")
    execute_task.add_argument("--workspace", required=True)
    execute_task.add_argument("--task-id", required=True)
    execute_task.add_argument("--provider", default="stub")
    execute_task.add_argument("--model")
    execute_runs = execute_subparsers.add_parser("runs")
    execute_runs.add_argument("--workspace", required=True)
    execute_traces = execute_subparsers.add_parser("traces")
    execute_traces.add_argument("--workspace", required=True)
    execute_traces.add_argument("--execution-run-id")

    validation = subparsers.add_parser("validation")
    validation_subparsers = validation.add_subparsers(dest="action", required=True)
    validation_runs = validation_subparsers.add_parser("runs")
    validation_runs.add_argument("--workspace", required=True)
    escalations = validation_subparsers.add_parser("escalations")
    escalations.add_argument("--workspace", required=True)

    workflow = subparsers.add_parser("workflow")
    workflow_subparsers = workflow.add_subparsers(dest="action", required=True)
    run_next = workflow_subparsers.add_parser("run-next")
    run_next.add_argument("--workspace", required=True)
    run_next.add_argument("--provider", default="stub")
    run_next.add_argument("--model")
    run_until_blocked = workflow_subparsers.add_parser("run-until-blocked")
    run_until_blocked.add_argument("--workspace", required=True)
    run_until_blocked.add_argument("--provider", default="stub")
    run_until_blocked.add_argument("--model")
    run_until_blocked.add_argument("--max-steps", type=int, default=20)

    return parser
