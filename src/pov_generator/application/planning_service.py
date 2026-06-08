from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..common.errors import NotFoundError
from ..common.logging import get_logger
from ..common.serialization import json_loads, utc_now_iso
from ..domain.planning import AdmissionCheck, CandidateEvaluation, PlanningDecision
from ..domain.process_state import SetRootTaskPatch
from ..domain.project_state import ProjectState
from ..domain.registry import RegistrySnapshot, TemplateSpec
from ..domain.tasks import TaskRecord, initial_task_status
from ..infrastructure.sqlite_runtime import SqliteRuntime


def _resolve_state_field(state: ProjectState, field_name: str) -> object | None:
    """Получить значение требуемого «поля состояния» из нового двухслойного state.

    Шаблоны декларируют ``requires.state: [...]`` со старыми именами полей.
    Здесь мы транслируем эти имена в актуальные источники:

    - ``business_request`` — иммутабельное поле manifest;
    - ``goal`` — формулировка положения ``project.goal`` в Layer A.

    Возвращает ``None`` или пустую строку, если значения нет — это сигнал
    admission'у заблокировать задачу.
    """
    if field_name == "business_request":
        return state.manifest.business_request
    if field_name == "goal":
        return state.knowledge.goal_statement()
    return None


logger = get_logger("planner")

# Потолок числа инстансов одного fan-out. Защита от разрастания графа задач,
# если массив-источник внезапно огромен. Переопределяется
# POV_MAX_FAN_OUT_INSTANCES (целое > 0).
DEFAULT_MAX_FAN_OUT_INSTANCES = 100


