from __future__ import annotations

import argparse
from pathlib import Path
import sys

from ..application.planning_service import PlanningService
from ..application.project_service import ProjectService
from ..application.registry_service import RegistryService
from ..common.errors import PovGeneratorError
from ..common.serialization import json_dumps, to_primitive
from ..domain.registry import ObjectRef
from ..infrastructure.filesystem_registry import FilesystemRegistryLoader
from ..infrastructure.sqlite_runtime import SqliteRuntime


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[3]
    registry_service = RegistryService(FilesystemRegistryLoader(repo_root / "templates"))
    runtime = SqliteRuntime()
    project_service = ProjectService(runtime)
    planning_service = PlanningService(runtime)

    try:
        _dispatch(args, registry_service, project_service, planning_service)
    except PovGeneratorError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _dispatch(args, registry_service: RegistryService, project_service: ProjectService, planning_service: PlanningService) -> None:
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
            enabled_pack_refs = tuple(args.domain_pack or [])
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
            print(json_dumps({"manifest": bootstrap.manifest, "state": bootstrap.state}))
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

    return parser
