# Режим участия пользователя (clarification mode)

> Сводный практический отчёт по подсистеме «насколько часто система обращается
> к менеджеру с уточнениями». Нормативная спека — [`specs/12_clarification_escalation.md`](../specs/12_clarification_escalation.md).

---

## 1. Зачем

Система-аналитик должна задавать менеджеру **только важные** вопросы и не
шуметь деталями реализации, но при этом не прятать критичные неопределённости.
Режим участия — это **порог автономного решения**, а не право доступа.

Принципы из спеки (`specs/12_clarification_escalation.md:13-19`):

- минимизировать лишнее вмешательство пользователя,
- не скрывать критичные неопределённости,
- формулировать вопросы от бизнес-потребности, а не от внутренней реализации,
- блокировать только ту часть работы, которую нельзя продолжать корректно.

**Инвариант CE13:** право пользователя посмотреть, оспорить, переоткрыть,
отвергнуть или повысить видимость положения **универсально** и не зависит от
режима. Autopilot не «прячет» решения — он принимает их автономно, всё остаётся
в истории и доступно для пересмотра.

---

## 2. Режимы и матрица видимости

Тип: `ClarificationMode = Literal["autopilot", "balanced", "control", "expert"]`
(`src/pov_generator/domain/clarifications.py:13`). Дефолт проекта — `balanced`
(`src/pov_generator/domain/process_state.py:151`).

Источник истины — `_PROACTIVE_ASK_LEVELS` в `process_state.py:116-121`:

```python
_PROACTIVE_ASK_LEVELS: dict[ClarificationMode, frozenset[VisibilityLevel]] = {
    "autopilot": frozenset({"principal"}),
    "balanced":  frozenset({"principal", "architectural"}),
    "control":   frozenset({"principal", "architectural"}),
    "expert":    frozenset({"principal", "architectural", "technical"}),
}
```

Семантика:

| Режим | Спрашивается проактивно | Остальное |
|---|---|---|
| `autopilot` | только `principal` (бизнес-цели, критическое согласование) | architectural + technical → автономно (assume/defer) |
| `balanced` (default) | `principal` + `architectural` | technical → автономно |
| `control` | `principal` + `architectural` | то же покрытие, что balanced (см. §10, открытое расхождение со спекой) |
| `expert` | все три уровня | максимальная вовлечённость, почти ничего не решается автономно |

---

## 3. Visibility — единственная ось решения (CE12)

В текущем коде **нет** `_ROLE_FLOOR` и **нет** `min_participation_mode` —
архитектура была упрощена. Решение ask/assume/defer принимается **только**
по `candidate.visibility`. `decision_owner_role` оставлен как
**информационная** ось (UI-группировка + подсказки LLM при составлении
формулировки), но на действие **не влияет**.

Маппинг роли → дефолтная visibility (`clarification_service.py:46-58`):

```python
_ROLE_DEFAULT_VISIBILITY: dict[DecisionOwnerRole, VisibilityLevel] = {
    "business":      "principal",
    "client":        "principal",
    "security":      "principal",
    "data_owner":    "architectural",
    "methodologist": "architectural",
    "architect":     "technical",
}
```

Применяется в `candidate_from_question(...)` только когда `visibility is None`
(строки 717-719). Эмиттер может **явно переопределить** visibility — например,
gate signoff с `decision_owner_role=client` принудительно ставит
`visibility="principal"` (`validation_service.py:570`).

**CE14:** эмиттер обязан проставлять `visibility` явно либо позволить
координатору вывести её из `decision_owner_role` через дефолт.

---

## 4. Дополнительные сигналы

`confidence_without_user` — в решении ask/assume/defer **НЕ участвует** (CE12).
Эмиттер сам решает, прикреплять ли `default_assumption` исходя из своей
уверенности.

- `default_assumption: str | None` — если задан, координатор имеет право «тихо»
  принять допущение, когда mode не требует surface. Без него — переходит к `defer`.
- `blocking_scope: Literal["none", "task", "subtree", "objective"]` —
  `"objective"` форсирует `ask` даже вне proactive-set (страховка для gate signoff).

Confidence используется косвенно — в валидации артефакта: payload с
`confidence < 0.45` → finding `low_confidence` + candidate с `severity=high` /
`blocking_scope="task"` (`validation_service.py:266-296`). Порог 0.45 жёстко
зашит и общесистемный, не зависит от режима.

---

## 5. Алгоритм `_decide_action`

`clarification_service.py:1126-1150`:

```python
def _decide_action(self, candidate, mode) -> Literal["ask", "assume", "defer"]:
    # 1. mode хочет surface этого уровня visibility?
    if candidate.visibility in proactive_ask_levels(mode):
        return "ask"
    # 2. иначе есть безопасный путь автономно?
    if candidate.default_assumption:
        return "assume"
    # 3. gate sign-off — страховка независимо от visibility
    if candidate.blocking_scope == "objective":
        return "ask"
    # 4. мягкий skip — в инбоксе будет в фильтре «Отложено»
    return "defer"
```

