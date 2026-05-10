# Состояние проектного понимания

> **Статус:** v2.1 · черновик · 2026-05-09

`ProblemState` хранит текущее понимание проекта. Он не хранит структуру графа задач и не задает порядок выполнения.

---

## Содержимое

```python
class ProblemState(BaseModel):
    project_id: UUID
    objective_ref: str
    root_task_id: UUID | None
    business_request: str
    goal: str | None
    facts: dict[str, FactRecord]
    assumptions: dict[str, AssumptionRecord]
    constraints: dict[str, ConstraintRecord]
    risks: dict[str, RiskRecord]
    gaps: dict[str, GapRecord]
    decisions: dict[str, DecisionRecord]
    readiness: dict[str, ReadinessRecord]
    domain_signals: dict[str, DomainSignalRecord]
    active_domain_packs: dict[str, ActiveDomainPackRecord]
    active_methodology_packs: dict[str, ActiveMethodologyPackRecord]
    clarification_mode: Literal["autopilot", "balanced", "control", "expert"]
    clarification_requests: dict[str, ClarificationRequestRecord]
    version: int
```

---

## Ключевые записи

Gap:

```python
class GapRecord(BaseModel):
    id: str
    title: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    blocking: bool
    related_task_ids: list[UUID]
    related_artifact_ids: list[UUID]
```

Readiness:

```python
class ReadinessRecord(BaseModel):
    id: str
    status: Literal["missing", "partial", "ready", "waived"]
    blocking: bool
    confidence: float
    evidence_refs: list[str]
```

Активный доменный пакет:

```python
class ActiveDomainPackRecord(BaseModel):
    ref: str
    status: Literal["candidate", "active", "disabled"]
    source: Literal["llm_detector", "operator", "artifact", "system"]
    rationale: str
    confidence: float
```

Активный методологический пакет:

```python
class ActiveMethodologyPackRecord(BaseModel):
    ref: str
    status: Literal["active", "disabled"]
    source: Literal["operator", "objective_default", "system"]
    rationale: str
    activated_at: datetime
```

В MVP активным может быть не более одного методологического пакета. Множественная активация — расширение после MVP.

Уточнение:

```python
class ClarificationRequestRecord(BaseModel):
    id: str
    status: Literal["open", "answered", "assumed", "deferred", "cancelled"]
    priority: Literal["low", "medium", "high", "critical"]
    question: str
    reason: str
    impact: str
    blocking_scope: Literal["none", "task", "subtree", "objective"]
    related_task_ids: list[UUID]
    related_artifact_refs: list[str]
    selected_answer: str | None
    accepted_assumption: str | None
```

---

## Модель изменений

`ProblemState` изменяется только через события.

Разрешенные операции:

```text
set_goal
add_fact
upsert_assumption
upsert_constraint
upsert_risk
open_gap
close_gap
upsert_decision
upsert_readiness
detect_domain_signal
activate_domain_pack
disable_domain_pack
activate_methodology_pack
disable_methodology_pack
set_root_task
set_clarification_mode
open_clarification
answer_clarification
accept_clarification_assumption
close_clarification
```

Исполнитель не применяет изменение напрямую. Он может только предложить изменения, которые слой управления процессом применит после валидации.

---

## Активация доменов

```text
найден доменный сигнал
  -> кандидатный доменный пакет
  -> решение об активации
  -> активный доменный пакет в ProblemState
  -> раскрытие графа применяет доменные расширения
```

Доменный сигнал сам по себе не обязан автоматически активировать доменный пакет, если политика требует подтверждения.

---

## Что читает планировщик

Планировщик использует:

- исходный бизнес-запрос;
- факты, ограничения и риски;
- открытые блокирующие пробелы;
- решения;
- готовность;
- активные доменные пакеты;
- доменные сигналы.
- открытые блокирующие уточнения.

Планировщик не использует `ProblemState` как хранилище маршрута.

---

## Уточнения и допущения

`ProblemState` хранит не сырой диалог, а нормализованное состояние уточнений:

- какие вопросы сейчас открыты;
- какие ответы уже получены;
- какие допущения приняты системой;
- какие задачи или артефакты затронуты;
- какой режим участия пользователя выбран.

Сырые кандидаты уточнений и технические трассы хранятся в журнале уточнений и событий. В `ProblemState` попадает только то, что влияет на понимание проекта и планирование.

Ответ пользователя может быть применен к `ProblemState` только через проверенные эффекты:

- факт;
- ограничение;
- решение;
- риск;
- пробел;
- готовность;
- доменный сигнал или активный доменный пакет;
- допущение.

Открытый блокирующий вопрос считается управляемым пробелом. Он блокирует только указанную область графа, а не весь проект автоматически.

---

## Инварианты

| ID | Правило |
|---|---|
| PS1 | Все изменения добавляются через события, без перезаписи истории. |
| PS2 | Готовность `ready` требует подтверждающих свидетельств. |
| PS3 | Блокирующий пробел влияет на допуск к запуску. |
| PS4 | Активный доменный пакет должен существовать в реестре. |
| PS5 | `ProblemState` не хранит граф задач. |
| PS6 | `ProblemState` не хранит линейный маршрут выполнения. |
| PS7 | Ответ пользователя применяется как проверенный эффект, а не как произвольная мутация состояния. |
| PS8 | Открытое уточнение должно иметь связанную область влияния. |
| PS9 | Активный `methodology_pack` должен существовать в реестре. |
| PS10 | В MVP — не более одного активного `methodology_pack` на проект. |
