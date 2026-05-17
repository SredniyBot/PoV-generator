"""Доменная модель CheckpointSession — точка вовлечения пользователя
в принятие решений перед генерацией артефакта (v3.0).

Что такое checkpoint:
    Перед задачей, которая генерирует артефакт, ExecutionService делает
    pre-flight вызов LLM: «перечисли решения, которые ты собираешься
    принять для этой задачи». Получив список, система фильтрует его по
    уровню вовлечения пользователя (см. levels_for_mode).

    Если после фильтра остались решения — создаётся CheckpointSession.
    Workflow приостанавливается. Пользователь видит сессию в UI:
    серию карточек-решений, по каждой может подтвердить дефолт,
    выбрать альтернативу, дать свой ответ или отложить. После
    submit — workflow возобновляется с зафиксированными решениями.

    Если фильтр оставил пустой список (autopilot, или все решения
    ниже уровня режима) — сессия не создаётся, решения сразу попадают
    в реестр со статусом `accepted_default`, workflow идёт дальше.

Связи:
    - Session принадлежит одной задаче (one-to-one): per-task checkpoint.
      Если задача переиспользует тот же артефакт в нескольких runs,
      создаётся отдельная сессия в каждом run.
    - Session ссылается на ID решений в реестре (Decision). Сами решения
      хранятся в decisions; checkpoint — это контекст «когда показано и
      что выбрал пользователь».

Спецификация: ``specs/12_clarification_escalation.md`` раздел v3.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


#: Жизненный цикл сессии.
#:
#: - ``pending`` — создана, ждёт реакции пользователя.
#: - ``finalized`` — пользователь дал ответы (или массово подтвердил
#:   дефолты), workflow может продолжаться.
#: - ``expired`` — закрыта без явного ответа (будущая фича для timeout).
#: - ``cancelled`` — отменена (например, при отмене workflow).
CheckpointStatus = Literal["pending", "finalized", "expired", "cancelled"]


#: Что пользователь сделал с одним решением в checkpoint.
#:
#: Соответствует ``Decision.user_action``, но с уточнённой семантикой
#: применимости к чекпоинту:
#: - ``accept_default`` — оставил предложение LLM.
#: - ``select_alternative`` — выбрал другой option_id из альтернатив.
#: - ``free_text`` — дал свободный ответ.
#: - ``defer`` — отложил (применится дефолт, помечен).
CheckpointAnswerKind = Literal[
    "accept_default",
    "select_alternative",
    "free_text",
    "defer",
]


@dataclass(frozen=True)
class CheckpointAnswer:
    """Один ответ пользователя на один decision в сессии.

    Создаётся в момент submit, по одной на каждое решение, попавшее в
    сессию. Не персистируется отдельно — применяется к Decision и
    логируется как событие.

    Args:
        decision_id: к какому решению относится ответ.
        kind: тип ответа (см. CheckpointAnswerKind).
        selected_option_id: для single-mode select_alternative — какой
            option_id выбран. None в остальных случаях.
        selected_option_ids: v3.1 для multi-mode select_alternative —
            множество выбранных option_id. Если задано — используется
            оно (selected_option_id игнорируется).
        free_text: если kind == "free_text" — свободный ответ.
    """

    decision_id: str
    kind: CheckpointAnswerKind
    selected_option_id: str | None = None
    selected_option_ids: tuple[str, ...] | None = None
    free_text: str | None = None


@dataclass(frozen=True)
class CheckpointSession:
    """Сессия чекпоинта: одна задача, одна остановка, набор решений.

    Args:
        session_id: UUID сессии.
        project_id: проект, в котором она возникла.
        task_id: задача, перед которой стоит чекпоинт.
        task_title: человекочитаемое название задачи для UI (чтобы не
            ходить отдельно в task-graph за заголовком).
        artifact_role: какой артефакт будет сгенерирован после чекпоинта
            (для UI: «Перед requirements_specification — 5 решений»).
        status: жизненный цикл (см. CheckpointStatus).
        decision_ids: ID решений в реестре, которые попали в эту сессию
            (отфильтрованы по уровню режима пользователя). Сами решения
            хранятся в decisions; здесь — только связь.
        created_at: ISO-8601 UTC.
        finalized_at: время финализации (нужно для аудита, и для UX —
            «эта сессия закрыта Х минут назад»).
        finalized_by: actor — кто финализировал. ``user`` если submit
            из UI, ``system`` если auto-finalize (timeout / cancel).
    """

    session_id: str
    project_id: str
    task_id: str
    task_title: str
    artifact_role: str
    status: CheckpointStatus
    decision_ids: tuple[str, ...]
    created_at: str = ""
    finalized_at: str | None = None
    finalized_by: str | None = None

    # ---- удобные производные свойства -------------------------------------

    @property
    def is_pending(self) -> bool:
        """Сессия ещё ждёт ответа пользователя."""
        return self.status == "pending"

    @property
    def is_actionable(self) -> bool:
        """Можно ли применять ответы (только к pending сессиям)."""
        return self.status == "pending"
