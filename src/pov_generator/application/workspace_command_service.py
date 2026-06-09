from __future__ import annotations

import re
import uuid
from pathlib import Path

from ..common.errors import ConflictError
from ..common.logging import get_logger
from ..common.serialization import utc_now_iso
from ..domain.positions import REQUISITE_POSITION_PREFIX, Position
from ..domain.project_knowledge import RejectPositionPatch, UpsertPositionPatch
from ..domain.registry import ObjectRef
from ..domain.workspace_views import CommandResultView, ProjectCreatedView

logger = get_logger("project")
from .checkpoint_service import CheckpointService
from .domain_pack_selection_service import DomainPackSelectionService
from .planning_service import PlanningService
from .project_lock import ensure_project_unlocked
from .project_service import ProjectService
from .registry_service import RegistryService
from .workflow_service import WorkflowService
from .workspace_catalog import WorkspaceCatalog
from .workspace_query_service import gather_requisites

GRAPH_PROJECTIONS = ("task_graph", "situation", "timeline", "artifacts", "clarifications", "review", "state", "debug")


class WorkspaceCommandService:
    def __init__(
        self,
        catalog: WorkspaceCatalog,
        registry_service: RegistryService,
        project_service: ProjectService,
        planning_service: PlanningService,
        workflow_service: WorkflowService,
        domain_pack_selection_service: DomainPackSelectionService,
        checkpoint_service: CheckpointService,
    ) -> None:
        self._catalog = catalog
        self._registry_service = registry_service
        self._project_service = project_service
        self._planning_service = planning_service
        self._workflow_service = workflow_service
        self._domain_pack_selection_service = domain_pack_selection_service
        self._checkpoint_service = checkpoint_service

    def run_next(self, project_id: str, *, provider: str | None = None, model: str | None = None) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.run_next(workspace_ref.workspace, snapshot, provider=provider, model=model)
        status = "accepted" if result.planning_outcome == "selected" else "blocked"
        summary = (
            f"Запущена задача '{result.selected_step_id}'."
            if result.task_id
            else (result.reasons[0] if result.reasons else "Команда не изменила состояние проекта.")
        )
        return CommandResultView(
            status=status,
            command_name="run-next",
            summary=summary,
            changed_projections=GRAPH_PROJECTIONS,
            resource_id=result.task_id,
        )

    def run_until_blocked(
        self,
        project_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int = 1000,
    ) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.run_until_blocked(
            workspace_ref.workspace,
            snapshot,
            provider=provider,
            model=model,
            max_steps=max_steps,
        )
        if result.stopped_reason == "objective_completed":
            status = "accepted"
            summary = f"Цель завершена: выполнено задач {len(result.steps)}."
        elif result.stopped_reason == "validation_failed":
            status = "warning"
            summary = "Процесс остановлен: ревью или валидация требуют внимания."
        elif result.stopped_reason == "planner_blocked":
            status = "blocked"
            summary = "Процесс остановлен: автоматических следующих задач сейчас нет."
        else:
            status = "warning"
            summary = f"Процесс остановлен со статусом '{result.stopped_reason}' после {len(result.steps)} задач."
        return CommandResultView(
            status=status,
            command_name="run-until-blocked",
            summary=summary,
            changed_projections=GRAPH_PROJECTIONS,
        )

    def retry_task(
        self,
        project_id: str,
        *,
        task_id: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.retry_task(
            workspace_ref.workspace,
            snapshot,
            task_id=task_id,
            provider=provider,
            model=model,
        )
        if result.validation_status == "passed":
            status = "accepted"
            summary = f"Задача '{result.selected_step_id or task_id}' успешно выполнена повторно."
        else:
            status = "warning"
            summary = result.reasons[0] if result.reasons else "Повторный запуск задачи завершился с ошибкой."
        return CommandResultView(
            status=status,
            command_name="retry-task",
            summary=summary,
            changed_projections=GRAPH_PROJECTIONS,
            resource_id=task_id,
        )

    def set_goal(self, project_id: str, *, text: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        self._project_service.set_goal(workspace_ref.workspace, text)
        return CommandResultView(
            status="accepted",
            command_name="set-goal",
            summary="Цель проекта обновлена.",
            changed_projections=("shell", "situation", "timeline", "state"),
        )

    def close_gap(self, project_id: str, *, gap_id: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        self._project_service.close_gap(workspace_ref.workspace, gap_id)
        return CommandResultView(
            status="accepted",
            command_name="close-gap",
            summary=f"Gap '{gap_id}' закрыт.",
            changed_projections=("situation", "timeline", "state", "task_graph"),
            resource_id=gap_id,
        )

    def set_readiness(
        self,
        project_id: str,
        *,
        dimension: str,
        status: str,
        blocking: bool,
        confidence: float,
    ) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        self._project_service.set_readiness(
            workspace_ref.workspace,
            dimension=dimension,
            status=status,
            blocking=blocking,
            confidence=confidence,
        )
        return CommandResultView(
            status="accepted",
            command_name="set-readiness",
            summary=f"Readiness '{dimension}' обновлена.",
            changed_projections=("situation", "timeline", "state", "task_graph"),
            resource_id=dimension,
        )

    def enable_domain_pack(self, project_id: str, *, pack_ref: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        snapshot = self._validated_snapshot()
        pack = snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref))
        self._project_service.enable_domain_pack(workspace_ref.workspace, pack)
        self._planning_service.expand_graph(workspace_ref.workspace, snapshot)
        return CommandResultView(
            status="accepted",
            command_name="enable-domain-pack",
            summary=f"Подключён доменный пакет '{pack_ref}'.",
            changed_projections=("shell", "task_graph", "situation", "timeline", "clarifications", "state", "debug"),
            resource_id=pack_ref,
        )

    def provide_requisite(
        self,
        project_id: str,
        *,
        key: str,
        mode: str = "reference",
        value: str = "",
        attachment_id: str = "",
        note: str = "",
    ) -> CommandResultView:
        """Разрешить реквизит: предоставить данные ИЛИ осознанно обойти.

        Реквизиты v2 (Ф3+Ф4). Режимы данных: ``value`` кладёт значение в слой A
        пользовательским фактом (``source="user"``) — он попадает в контекст
        зависимых задач; ``file``/``reference`` — без значения. Режимы обхода
        (честный гейтинг): ``assumption`` («допущение») кладёт рабочий дефолт как
        положение-допущение (🟡, можно override решением); ``deferred`` («позже»)
        и ``not_applicable`` («неприменимо») — без положения. Любой режим
        снимает гранулярный блок задачи-потребителя (admission учитывает только
        непредоставленные реквизиты).

        Безопасность (инвариант 6): вид ``credential`` и режимы без значения
        НИКОГДА не несут секрет в контекст/артефакты (только пометка «выдано вне
        системы»); прежнее value/assumption-положение при смене на режим без
        значения снимается. Предоставление — событие: затем идёт переоценка
        графа (``expand_graph``), чтобы задача-потребитель могла продолжиться.
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        workspace = workspace_ref.workspace
        runtime = self._project_service.runtime
        ensure_project_unlocked(runtime, workspace)

        # Метаданные реквизита (вид/заголовок/потребитель) для безопасности и
        # содержательного положения. Реквизит мог стать невидимым (артефакт
        # переехал) — тогда работаем по ключу как есть.
        items, _, _ = gather_requisites(runtime, workspace)
        match = next(
            (it for it in items if (it.key or it.title) == key or it.title == key), None
        )
        title = match.title if match else key
        needed_for = match.needed_for if match else ""
        kind = match.kind if match else "other"

        # Инвариант безопасности: секрет не персистится. credential нельзя
        # «предположить» или передать значением — только reference.
        if kind == "credential" and mode in {"value", "assumption"}:
            mode = "reference"
        # Значение несут только value/assumption; остальные режимы — без значения.
        if mode not in {"value", "assumption"}:
            value = ""

        runtime.mark_requisite_provided(
            workspace,
            requisite_key=key,
            note=note,
            mode=mode,
            value=value,
            attachment_id=attachment_id,
        )

        # Втекание в слой A. value → пользовательский факт; assumption → рабочее
        # допущение (🟡). Остальные режимы значение в контексте не держат.
        position_id = f"{REQUISITE_POSITION_PREFIX}{key}"
        if mode in {"value", "assumption"} and value.strip():
            if mode == "assumption":
                position_type = "assumption"
                statement = f"Допущение пользователя по реквизиту «{title}»"
            else:
                position_type = "fact"
                statement = f"Данные, предоставленные пользователем по реквизиту «{title}»"
            if needed_for:
                statement += f" (нужно для: {needed_for})"
            statement += f":\n\n{value.strip()}"
            runtime.apply_knowledge_patch(
                workspace,
                UpsertPositionPatch(
                    Position(
                        identifier=position_id,
                        type=position_type,
                        statement=statement,
                        visibility="architectural",
                        scope="global",
                        source="user",
                        taken_by="requisite",
                        taken_at=utc_now_iso(),
                        tags=("requisite", "user_input"),
                    )
                ),
                actor="requisite",
                reason=f"provided requisite {key} ({mode})",
            )
        else:
            # reference/file: значение в контексте не держим. Снимаем прежнее
            # value-положение (например, переключение credential value →
            # reference), чтобы секрет/устаревшее значение не осталось.
            knowledge = runtime.load_project_state(workspace).knowledge
            existing = knowledge.positions.get(position_id)
            if existing is not None and existing.status == "active":
                runtime.apply_knowledge_patch(
                    workspace,
                    RejectPositionPatch(
                        position_id=position_id,
                        reason=f"requisite {key} re-provided as {mode}",
                    ),
                    actor="requisite",
                    reason=f"provided requisite {key} ({mode})",
                )

        # Предоставление — событие: переоценка графа (дозапуск/реплан потребителя).
        snapshot = self._validated_snapshot()
        self._planning_service.expand_graph(workspace, snapshot)

        return CommandResultView(
            status="accepted",
            command_name="provide-requisite",
            summary=f"Реквизит «{title}» отмечен как предоставленный.",
            changed_projections=("situation", "timeline", "state", "task_graph"),
            resource_id=key,
        )

    def unprovide_requisite(self, project_id: str, *, key: str) -> CommandResultView:
        """Снять предоставление реквизита (реквизиты v7, un-provide).

        Удаляет запись предоставления и снимает связанное value/assumption-
        положение слоя A (если было) — данные перестают втекать в контекст.
        После этого граф переоценивается: если реквизит блокирующий, его
        задача-потребитель снова заблокируется (честный гейтинг). Снятие
        положения проходит через журналируемый knowledge-patch (аудит).
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        workspace = workspace_ref.workspace
        runtime = self._project_service.runtime
        ensure_project_unlocked(runtime, workspace)

        runtime.delete_requisite_provision(workspace, key)

        position_id = f"{REQUISITE_POSITION_PREFIX}{key}"
        existing = runtime.load_project_state(workspace).knowledge.positions.get(position_id)
        if existing is not None and existing.status == "active":
            runtime.apply_knowledge_patch(
                workspace,
                RejectPositionPatch(
                    position_id=position_id,
                    reason=f"requisite {key} un-provided by user",
                ),
                actor="requisite",
                reason=f"un-provided requisite {key}",
            )

        snapshot = self._validated_snapshot()
        self._planning_service.expand_graph(workspace, snapshot)

        return CommandResultView(
            status="accepted",
            command_name="unprovide-requisite",
            summary="Предоставление реквизита снято.",
            changed_projections=("situation", "timeline", "state", "task_graph"),
            resource_id=key,
        )

    def set_clarification_mode(self, project_id: str, *, mode: str) -> CommandResultView:
        """Сменить режим участия пользователя (v3.2).

        Делегирует в `CheckpointService.set_participation_mode`, который:
        - применяет SetClarificationModePatch к ProcessState;
        - реэвалюирует существующие proposed-decisions: те, что больше
          не должны показываться в новом режиме — auto-accept default;
        - финализирует pending checkpoint-сессии, у которых все
          decisions стали закрытыми;
        - переводит соответствующие failed-задачи обратно в ready.

        Этот endpoint возвращает понятный пользователю summary
        («приняты автоматически X, разблокированы Y задач»).
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        result = self._checkpoint_service.set_participation_mode(workspace_ref.workspace, mode)
        if result.resumed_task_count > 0:
            summary = (
                f"Режим участия изменён на «{mode}». "
                f"Автоматически приняты {result.auto_accepted_count} решений, "
                f"разблокированы {result.resumed_task_count} задач."
            )
        elif result.auto_accepted_count > 0:
            summary = (
                f"Режим участия изменён на «{mode}». "
                f"Автоматически приняты {result.auto_accepted_count} решений."
            )
        else:
            summary = f"Режим участия изменён на «{mode}»."
        return CommandResultView(
            status="accepted",
            command_name="set-clarification-mode",
            summary=summary,
            changed_projections=("shell", "situation", "timeline", "state", "task_graph", "overview"),
            resource_id=mode,
        )

    def set_methodology(self, project_id: str, *, pack_ref: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        snapshot = self._validated_snapshot()
        snapshot.resolve_methodology_pack(ObjectRef.parse(pack_ref))
        self._project_service.set_methodology(workspace_ref.workspace, pack_ref)
        return CommandResultView(
            status="accepted",
            command_name="set-methodology",
            summary=f"Активная методология обновлена: '{pack_ref}'.",
            changed_projections=("shell", "situation", "timeline", "state", "debug"),
            resource_id=pack_ref,
        )

    def activate_next_objective(
        self,
        project_id: str,
        *,
        new_objective_ref: str,
    ) -> CommandResultView:
        """Переключить workspace на следующий objective (ТЗ → архитектура).

        Состояние (knowledge + process) сохраняется; новый objective получает
        собственное дерево задач рядом со старым. Существующие артефакты
        автоматически становятся доступны новым задачам через
        ``requires.artifacts.optional``.
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        new_ref_obj = ObjectRef.parse(new_objective_ref)
        new_spec = snapshot.resolve_objective(new_ref_obj)
        # Реквизиты v2 (Ф4): переход на реализацию больше НЕ держится огульно.
        # Честный гейтинг теперь гранулярный — непредоставленный блокирующий
        # реквизит держит в admission только свою задачу-потребителя
        # (см. planning_service._recompute_admission, check "blocking_requisites"),
        # а не весь переход и не генератор. Пользователь входит в этап реализации,
        # видит «ждёт данные X» у конкретных узлов и предоставляет по ходу.
        methodology_ref = (
            new_spec.default_methodology_pack_ref.as_string()
            if new_spec.default_methodology_pack_ref is not None
            else None
        )
        self._project_service.activate_next_objective(
            workspace_ref.workspace,
            new_ref_obj,
            default_methodology_pack_ref=methodology_ref,
        )
        self._planning_service.expand_graph(workspace_ref.workspace, snapshot)
        return CommandResultView(
            status="accepted",
            command_name="activate-next-objective",
            summary=f"Активирован следующий objective: '{new_objective_ref}'.",
            changed_projections=("shell", "situation", "task_graph", "timeline", "state"),
            resource_id=new_objective_ref,
        )

    def create_project(
        self,
        *,
        name: str,
        objective_ref: str,
        request_text: str,
        domain_pack_refs: tuple[str, ...] = (),
        selection_provider: str | None = None,
        selection_model: str | None = None,
        defer_setup: bool = False,
    ) -> ProjectCreatedView:
        snapshot = self._validated_snapshot()
        objective_object_ref = ObjectRef.parse(objective_ref)
        # Отложенный setup нужен ТОЛЬКО для авто-подбора: подбор должен видеть и
        # запрос, и вложения, а вложения грузятся уже после создания проекта.
        # При явном выборе пакетов вложения на выбор не влияют — setup сразу.
        deferred = defer_setup and not domain_pack_refs
        if domain_pack_refs:
            resolved_pack_refs = tuple(sorted(set(domain_pack_refs)))
            packs = tuple(snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref)) for pack_ref in resolved_pack_refs)
        elif deferred:
            # Подбор пакетов и разворот графа откладываем до finalize-setup.
            resolved_pack_refs = ()
            packs = ()
        else:
            selection = self._domain_pack_selection_service.select_for_request(
                snapshot,
                objective_ref=objective_object_ref.as_string(),
                request_text=request_text.strip(),
                provider=selection_provider,
                model=selection_model,
            )
            resolved_pack_refs = selection.selected_pack_refs
            packs = tuple(snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref)) for pack_ref in resolved_pack_refs)
        workspace = self._allocate_workspace(name)
        objective_spec = snapshot.resolve_objective(objective_object_ref)
        methodology_ref = (
            objective_spec.default_methodology_pack_ref.as_string()
            if objective_spec.default_methodology_pack_ref is not None
            else None
        )
        bootstrap = self._project_service.init_project(
            workspace=workspace,
            name=name.strip(),
            objective_ref=objective_object_ref,
            request_text=request_text.strip(),
            domain_packs=packs,
            default_methodology_pack_ref=methodology_ref,
        )
        # Выбор доменных пакетов — внутреннее системное решение (process-слой:
        # активированные паки). НЕ записываем его как факт знаний: иначе текст
        # «Автоматический модуль подбора доменных пакетов (openrouter) выбрал…»
        # утекал в «Контекст проекта» каждой задачи как 🔵 факт «из запроса» и
        # уводил ранние задачи в мета-рассуждение о самой системе подбора
        # (разбор инцидента РТК). Обоснование выбора — в логах селектора.
        if not deferred:
            self._planning_service.expand_graph(workspace, snapshot)
        logger.info(
            f"проект создан «{bootstrap.manifest.name}»"
            + (" (setup отложен до загрузки вложений)" if deferred else ""),
            objective=bootstrap.manifest.objective_ref.split("@")[0],
        )
        return ProjectCreatedView(
            project_id=bootstrap.manifest.project_id,
            name=bootstrap.manifest.name,
            objective_ref=bootstrap.manifest.objective_ref,
            domain_pack_refs=resolved_pack_refs,
            workspace_path=str(workspace),
            setup_pending=deferred,
        )

    def finalize_project_setup(
        self,
        project_id: str,
        *,
        selection_provider: str | None = None,
        selection_model: str | None = None,
    ) -> ProjectCreatedView:
        """Завершить отложенный setup: подобрать доменные пакеты по запросу И
        загруженным вложениям, активировать их и развернуть граф.

        Идемпотентно: если граф уже развёрнут (есть задачи), повторный вызов
        ничего не меняет — возвращает текущее состояние. Подбор видит полный
        входной корпус (бизнес-запрос + извлечённый текст вложений из слоя A),
        поэтому большая часть контекста из файлов влияет на выбор пакетов.
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        workspace = workspace_ref.workspace
        snapshot = self._validated_snapshot()
        runtime = self._project_service.runtime
        manifest = self._project_service.load_manifest(workspace)
        objective_object_ref = ObjectRef.parse(manifest.objective_ref)

        # Идемпотентность: setup уже выполнен (граф развёрнут) — выходим.
        existing_tasks = runtime.list_tasks(workspace)
        if existing_tasks:
            state = self._project_service.load_project_state(workspace)
            return ProjectCreatedView(
                project_id=manifest.project_id,
                name=manifest.name,
                objective_ref=manifest.objective_ref,
                domain_pack_refs=tuple(sorted(state.process.active_domain_pack_records.keys())),
                workspace_path=str(workspace),
                setup_pending=False,
            )

        selection_text = self._selection_corpus(workspace, manifest.business_request)
        selection = self._domain_pack_selection_service.select_for_request(
            snapshot,
            objective_ref=objective_object_ref.as_string(),
            request_text=selection_text,
            provider=selection_provider,
            model=selection_model,
        )
        for pack_ref in selection.selected_pack_refs:
            pack = snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref))
            self._project_service.enable_domain_pack(
                workspace,
                pack,
                actor="system",
                reason="auto-selected from request + attachments",
            )
        self._planning_service.expand_graph(workspace, snapshot)
        logger.info(
            f"setup проекта завершён «{manifest.name}»",
            packs=len(selection.selected_pack_refs),
        )
        return ProjectCreatedView(
            project_id=manifest.project_id,
            name=manifest.name,
            objective_ref=manifest.objective_ref,
            domain_pack_refs=selection.selected_pack_refs,
            workspace_path=str(workspace),
            setup_pending=False,
        )

    # Бюджет корпуса для подбора пакетов: запрос + вложения. Подбор — задача
    # «понять домен», полный текст не нужен; ограничиваем, чтобы не раздувать
    # промпт селектора, но даём вложениям весомую долю контекста.
    _SELECTION_CORPUS_CHAR_LIMIT = 60_000

    def _selection_corpus(self, workspace: Path, business_request: str) -> str:
        """Корпус для подбора пакетов: бизнес-запрос + извлечённый текст входных
        вложений (положения слоя A ``attachment.*``). Без вложений = только запрос."""
        from .attachment_service import ATTACHMENT_POSITION_PREFIX

        parts: list[str] = []
        request = (business_request or "").strip()
        if request:
            parts.append(f"Бизнес-запрос:\n{request}")
        state = self._project_service.load_project_state(workspace)
        for position in state.knowledge.active():
            if position.type != "fact" or not position.identifier.startswith(
                ATTACHMENT_POSITION_PREFIX
            ):
                continue
            text = (position.statement or "").strip()
            if text:
                parts.append(text)
        corpus = "\n\n".join(parts)
        if len(corpus) > self._SELECTION_CORPUS_CHAR_LIMIT:
            corpus = (
                corpus[: self._SELECTION_CORPUS_CHAR_LIMIT].rstrip()
                + "\n\n… [входной корпус обрезан для подбора пакетов]"
            )
        return corpus

    def _validated_snapshot(self):
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Команды UI заблокированы.")
        return snapshot

    def _allocate_workspace(self, name: str) -> Path:
        bucket = self._catalog.runtime_root / "ui_cases"
        bucket.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9а-яё]+", "-", name.strip().lower())
        slug = slug.strip("-")[:32] or "project"
        return bucket / f"{slug}-{uuid.uuid4().hex[:8]}"
