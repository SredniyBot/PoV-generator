"""Доменная модель Decision — первоклассная сущность реестра решений (v3.0).

Спецификация: ``specs/12_clarification_escalation.md`` раздел «v3.0 —
реестр решений + checkpoint». Критерии классификации:
``docs/decision_level_criteria.md``.

Концепция:
    Раньше система ловила только те решения, в которых LLM явно застряла
    (``ClarificationRequest``). Все остальные решения принимались молча и
    становились невидимыми. Это делало режим участия рассогласованным с
    обещанием UI.

    В v3.0 каждое решение, принимаемое LLM при сборке артефакта,
    фиксируется в реестре. Уровень решения (business / architecture /
    detail) определяется универсальными критериями. Режим участия
    пользователя — кумулятивный фильтр по уровням: какие из решений
    показываются пользователю в pre-flight checkpoint, какие применяются
    автоматически (но остаются видимыми постфактум).

Связь с ``ClarificationRequest``:
    ``ClarificationRequest`` v2.2 не удаляется. Он становится частным
    случаем — ``Decision`` со ``source = "reactive_validation"`` (и/или
    ``status = "pending_user"``) для случаев, когда pre-flight не
    предусмотрел некое решение, а валидация обнаружила пробел.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Типы-перечисления
# ---------------------------------------------------------------------------

#: Уровень вовлечения, на котором решение естественно принимается.
#: См. ``docs/decision_level_criteria.md`` для развёрнутых критериев.
DecisionLevel = Literal["business", "architecture", "detail"]

#: Жизненный цикл записи в реестре.
#:
#: - ``proposed`` — LLM предложила в pre-flight, пользователь ещё не реагировал.
#: - ``accepted_default`` — пользователь подтвердил предложение LLM (явно или
#:   неявно через autopilot / истечение checkpoint-окна).
#: - ``user_overridden`` — пользователь выбрал альтернативу или дал свободный
#:   ответ, отличный от предложенного.
#: - ``deferred`` — пользователь увидел, но явно отложил (применился дефолт,
#:   но запись помечена как требующая возврата).
#: - ``locked_in`` — решение зафиксировано и применено в генерации; пересмотр
#:   возможен только через явный override (см. CE20).
#: - ``superseded`` — пересмотрено более новым решением (хранится для аудита).
DecisionStatus = Literal[
    "proposed",
    "accepted_default",
    "user_overridden",
    "deferred",
    "locked_in",
    "superseded",
]

#: Откуда решение попало в реестр (v3.7).
#:
#: - ``pre_flight`` — task-level identification (источник 1). Имя enum
#:   сохранено для backward-compat с БД; функционально — «выявление
#:   решений на запуске задачи».
#: - ``emergent`` — post-artifact extraction (источник 2). LLM вытащила
#:   имплицитное проектное решение из готового артефакта.
#: - ``reactive_validation`` — legacy fallback-путь v2.2: валидация
#:   обнаружила пробел / низкую уверенность в payload.
#: - ``user_manual`` — пользователь сам добавил решение через UI.
#:
#: История: в v3.6 был ``phase_gap_analysis`` (третий источник). В v3.7
#: удалён — избыточная абстракция, identification + extraction уже
#: покрывают пространство явных + имплицитных решений.
DecisionSource = Literal[
    "pre_flight",
    "emergent",
    "reactive_validation",
    "user_manual",
]

#: Что сделал пользователь с записью на её текущем checkpoint.
#:
#: - ``not_shown`` — не показывалась (autopilot, или уровень ниже режима).
#: - ``accepted_default`` — подтвердил дефолт одним кликом / не возразил.
#: - ``modified`` — выбрал альтернативу или ввёл свободный ответ.
#: - ``deferred`` — явно отложил.
#: - ``pending`` — показано, реакции пока нет.
DecisionUserAction = Literal[
    "not_shown",
    "accepted_default",
    "modified",
    "deferred",
    "pending",
]

#: Режим ответа: как пользователь может ответить.
#:
#: - ``single`` — выбор одного варианта из альтернатив (radio).
#: - ``multiple`` — выбор нескольких вариантов (checkboxes).
#: - ``free_text`` — только свободный ответ, альтернатив нет (или они
#:   опциональны как подсказки).
#: - ``confirmation`` — да/нет / подтверждение действия (рендерится как
#:   одна кнопка с описанием в title).
#:
#: По умолчанию ``single`` — самый частый кейс. Пришёл из legacy
#: ``ClarificationCandidate.answer_mode``; миграция v3.1 расширила Decision
#: до полного покрытия answer-mode space.
DecisionAnswerMode = Literal["single", "multiple", "free_text", "confirmation"]


# ---------------------------------------------------------------------------
# Доменные структуры
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionAlternative:
    """Один из вариантов в пространстве выбора для решения.

    Каждый вариант — самодостаточный: пользователь должен иметь возможность
    сделать выбор только по тексту альтернативы, без необходимости
    переключаться на другие материалы.

    Args:
        option_id: стабильный идентификатор внутри решения. Используется
            для ссылки из ``Decision.chosen_option_id`` и для UI-радиокнопок.
        label: короткое название варианта (3-7 слов).
        description: развёрнутое описание — что это, как влияет.
        pros: ключевые плюсы (с привязкой к контексту, не общие слова).
        cons: ключевые минусы / риски.
        confidence: оценка LLM, насколько этот вариант хороший выбор
            (0..1). ``None`` означает «не рассчитывалась» (например, для
            пользовательского свободного ответа).
    """

    option_id: str
    label: str
    description: str = ""
    pros: tuple[str, ...] = field(default_factory=tuple)
    cons: tuple[str, ...] = field(default_factory=tuple)
    confidence: float | None = None


@dataclass(frozen=True)
class Decision:
    """Одно решение в реестре проекта.

    Минимальный набор полей, по которым решение считается «полным»
    (см. CE19 в specs/12): ``title``, ``description``, ``chosen_option_id``
    с соответствующей альтернативой в ``alternatives``, ``rationale``,
    ``level``, ``level_rationale``, ``confidence``, ``source``.

    Без любого из перечисленных запись недостаточно полна для
    отображения пользователю и должна быть либо доработана LLM, либо
    помечена как требующая ручного ввода.

    Args:
        decision_id: UUID, генерируется при создании.
        project_id: проект, к которому относится решение.
        title: короткая формулировка решения, человеко-читаемая
            («Выбор СУБД для основного сервиса»).
        description: что именно решается, с контекстом — достаточно, чтобы
            понять, в чём суть, без обращения к артефакту.
        chosen_option_id: ID выбранного варианта в ``alternatives``.
            Может быть пустым в момент создания (если LLM ещё не
            предложила) — но к моменту checkpoint обязан быть.
        alternatives: пространство выбора (≥1 элемент; 0 — для
            placeholder-decision, ожидающего LLM-обогащения).
        rationale: почему именно этот вариант предложен как дефолт.
            Не «потому что это лучше» — конкретно: «потому что в контексте
            X, и для NFR Y».
        level: классификация решения по уровню вовлечения.
        level_rationale: короткое объяснение выбора уровня (1-3 предложения)
            со ссылкой на критерии. Нужно для прозрачности и для возможности
            пользователя оспорить классификацию.
        confidence: уверенность LLM в выборе дефолта (0..1). Используется
            для подсветки рискованного — низкая уверенность визуально
            заметна в реестре в любом режиме.
        status: жизненный цикл записи.
        source: откуда решение попало в реестр.
        source_task_id: задача, в ходе которой решение возникло (если есть).
        affected_artifact_ids: артефакты, в формировании которых это
            решение участвовало.
        depends_on_decision_ids: другие решения, от которых это зависит
            (для каскадного пересчёта при override).
        user_action: что сделал пользователь на текущем checkpoint.
        original_chosen_option_id: исходный дефолт от LLM (отличается от
            ``chosen_option_id``, если пользователь переопределил).
        user_free_text_answer: свободный ответ пользователя, если он
            предпочёл его вариантам.
        free_form_level_override: пользователь переклассифицировал уровень
            (None означает «принимаю классификацию LLM как есть»).
        created_at: ISO-8601 UTC.
        updated_at: ISO-8601 UTC.
    """

    decision_id: str
    project_id: str
    title: str
    description: str
    chosen_option_id: str
    alternatives: tuple[DecisionAlternative, ...]
    rationale: str
    level: DecisionLevel
    level_rationale: str
    confidence: float
    status: DecisionStatus
    source: DecisionSource
    source_task_id: str | None = None
    affected_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    depends_on_decision_ids: tuple[str, ...] = field(default_factory=tuple)
    user_action: DecisionUserAction = "not_shown"
    original_chosen_option_id: str | None = None
    user_free_text_answer: str | None = None
    free_form_level_override: DecisionLevel | None = None
    created_at: str = ""
    updated_at: str = ""
    # v3.1 — миграция legacy clarifications в Decision.
    # ``answer_mode`` определяет UI:
    #   single → radio (chosen_option_id заполнен)
    #   multiple → checkboxes (chosen_option_ids заполнен, может быть пустым)
    #   free_text → textarea (user_free_text_answer заполнен)
    #   confirmation → одна кнопка «подтвердить»
    # Для single-mode `chosen_option_ids` остаётся пустым tuple, использовать
    # `chosen_option_id`. Для multi — наоборот. Свойство `effective_chosen_ids`
    # унифицирует доступ.
    answer_mode: DecisionAnswerMode = "single"
    chosen_option_ids: tuple[str, ...] = field(default_factory=tuple)
    # v3.4: пользователь может пометить рискованное решение как «я
    # просмотрел и согласен» — это снимает индикатор «система не уверена»
    # в UI. Решение при этом не меняется (ни choice, ни alternatives);
    # это аудит-метка вида «человек видел и подтвердил». Используется для
    # ситуаций, когда дефолт LLM на самом деле адекватен, но confidence < 0.5
    # из-за общей неопределённости — пользователь не хочет шумных меток.
    user_verified: bool = False
    user_verified_at: str | None = None

    # ---- удобные производные свойства -------------------------------------

    @property
    def effective_level(self) -> DecisionLevel:
        """Уровень с учётом возможной пользовательской переклассификации."""
        return self.free_form_level_override or self.level

    @property
    def effective_chosen_ids(self) -> tuple[str, ...]:
        """Унифицированный доступ к выбранным option_id вне зависимости от mode.

        - single + non-empty chosen_option_id → (chosen_option_id,)
        - multiple → chosen_option_ids
        - free_text → пустой tuple (выбор не через option_id)
        """
        if self.answer_mode == "multiple":
            return self.chosen_option_ids
        if self.chosen_option_id:
            return (self.chosen_option_id,)
        return ()

    @property
    def chosen_alternative(self) -> DecisionAlternative | None:
        """Текущий выбранный вариант для single-mode (если есть и валиден).

        Для multi-mode возвращает первый из chosen_option_ids.
        Для free_text — None.
        """
        primary_ids = self.effective_chosen_ids
        if not primary_ids:
            return None
        for alt in self.alternatives:
            if alt.option_id == primary_ids[0]:
                return alt
        return None

    @property
    def effective_confidence(self) -> float:
        """v3.5: «фактическая» уверенность системы в выбранном дефолте.

        Берётся МИНИМУМ из двух сигналов:
        1) overall `Decision.confidence` — общая уверенность LLM в этой
           развилке в целом (e.g. насколько чётко поставлен вопрос).
        2) confidence у выбранной альтернативы — насколько LLM уверена
           именно в той опции, которая поедет в артефакт.

        Min — защитная семантика: если ХОТЯ БЫ один сигнал низкий, считаем
        решение рискованным. Это соответствует поведению UI: пилл «система
        не уверена» загорается, как только пользователю стоит присмотреться.
        Для legacy-записей без per-alt confidence min сводится к overall.

        Возвращает float в [0, 1].
        """
        alt = self.chosen_alternative
        if alt is not None and alt.confidence is not None:
            return float(min(self.confidence, alt.confidence))
        return float(self.confidence)

    @property
    def is_low_confidence(self) -> bool:
        """Маркер для подсветки рискованного в любом режиме.

        Граница 0.5 — эмпирическая, ниже неё система себя не считает
        способной принять решение «не глядя». Конкретный порог потом
        можно вынести в конфигурацию.

        v3.4: пользователь может явно «верифицировать» рискованное решение
        (см. `user_verified`) — это снимает индикатор. Метка остаётся
        только пока в UI стоит «непросмотрено».

        v3.5: учитывает per-alternative confidence через
        `effective_confidence` (min из overall + chosen-alt confidence).
        Это даёт более точную картину: даже если общая уверенность задачи
        высокая, но LLM не уверена в выбранном варианте — флаг загорится.
        """
        return self.effective_confidence < 0.5 and not self.user_verified

    @property
    def was_user_modified(self) -> bool:
        """True, если пользователь явно изменил выбор LLM."""
        return self.status == "user_overridden" or self.user_action == "modified"


@dataclass(frozen=True)
class DecisionInput:
    """Lightweight payload, который эмиттер передаёт в CheckpointService
    для регистрации нового Decision (v3.1 — replaces ClarificationCandidate).

    Используется эмиттерами:
    - validation_service (когда валидация артефакта нашла пробел)
    - methodology_rules (когда правило методологии выпало)
    - quality_gate signoff
    - любые будущие источники (domain_pack, user_manual)

    CheckpointService переводит DecisionInput → Decision, сохраняет в
    реестр и при необходимости создаёт CheckpointSession.

    Args:
        title: краткая формулировка вопроса для UI (1 строка).
        description: развёрнутое описание (для CheckpointSession).
        alternatives: возможные варианты ответа (≥0; для free_text может быть 0).
        recommended_option_id: какой вариант предложен по умолчанию (опционально).
        rationale: почему предложен именно этот дефолт.
        level: уровень вовлечения (business/architecture/detail).
        answer_mode: формат ответа (single/multiple/free_text/confirmation).
        confidence: уверенность системы в дефолте (0..1).
        source: откуда возникло решение ("reactive_validation" / "emergent" / ...).
        source_task_id: id задачи, которая привела к появлению решения.
        affected_artifact_ids: артефакты, на которые повлияет это решение.
    """

    title: str
    description: str
    alternatives: tuple[DecisionAlternative, ...]
    recommended_option_id: str
    rationale: str
    level: DecisionLevel
    answer_mode: DecisionAnswerMode = "single"
    confidence: float = 0.5
    source: DecisionSource = "reactive_validation"
    source_task_id: str | None = None
    affected_artifact_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Режим участия → набор уровней (cumulative)
# ---------------------------------------------------------------------------

#: Кумулятивные уровни для каждого режима участия (CE17 v3.0).
#:
#: - ``autopilot``: пустой набор; checkpoint не показывается вообще.
#: - ``balanced``: пользователь видит только бизнес-решения.
#: - ``control``: бизнес + архитектура.
#: - ``expert``: все три уровня, включая исполнительские детали.
#:
#: Сравнение с v2.2: там balanced и control были идентичны (visibility-set
#: {principal, architectural}); теперь они отличаются поведенчески —
#: balanced показывает только бизнес-уровень, control добавляет архитектуру.
ENGAGEMENT_LEVELS: dict[str, frozenset[DecisionLevel]] = {
    "autopilot": frozenset(),
    "balanced": frozenset({"business"}),
    "control": frozenset({"business", "architecture"}),
    "expert": frozenset({"business", "architecture", "detail"}),
}


def levels_for_mode(mode: str) -> frozenset[DecisionLevel]:
    """Какие уровни решений показываются пользователю в этом режиме.

    Если режим неизвестен — возвращается набор ``balanced`` как
    наиболее безопасный дефолт (показать хотя бы бизнес-уровень).
    """
    return ENGAGEMENT_LEVELS.get(mode, ENGAGEMENT_LEVELS["balanced"])


def should_surface_to_user(decision: Decision, mode: str) -> bool:
    """Должно ли это решение попасть в checkpoint пользователя.

    Учитывает:
    - кумулятивные уровни режима (см. ENGAGEMENT_LEVELS);
    - возможную переклассификацию уровня пользователем
      (effective_level, а не сырой level).

    Не зависит от ``status`` — функция отвечает только на «соответствует
    ли уровень режиму», а статус определяет, что делать дальше (показать
    как proposed / уже решено / итд).
    """
    return decision.effective_level in levels_for_mode(mode)