Дальше (`register_candidates`, строки 199-243):

- **Дедуп B3** (layer 1+2): `find_clarification_by_source(...)` и cross-task
  `find_clarification_in_project_by_question(...)`. Дубль → `action="reuse_existing"`.
- `assume` + есть `default_assumption` → создаётся `ClarificationRequest(status="assumed")` **и** применяется
  `UpsertPositionPatch(type="assumption")` в Layer A с id `clarification.{request_id}`.
  Запрос помечается `auto_resolved=True`.
- `defer` → `status="deferred"`, тоже `auto_resolved=True`, в Layer A ничего не пишется.
- `ask` → `status="open"`, ждёт пользователя.

Audit event: `assumed_auto` / `deferred_auto` / `created`.

---

## 6. Источники кандидатов

| Эмиттер | Файл | source_type | decision_owner_role | visibility | blocking_scope | default_assumption |
|---|---|---|---|---|---|---|
| Validation: low_confidence | `validation_service.py:279-296` | `validation` | `business` (дефолт) | `principal` (через role-default) | `task` | нет |
| Quality gate signoff | `validation_service.py:556-580` | `quality_gate` | `_normalize_decision_owner_role(gate.approver_role)` → `client` / `security` | **явно** `"principal"` | `"objective"` | нет |
| Methodology rule | `methodology_rules.py:248-270` | `methodology_pack` | хардкод `"methodologist"` | дефолт `"technical"`, override через `emit.visibility` в YAML | дефолт `"none"`, override через `emit.blocking_scope` | `_safe_assumption_for_rule(...)` — обычно есть |
| `candidate_from_question` (общий) | `clarification_service.py:683-742` | любой | дефолт `"business"` | если не передана — `default_visibility_for_role(role)` | `"task"` | опционально |

**Domain pack contributions** в спеке упомянуты (`specs/12_clarification_escalation.md:43, 328-336`),
но в коде ни одного эмиттера с `source_type="domain_pack"` нет — место под будущую работу.

`_normalize_decision_owner_role` (`validation_service.py:28-47`) маппит свободный
`gate.approver_role` на канонический `DecisionOwnerRole`: известные роли — как есть,
алиасы `dpo`/`ciso` → `security`, `owner` → `client`, `stakeholder`/`bo`/`po` → `business`,
fallback: для human_approval gate — `client`, иначе `business`.

---

## 7. CE11: LLM-подготовка формулировки

Триггер `_needs_llm_draft` (`clarification_service.py:781-788`): нет description,
нет options, `answer_mode="free_text"` или у одного option `confidence is None`.

Что делает LLM в `_draft_system_prompt` (строки 927-954):

- генерирует самодостаточное `description` (3-10 предложений),
- генерирует осмысленные доменные `options` с `confidence` по каждому,
- выбирает `answer_mode` (`single` / `multiple`),
- **может переопределить `visibility` и `decision_owner_role`** — влияет на ask/assume/defer.

Контекст для LLM (`_clarification_context`, строки 873-925): `business_request`,
`goal_statement`, активные domain packs, первые 12 фактов/допущений/gaps,
до 5 affected tasks, до 3 связанных artifacts с excerpt 4000 символов.

Schema валидации ответа (`_draft_schema` + `_normalize_draft_payload`,
строки 987-1103) защищает от мусора: невалидная visibility → fallback,
неизвестная роль → fallback, «другое»-варианты дропаются (CE10).

Провайдер — тот же, что в основном workflow (`POV_EXECUTION_PROVIDER`),
сложность `"standard"`.

**CE1:** LLM не задаёт вопрос напрямую — только готовит карточку, координатор
принимает решение.

---

## 8. Как меняется режим — API + UI

**Хранилище:** `ProcessState.clarification_mode` (Layer B), default `"balanced"`.
Меняется только через `SetClarificationModePatch` + `apply_process_patch`.

**Команда** `WorkspaceCommandService.set_clarification_mode` →
`ClarificationService.set_mode(workspace, mode)`.

`set_mode` (`clarification_service.py:587-653`) делает **два шага**:

1. Применяет `SetClarificationModePatch`.
2. **Пере-оценивает все `open` запросы** против нового режима: persisted
   `ClarificationRequest` превращается обратно в candidate-shim через
   `_candidate_from_request` и прогоняется через `_decide_action`:
   - `assume` → `accept_assumption` + `mark_clarification_auto_resolved`;
   - `defer` → `defer_clarification(reason="Авто-отложено: смена режима участия")` + auto_resolved;
   - `ask` (например, objective без default_assumption) → остаётся `open`.

Возвращает `ReevaluationSummary(mode, auto_assumed, auto_deferred, kept_open)` —
UI показывает toast «Закрыто X, отложено Y, осталось N».

**REST API:** `POST /api/projects/{project_id}/commands/set-clarification-mode`
с body `{"mode": "autopilot"|"balanced"|"control"|"expert"}` (`interfaces/api.py:473-478`).

**CLI:** команды смены режима **нет** — только REST + UI.