def _max_fan_out_instances() -> int:
    raw = os.environ.get("POV_MAX_FAN_OUT_INSTANCES", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_FAN_OUT_INSTANCES
        if value > 0:
            return value
    return DEFAULT_MAX_FAN_OUT_INSTANCES


def _trailing_attempt(stable_key: str) -> int | None:
    """Номер попытки из хвоста stable_key инстанса fan-out (``…:<attempt>``)."""
    try:
        return int(stable_key.rsplit(":", 1)[-1])
    except ValueError:
        return None


def _safe_resolve_template(snapshot: RegistrySnapshot, template_ref: str) -> TemplateSpec | None:
    """Резолв шаблона СУЩЕСТВУЮЩЕЙ задачи с устойчивостью к дрейфу реестра.

    Старый проект может ссылаться на шаблон, удалённый из templates/ (например
    `common.ambiguity_gap_analysis@1.0.0` после слияния разбора запроса) — и
    snapshot пиннинг его не воскресит. Чтобы это не роняло загрузку проекта
    (план/граф/обзор), такую «осиротевшую» задачу пропускаем (None), а не
    падаем NotFoundError. Сама задача остаётся в БД и видна в графе со своим
    сохранённым статусом."""
    try:
        return snapshot.resolve_template(template_ref)
    except NotFoundError:
        logger.warning(f"шаблон задачи не найден в реестре (дрейф) — задача пропущена: {template_ref}")
        return None


# --- статический скелет графа (для подвкладок гейтов, Ф1) ---------------------


@dataclass(frozen=True)
class SkeletonNode:
    """Узел статического скелета графа задач цели — построен ТОЛЬКО из реестра,
    без runtime. Используется для предпросмотра графа ещё не запущенного гейта.
    """

    template_ref: str
    template_type: str
    title: str
    origin_kind: str
    origin_ref: str
    slot_id: str | None
    depth: int
    children: tuple["SkeletonNode", ...] = field(default_factory=tuple)


def _skeleton_from_template(
    snapshot: RegistrySnapshot,
    *,
    template: TemplateSpec,
    active_pack_refs: tuple[str, ...],
    origin_kind: str,
    origin_ref: str,
    slot_id: str | None,
    depth: int,
    seen: frozenset[str],
) -> SkeletonNode:
    ref_str = template.ref.as_string()
    children: list[SkeletonNode] = []
    # Композит раскрываем по тем же правилам, что и runtime (_expand_composites):
    # фиксированные children + вклады активных доменных пакетов по слотам.
    # fan_out НЕ раскрываем — его инстансы зависят от артефактов прогона; он
    # остаётся одним нераскрытым узлом-заглушкой.
    if template.template_type == "composite" and ref_str not in seen:
        seen = seen | {ref_str}
        for child in template.children:
            child_template = snapshot.resolve_template(child.task_ref)
            children.append(
                _skeleton_from_template(
                    snapshot,
                    template=child_template,
                    active_pack_refs=active_pack_refs,
                    origin_kind="base_child",
                    origin_ref=child.identifier,
                    slot_id=None,
                    depth=depth + 1,
                    seen=seen,
                )
            )
        for slot in template.slots:
            for pack_ref in active_pack_refs:
                try:
                    pack = snapshot.resolve_domain_pack(pack_ref)
                except Exception:
                    continue
                for contribution in pack.contributions:
                    if contribution.slot_id != slot.identifier:
                        continue
                    for item in contribution.items:
                        if item.task_ref is None:
                            continue
                        contribution_template = snapshot.resolve_template(item.task_ref)
                        children.append(
                            _skeleton_from_template(
                                snapshot,
                                template=contribution_template,
                                active_pack_refs=active_pack_refs,
                                origin_kind="domain_contribution",
                                origin_ref=f"{pack.ref.as_string()}:{item.identifier}",
                                slot_id=slot.identifier,
                                depth=depth + 1,
                                seen=seen,
                            )
                        )
    return SkeletonNode(
        template_ref=ref_str,
        template_type=template.template_type,
        title=template.title,
        origin_kind=origin_kind,
        origin_ref=origin_ref,
        slot_id=slot_id,
        depth=depth,
        children=tuple(children),
    )


def walk_composite_skeleton(
    snapshot: RegistrySnapshot,
    root_task_ref: object,
    active_pack_refs: tuple[str, ...],
) -> SkeletonNode:
    """Статический скелет графа задач цели из её корневого шаблона. Чистая
    функция (без обращений к runtime) — основа предпросмотра графа гейта,
    который ещё не запускался (Ф1)."""
    root_template = snapshot.resolve_template(root_task_ref)
    origin_ref = (
        root_task_ref.as_string() if hasattr(root_task_ref, "as_string") else str(root_task_ref)
    )
    return _skeleton_from_template(
        snapshot,
        template=root_template,
        active_pack_refs=active_pack_refs,
        origin_kind="objective_root",
        origin_ref=origin_ref,
        slot_id=None,
        depth=0,
        seen=frozenset(),
    )


def count_skeleton_leaves(node: SkeletonNode) -> int:
    """Число «листовых» узлов скелета (leaf + нераскрытый fan-out) — для
    счётчика «N задач» в подвкладке ещё не запущенного гейта."""
    if not node.children:
        return 1 if node.template_type in {"leaf", "fan_out"} else 0
    return sum(count_skeleton_leaves(child) for child in node.children)


class PlanningService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def expand_graph(self, workspace: Path, snapshot: RegistrySnapshot) -> tuple[TaskRecord, ...]:
        manifest = self._runtime.load_manifest(workspace)
        objective = snapshot.resolve_objective(manifest.objective_ref)
        root_template = snapshot.resolve_template(objective.root_task_ref)
        root_key = f"{manifest.project_id}:root:{objective.root_task_ref.as_string()}"
        root = self._runtime.find_task_by_stable_key(workspace, root_key)
        if root is None:
            root = self._create_task(
                workspace,
                project_id=manifest.project_id,
                objective_ref=manifest.objective_ref,
                parent_task_id=None,
                template=root_template,
                origin_kind="objective_root",
                origin_ref=objective.ref.as_string(),
                stable_key=root_key,
                depth=0,
                slot_id=None,
            )
            self._runtime.apply_process_patch(
                workspace,
                SetRootTaskPatch(task_id=root.task_id),
                actor="planner",
                reason="root task created",
            )
        self._expand_composites(workspace, snapshot)
        self._expand_fan_outs(workspace, snapshot)
        return tuple(self._runtime.list_tasks(workspace))

    def admissible_candidates(
        self, workspace: Path, snapshot: RegistrySnapshot
    ) -> tuple[CandidateEvaluation, ...]:
        """Полный набор допустимых leaf-задач для параллельного диспатча.

        Делает ту же подготовку, что и :meth:`plan` (расширение графа,
        пересчёт завершённости композитов, ре-адмиссия), но НЕ выбирает одну
        задачу и НЕ пишет PlanningDecision — возвращает все admissible, чтобы
        шедулер мог запустить независимые из них одновременно. Выбор «какие
        именно запускать» (с учётом непересечения write-set и cap) — забота
        шедулера, а не планировщика (SRP).

        Сериализация записей внутри (transition mark_ready/blocked,
        expand_graph) обеспечивается per-workspace локом runtime.
        """
        self.expand_graph(workspace, snapshot)
        self._refresh_composite_completion(workspace)
        tasks = list(self._runtime.list_tasks(workspace))
        candidates = self._recompute_admission(workspace, snapshot, tasks)
        admitted = tuple(c for c in candidates if c.admissible)
        # Стабильный порядок: по приоритету (score) убыв., затем по task_key —
        # детерминированный диспатч даже при равных приоритетах.
        return tuple(sorted(admitted, key=lambda c: (-c.score, c.task_key)))

    def plan(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        mode: str = "dry-run",
        *,
        record: bool = True,
        refresh_composition: bool = True,
    ) -> PlanningDecision:
        del refresh_composition
        tasks = list(self.expand_graph(workspace, snapshot))
        self._refresh_composite_completion(workspace)
        tasks = list(self._runtime.list_tasks(workspace))
        candidates = self._recompute_admission(workspace, snapshot, tasks)
        admitted = [candidate for candidate in candidates if candidate.admissible]
        selected = max(admitted, key=lambda item: item.score, default=None)
        manifest = self._runtime.load_manifest(workspace)

        if selected is None:
            blocked = tuple(
                {
                    "task_id": candidate.task_id,
                    "task_key": candidate.task_key,
                    "title": candidate.title,
                    "reasons": list(candidate.reasons),
                }
                for candidate in candidates
                if not candidate.admissible
            )
            outcome = "objective_completed" if self._objective_completed(workspace, snapshot) else "blocked"
            reasons = (
                ("Цель проекта завершена.",)
                if outcome == "objective_completed"
                else ("Нет допустимых задач. Проверьте блокировки, входные артефакты и readiness.",)
            )
            decision = PlanningDecision(
                decision_id=str(uuid.uuid4()),
                project_id=manifest.project_id,
                objective_ref=manifest.objective_ref,
                mode=mode,
                outcome=outcome,
                selected_task_id=None,
                selected_task_key=None,
                selected_template_ref=None,
                admitted_task_ids=(),
                blocked_task_summaries=blocked,
                ranking_strategy="deterministic",
                candidates=tuple(candidates),
                reasons=reasons,
                created_at=utc_now_iso(),
            )
            if record:
                self._runtime.record_planning_decision(workspace, decision)
                if outcome == "objective_completed":
                    logger.info("цель проекта достигнута")
                else:
                    logger.warning(
                        "план заблокирован: нет допустимых задач",
                        blocked=len(blocked),
                    )
            return decision

        decision = PlanningDecision(
            decision_id=str(uuid.uuid4()),
            project_id=manifest.project_id,
            objective_ref=manifest.objective_ref,
            mode=mode,
            outcome="selected",
            selected_task_id=selected.task_id,
            selected_task_key=selected.task_key,
            selected_template_ref=selected.template_ref,
            admitted_task_ids=tuple(candidate.task_id for candidate in admitted),
            blocked_task_summaries=tuple(
                {
                    "task_id": candidate.task_id,
                    "task_key": candidate.task_key,
                    "title": candidate.title,
                    "reasons": list(candidate.reasons),
                }
                for candidate in candidates
                if not candidate.admissible
            ),
            ranking_strategy="deterministic",
            candidates=tuple(candidates),
            reasons=(f"Выбрана допустимая задача '{selected.title}'.",),
            created_at=utc_now_iso(),
        )
        if record:
            self._runtime.record_planning_decision(workspace, decision)
            logger.info(
                "план: выбрана задача",
                task=selected.title or selected.task_key,
                admitted=len(admitted),
            )
        return decision

    def planning_history(self, workspace: Path) -> list[PlanningDecision]:
        return self._runtime.list_planning_decisions(workspace)

    def transition_task(
        self,
        workspace: Path,
        task_id: str,
        command: str,
        *,
        payload: dict[str, object] | None = None,
    ):
        return self._runtime.transition_task(workspace, task_id, command, payload=payload)

    def list_tasks(self, workspace: Path):
        return self._runtime.list_tasks(workspace)

    def list_task_events(self, workspace: Path, task_id: str | None = None):
        return self._runtime.list_task_events(workspace, task_id=task_id)

    def _expand_composites(self, workspace: Path, snapshot: RegistrySnapshot) -> None:
        changed = True
        while changed:
            changed = False
            process = self._runtime.load_process_state(workspace)
            active_pack_refs = tuple(sorted(process.active_domain_pack_records.keys()))
            tasks = self._runtime.list_tasks(workspace)
            by_id = {task.task_id: task for task in tasks}
            for task in tasks:
                if task.template_type != "composite" or task.status in {"obsolete", "skipped"}:
                    continue
                template = _safe_resolve_template(snapshot, task.template_ref)
                if template is None:
                    continue
                for child in template.children:
                    child_template = snapshot.resolve_template(child.task_ref)
                    stable_key = f"{task.stable_key}:child:{child.identifier}:{child.task_ref.as_string()}"
                    if self._runtime.find_task_by_stable_key(workspace, stable_key) is None:
                        self._create_task(
                            workspace,
                            project_id=task.project_id,
                            objective_ref=task.objective_ref,
                            parent_task_id=task.task_id,
                            template=child_template,
                            origin_kind="base_child",
                            origin_ref=child.identifier,
                            stable_key=stable_key,
                            depth=task.depth + 1,
                            slot_id=None,
                        )
                        changed = True
                for slot in template.slots:
                    for pack_ref in active_pack_refs:
                        pack = snapshot.resolve_domain_pack(pack_ref)
                        for contribution in pack.contributions:
                            if contribution.slot_id != slot.identifier:
                                continue
                            for item in contribution.items:
                                if item.task_ref is None:
                                    continue
                                contribution_template = snapshot.resolve_template(item.task_ref)
                                stable_key = f"{task.stable_key}:slot:{slot.identifier}:{pack.ref.as_string()}:{item.identifier}"
                                if self._runtime.find_task_by_stable_key(workspace, stable_key) is None:
                                    self._create_task(
                                        workspace,
                                        project_id=task.project_id,
                                        objective_ref=task.objective_ref,
                                        parent_task_id=task.task_id,
                                        template=contribution_template,
                                        origin_kind="domain_contribution",
                                        origin_ref=f"{pack.ref.as_string()}:{item.identifier}",
                                        stable_key=stable_key,
                                        depth=task.depth + 1,
                                        slot_id=slot.identifier,
                                    )
                                    changed = True
            if changed:
                continue
            self._refresh_composite_completion_from_tasks(workspace, by_id)

    def _expand_fan_outs(self, workspace: Path, snapshot: RegistrySnapshot) -> None:
        tasks = self._runtime.list_tasks(workspace)
        for task in tasks:
            if task.template_type != "fan_out" or task.status != "waiting_for_fan_out_source":
                continue
            template = _safe_resolve_template(snapshot, task.template_ref)
            if template is None or template.fan_out_spec is None or template.children_template_ref is None:
                continue
            artifact = self._runtime.latest_artifact_by_role(workspace, template.fan_out_spec.artifact_role)
            if artifact is None:
                continue
            content_str = self._runtime.load_artifact_content(workspace, artifact.artifact_id)
            content = json_loads(content_str)
            array: object = content
            for part in template.fan_out_spec.array_path.split("."):
                if not isinstance(array, dict):
                    array = []
                    break
                array = array.get(part, [])
            if not isinstance(array, list):
                array = []

            # Потолок ширины: не разворачиваемся молча в тысячи задач. Падаем
            # явно (обёртка → failed с понятным сообщением), чтобы оператор
            # увидел причину и либо сократил источник, либо поднял лимит.
            limit = _max_fan_out_instances()
            if len(array) > limit:
                message = (
                    f"Fan-out даёт {len(array)} инстансов — больше потолка {limit}. "
                    f"Сократите источник '{template.fan_out_spec.artifact_role}' "
                    f"или поднимите POV_MAX_FAN_OUT_INSTANCES."
                )
                logger.error(
                    "fan-out: превышен потолок ширины",
                    task=task.task_id,
                    count=len(array),
                    limit=limit,
                )
                self._runtime.transition_task(
                    workspace, task.task_id, "fail", payload={"error_message": message}
                )
                continue

            # Повторное разворачивание (reset_fan_out поднял attempt): инстансы
            # прошлых попыток больше не актуальны. Помечаем obsolete те из них,
            # что не завершены — иначе старый failed-ребёнок навсегда блокирует
            # завершение обёртки. Завершённые не трогаем (доменный запрет на
            # obsolete из completed; для gating они безвредны).
            instance_prefix = f"{task.stable_key}:instance:"
            for existing in tasks:
                if (
                    existing.parent_task_id == task.task_id
                    and existing.origin_kind == "fan_out_instance"
                    and existing.stable_key.startswith(instance_prefix)
                    and existing.status not in {"completed", "obsolete"}
                ):
                    existing_attempt = _trailing_attempt(existing.stable_key)
                    if existing_attempt is not None and existing_attempt != task.attempt:
                        self._runtime.transition_task(workspace, existing.task_id, "obsolete")

            child_template = snapshot.resolve_template(template.children_template_ref)
            for idx, item in enumerate(array):
                item_key = str(item.get(template.fan_out_spec.key_field, idx)) if isinstance(item, dict) else str(idx)
                stable_key = f"{task.stable_key}:instance:{item_key}:{task.attempt}"
                if self._runtime.find_task_by_stable_key(workspace, stable_key) is not None:
                    continue
                self._create_task(
                    workspace,
                    project_id=task.project_id,
                    objective_ref=task.objective_ref,
                    parent_task_id=task.task_id,
                    template=child_template,
                    origin_kind="fan_out_instance",
                    origin_ref=item_key,
                    stable_key=stable_key,
                    depth=task.depth + 1,
                    slot_id=None,
                )
            # Transition wrapper to waiting_for_children (even if 0 instances)
            self._runtime.transition_task(workspace, task.task_id, "expand_fan_out")
            # If no items in source array, immediately complete the wrapper
            if not array:
                self._runtime.transition_task(workspace, task.task_id, "complete")

    def _create_task(
        self,
        workspace: Path,
        *,
        project_id: str,
        objective_ref: str,
        parent_task_id: str | None,
        template: TemplateSpec,
        origin_kind: str,
        origin_ref: str,
        stable_key: str,
        depth: int,
        slot_id: str | None,
    ) -> TaskRecord:
        now = utc_now_iso()
        task = TaskRecord(
            task_id=str(uuid.uuid4()),
            project_id=project_id,
            objective_ref=objective_ref,
            parent_task_id=parent_task_id,
            template_ref=template.ref.as_string(),
            template_type=template.template_type,
            title=template.title,
            status=initial_task_status(template.template_type),
            origin_kind=origin_kind,  # type: ignore[arg-type]
            origin_ref=origin_ref,
            stable_key=stable_key,
            depth=depth,
            slot_id=slot_id,
            attempt=1,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        return self._runtime.create_task(workspace, task)

    def _recompute_admission(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        tasks: list[TaskRecord],
    ) -> tuple[CandidateEvaluation, ...]:
        state = self._runtime.load_project_state(workspace)
        process = state.process
        candidates: list[CandidateEvaluation] = []
        completed_artifact_roles = {artifact.artifact_role for artifact in self._runtime.list_artifacts(workspace)}
        leaf_tasks = [task for task in tasks if task.template_type == "leaf"]
        task_by_id = {task.task_id: task for task in tasks}
        # RG-B: топо-ранг инстансов веера (волны сборки по зависимостям) —
        # вычитается из приоритета, чтобы листья DAG исполнялись раньше зависимых.
        fanout_ranks = self._fanout_instance_ranks(workspace, snapshot, tasks)
        # v3.1: «открытые уточнения» теперь — Decision со status="proposed".
        # Используется в _clarification_blockers для admission-check
        # blocking_clarifications.
        open_decisions = self._runtime.list_decisions(
            workspace,
            project_id=state.manifest.project_id,
            status="proposed",
        )

        # Реквизиты v2 (Ф4): честный гранулярный гейтинг. Непредоставленный
        # блокирующий реквизит держит ТОЛЬКО задачу-потребителя (свой компонент),
        # а не весь переход и не задачу-генератор. consumer_ref реквизита =
        # origin_ref fan-out-задачи компонента (component_model.key_field=id).
        # Локальный импорт во избежание цикла модулей.
        from .workspace_query_service import gather_requisites

        unprovided_blocking_by_consumer: dict[str, list[str]] = {}
        requisite_items, _, _ = gather_requisites(self._runtime, workspace)
        for requisite in requisite_items:
            if requisite.blocking and requisite.status != "provided" and requisite.consumer_ref:
                unprovided_blocking_by_consumer.setdefault(requisite.consumer_ref, []).append(
                    requisite.title
                )

        for task in leaf_tasks:
            if task.status in {"completed", "failed", "obsolete", "skipped", "in_progress"}:
                continue
            template = _safe_resolve_template(snapshot, task.template_ref)
            if template is None:
                continue
            checks: list[AdmissionCheck] = []

            missing_fields = [
                field_name
                for field_name in template.inputs.required_problem_fields
                if _resolve_state_field(state, field_name) in (None, "")
            ]
            checks.append(
                AdmissionCheck(
                    "required_state",
                    not missing_fields,
                    "Нет обязательных полей состояния: " + ", ".join(missing_fields)
                    if missing_fields
                    else "Обязательные поля состояния доступны",
                )
            )

            missing_artifacts = [
                role for role in template.inputs.required_artifact_roles if role not in completed_artifact_roles
            ]
            checks.append(
                AdmissionCheck(
                    "required_artifacts",
                    not missing_artifacts,
                    "Нет обязательных артефактов: " + ", ".join(missing_artifacts)
                    if missing_artifacts
                    else "Обязательные артефакты доступны",
                )
            )

            missing_readiness = [
                dimension
                for dimension in template.inputs.required_readiness
                if process.readiness.get(dimension) is None
                or process.readiness[dimension].status not in {"ready", "waived"}
            ]
            checks.append(
                AdmissionCheck(
                    "required_readiness",
                    not missing_readiness,
                    "Не хватает readiness: " + ", ".join(missing_readiness)
                    if missing_readiness
                    else "Readiness-предпосылки выполнены",
                )
            )

            forbidden_gaps = [
                gap_id
                for gap_id in template.inputs.forbidden_open_gaps
                if gap_id in process.active_gaps
            ]
            checks.append(
                AdmissionCheck(
                    "forbidden_open_gaps",
                    not forbidden_gaps,
                    "Есть блокирующие gaps: " + ", ".join(forbidden_gaps)
                    if forbidden_gaps
                    else "Блокирующих gaps нет",
                )
            )

            missing_domain_packs = [
                pack_ref
                for pack_ref in template.inputs.required_domain_packs
                if pack_ref not in process.active_domain_pack_records
            ]
            checks.append(
                AdmissionCheck(
                    "required_domain_packs",
                    not missing_domain_packs,
                    "Не подключены доменные пакеты: " + ", ".join(missing_domain_packs)
                    if missing_domain_packs
                    else "Требуемые доменные пакеты подключены",
                )
            )

            finalization_blockers = self._finalization_blockers(task, template, leaf_tasks)
            checks.append(
                AdmissionCheck(
                    "active_subtree_completion",
                    not finalization_blockers,
                    "Перед финализацией нужно завершить: " + ", ".join(finalization_blockers)
                    if finalization_blockers
                    else "Поддерево готово для этой задачи",
                )
            )

            clarification_blockers = self._clarification_blockers(task, task_by_id, open_decisions)
            checks.append(
                AdmissionCheck(
                    "blocking_clarifications",
                    not clarification_blockers,
                    "Есть открытые уточнения: " + ", ".join(clarification_blockers)
                    if clarification_blockers
                    else "Блокирующих уточнений нет",
                )
            )

            requisite_blockers = unprovided_blocking_by_consumer.get(task.origin_ref or "", [])
            checks.append(
                AdmissionCheck(
                    "blocking_requisites",
                    not requisite_blockers,
                    "Ждёт данные от пользователя: " + ", ".join(requisite_blockers)
                    if requisite_blockers
                    else "Обязательные реквизиты предоставлены",
                )
            )

            fanout_blockers = self._sibling_fanout_blockers(task, template, tasks, snapshot)
            checks.append(
                AdmissionCheck(
                    "sibling_fanout_complete",
                    not fanout_blockers,
                    "Ждёт полного завершения веера: " + ", ".join(fanout_blockers)
                    if fanout_blockers
                    else "Веер-источник завершён",
                )
            )

            admissible = all(check.passed for check in checks)
            if admissible and task.status != "ready":
                self._runtime.transition_task(workspace, task.task_id, "mark_ready")
            if not admissible and task.status != "blocked":
                self._runtime.transition_task(
                    workspace,
                    task.task_id,
                    "mark_blocked",
                    payload={"reason": "; ".join(check.detail for check in checks if not check.passed)},
                )

            reasons = tuple(check.detail for check in checks if not check.passed)
            candidates.append(
                CandidateEvaluation(
                    task_id=task.task_id,
                    task_key=task.task_key,
                    title=task.title,
                    template_ref=task.template_ref,
                    admissible=admissible,
                    score=template.planning.priority - fanout_ranks.get(task.task_id, 0),
                    checks=tuple(checks),
                    reasons=reasons,
                )
            )
        return tuple(candidates)

    def _clarification_blockers(
        self,
        task: TaskRecord,
        task_by_id: dict[str, TaskRecord],
        decisions,
    ) -> list[str]:
        """v3.1: блокирующие «уточнения» = open Decision-записи, привязанные
        к этой задаче (через ``source_task_id``).

        Decision-модель не имеет понятия ``blocking_scope``/``affected_task_ids``
        как у legacy ClarificationRequest — задача либо является источником
        решения, либо нет. Этого достаточно для admission-check: «не пускаем
        задачу, у которой есть нерешённый proposed Decision».
        """
        blockers: list[str] = []
        for decision in decisions:
            if decision.source_task_id and decision.source_task_id == task.task_id:
                blockers.append(decision.title)
        return blockers

    def _finalization_blockers(
        self,
        task: TaskRecord,
        template: TemplateSpec,
        leaf_tasks: list[TaskRecord],
    ) -> list[str]:
        if not template.identifier.startswith("common.requirements_spec_generation"):
            return []
        blockers = []
        for other in leaf_tasks:
            if other.task_id == task.task_id:
                continue
            if other.template_ref.startswith("common.requirements_spec_review@"):
                continue
            if other.status not in {"completed", "skipped"}:
                blockers.append(other.title)
        return blockers

    def _sibling_fanout_blockers(
        self,
        task: TaskRecord,
        template: TemplateSpec,
        tasks: list[TaskRecord],
        snapshot: RegistrySnapshot,
    ) -> list[str]:
        """Лист, потребляющий роль, которую производит веер-сиблинг, ждёт ПОЛНОГО
        завершения веера (всех инстансов), а не первого артефакта этой роли.

        `requires.artifacts` гейтит по присутствию роли — первый инстанс веера
        его удовлетворяет. Но узлы интеграции/проверки/сводки должны видеть ВСЕ
        результаты веера. Поэтому если этот лист — сиблинг незавершённого веера в
        том же композите и потребляет роль, которую веер производит, держим лист
        заблокированным, пока обёртка веера не `completed`.
        """
        if task.parent_task_id is None:
            return []
        consumed = set(template.inputs.required_artifact_roles) | set(
            template.inputs.optional_artifact_roles
        )
        if not consumed:
            return []
        blockers: list[str] = []
        for other in tasks:
            if (
                other.parent_task_id != task.parent_task_id
                or other.template_type != "fan_out"
                or other.status == "completed"
            ):
                continue
            fan_template = _safe_resolve_template(snapshot, other.template_ref)
            if fan_template is None or not fan_template.children_template_ref:
                continue
            child = _safe_resolve_template(snapshot, fan_template.children_template_ref)
            if child is None:
                continue
            if consumed & set(child.outputs.artifact_roles):
                blockers.append(other.title)
        return blockers

    def _fanout_instance_ranks(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        tasks: list[TaskRecord],
    ) -> dict[str, int]:
        """Топо-ранг каждого инстанса веера по DAG зависимостей его источника.

        Ранг 0 — у элементов без зависимостей (листья DAG), глубже — выше. Ранг
        вычитается из приоритета инстанса, так что при серийном исполнении группы
        компоненты собираются волнами в порядке зависимостей. Зависимости берём
        обобщённо: ``consumed_interfaces[].component`` (модель компонентов),
        ``dependencies``/``depends_on`` (спеки/сервисы)."""
        instances_by_parent: dict[str, list[TaskRecord]] = {}
        for task in tasks:
            if (
                task.template_type == "leaf"
                and task.origin_kind == "fan_out_instance"
                and task.parent_task_id
            ):
                instances_by_parent.setdefault(task.parent_task_id, []).append(task)
        if not instances_by_parent:
            return {}
        ranks: dict[str, int] = {}
        for wrapper in tasks:
            if wrapper.template_type != "fan_out" or wrapper.task_id not in instances_by_parent:
                continue
            template = _safe_resolve_template(snapshot, wrapper.template_ref)
            if template is None or template.fan_out_spec is None:
                continue
            spec = template.fan_out_spec
            artifact = self._runtime.latest_artifact_by_role(workspace, spec.artifact_role)
            if artifact is None:
                continue
            try:
                content = json_loads(
                    self._runtime.load_artifact_content(workspace, artifact.artifact_id)
                )
            except Exception:  # noqa: BLE001 — нет/битый источник: ранги нулевые
                continue
            array: object = content
            for part in spec.array_path.split("."):
                array = array.get(part, []) if isinstance(array, dict) else []
            if not isinstance(array, list):
                continue
            deps_map: dict[str, list[str]] = {}
            for item in array:
                if isinstance(item, dict):
                    key = str(item.get(spec.key_field, ""))
                    if key:
                        deps_map[key] = self._item_dependency_keys(item)
            cache: dict[str, int] = {}
            for inst in instances_by_parent[wrapper.task_id]:
                ranks[inst.task_id] = self._topo_rank(
                    inst.origin_ref or "", deps_map, cache, set()
                )
        return ranks

    @staticmethod
    def _item_dependency_keys(item: dict[str, object]) -> list[str]:
        deps: list[str] = []
        for edge in item.get("consumed_interfaces") or []:
            if isinstance(edge, dict) and edge.get("component"):
                deps.append(str(edge["component"]))
        for field_name in ("dependencies", "depends_on"):
            for dep in item.get(field_name) or []:
                deps.append(str(dep))
        return deps

    @staticmethod
    def _topo_rank(
        item_id: str,
        deps_map: dict[str, list[str]],
        cache: dict[str, int],
        visiting: set[str],
    ) -> int:
        if item_id in cache:
            return cache[item_id]
        if item_id in visiting or item_id not in deps_map:
            return 0  # цикл или внешняя ссылка → корень волны
        visiting.add(item_id)
        best = 0
        for dep in deps_map[item_id]:
            if dep in deps_map:
                best = max(best, 1 + PlanningService._topo_rank(dep, deps_map, cache, visiting))
        visiting.discard(item_id)
        cache[item_id] = best
        return best

    def _refresh_composite_completion(self, workspace: Path) -> None:
        self._refresh_composite_completion_from_tasks(
            workspace,
            {task.task_id: task for task in self._runtime.list_tasks(workspace)},
        )

    def _refresh_composite_completion_from_tasks(self, workspace: Path, by_id: dict[str, TaskRecord]) -> None:
        children_by_parent: dict[str, list[TaskRecord]] = {}
        for task in by_id.values():
            if task.parent_task_id:
                children_by_parent.setdefault(task.parent_task_id, []).append(task)
        for task in sorted(by_id.values(), key=lambda item: item.depth, reverse=True):
            if task.template_type not in {"composite", "fan_out"} or task.status == "completed":
                continue
            # Obsolete-дети (например, инстансы прошлой попытки fan-out) не
            # участвуют в gating — иначе завершение обёртки было бы недостижимо.
            children = [
                child
                for child in children_by_parent.get(task.task_id, [])
                if child.status != "obsolete"
            ]
            if children and all(child.status in {"completed", "skipped"} for child in children):
                self._runtime.transition_task(workspace, task.task_id, "complete")

    def _objective_completed(self, workspace: Path, snapshot: RegistrySnapshot) -> bool:
        manifest = self._runtime.load_manifest(workspace)
        objective = snapshot.resolve_objective(manifest.objective_ref)
        all_artifacts = list(self._runtime.list_artifacts(workspace))
        roles_present = {artifact.artifact_role for artifact in all_artifacts}
        artifacts_ok = all(
            artifact_ref.identifier.rsplit(".", 1)[-1] in roles_present
            for artifact_ref in objective.done_artifact_refs
        )
        if not artifacts_ok:
            return False
        for gate_ref in objective.done_gate_refs:
            gate = snapshot.resolve_quality_gate(gate_ref)
            if gate.check_type == "human_approval":
                # Ф3: human-approval gate проходится согласованием итогового
                # артефакта с заказчиком (ArtifactRecord.signed_off), а не
                # отдельным решением в реестре. Поэтому, пока артефакт нужной
                # роли не согласован, планировщик возвращает blocked.
                required_roles = set(getattr(gate, "required_artifact_roles", ()) or ())
                signed = any(
                    art.artifact_kind == "primary"
                    and art.signed_off
                    and (not required_roles or art.artifact_role in required_roles)
                    for art in all_artifacts
                )
                if not signed:
                    return False
        return True
