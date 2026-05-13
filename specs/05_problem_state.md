# Состояние проектного понимания

> **Статус:** v3.0 · черновик · 2026-05-13. Заменяет однородный
> `ProblemState` из v2.* двумя слоями: знания и процесс.

Состояние проекта разделено на **два независимых слоя** с разной природой
изменений и разной аудиторией:

- **Слой A — Знания о проекте** (`ProjectKnowledge`): то, что **известно
  или принято** про проект. Однородная коллекция «положений»: фактов,
  допущений, решений, ограничений, рисков. Артефакты опираются на этот
  слой.
- **Слой B — Состояние процесса** (`ProcessState`): то, что описывает
  **где сейчас работа**. Пробелы, готовность, активные пакеты, режим
  вовлечённости пользователя.

Композитный снимок состояния проекта — `ProjectState`:

```python
class ProjectState(BaseModel):
    manifest: ProjectManifest         # иммутабельный seed проекта
    knowledge: ProjectKnowledge       # Layer A
    process: ProcessState             # Layer B
```

Состав `ProjectManifest` (иммутабельные данные проекта, хранится в
`project.json`):

```python
class ProjectManifest(BaseModel):
    project_id: UUID
    name: str
    objective_ref: str
    business_request: str
    created_at: datetime
```

---

## Слой A — Знания о проекте

Однородная коллекция положений. У каждого положения одна и та же
операционная форма; роль определяется полем `type`.

```python
class ProjectKnowledge(BaseModel):
    positions: dict[str, Position]   # по identifier'у
    version: int
    updated_at: datetime
```

### Положение проекта

```python
class Position(BaseModel):
    identifier: str
    type: Literal["fact", "assumption", "decision", "constraint", "risk"]
    statement: str                                  # формулировка простым языком
    visibility: Literal["principal", "architectural", "technical"]
    scope: Literal["global", "domain", "local"]
    source: Literal["input", "user", "system", "clarification", "artifact"]
    taken_by: str                                   # actor (user_id, "system", "clarification:<id>", ...)
    taken_at: datetime
    confidence: float                               # 0.0 .. 1.0
    tags: tuple[str, ...]
    alternatives: tuple[PositionAlternative, ...]   # рассматривавшиеся варианты
    related_position_ids: tuple[str, ...]
    status: Literal["active", "superseded", "rejected"]
    supersedes: str | None
    superseded_at: datetime | None
    rejection_reason: str | None
```

Типы положений:

- **fact** — что-то истинное (извлечено из входа или подтверждено).
- **assumption** — выведено системой, не подтверждено пользователем.
- **decision** — выбрано между альтернативами.
- **constraint** — жёсткая граница (бюджет, срок, регуляторика).
- **risk** — известная опасность.

Тип — роль положения в понимании проекта, не подкласс. Операционная
форма одна на все типы; это даёт однородные проекции и одинаковое UI.

### Уровни видимости

- `principal` — бизнес-цель, главное ограничение, целевой пользователь.
- `architectural` — выбор подхода, контур решения, способ интеграции.
- `technical` — деталь схемы данных, библиотека, тонкости поведения.

Уровень видимости влияет на:

- **UI:** principal-положения в журнале всегда сверху, technical —
  свёрнуты по умолчанию.
- **Engagement-алинейка:** см. `12_clarification_escalation.md`. Чем
  ниже уровень, тем выше engagement требуется для проактивного вопроса.

Право видеть и оспорить положение **не зависит** от engagement-режима.

### Цель проекта как положение

Цель проекта живёт в Слое A с стабильным идентификатором
`project.goal`, типом `fact`, видимостью `principal`, scope `global`.
Это не отдельное поле в state — это положение в общей коллекции.

Удобный аксессор `ProjectKnowledge.goal_statement() -> str | None`
возвращает формулировку цели или `None`, если не задана.

### Патчи слоя A

```text
UpsertPositionPatch        # добавить или заменить по identifier'у
SupersedePositionPatch     # заменить с историей: старое → superseded
RejectPositionPatch        # явно отвергнуть (без замены)
ElevateVisibilityPatch     # поднять уровень видимости при оспаривании
```

Применяются через `apply_knowledge_patch(knowledge, patch) -> ProjectKnowledge`.

---

## Слой B — Состояние процесса

Динамическое состояние работы. Не содержит знаний о проекте.

```python
class ProcessState(BaseModel):
    root_task_id: UUID | None
    active_gaps: dict[str, GapRecord]
    readiness: dict[str, ReadinessRecord]
    domain_signals: dict[str, DomainSignalRecord]
    active_domain_packs: dict[str, ActiveDomainPackRecord]
    active_methodology_packs: dict[str, ActiveMethodologyPackRecord]
    clarification_mode: Literal["autopilot", "balanced", "control", "expert"]
    version: int
    updated_at: datetime
```

