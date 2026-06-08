"""RG-B: гейт «дождись полного завершения веера» (sibling_fanout_complete).

Узлы интеграции/проверки/сводки графа реализации потребляют роль, которую
производит веер компонентов. Без гейта они стартовали бы после ПЕРВОГО инстанса
(requires гейтит по присутствию роли). Гейт держит их, пока обёртка веера не
`completed`. Метод `_sibling_fanout_blockers` чистый — тестируем напрямую на
реальном снапшоте реестра.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.planning_service import PlanningService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.tasks import TaskRecord
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

REPO_ROOT = Path(__file__).resolve().parents[1]


def _snapshot():
    snapshot, report = RegistryService(
        FilesystemRegistryLoader(REPO_ROOT / "templates")
    ).validate()
    assert report.is_valid
    return snapshot


def _task(
    *,
    task_id: str,
    template_ref: str,
    template_type: str,
    status: str = "ready",
    parent_task_id: str | None = "parent",
    title: str = "t",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        project_id="p",
        objective_ref="implementation.realize@1.0.0",
        parent_task_id=parent_task_id,
        template_ref=template_ref,
        template_type=template_type,
        title=title,
        status=status,  # type: ignore[arg-type]
        origin_kind="base_child",  # type: ignore[arg-type]
        origin_ref="",
        stable_key=task_id,
        depth=2,
        slot_id=None,
        attempt=1,
        error_message=None,
        created_at="2026-06-08T00:00:00Z",
        updated_at="2026-06-08T00:00:00Z",
    )


def test_integration_blocked_until_fanout_completed() -> None:
    snapshot = _snapshot()
    planner = PlanningService(None)  # метод не использует runtime

    fanout = _task(
        task_id="fan",
        template_ref="implementation.component_implementation_fanout@1.0.0",
        template_type="fan_out",
        status="waiting_for_children",
        title="Реализация компонентов (веер)",
    )
    integration = _task(
        task_id="int",
        template_ref="implementation.system_integration@1.0.0",
        template_type="leaf",
    )
    integration_template = snapshot.resolve_template(
        "implementation.system_integration@1.0.0"
    )

    # Веер не завершён → интеграция заблокирована.
    blockers = planner._sibling_fanout_blockers(
        integration, integration_template, [fanout, integration], snapshot
    )
    assert blockers == ["Реализация компонентов (веер)"]

    # Веер завершён → интеграция свободна.
    fanout_done = _task(
        task_id="fan",
        template_ref="implementation.component_implementation_fanout@1.0.0",
        template_type="fan_out",
        status="completed",
        title="Реализация компонентов (веер)",
    )
    assert planner._sibling_fanout_blockers(
        integration, integration_template, [fanout_done, integration], snapshot
    ) == []


def test_unrelated_leaf_not_blocked_by_fanout() -> None:
    snapshot = _snapshot()
    planner = PlanningService(None)

    fanout = _task(
        task_id="fan",
        template_ref="implementation.component_implementation_fanout@1.0.0",
        template_type="fan_out",
        status="waiting_for_children",
    )
    # scaffold не потребляет роль веера (component_implementation) → не блокируется.
    scaffold = _task(
        task_id="sc",
        template_ref="implementation.scaffold@1.0.0",
        template_type="leaf",
    )
    scaffold_template = snapshot.resolve_template("implementation.scaffold@1.0.0")
    assert planner._sibling_fanout_blockers(
        scaffold, scaffold_template, [fanout, scaffold], snapshot
    ) == []


def test_topo_rank_orders_by_dependency_depth() -> None:
    deps = {"A": [], "B": ["A"], "C": ["B"], "D": ["A"]}
    cache: dict[str, int] = {}
    rank = lambda x: PlanningService._topo_rank(x, deps, cache, set())  # noqa: E731
    assert rank("A") == 0  # лист DAG — корень волны
    assert rank("B") == 1
    assert rank("C") == 2
    assert rank("D") == 1


def test_topo_rank_handles_cycles_and_external_refs() -> None:
    deps = {"A": ["B"], "B": ["A"], "C": ["external"]}
    # Цикл не зацикливает (guard) и даёт ограниченный неотрицательный ранг.
    rank_a = PlanningService._topo_rank("A", deps, {}, set())
    assert isinstance(rank_a, int) and 0 <= rank_a <= len(deps)
    # Внешняя ссылка (нет в карте) не считается зависимостью → ранг 0.
    assert PlanningService._topo_rank("C", deps, {}, set()) == 0


def test_item_dependency_keys_reads_all_edge_shapes() -> None:
    item = {
        "consumed_interfaces": [{"component": "X", "interface": "api"}, {"interface": "z"}],
        "dependencies": ["Y"],
        "depends_on": ["W"],
    }
    assert PlanningService._item_dependency_keys(item) == ["X", "Y", "W"]


def test_different_parent_fanout_does_not_block() -> None:
    snapshot = _snapshot()
    planner = PlanningService(None)

    fanout_other_parent = _task(
        task_id="fan",
        template_ref="implementation.component_implementation_fanout@1.0.0",
        template_type="fan_out",
        status="waiting_for_children",
        parent_task_id="other-parent",
    )
    integration = _task(
        task_id="int",
        template_ref="implementation.system_integration@1.0.0",
        template_type="leaf",
        parent_task_id="parent",
    )
    integration_template = snapshot.resolve_template(
        "implementation.system_integration@1.0.0"
    )
    assert planner._sibling_fanout_blockers(
        integration, integration_template, [fanout_other_parent, integration], snapshot
    ) == []