**UI:** селектор в `WorkspaceHeader` (`ui/workspace/src/ui.tsx:409-499`).
Опции и описания — `CLARIFICATION_MODE_OPTIONS` (строки 390-407). Лейблы:
«Автопилот / Сбалансированный / Контроль / Экспертный».

---

## 9. Жёсткие инварианты (CE-номера)

Из `specs/12_clarification_escalation.md:341-357`:

| ID | Правило | Реализация |
|---|---|---|
| CE1 | LLM не задаёт вопрос напрямую | Исполнитель возвращает только candidates; LLM формирует карточку, не диалог |
| CE2 | Каждый вопрос имеет причину, влияние, scope блокировки | Обязательные поля `rationale`/`impact`/`blocking_scope` |
| CE3 | Каждый ответ/допущение в истории | `clarification_events` + Layer A `clarification.{request_id}` |
| CE4 | Обязательный вопрос нельзя молча заменить допущением | `blocking_scope="objective"` форсит `ask` |
| CE5 | Неблокирующий вопрос не останавливает проект | `blocking_scope="none"` — advisory follow-ups |
| CE6 | Эскалация — для исключительных случаев | `EscalationTicket` отделён от `ClarificationRequest` |
| CE7 | После ответа — перепланирование | `answer_clarification` запускает `_auto_retry_failed_tasks` |
| CE8 | Вопрос визуально важнее описания/опций | UI требование |
| CE9 | У каждого option есть confidence или явная пометка её отсутствия | `ClarificationOption.confidence: float \| None` |
| CE10 | «Другое» не option (свободный ответ всегда есть) | `_is_custom_answer_label` отбрасывает такие варианты |
| **CE11** | description/options/confidence/visibility/role формирует LLM | `_enrich_candidate` + `_draft_*` методы |
| **CE12** | Visibility — единственная ось ask/assume/defer; role информационна | `_decide_action` использует только visibility |
| **CE13** | Право оспорить положение универсально, не зависит от mode | Покрыто тестами `test_*_works_in_autopilot_mode` |
| CE14 | Эмиттер обязан задать visibility явно либо дать координатору вывести её из role | `candidate_from_question` + role-default |

---

## 10. Открытые расхождения и места для будущей работы

1. **`balanced` vs `control` неотличимы.** Спека объявляет, что `control` имеет
   «то же покрытие, но более строгие пороги уверенности» в architectural-зоне.
   В коде они идентичны, а confidence в `_decide_action` вообще не участвует.
   UX-обещание есть, поведенческой разницы нет → очевидный кандидат на доработку.

2. **Domain pack contributions не реализованы.** `source_type="domain_pack"`
   объявлен в типе и упомянут в спеке, но эмиттера в `src/` нет. Когда подключение
   доменного пака должно породить вопрос «применять ли домен X?», такого пути сейчас нет.

3. **CLI без команды смены режима.** Только REST + UI.

4. **`confidence_without_user` — мёртвый сигнал в решении.** Заполняется
   эмиттерами (например, `validation_service.py:292`), но `_decide_action`
   его игнорирует. Может быть основой будущей политики «control = ужесточить порог».

5. **`auto_resolved`-UX недо-рендерится.** Поле в модели есть, но в UI бэйджа
   «🤖 решено автоматически» / счётчика в инбоксе по факту не видно.

6. **`set_mode` не пере-открывает `assumed` / `deferred` при переключении
   autopilot → expert.** Сознательный выбор (CE3 — ответ остаётся в истории),
   но возможный UX-pain.

7. **`_normalize_decision_owner_role`** имеет жёсткий словарь алиасов.
   Расширение под новые роли gate (`legal`, `cfo`) требует правки кода —
   лучше вынести в реестр.

---

## 11. Ключевые файлы для будущих правок

- **Алгоритм решения и LLM-подготовка:** `src/pov_generator/application/clarification_service.py`
  (`_decide_action`, `set_mode`, `_enrich_candidate`, `_draft_system_prompt`, `_ROLE_DEFAULT_VISIBILITY`).
- **Таблица режим → visibility:** `src/pov_generator/domain/process_state.py:116-126`
  (`_PROACTIVE_ASK_LEVELS`, `proactive_ask_levels`, `should_ask_user_for`).
- **Типы:** `src/pov_generator/domain/clarifications.py` — `ClarificationMode`,
  `DecisionOwnerRole`, `ClarificationCandidate`, `ClarificationRequest`.
- **Эмиттеры:** `application/validation_service.py:266-367, 538-582`,
  `application/methodology_rules.py:170-279`.
- **Команда:** `application/workspace_command_service.py:226-245`.
- **API:** `interfaces/api.py:473-478`.
- **UI:** `ui/workspace/src/ui.tsx:390-499` (селектор + копирайтинг),
  `ui/workspace/src/App.tsx` (wiring + лейблы).
- **Тесты:** `tests/test_visibility_engagement.py` (матрица решений),
  `tests/test_autopilot_reevaluation.py` (поведение `set_mode`),
  `tests/test_decision_owner_role.py` (роль ↔ visibility).