### Ключевые записи

```python
class GapRecord(BaseModel):
    identifier: str
    title: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    blocking: bool
    opened_at: datetime
    closed_at: datetime | None

class ReadinessRecord(BaseModel):
    dimension: str
    status: Literal["missing", "partial", "ready", "waived"]
    blocking: bool
    confidence: float
    evidence: tuple[str, ...]
    updated_at: datetime

class ActiveDomainPackRecord(BaseModel):
    ref: str
    domain: str
    status: Literal["candidate", "active", "disabled"]
    source: Literal["llm_detector", "operator", "artifact", "system", "bootstrap"]
    rationale: str
    confidence: float
    activated_at: datetime

class ActiveMethodologyPackRecord(BaseModel):
    ref: str
    status: Literal["active", "disabled"]
    source: Literal["operator", "objective_default", "system", "bootstrap"]
    rationale: str
    activated_at: datetime
```

В MVP активным может быть не более одного `methodology_pack` на проект
(PS10).

### Алинейка engagement-видимости

`ProcessState.should_ask_user_for(visibility: VisibilityLevel) -> bool`
возвращает, надо ли проактивно выносить положение этого уровня на
пользователя:

| Режим | Проактивно спрашиваются уровни |
|---|---|
| `autopilot` | `principal` |
| `balanced` | `principal`, `architectural` |
| `control` | `principal`, `architectural` |
| `expert` | `principal`, `architectural`, `technical` |

Право пользователя посмотреть и оспорить любое положение —
**универсально** и не регулируется engagement-режимом.

### Патчи слоя B

```text
SetRootTaskPatch
UpsertGapPatch / CloseGapPatch
UpsertReadinessPatch
DetectDomainSignalPatch
ActivateDomainPackPatch / DisableDomainPackPatch
ActivateMethodologyPackPatch / DisableMethodologyPackPatch
SetClarificationModePatch
```

Применяются через `apply_process_patch(state, patch) -> ProcessState`.

---

## События состояния

История изменений обоих слоёв — единый журнал `StateEvent`:

```python
class StateEvent(BaseModel):
    layer: Literal["knowledge", "process"]
    version: int
    patch_type: str
    payload: dict[str, object]
    actor: str
    reason: str
    created_at: datetime
```

Хронологический обход позволяет восстановить любое состояние на любой
момент времени. Поле `layer` различает, к какому слою относилось
изменение.

---

## Что читает планировщик

Из Слоя A:

- цель проекта (через `goal_statement()`);
- активные положения для admission'а (через тип/scope/tags по нужде).

Из Слоя B:

- открытые блокирующие пробелы;
- активные domain/methodology packs;
- readiness для каждого измерения;
- открытые блокирующие уточнения (через runtime).

Из manifest'a:

- `business_request` для шаблонов задач, требующих исходный текст.

Планировщик не использует ни один из слоёв как хранилище маршрута.

---

## Что хранит, чего не хранит

| Слой | Хранит | Не хранит |
|---|---|---|
| `ProjectManifest` | identity-данные (id, имя, objective, исходный запрос, время создания) | состояние работы; знание |
| `ProjectKnowledge` | положения проекта (факты/допущения/решения/ограничения/риски) | граф задач; маршрут; ход работы |
| `ProcessState` | пробелы/готовность/активные паки/режим | конкретные факты; формулировку цели |
| Event log | хронологию патчей со ссылками на actor/reason | финальный снимок (он в snapshot-таблицах) |

---

## Инварианты

| ID | Правило |
|---|---|
| PS1 | Все изменения проходят через патчи; прямая мутация запрещена. |
| PS2 | Готовность `ready` требует подтверждающих свидетельств. |
| PS3 | Блокирующий пробел влияет на admission к запуску. |
| PS4 | Активный domain pack должен существовать в реестре. |
| PS5 | `ProjectState` не хранит граф задач. |
| PS6 | `ProjectState` не хранит линейный маршрут выполнения. |
| PS7 | Ответ пользователя применяется как разрешённое изменение слоя A (положение). |
| PS8 | Положение со `status='superseded'` имеет `superseded_at`. |
| PS9 | Активный methodology pack должен существовать в реестре. |
| PS10 | В MVP — не более одного активного methodology pack на проект. |
| PS11 | Артефакты ссылаются на использованные положения слоя A через их identifier'ы. |
| PS12 | Слои A и B имеют независимые версии и независимые event-потоки. |
| PS13 | Цель проекта живёт в слое A как положение с identifier'ом `project.goal`. |
| PS14 | Уровень видимости положения не может быть понижен (только повышен через `ElevateVisibilityPatch`). |
