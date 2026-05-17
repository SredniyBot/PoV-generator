"""Сервис управления pre-flight checkpoint-сессиями (v3.0).

Что делает:
    - Принимает результат :class:`DecisionPlanningService` (список
      ``Decision`` со статусом ``proposed``).
    - Фильтрует по уровню вовлечения пользователя (mode проекта).
    - Сохраняет ВСЕ решения в реестр (даже те, что ниже уровня —
      они идут как ``accepted_default``, видимы постфактум).
    - Если после фильтра остались — создаёт :class:`CheckpointSession`
      со статусом ``pending`` и возвращает её.
    - Если не осталось — возвращает None, workflow продолжается без паузы.

Также:
    - Обрабатывает ответы пользователя (``submit_answers``): применяет к
      Decision-записям, финализирует сессию.

Не делает:
    - Не вызывает LLM (это DecisionPlanningService).
    - Не запускает основную генерацию (это ExecutionService).
    - Не управляет timeout / auto-defer (будущая фича).

Спецификация: ``specs/12_clarification_escalation.md`` раздел v3.0.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.process_state import ProcessState  # noqa: F401

from ..common.errors import ConflictError, NotFoundError
from ..common.serialization import utc_now_iso
from ..domain.checkpoints import (
    CheckpointAnswer,
    CheckpointSession,
)
from ..domain.decisions import (
    Decision,
    DecisionInput,
    levels_for_mode,
    should_surface_to_user,
)
from ..infrastructure.sqlite_runtime import SqliteRuntime


@dataclass(frozen=True)
class ModeChangeResult:
    """v3.2: что произошло после смены режима участия.

    Используется UI/API для отображения toast'а после переключения:
    «Принято автоматически: N, продолжается M задач».
    """

    mode: str
    auto_accepted_count: int
    finalized_session_count: int
    resumed_task_count: int


@dataclass(frozen=True)
class CheckpointCreationResult:
    """Что вернул сервис после обработки результата planning.

    Контракт для ExecutionService:
    - Если ``session is None`` — нечего предъявлять пользователю, можно
      идти на основную генерацию. Все решения уже в реестре.
    - Если ``session`` есть — workflow должен встать на паузу, дождаться
      финализации, затем продолжить.
    """

    session: CheckpointSession | None
    surfaced_count: int  # сколько решений отфильтровалось «на уровень»
    silent_count: int    # сколько решений пошло мимо checkpoint (ниже уровня)


class CheckpointService:
    """Логика checkpoint-сессий: создание, обработка ответов, финализация."""

    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    # ---- create -----------------------------------------------------------

    def process_planned_decisions(
        self,
        workspace: Path,
        *,
        project_id: str,
        task_id: str,
        task_title: str,
        artifact_role: str,
        decisions: tuple[Decision, ...],
        mode: str,
    ) -> CheckpointCreationResult:
        """Принять список planned decisions, сохранить в реестр,
        опционально создать checkpoint-сессию.

        Логика:
        1. Делим decisions на surfaced (на уровень режима) и silent (ниже).
        2. Сохраняем все: surfaced → status ``proposed``, silent →
           ``accepted_default`` (с пометкой ``user_action="not_shown"``).
        3. Если surfaced пуст → возвращаем без сессии.
        4. Иначе создаём CheckpointSession в статусе ``pending`` со
           ссылками на surfaced.decision_ids.

        Args:
            workspace: путь к воркспейсу проекта.
            project_id: проект.
            task_id: задача, перед которой создаётся checkpoint.
            task_title, artifact_role: для UI checkpoint'а.
            decisions: результат pre-flight планирования.
            mode: текущий режим проекта (clarification_mode).
        """
        surfaced: list[Decision] = []
        silent: list[Decision] = []
        for d in decisions:
            if should_surface_to_user(d, mode):
                surfaced.append(d)
            else:
                silent.append(d)

        # Сохраняем silent сразу как accepted_default
        for d in silent:
            saved = replace(
                d,
                project_id=project_id,
                status="accepted_default",
                user_action="not_shown",
            )
            self._runtime.upsert_decision(workspace, saved)

        # surfaced сохраняем как proposed (status уже proposed из planning;
        # перезаписываем project_id на всякий случай)
        surfaced_ids: list[str] = []
        for d in surfaced:
            saved = replace(d, project_id=project_id)
            self._runtime.upsert_decision(workspace, saved)
            surfaced_ids.append(saved.decision_id)

        if not surfaced_ids:
            return CheckpointCreationResult(
                session=None,
                surfaced_count=0,
                silent_count=len(silent),
            )

        session = CheckpointSession(
            session_id=str(uuid.uuid4()),
            project_id=project_id,
            task_id=task_id,
            task_title=task_title,
            artifact_role=artifact_role,
            status="pending",
            decision_ids=tuple(surfaced_ids),
        )
        saved_session = self._runtime.upsert_checkpoint_session(workspace, session)
        return CheckpointCreationResult(
            session=saved_session,
            surfaced_count=len(surfaced_ids),
            silent_count=len(silent),
        )

    # ---- answer / finalize ------------------------------------------------

    def submit_answers(
        self,
        workspace: Path,
        *,
        session_id: str,
        answers: tuple[CheckpointAnswer, ...],
        actor: str = "user",
    ) -> CheckpointSession:
        """Применить ответы пользователя на сессию и финализировать её.

        Каждый ответ обновляет соответствующий ``Decision``:
        - ``accept_default`` → status="accepted_default", user_action="accepted_default"
        - ``select_alternative`` → меняет chosen_option_id, status="user_overridden",
          user_action="modified"
        - ``free_text`` → user_free_text_answer заполняется, status="user_overridden",
          user_action="modified"
        - ``defer`` → status="deferred", user_action="deferred"

        После применения сессия переходит в статус ``finalized``.

        Validation:
        - Сессия должна быть в статусе ``pending``.
        - Все ``decision_id`` в ответах должны быть в ``session.decision_ids``.
        - Можно ответить НЕ на все decisions сессии — оставшиеся
          автоматически уходят в ``accepted_default`` (как массовое
          подтверждение оставшихся).
        """
        session = self._runtime.get_checkpoint_session(workspace, session_id)
        if not session.is_actionable:
            raise ConflictError(
                f"Сессия {session_id} в статусе {session.status}, ответы не применимы"
            )

        valid_ids = set(session.decision_ids)
        answered_ids: set[str] = set()

        # Применяем ответы пользователя
        for ans in answers:
            if ans.decision_id not in valid_ids:
                raise ConflictError(
                    f"decision_id {ans.decision_id!r} не принадлежит сессии {session_id}"
                )
            if ans.decision_id in answered_ids:
                raise ConflictError(
                    f"повторный ответ на decision {ans.decision_id!r} в одной отправке"
                )
            answered_ids.add(ans.decision_id)
            self._apply_answer(workspace, decision_id=ans.decision_id, answer=ans)

        # Decision'ы, на которые пользователь не ответил → массовое accept_default
        # (это поведение «закрой сессию, дефолты применятся»). Если он явно
        # хотел иначе — должен был явно ответить.
        for decision_id in valid_ids - answered_ids:
            decision = self._runtime.get_decision(workspace, decision_id)
            saved = replace(
                decision,
                status="accepted_default",
                user_action="accepted_default",
                updated_at=utc_now_iso(),
            )
            self._runtime.upsert_decision(workspace, saved)

        # Финализируем сессию
        finalized = replace(
            session,
            status="finalized",
            finalized_at=utc_now_iso(),
            finalized_by=actor,
        )
        saved_session = self._runtime.upsert_checkpoint_session(workspace, finalized)

        # v3.0 — auto-resume: задача, которая была failed из-за паузы,
        # переводится обратно в ready. Это позволит планнеру при следующем
        # run_next / start_run немедленно её подобрать; pre-flight в
        # ExecutionService увидит finalized session и пропустит планирование,
        # сразу подтянет locked-in decisions в основной промпт.
        try:
            task = self._runtime.get_task(workspace, session.task_id)
            if task.status == "failed":
                self._runtime.transition_task(
                    workspace,
                    session.task_id,
                    "retry",
                    payload={
                        "reason": "auto-retry after checkpoint finalized",
                        "source": "checkpoint_submit",
                        "checkpoint_session_id": session.session_id,
                    },
                )
        except Exception:
            # Не блокируем submit, если транзишн не прошёл — пользователь
            # сможет вручную ретрайнуть задачу.
            pass

        return saved_session

    def _apply_answer(
        self,
        workspace: Path,
        *,
        decision_id: str,
        answer: CheckpointAnswer,
    ) -> None:
        """Применить один ответ пользователя к Decision-записи."""
        decision = self._runtime.get_decision(workspace, decision_id)
        original_choice = decision.chosen_option_id

        if answer.kind == "accept_default":
            saved = replace(
                decision,
                status="accepted_default",
                user_action="accepted_default",
                updated_at=utc_now_iso(),
            )
        elif answer.kind == "select_alternative":
            valid_options = {alt.option_id for alt in decision.alternatives}
            # v3.1: multi-select поддержка. Если decision.answer_mode == "multiple",
            # ожидаем selected_option_ids (tuple). Иначе single — selected_option_id.
            if decision.answer_mode == "multiple":
                ids = answer.selected_option_ids
                if not ids:
                    # Fallback: single option_id обёрнут в tuple
                    if answer.selected_option_id is not None:
                        ids = (answer.selected_option_id,)
                    else:
                        raise ConflictError(
                            "answer.kind=select_alternative для multi-mode "
                            "требует selected_option_ids (tuple)"
                        )
                invalid = [oid for oid in ids if oid not in valid_options]
                if invalid:
                    raise ConflictError(
                        f"option_id {invalid!r} нет среди альтернатив решения {decision_id!r}"
                    )
                saved = replace(
                    decision,
                    chosen_option_ids=tuple(ids),
                    # Для multi-mode chosen_option_id — первый из выбранных (UI compat)
                    chosen_option_id=ids[0] if ids else "",
                    original_chosen_option_id=original_choice,
                    status="user_overridden",
                    user_action="modified",
                    updated_at=utc_now_iso(),
                )
            else:
                if answer.selected_option_id is None:
                    raise ConflictError(
                        "answer.kind=select_alternative требует selected_option_id"
                    )
                if answer.selected_option_id not in valid_options:
                    raise ConflictError(
                        f"option_id {answer.selected_option_id!r} нет среди альтернатив "
                        f"решения {decision_id!r}"
                    )
                saved = replace(
                    decision,
                    chosen_option_id=answer.selected_option_id,
                    chosen_option_ids=(),  # single mode — не используем
                    original_chosen_option_id=original_choice,
                    status="user_overridden",
                    user_action="modified",
                    updated_at=utc_now_iso(),
                )
        elif answer.kind == "free_text":
            if not answer.free_text:
                raise ConflictError("answer.kind=free_text требует непустой free_text")
            saved = replace(
                decision,
                user_free_text_answer=answer.free_text,
                original_chosen_option_id=original_choice,
                status="user_overridden",
                user_action="modified",
                updated_at=utc_now_iso(),
            )
        elif answer.kind == "defer":
            saved = replace(
                decision,
                status="deferred",
                user_action="deferred",
                updated_at=utc_now_iso(),
            )
        else:
            raise ConflictError(f"неизвестный CheckpointAnswerKind: {answer.kind!r}")

        self._runtime.upsert_decision(workspace, saved)

    # ---- v3.1: регистрация решений из эмиттеров ------------------------------

    def register_decision_inputs(
        self,
        workspace: Path,
        *,
        project_id: str,
        decision_inputs: tuple[DecisionInput, ...],
    ) -> tuple[Decision, ...]:
        """Создать Decision-записи из payload эмиттеров (validation,
        methodology, gates).

        Группирует по source_task_id; для каждой группы создаётся отдельная
        CheckpointSession в режиме "expert" (post-validation decisions —
        forcibly surface, пользователь должен их видеть). Если task_id
        отсутствует — Decision просто сохраняется в реестр без сессии.

        Returns:
            Tuple созданных Decision-объектов.
        """
        if not decision_inputs:
            return ()
        now = utc_now_iso()
        # Группировка по task_id
        by_task: dict[str | None, list[Decision]] = {}
        for di in decision_inputs:
            decision = Decision(
                decision_id=str(uuid.uuid4()),
                project_id=project_id,
                title=di.title,
                description=di.description,
                chosen_option_id=di.recommended_option_id,
                chosen_option_ids=(),
                alternatives=di.alternatives,
                rationale=di.rationale,
                level=di.level,
                level_rationale="",
                confidence=max(0.0, min(1.0, float(di.confidence))),
                status="proposed",
                source=di.source,
                source_task_id=di.source_task_id,
                affected_artifact_ids=di.affected_artifact_ids,
                answer_mode=di.answer_mode,
                created_at=now,
                updated_at=now,
            )
            by_task.setdefault(di.source_task_id, []).append(decision)

        saved_decisions: list[Decision] = []
        for task_id, decisions in by_task.items():
            if task_id is not None:
                # Создаём сессию — forcibly surface через mode="expert".
                # process_planned_decisions сам сохранит каждый Decision.
                self.process_planned_decisions(
                    workspace,
                    project_id=project_id,
                    task_id=task_id,
                    task_title=self._task_title(workspace, task_id),
                    artifact_role="",
                    decisions=tuple(decisions),
                    mode="expert",
                )
                # Подтягиваем сохранённые версии (с финальными статусами)
                for d in decisions:
                    saved_decisions.append(self._runtime.get_decision(workspace, d.decision_id))
            else:
                # task_id неизвестен — сохраняем без сессии (видимо только в реестре)
                for d in decisions:
                    self._runtime.upsert_decision(workspace, d)
                    saved_decisions.append(self._runtime.get_decision(workspace, d.decision_id))
        return tuple(saved_decisions)

    def _task_title(self, workspace: Path, task_id: str) -> str:
        try:
            return self._runtime.get_task(workspace, task_id).title or task_id
        except Exception:
            return task_id

    # ---- mode (participation level) ------------------------------------------

    def set_participation_mode(self, workspace: Path, mode: str) -> "ModeChangeResult":
        """Сменить режим участия + реэвалюировать существующие pending решения.

        v3.2: при понижении уровня вовлечения (например, balanced → autopilot)
        все proposed-решения, которые в новом режиме УЖЕ не должны
        показываться пользователю, автоматически принимаются с дефолтом
        (status='accepted_default', user_action='not_shown').

        Если у pending CheckpointSession все её decisions после этого
        стали закрытыми — сессия финализируется, а связанная failed-task
        переводится в ready (тот же auto-resume, что в submit_answers).

        Это позволяет переключение в autopilot мгновенно разблокировать
        workflow — все ждущие решения принимаются дефолтами, задачи
        возобновляются, пользователь больше ничего не должен делать.

        При повышении уровня (autopilot → expert) — обратной реэвалюации
        нет (решения уже приняты как accepted_default; пользователь может
        переоткрыть их вручную в реестре).
        """
        from ..domain.process_state import SetClarificationModePatch
        state = self._runtime.apply_process_patch(
            workspace,
            SetClarificationModePatch(mode=mode),
            actor="operator",
            reason="participation mode changed",
        )

        project_id = state.manifest.project_id
        auto_accepted_ids: list[str] = []
        finalized_sessions: list[str] = []
        resumed_tasks: list[str] = []

        # 1. Auto-accept все proposed-решения, что новому режиму не нужны
        proposed = self._runtime.list_decisions(
            workspace, project_id=project_id, status="proposed"
        )
        for d in proposed:
            if not should_surface_to_user(d, mode):
                updated = replace(
                    d,
                    status="accepted_default",
                    user_action="not_shown",
                    updated_at=utc_now_iso(),
                )
                self._runtime.upsert_decision(workspace, updated)
                auto_accepted_ids.append(d.decision_id)

        # 2. Финализация sessions, у которых все decisions закрыты, +
        #    авто-резюм связанных failed-задач (тот же путь, что в submit_answers).
        if auto_accepted_ids:
            pending_sessions = self._runtime.list_checkpoint_sessions(
                workspace, project_id=project_id, status="pending"
            )
            for session in pending_sessions:
                # Перечитываем decisions в актуальных статусах
                still_proposed = False
                for did in session.decision_ids:
                    try:
                        if self._runtime.get_decision(workspace, did).status == "proposed":
                            still_proposed = True
                            break
                    except Exception:
                        continue
                if still_proposed:
                    continue
                # Все resolved → финализируем сессию
                finalized = replace(
                    session,
                    status="finalized",
                    finalized_at=utc_now_iso(),
                    finalized_by="mode_change",
                )
                self._runtime.upsert_checkpoint_session(workspace, finalized)
                finalized_sessions.append(session.session_id)
                # Auto-resume failed task
                try:
                    task = self._runtime.get_task(workspace, session.task_id)
                    if task.status == "failed":
                        self._runtime.transition_task(
                            workspace,
                            session.task_id,
                            "retry",
                            payload={
                                "reason": "auto-retry after mode change",
                                "source": "set_participation_mode",
                                "new_mode": mode,
                            },
                        )
                        resumed_tasks.append(session.task_id)
                except Exception:
                    # Не блокируем mode change при ошибке retry
                    pass

        return ModeChangeResult(
            mode=mode,
            auto_accepted_count=len(auto_accepted_ids),
            finalized_session_count=len(finalized_sessions),
            resumed_task_count=len(resumed_tasks),
        )

    # ---- helpers ----------------------------------------------------------

    def get_session(self, workspace: Path, session_id: str) -> CheckpointSession:
        """Достать сессию по id (NotFoundError при отсутствии)."""
        return self._runtime.get_checkpoint_session(workspace, session_id)

    def list_pending(self, workspace: Path, *, project_id: str) -> list[CheckpointSession]:
        """Активные (pending) сессии проекта."""
        return self._runtime.list_checkpoint_sessions(
            workspace, project_id=project_id, status="pending"
        )
