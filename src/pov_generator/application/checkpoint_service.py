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

from ..common.errors import ConflictError, NotFoundError
from ..common.serialization import utc_now_iso
from ..domain.checkpoints import (
    CheckpointAnswer,
    CheckpointSession,
)
from ..domain.decisions import (
    Decision,
    levels_for_mode,
    should_surface_to_user,
)
from ..infrastructure.sqlite_runtime import SqliteRuntime


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
        return self._runtime.upsert_checkpoint_session(workspace, finalized)

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
            if answer.selected_option_id is None:
                raise ConflictError(
                    f"answer.kind=select_alternative требует selected_option_id"
                )
            valid_options = {alt.option_id for alt in decision.alternatives}
            if answer.selected_option_id not in valid_options:
                raise ConflictError(
                    f"option_id {answer.selected_option_id!r} нет среди альтернатив "
                    f"решения {decision_id!r}"
                )
            saved = replace(
                decision,
                chosen_option_id=answer.selected_option_id,
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

    # ---- helpers ----------------------------------------------------------

    def get_session(self, workspace: Path, session_id: str) -> CheckpointSession:
        """Достать сессию по id (NotFoundError при отсутствии)."""
        return self._runtime.get_checkpoint_session(workspace, session_id)

    def list_pending(self, workspace: Path, *, project_id: str) -> list[CheckpointSession]:
        """Активные (pending) сессии проекта."""
        return self._runtime.list_checkpoint_sessions(
            workspace, project_id=project_id, status="pending"
        )
