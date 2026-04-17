# Спецификация шаблонов задач: Stage Gate 1 «Преобразование бизнес-запроса в ТЗ»

**Область применения:** первый макро-гейт платформы E2E-генерации PoV.
**Поддомены первой очереди (из `PoV.md`):** RAG-системы, простые ML-модели.
**Принципы (сверка с `PoV.md`):** фокус на потребностях, прозрачность, воспроизводимость, самоконтроль.
**Архитектурные контракты (сверка с `ТЗ_Архитектура.md`):** типология шаблонов (Composite / Executable / Dynamic), контракты зависимостей (Hard / Soft / Semantic), конечный автомат статусов, Interruption Gateway.

Внутри гейта используются только два целевых поддомена. Там, где задачи зависят от выбранного поддомена (например, формирование разделов ТЗ), это отражено через Dynamic-шаблоны или через условия `spawn_conditions`. Универсальные задачи оставлены в базовом виде, но их промпты-подсказки заточены под RAG/ML.

---

## Часть 1. Дерево задач

```
BIZ_TO_TZ_000 [Composite] — Преобразование бизнес-запроса в ТЗ (корень гейта)
│
├── BIZ_TO_TZ_100 [Composite] — Приём и нормализация запроса
│   ├── BIZ_TO_TZ_101 [Executable] — Парсинг исходного запроса в структурированный формат
│   ├── BIZ_TO_TZ_102 [Executable] — Проверка минимальной полноты сырого запроса
│   └── BIZ_TO_TZ_103 [Executable] — Классификация поддомена (RAG / ML / вне поддержки)
│
├── BIZ_TO_TZ_200 [Composite] — Ранняя оценка выполнимости (early feasibility)
│   ├── BIZ_TO_TZ_201 [Executable] — Проверка попадания в поддерживаемые поддомены
│   ├── BIZ_TO_TZ_202 [Executable] — Поиск паттернов нерешаемости (этика, out-of-scope, физическая невозможность)
│   └── BIZ_TO_TZ_203 [Executable] — Вердикт выполнимости с обоснованием
│
├── BIZ_TO_TZ_300 [Composite] — Понимание потребности (Why-анализ)
│   ├── BIZ_TO_TZ_301 [Executable] — Извлечение декларируемой цели из запроса
│   ├── BIZ_TO_TZ_302 [Executable] — Гипотезы о первопричине («зачем это клиенту?»)
│   ├── BIZ_TO_TZ_303 [Executable] — Гипотезы о текущем baseline-процессе
│   ├── BIZ_TO_TZ_304 [Executable] — Выявление стейкхолдеров и их интересов
│   └── BIZ_TO_TZ_305 [Executable] — Консолидация модели потребности
│
├── BIZ_TO_TZ_400 [Composite] — Извлечение декларативного знания из запроса
│   ├── BIZ_TO_TZ_401 [Executable] — Извлечение упоминаний данных
│   ├── BIZ_TO_TZ_402 [Executable] — Извлечение упоминаний метрик и целевых значений
│   ├── BIZ_TO_TZ_403 [Executable] — Извлечение упоминаний ограничений (время, ресурсы, стек)
│   ├── BIZ_TO_TZ_404 [Executable] — Извлечение упоминаний критериев приёмки
│   └── BIZ_TO_TZ_405 [Executable] — Извлечение упоминаний интеграций и внешних систем
│
├── BIZ_TO_TZ_500 [Composite] — Формирование gap-листа (что неизвестно)
│   ├── BIZ_TO_TZ_501 [Executable] — Сверка извлечённого с чеклистом поддомена (RAG-чеклист / ML-чеклист)
│   ├── BIZ_TO_TZ_502 [Executable] — Приоритезация пробелов (blocking / important / nice-to-have)
│   └── BIZ_TO_TZ_503 [Executable] — Формирование структурированного gap-листа
│
├── BIZ_TO_TZ_600 [Dynamic] — Сбор недостающей информации у заказчика
│   ├── BIZ_TO_TZ_601 [Executable] — Формирование пакетного опросника (если пробелов ≥ N)
│   ├── BIZ_TO_TZ_602 [Executable] — Формирование точечного вопроса (если пробел единичный)
│   ├── BIZ_TO_TZ_603 [Executable/Human] — Отправка запроса заказчику через Interruption Gateway
│   ├── BIZ_TO_TZ_604 [Executable] — Парсинг ответа заказчика в структурированный формат
│   └── BIZ_TO_TZ_605 [Executable] — Валидация ответа (отвечает ли на заданный вопрос)
│
├── BIZ_TO_TZ_700 [Composite] — Работа с данными
│   ├── BIZ_TO_TZ_701 [Executable] — Инвентаризация источников данных
│   ├── BIZ_TO_TZ_702 [Dynamic] — Уточнение характеристик источников
│   │   ├── BIZ_TO_TZ_702_A [Executable] — Формирование вопросов о формате и объёме
│   │   ├── BIZ_TO_TZ_702_B [Executable] — Формирование вопросов о качестве и разметке
│   │   └── BIZ_TO_TZ_702_C [Executable] — Формирование вопросов о легальности и доступе
│   ├── BIZ_TO_TZ_703 [Executable] — Оценка достаточности данных для выбранного поддомена
│   └── BIZ_TO_TZ_704 [Executable] — Решение о необходимости синтетических данных
│
├── BIZ_TO_TZ_800 [Composite] — Формализация требований
│   ├── BIZ_TO_TZ_801 [Executable] — Формулирование функциональных требований
│   ├── BIZ_TO_TZ_802 [Executable] — Формулирование нефункциональных требований
│   ├── BIZ_TO_TZ_803 [Executable] — Фиксация ограничений
│   ├── BIZ_TO_TZ_804 [Executable] — Фиксация критериев приёмки с измеримыми метриками
│   └── BIZ_TO_TZ_805 [Executable] — Проверка согласованности требований между собой
│
├── BIZ_TO_TZ_900 [Dynamic] — Архитектурный анализ (заточен под RAG / простой ML)
│   ├── BIZ_TO_TZ_901 [Executable] — Определение класса задачи внутри поддомена
│   ├── BIZ_TO_TZ_902 [Executable] — Выбор архитектурного шаблона для RAG (если поддомен = RAG)
│   ├── BIZ_TO_TZ_903 [Executable] — Выбор архитектурного шаблона для ML (если поддомен = ML)
│   ├── BIZ_TO_TZ_904 [Executable] — Обоснование выбранного подхода
│   └── BIZ_TO_TZ_905 [Executable] — Определение baseline-решения
│
├── BIZ_TO_TZ_A00 [Composite] — Сборка документа ТЗ
│   ├── BIZ_TO_TZ_A01 [Executable] — Раздел «Контекст и потребность»
│   ├── BIZ_TO_TZ_A02 [Executable] — Раздел «Данные»
│   ├── BIZ_TO_TZ_A03 [Executable] — Раздел «Функциональные и нефункциональные требования»
│   ├── BIZ_TO_TZ_A04 [Executable] — Раздел «Архитектурный подход»
│   ├── BIZ_TO_TZ_A05 [Executable] — Раздел «Критерии приёмки»
│   ├── BIZ_TO_TZ_A06 [Executable] — Раздел «Ограничения и допущения»
│   └── BIZ_TO_TZ_A07 [Executable] — Сборка финального документа (оглавление, сквозная нумерация)
│
└── BIZ_TO_TZ_B00 [Composite] — Валидация и согласование ТЗ
    ├── BIZ_TO_TZ_B01 [Executable] — Проверка полноты по чеклисту ТЗ
    ├── BIZ_TO_TZ_B02 [Executable] — Проверка внутренней непротиворечивости
    ├── BIZ_TO_TZ_B03 [Executable] — Проверка трассируемости требований к исходному запросу
    ├── BIZ_TO_TZ_B04 [Executable/Human] — Отправка ТЗ заказчику на согласование
    ├── BIZ_TO_TZ_B05 [Executable] — Парсинг и классификация комментариев заказчика
    └── BIZ_TO_TZ_B06 [Executable] — Финальная фиксация согласованного ТЗ
```

**Замечания к дереву:**

- **Итеративность через Obsolete.** Комментарии заказчика в `B05` могут привести к перепланированию — ранее завершённые задачи (например, `800` или `900`) помечаются `Obsolete` и перепорождаются. Механизм — штатный Bubble Up из `ТЗ_Архитектура.md`, 3.3.
- **Параллельные ветки.** `300` (понимание потребности) и `400` (извлечение декларативного знания) независимы — могут идти параллельно, оба потребляют только нормализованный запрос из `100`.
- **Ветка `600` — точка нативного вмешательства человека.** Это Dynamic-задача: решение «опросник или точечный вопрос» принимается на лету по `gap_list.priority_counts`.
- **Ветка `700` зависит от `400.401`.** Инвентаризация источников использует уже извлечённые упоминания данных; если их нет — порождает `600` для уточнения.

---

## Часть 2. Шаблоны задач

Далее идут полные YAML-шаблоны. Для краткости поля `input_requirements.description` и `outputs.description` лаконичны, но однозначны.

### 2.0. Корень гейта

```yaml
task_id: BIZ_TO_TZ_000
name: "Преобразование бизнес-запроса в ТЗ"
type: Composite
description: "Корневая задача Stage Gate 1. Статически маршрутизирует последовательность блоков от приёма запроса до согласованного ТЗ."
parent_task: null
spawn_conditions: "Создаётся Stage-Gate Manager при старте проекта после получения сырого бизнес-запроса."
input_requirements:
  - artifact: "raw_business_request"
    contract: Hard
    description: "Исходный запрос заказчика в свободной форме (текст, возможно с приложениями). Загружается Системой общения с пользователем."
outputs:
  - artifact: "approved_technical_specification"
    format: "Структурированный Markdown-документ + JSON-метаданные"
    description: "Согласованное ТЗ с трассируемостью к исходному запросу. Сигнализирует Stage-Gate Manager о готовности к переходу на следующий гейт."
possible_children:
  - task_id: BIZ_TO_TZ_100
    condition: "Всегда (первый шаг)"
  - task_id: BIZ_TO_TZ_200
    condition: "После успешного завершения BIZ_TO_TZ_100"
  - task_id: BIZ_TO_TZ_300
    condition: "После успешного завершения BIZ_TO_TZ_200 с вердиктом feasible=true"
  - task_id: BIZ_TO_TZ_400
    condition: "После успешного завершения BIZ_TO_TZ_200 с вердиктом feasible=true (параллельно с 300)"
  - task_id: BIZ_TO_TZ_500
    condition: "После завершения BIZ_TO_TZ_300 и BIZ_TO_TZ_400"
  - task_id: BIZ_TO_TZ_600
    condition: "Если gap_list не пуст"
  - task_id: BIZ_TO_TZ_700
    condition: "После сбора базовой информации (600 завершён хотя бы один раз или gap_list не содержит data-пробелов)"
  - task_id: BIZ_TO_TZ_800
    condition: "После 600 и 700"
  - task_id: BIZ_TO_TZ_900
    condition: "После 800"
  - task_id: BIZ_TO_TZ_A00
    condition: "После 900"
  - task_id: BIZ_TO_TZ_B00
    condition: "После A00"
```

---

### 2.1. Блок 100 — Приём и нормализация запроса

```yaml
task_id: BIZ_TO_TZ_100
name: "Приём и нормализация запроса"
type: Composite
description: "Контейнер для трёх задач первичной обработки: парсинг → проверка полноты → классификация поддомена."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "Порождается корневой задачей в первую очередь."
input_requirements:
  - artifact: "raw_business_request"
    contract: Hard
    description: "Сырой текст запроса от заказчика."
outputs:
  - artifact: "normalized_request"
    format: "JSON"
    description: "Структурированное представление запроса + классификация поддомена."
possible_children:
  - task_id: BIZ_TO_TZ_101
    condition: "Всегда (первая подзадача)"
  - task_id: BIZ_TO_TZ_102
    condition: "После успешного завершения 101"
  - task_id: BIZ_TO_TZ_103
    condition: "После успешного завершения 102"
```

```yaml
task_id: BIZ_TO_TZ_101
name: "Парсинг сырого запроса в структурированный формат"
type: Executable
description: "Преобразует свободный текст заказчика в JSON со стандартными полями: заголовок, тело, приложения, метаданные. Не интерпретирует смысл — только структурирует то, что явно присутствует."
parent_task: BIZ_TO_TZ_100
spawn_conditions: "Первой в блоке 100."
input_requirements:
  - artifact: "raw_business_request"
    contract: Hard
    description: "Сырой текст запроса."
outputs:
  - artifact: "parsed_request"
    format: "JSON {title, body, attachments[], raw_metadata}"
    description: "Структурированный запрос без семантической интерпретации."
execution_type: LLM
prompt_template_hint: "Извлеки из текста: (1) краткий заголовок, (2) основное тело запроса, (3) список упомянутых вложений/ссылок, (4) явные метаданные (дата, автор, контакты). Не придумывай отсутствующее — помечай null. Верни строгий JSON."
constraints:
  - "Запрещено добавлять поля, не упомянутые явно в исходном тексте"
  - "Запрещены интерпретации смысла — только структурное разделение"
  - "Лимит одного LLM-вызова без самокоррекции"
escalation_conditions:
  - "Исходный текст не парсится как осмысленный запрос (например, бинарный мусор, пустая строка)"
  - "Исходный текст короче 20 символов — недостаточно для любого парсинга"
```

```yaml
task_id: BIZ_TO_TZ_102
name: "Проверка минимальной полноты сырого запроса"
type: Executable
description: "Бинарное решение: содержит ли запрос хотя бы (а) упоминание желаемого результата ИЛИ (б) описание проблемы. Если нет — возвращаем на доработку заказчику до старта любой обработки."
parent_task: BIZ_TO_TZ_100
spawn_conditions: "После завершения 101."
input_requirements:
  - artifact: "parsed_request"
    contract: Hard
    description: "Результат парсинга из 101."
outputs:
  - artifact: "request_completeness_verdict"
    format: "JSON {is_complete: bool, missing: [enum], reasoning: str}"
    description: "Вердикт о минимальной полноте + список критичных пробелов."
execution_type: LLM
prompt_template_hint: "Проверь чеклист: (1) есть ли явное или подразумеваемое описание желаемого результата? (2) есть ли описание проблемы или контекста? (3) понятно ли, кто заказчик? Если хотя бы (1) или (2) отсутствует — is_complete=false с перечислением отсутствующих пунктов."
constraints:
  - "Решение строго бинарное — is_complete=true только при выполнении минимального порога"
  - "Чеклист жёсткий, не допускает творческой интерпретации"
escalation_conditions:
  - "is_complete=false — эскалация к заказчику через Interruption Gateway с просьбой дополнить запрос (не эскалация к разработчику — это нормальный ход событий)"
```

```yaml
task_id: BIZ_TO_TZ_103
name: "Классификация поддомена"
type: Executable
description: "Относит запрос к одному из поддерживаемых поддоменов: RAG-система, простая ML-модель, неподдерживаемый. Влияет на выбор чеклистов в последующих задачах."
parent_task: BIZ_TO_TZ_100
spawn_conditions: "После завершения 102 с is_complete=true."
input_requirements:
  - artifact: "parsed_request"
    contract: Hard
    description: "Структурированный запрос."
  - artifact: "supported_subdomains_registry"
    contract: Hard
    description: "Реестр поддоменов с признаками классификации (из Template Registry)."
outputs:
  - artifact: "subdomain_classification"
    format: "JSON {subdomain: enum[RAG, ML, UNSUPPORTED], confidence: float, reasoning: str, alternative_hypotheses: []}"
    description: "Поддомен с обоснованием и альтернативами на случай низкой уверенности."
execution_type: LLM
prompt_template_hint: "Проанализируй запрос по признакам: (RAG) нужен поиск/ответы по корпусу документов, нужна работа с неструктурированным текстом, упоминается knowledge base; (ML) нужно предсказание/классификация/регрессия на структурированных данных, есть целевая переменная. Если ни один признак явно не выражен — UNSUPPORTED. Верни confidence ∈ [0,1]."
constraints:
  - "Поддерживаемые значения subdomain — только RAG, ML, UNSUPPORTED"
  - "При confidence < 0.7 обязательно заполнить alternative_hypotheses"
  - "Не изобретать новые поддомены"
escalation_conditions:
  - "subdomain = UNSUPPORTED — переход в Failed с обоснованием, далее эскалация через Interruption Gateway для принятия решения человеком (переопределить/отклонить проект)"
  - "confidence < 0.4 даже после самокоррекции — эскалация для ручной классификации"
```

---

### 2.2. Блок 200 — Ранняя оценка выполнимости

```yaml
task_id: BIZ_TO_TZ_200
name: "Ранняя оценка выполнимости"
type: Composite
description: "Контейнер для early feasibility check. Цель — сдвинуть эскалацию невозможных задач на максимально ранний срок (см. 'Предопределение невозможных задач' в PoV.md)."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После завершения 100."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Результат блока 100."
outputs:
  - artifact: "feasibility_verdict"
    format: "JSON"
    description: "Вердикт: проект выполним / невыполним / выполним с оговорками."
possible_children:
  - task_id: BIZ_TO_TZ_201
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_202
    condition: "Всегда (параллельно с 201)"
  - task_id: BIZ_TO_TZ_203
    condition: "После завершения 201 и 202"
```

```yaml
task_id: BIZ_TO_TZ_201
name: "Проверка попадания в поддерживаемые поддомены"
type: Executable
description: "Формальная проверка: классификация из 103 входит ли в список поддерживаемых MVP-поддоменов? Это детерминированная бизнес-логика, не LLM-вызов."
parent_task: BIZ_TO_TZ_200
spawn_conditions: "Первой в блоке 200."
input_requirements:
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Классификация из 103."
  - artifact: "supported_subdomains_registry"
    contract: Hard
    description: "Реестр поддоменов MVP."
outputs:
  - artifact: "subdomain_support_check"
    format: "JSON {supported: bool, subdomain: str}"
    description: "Булевый флаг поддержки поддомена."
execution_type: Tool
prompt_template_hint: "Детерминированный скрипт: subdomain_classification.subdomain ∈ supported_subdomains_registry.active → supported=true."
constraints:
  - "Без LLM-вызова — чистая сверка"
escalation_conditions:
  - "Не эскалирует сама; результат используется в 203"
```

```yaml
task_id: BIZ_TO_TZ_202
name: "Поиск паттернов нерешаемости"
type: Executable
description: "LLM-анализ запроса на наличие явных признаков невозможности: этические/правовые проблемы, out-of-scope требования (реалтайм-система при нашем фокусе на PoV, работа с проприетарными данными без доступа), физически несовместимые ограничения."
parent_task: BIZ_TO_TZ_200
spawn_conditions: "Параллельно с 201."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос из блока 100."
  - artifact: "unfeasibility_patterns_catalog"
    contract: Hard
    description: "Каталог известных паттернов нерешаемости (из Template Registry). Для MVP содержит: этические нарушения, требования реалтайма с жёстким SLA, требования к датасетам которые невозможно получить легально, self-referencing задачи."
outputs:
  - artifact: "unfeasibility_flags"
    format: "JSON {detected_patterns: [{pattern_id, evidence, severity}], overall_blocker: bool}"
    description: "Список обнаруженных паттернов с цитатами-доказательствами и оценкой критичности."
execution_type: LLM
prompt_template_hint: "Для каждого паттерна из каталога — проверь, есть ли в запросе прямые или косвенные признаки. Обязательно приводи цитату из запроса как evidence. Severity: blocker / warning / info. overall_blocker = true только если есть хотя бы один blocker."
constraints:
  - "Запрещено выдумывать паттерны вне каталога"
  - "Каждый detected_pattern обязан иметь evidence — прямую или парафразированную цитату"
  - "Лимит 2 попытки самокоррекции"
escalation_conditions:
  - "LLM не может однозначно определить наличие паттерна после самокоррекции — эскалация"
  - "Обнаружен паттерн с severity=blocker, связанный с этикой или легальностью — немедленная эскалация к человеку без продолжения пайплайна"
```

```yaml
task_id: BIZ_TO_TZ_203
name: "Вердикт выполнимости"
type: Executable
description: "Синтез результатов 201 и 202 в итоговый вердикт. Определяет, продолжать ли пайплайн."
parent_task: BIZ_TO_TZ_200
spawn_conditions: "После завершения 201 и 202."
input_requirements:
  - artifact: "subdomain_support_check"
    contract: Hard
    description: "Результат 201."
  - artifact: "unfeasibility_flags"
    contract: Hard
    description: "Результат 202."
outputs:
  - artifact: "feasibility_verdict"
    format: "JSON {feasible: bool, with_caveats: bool, caveats: [str], blockers: [str], recommendation: enum[PROCEED, PROCEED_WITH_CONFIRMATION, ABORT]}"
    description: "Итоговый вердикт выполнимости с рекомендацией."
execution_type: LLM
prompt_template_hint: "Если supported=false ИЛИ overall_blocker=true → feasible=false, recommendation=ABORT. Если warning-паттерны есть, но blocker'ов нет → feasible=true, with_caveats=true, recommendation=PROCEED_WITH_CONFIRMATION. Иначе PROCEED."
constraints:
  - "Строгая логика: любой blocker = ABORT"
  - "Рекомендация PROCEED_WITH_CONFIRMATION означает, что заказчика надо явно предупредить о рисках"
escalation_conditions:
  - "recommendation = ABORT — задача завершается Failed, пайплайн останавливается, эскалация к человеку с полным обоснованием (blockers + evidence)"
  - "recommendation = PROCEED_WITH_CONFIRMATION — мягкая эскалация: заказчику отправляется уведомление с caveats для подтверждения"
```

---

### 2.3. Блок 300 — Понимание потребности (Why-анализ)

```yaml
task_id: BIZ_TO_TZ_300
name: "Понимание потребности"
type: Composite
description: "Контейнер для Why-анализа. Цель — не просто принять запрос как данность, а понять первопричину, текущий процесс, стейкхолдеров. Это основа для выявления скрытых требований."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После 200 с feasible=true."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
outputs:
  - artifact: "need_model"
    format: "JSON"
    description: "Консолидированная модель потребности: цель, первопричина, baseline, стейкхолдеры."
possible_children:
  - task_id: BIZ_TO_TZ_301
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_302
    condition: "После 301"
  - task_id: BIZ_TO_TZ_303
    condition: "После 301 (параллельно с 302)"
  - task_id: BIZ_TO_TZ_304
    condition: "После 301 (параллельно с 302 и 303)"
  - task_id: BIZ_TO_TZ_305
    condition: "После 302, 303, 304"
```

```yaml
task_id: BIZ_TO_TZ_301
name: "Извлечение декларируемой цели"
type: Executable
description: "Формулирует в одном предложении то, что заказчик явно просит сделать. Без интерпретаций и домыслов. Это основа для последующего Why-анализа."
parent_task: BIZ_TO_TZ_300
spawn_conditions: "Первой в блоке 300."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
outputs:
  - artifact: "declared_goal"
    format: "JSON {goal_statement: str, direct_quotes: [str]}"
    description: "Цель в одном предложении + прямые цитаты из запроса, подтверждающие формулировку."
execution_type: LLM
prompt_template_hint: "Сформулируй одним предложением то, что заказчик ЯВНО просит сделать. Не добавляй 'чтобы X' или 'для Y', если этого нет в запросе. Обязательно приведи 1-3 прямые цитаты."
constraints:
  - "goal_statement — одно предложение, без сложноподчинённой структуры"
  - "Запрещено добавлять цели, не подтверждённые direct_quotes"
  - "Без интерпретации why — только what"
escalation_conditions:
  - "В запросе не удаётся найти декларируемую цель даже после самокоррекции (обычно значит, что 102 пропустил неполный запрос) — эскалация с возвратом в 102"
```

```yaml
task_id: BIZ_TO_TZ_302
name: "Гипотезы о первопричине"
type: Executable
description: "Генерирует 2–4 гипотезы о том, ЗАЧЕМ заказчику нужно заявленное. Каждая гипотеза — кандидат на первопричину, подлежащий проверке в блоке 600."
parent_task: BIZ_TO_TZ_300
spawn_conditions: "После 301."
input_requirements:
  - artifact: "declared_goal"
    contract: Hard
    description: "Цель из 301."
  - artifact: "normalized_request"
    contract: Hard
    description: "Полный контекст запроса."
  - artifact: "subdomain_classification"
    contract: Soft
    description: "Помогает сузить пространство гипотез под типовые паттерны RAG/ML."
outputs:
  - artifact: "root_cause_hypotheses"
    format: "JSON [{hypothesis: str, supporting_signals: [str], plausibility: float, verification_question: str}]"
    description: "Список гипотез с сигналами из запроса, оценкой правдоподобия и предлагаемым вопросом для верификации у заказчика."
execution_type: LLM
prompt_template_hint: "Сгенерируй 2-4 гипотезы вида 'заказчику это нужно, ЧТОБЫ ...'. Для каждой: (1) какие сигналы в запросе её поддерживают, (2) насколько она правдоподобна [0-1], (3) какой вопрос задать заказчику, чтобы подтвердить или опровергнуть. Не более 4 гипотез — иначе теряется фокус."
constraints:
  - "Минимум 2, максимум 4 гипотезы"
  - "Обязательно поле verification_question для каждой гипотезы — оно используется блоком 600"
  - "Гипотезы должны быть взаимоисключающими (или явно пересекающимися с пометкой)"
escalation_conditions:
  - "Все сгенерированные гипотезы имеют plausibility < 0.3 — признак того, что запрос слишком абстрактен, нужен ранний контакт с заказчиком"
```

```yaml
task_id: BIZ_TO_TZ_303
name: "Гипотезы о текущем baseline-процессе"
type: Executable
description: "Как задача решается СЕЙЧАС без нашего решения? Генерирует гипотезы о существующем процессе/системе/ручной работе. Критично для RAG (замена чего?) и для ML (что было до модели?)."
parent_task: BIZ_TO_TZ_300
spawn_conditions: "После 301, параллельно с 302 и 304."
input_requirements:
  - artifact: "declared_goal"
    contract: Hard
    description: "Цель из 301."
  - artifact: "normalized_request"
    contract: Hard
    description: "Полный запрос — в нём часто есть упоминания текущей ситуации."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Для RAG типовой baseline — ручной поиск по документам / Ctrl+F / поиск в Confluence. Для ML — эвристики, ручная оценка, существующие системы."
outputs:
  - artifact: "baseline_hypotheses"
    format: "JSON [{baseline: str, evidence_from_request: str, verification_question: str}]"
    description: "Гипотезы о текущем процессе + как их проверить."
execution_type: LLM
prompt_template_hint: "Для RAG-поддомена типовые baseline: ручной поиск в документах, обращение к экспертам, Ctrl+F. Для ML-поддомена: эвристические правила, ручная классификация, экспертная оценка, отсутствие решения. Предложи 1-3 наиболее вероятных baseline'а с привязкой к фразам из запроса и вопросом для верификации."
constraints:
  - "1-3 гипотезы"
  - "Каждая гипотеза должна упоминать, кто (роль) выполняет baseline-действие"
  - "Поле verification_question обязательно"
escalation_conditions:
  - "Не эскалирует (низкокритичная задача, отсутствие данных восполняется в 600)"
```

```yaml
task_id: BIZ_TO_TZ_304
name: "Выявление стейкхолдеров и их интересов"
type: Executable
description: "Определяет роли, затронутые решением: прямые пользователи системы, заказчик, владелец данных, потребители результата. Для каждой роли — предполагаемый интерес."
parent_task: BIZ_TO_TZ_300
spawn_conditions: "После 301, параллельно с 302 и 303."
input_requirements:
  - artifact: "declared_goal"
    contract: Hard
    description: "Цель из 301."
  - artifact: "normalized_request"
    contract: Hard
    description: "Полный запрос."
outputs:
  - artifact: "stakeholders_map"
    format: "JSON [{role: str, interest: str, explicit_in_request: bool}]"
    description: "Карта стейкхолдеров с их интересами и пометкой, упомянуты ли они явно."
execution_type: LLM
prompt_template_hint: "Выдели роли: (1) явно упомянутые в запросе, (2) неизбежно присутствующие в задаче (например, для RAG-системы — всегда есть 'пользователь, задающий вопрос' и 'владелец корпуса документов'). Для каждой — интерес в задаче. Пометь, упомянута ли роль явно."
constraints:
  - "Минимум 2 роли для любой DS-задачи (потребитель результата + источник данных/запроса)"
  - "Для RAG обязательны: asker, corpus_owner; для ML: data_owner, consumer_of_predictions"
  - "Если explicit_in_request=false — роль порождает skрытое требование, которое надо выяснять в 600"
escalation_conditions:
  - "Не эскалирует самостоятельно; неявные стейкхолдеры попадают в gap-лист через 500"
```

```yaml
task_id: BIZ_TO_TZ_305
name: "Консолидация модели потребности"
type: Executable
description: "Собирает результаты 301-304 в единую модель потребности. Выявляет противоречия между гипотезами и ранжирует их по приоритету верификации."
parent_task: BIZ_TO_TZ_300
spawn_conditions: "После 302, 303, 304."
input_requirements:
  - artifact: "declared_goal"
    contract: Hard
    description: "Цель."
  - artifact: "root_cause_hypotheses"
    contract: Hard
    description: "Гипотезы о первопричине."
  - artifact: "baseline_hypotheses"
    contract: Hard
    description: "Гипотезы о baseline."
  - artifact: "stakeholders_map"
    contract: Hard
    description: "Стейкхолдеры."
outputs:
  - artifact: "need_model"
    format: "JSON {declared_goal, top_root_cause_hypothesis, top_baseline_hypothesis, stakeholders, open_questions_for_client: [str], internal_contradictions: [str]}"
    description: "Сводная модель потребности + открытые вопросы + обнаруженные противоречия."
execution_type: LLM
prompt_template_hint: "Объедини входы. Выбери top-гипотезу по plausibility. Найди противоречия (например, cтейкхолдер подразумевает массовое использование, а цель сформулирована для точечной задачи). Сформируй список открытых вопросов, которые должен подтвердить заказчик."
constraints:
  - "open_questions_for_client — не более 7 пунктов (ограничение когнитивной нагрузки на заказчика)"
  - "Если есть internal_contradictions — они ОБЯЗАТЕЛЬНО попадают в open_questions_for_client для явного разрешения"
escalation_conditions:
  - "Обнаружены противоречия, которые LLM не может сформулировать как вопрос (нечёткость) — эскалация на ревью человеком"
```

---

### 2.4. Блок 400 — Извлечение декларативного знания

Принцип: параллельная микро-декомпозиция — каждая листовая задача вычленяет один тип упоминаний. Это реализует уровень (a) из уточнения пользователя — мелкая декомпозиция там, где смешение решений снижает качество.

```yaml
task_id: BIZ_TO_TZ_400
name: "Извлечение декларативного знания из запроса"
type: Composite
description: "Контейнер для параллельного извлечения пяти типов упоминаний: данные, метрики, ограничения, критерии приёмки, интеграции. Каждая подзадача работает по узкой инструкции."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После 200 с feasible=true, параллельно с 300."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
outputs:
  - artifact: "extracted_declarations"
    format: "JSON {data_mentions, metric_mentions, constraint_mentions, acceptance_mentions, integration_mentions}"
    description: "Сводный объект всех извлечённых упоминаний."
possible_children:
  - task_id: BIZ_TO_TZ_401
    condition: "Всегда (все 5 параллельно)"
  - task_id: BIZ_TO_TZ_402
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_403
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_404
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_405
    condition: "Всегда"
```

```yaml
task_id: BIZ_TO_TZ_401
name: "Извлечение упоминаний данных"
type: Executable
description: "Находит в запросе все явные и косвенные упоминания данных: источники, форматы, объёмы, типы, временные рамки. НЕ классифицирует и НЕ оценивает достаточность — только выписывает."
parent_task: BIZ_TO_TZ_400
spawn_conditions: "Параллельно с 402-405."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
outputs:
  - artifact: "data_mentions"
    format: "JSON [{mention: str, quote: str, extracted_attributes: {type, format, volume, timeframe, source}}]"
    description: "Список упоминаний данных с прямыми цитатами и извлечёнными атрибутами (любые могут быть null)."
execution_type: LLM
prompt_template_hint: "Выпиши КАЖДОЕ упоминание, связанное с данными. Для каждого — прямая цитата и попытка извлечь атрибуты. Если какой-то атрибут не упомянут — null. Не додумывай. Примеры упоминаний: 'база клиентов', 'логи за 2024 год', 'документация в Confluence', '10 тысяч записей'."
constraints:
  - "Каждое упоминание ОБЯЗАНО иметь прямую цитату (quote)"
  - "Атрибуты заполняются только если явно упомянуты — не выводить по аналогии"
  - "Если упоминаний нет вообще — возвращать пустой массив, не эскалировать"
escalation_conditions:
  - "Не эскалирует (пустой список — валидный результат, обрабатывается в 500)"
```

```yaml
task_id: BIZ_TO_TZ_402
name: "Извлечение упоминаний метрик и целевых значений"
type: Executable
description: "Находит упоминания измеримых величин: точность, recall, время ответа, процент покрытия, бизнес-метрики. Фиксирует целевые значения, если названы."
parent_task: BIZ_TO_TZ_400
spawn_conditions: "Параллельно с 401, 403-405."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
  - artifact: "subdomain_classification"
    contract: Soft
    description: "Помогает идентифицировать domain-specific метрики (для RAG: retrieval precision, answer faithfulness; для ML: accuracy, F1, MAE)."
outputs:
  - artifact: "metric_mentions"
    format: "JSON [{metric_name: str, target_value: str|null, quote: str, category: enum[technical, business, ux]}]"
    description: "Упоминания метрик с целевыми значениями и классификацией по типу."
execution_type: LLM
prompt_template_hint: "Выпиши все упоминания измеримых величин. Для RAG следи за: 'точность ответов', 'релевантность', 'hallucinations', 'latency'. Для ML: 'точность предсказаний', 'precision/recall', 'время обучения'. Бизнес-метрики: 'сокращение времени', 'снижение нагрузки на поддержку'. UX: 'скорость ответа пользователю'."
constraints:
  - "Если метрика упомянута без целевого значения — target_value=null"
  - "Не придумывать метрики, которых нет в запросе"
  - "Категория обязательна (technical / business / ux)"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_403
name: "Извлечение упоминаний ограничений"
type: Executable
description: "Временные рамки, бюджетные ограничения, обязательный/запрещённый стек, требования по развёртыванию (onprem vs cloud), регуляторные ограничения."
parent_task: BIZ_TO_TZ_400
spawn_conditions: "Параллельно с остальными 40X."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
outputs:
  - artifact: "constraint_mentions"
    format: "JSON [{constraint_type: enum[time, budget, stack, deployment, regulatory, other], value: str, quote: str, is_hard: bool|null}]"
    description: "Упоминания ограничений с типом и прямой цитатой."
execution_type: LLM
prompt_template_hint: "Типы: time ('нужно к концу квартала'), budget ('в рамках существующих ресурсов'), stack ('обязательно Python', 'нельзя OpenAI API'), deployment ('работа в закрытом контуре'), regulatory ('152-ФЗ', 'GDPR'). is_hard=true для жёстких ограничений ('обязательно'), false для пожеланий ('желательно'), null если неясно."
constraints:
  - "constraint_type строго из перечня"
  - "Цитата обязательна"
escalation_conditions:
  - "Обнаружены ограничения с constraint_type=regulatory и is_hard=true — понижение приоритета до эскалации: результат записывается в unfeasibility_flags retrospectively и может вызвать перепроверку 200"
```

```yaml
task_id: BIZ_TO_TZ_404
name: "Извлечение упоминаний критериев приёмки"
type: Executable
description: "Формулировки вида 'система будет считаться работающей, если...', 'мы примем результат при...', дедлайны демонстраций, условия подписания акта."
parent_task: BIZ_TO_TZ_400
spawn_conditions: "Параллельно с остальными 40X."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
outputs:
  - artifact: "acceptance_mentions"
    format: "JSON [{criterion: str, quote: str, is_measurable: bool}]"
    description: "Упоминания критериев приёмки с оценкой измеримости."
execution_type: LLM
prompt_template_hint: "Ищи формулировки, задающие условие приёмки. is_measurable=true если критерий числовой или булевый ('точность ≥ 80%'), false если качественный ('решение должно быть удобным'). Не-измеримые критерии — сигнал к уточнению в 600."
constraints:
  - "Если критериев не найдено — пустой массив (типичная ситуация, будет заполняться в 800)"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_405
name: "Извлечение упоминаний интеграций и внешних систем"
type: Executable
description: "Системы, с которыми должно взаимодействовать решение: API, базы данных, существующие сервисы, системы хранения документов."
parent_task: BIZ_TO_TZ_400
spawn_conditions: "Параллельно с остальными 40X."
input_requirements:
  - artifact: "normalized_request"
    contract: Hard
    description: "Нормализованный запрос."
outputs:
  - artifact: "integration_mentions"
    format: "JSON [{system: str, interaction_type: enum[read, write, both, unknown], quote: str}]"
    description: "Упоминания внешних систем + предполагаемый тип взаимодействия."
execution_type: LLM
prompt_template_hint: "Для RAG типовые интеграции: Confluence, SharePoint, GitHub, корпоративная почта (как источник документов); LDAP/SSO (для авторизации). Для ML: БД с историческими данными, CRM, 1С. Определи direction: читаем ли мы из системы, пишем ли в неё."
constraints:
  - "Цитата обязательна"
  - "interaction_type=unknown если из запроса непонятно"
escalation_conditions:
  - "Не эскалирует"
```

---

### 2.5. Блок 500 — Формирование gap-листа

```yaml
task_id: BIZ_TO_TZ_500
name: "Формирование gap-листа"
type: Composite
description: "Сопоставляет извлечённые декларации с чеклистом обязательных полей для выбранного поддомена и формирует приоритизированный список того, что надо узнать у заказчика."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После завершения 300 и 400."
input_requirements:
  - artifact: "need_model"
    contract: Hard
    description: "Модель потребности из 300."
  - artifact: "extracted_declarations"
    contract: Hard
    description: "Декларации из 400."
outputs:
  - artifact: "gap_list"
    format: "JSON"
    description: "Приоритизированный список пробелов."
possible_children:
  - task_id: BIZ_TO_TZ_501
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_502
    condition: "После 501"
  - task_id: BIZ_TO_TZ_503
    condition: "После 502"
```

```yaml
task_id: BIZ_TO_TZ_501
name: "Сверка извлечённого с чеклистом поддомена"
type: Executable
description: "Детерминированная сверка: для RAG-поддомена проверяется наличие N обязательных полей (корпус документов, тип запросов пользователей, требуемая точность, язык, SLA по latency). Для ML — характер задачи (класс/регрессия), целевая переменная, наличие разметки, baseline-метрика."
parent_task: BIZ_TO_TZ_500
spawn_conditions: "Первой в блоке 500."
input_requirements:
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Определяет, какой чеклист применять."
  - artifact: "extracted_declarations"
    contract: Hard
    description: "Что уже известно."
  - artifact: "need_model"
    contract: Hard
    description: "Открытые вопросы из Why-анализа тоже идут в общий gap."
  - artifact: "subdomain_checklist_registry"
    contract: Hard
    description: "Реестр чеклистов для RAG и ML (из Template Registry). Содержит обязательные и желательные поля для каждого поддомена."
outputs:
  - artifact: "raw_gaps"
    format: "JSON [{field: str, category: str, required_by_checklist: bool, source: enum[checklist, need_model], hint: str}]"
    description: "Плоский список всех обнаруженных пробелов до приоритезации."
execution_type: Tool
prompt_template_hint: "Детерминированный скрипт: для каждого поля чеклиста проверить, есть ли соответствующее упоминание в extracted_declarations. Если нет — добавить в raw_gaps. Дополнительно перенести open_questions_for_client из need_model."
constraints:
  - "Без LLM-вызова — формальная сверка"
  - "hint — подсказка из registry, как переформулировать пробел в вопрос"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_502
name: "Приоритезация пробелов"
type: Executable
description: "Классифицирует каждый пробел по критичности: blocking (без ответа нельзя двигаться), important (влияет на ключевые решения), nice-to-have (уточнит детали, но не останавливает работу)."
parent_task: BIZ_TO_TZ_500
spawn_conditions: "После 501."
input_requirements:
  - artifact: "raw_gaps"
    contract: Hard
    description: "Плоский список из 501."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Для контекстной оценки критичности."
outputs:
  - artifact: "prioritized_gaps"
    format: "JSON [{gap, priority: enum[blocking, important, nice_to_have], reasoning: str}]"
    description: "Пробелы с присвоенным приоритетом."
execution_type: LLM
prompt_template_hint: "blocking — без этого нельзя писать архитектуру (например, для RAG: неизвестен корпус документов; для ML: неизвестна целевая переменная). important — влияет на ключевые решения, но можно начать без него. nice_to_have — уточнение деталей."
constraints:
  - "Пробел с required_by_checklist=true не может быть ниже important"
  - "reasoning обязательно"
escalation_conditions:
  - "Доля blocking-пробелов > 70% — признак крайне неполного запроса, эскалация для принятия решения: продолжить или вернуть запрос на полную переработку"
```

```yaml
task_id: BIZ_TO_TZ_503
name: "Формирование структурированного gap-листа"
type: Executable
description: "Оформляет итоговый gap-лист: группирует пробелы по темам (данные / требования / интеграции / стейкхолдеры), добавляет счётчики по приоритетам. Счётчики используются задачей 600 (Dynamic) для решения — опросник или точечный вопрос."
parent_task: BIZ_TO_TZ_500
spawn_conditions: "После 502."
input_requirements:
  - artifact: "prioritized_gaps"
    contract: Hard
    description: "Приоритезированные пробелы."
outputs:
  - artifact: "gap_list"
    format: "JSON {gaps_by_theme: {data, requirements, integrations, stakeholders, other}, priority_counts: {blocking: int, important: int, nice_to_have: int}, total: int}"
    description: "Структурированный gap-лист."
execution_type: LLM
prompt_template_hint: "Сгруппируй по темам. Посчитай по приоритетам. Это итоговый артефакт блока 500."
constraints:
  - "Все пробелы из prioritized_gaps должны быть распределены"
  - "Если gap не попадает ни в одну тему — группа 'other'"
escalation_conditions:
  - "Не эскалирует"
```

---

### 2.6. Блок 600 — Сбор недостающей информации у заказчика

Блок реализует вариант (c) из уточнения пользователя: сочетание пакетных опросников и точечных Human-задач через Interruption Gateway.

```yaml
task_id: BIZ_TO_TZ_600
name: "Сбор недостающей информации у заказчика"
type: Dynamic
description: "Динамически выбирает стратегию общения с заказчиком на основе gap_list.priority_counts. Если пробелов много или есть blocking — формирует пакетный опросник. Если пробел единичный или нужно переспросить — точечный вопрос."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После 500 с непустым gap_list. Может быть запущен повторно, если после ответа заказчика обнаружены новые пробелы (бабл-ап с декларацией новых 400-подзадач при перепланировании)."
input_requirements:
  - artifact: "gap_list"
    contract: Hard
    description: "Структурированный gap-лист."
  - artifact: "need_model"
    contract: Hard
    description: "Чтобы не дублировать уже озвученные гипотезы."
  - artifact: "previous_clarifications"
    contract: Soft
    description: "Ответы заказчика на предыдущих итерациях, чтобы не переспрашивать."
outputs:
  - artifact: "clarifications"
    format: "JSON {qa_pairs: [{question, answer, validated: bool, timestamp}]}"
    description: "Набор вопросов-ответов, который обогатит extracted_declarations."
dynamic_decision_logic: |
  if gap_list.total == 0:
    skip (задача завершается сразу, без детей)
  elif gap_list.priority_counts.blocking + gap_list.priority_counts.important >= 3:
    spawn: [601 (опросник), 603, 604, 605]
  else:
    for each gap:
      spawn: [602 (точечный вопрос), 603, 604, 605]
possible_children:
  - task_id: BIZ_TO_TZ_601
    condition: "priority_counts: blocking+important >= 3"
  - task_id: BIZ_TO_TZ_602
    condition: "priority_counts: blocking+important < 3"
  - task_id: BIZ_TO_TZ_603
    condition: "После 601 или 602"
  - task_id: BIZ_TO_TZ_604
    condition: "После получения ответа (603 переходит в Completed)"
  - task_id: BIZ_TO_TZ_605
    condition: "После 604"
```

```yaml
task_id: BIZ_TO_TZ_601
name: "Формирование пакетного опросника"
type: Executable
description: "Из gap_list формирует структурированный опросник для заказчика: группировка по темам, чёткие формулировки, закрытые варианты ответов где возможно."
parent_task: BIZ_TO_TZ_600
spawn_conditions: "Выбрана стратегия 'опросник'."
input_requirements:
  - artifact: "gap_list"
    contract: Hard
    description: "Gap-лист."
  - artifact: "question_templates_library"
    contract: Hard
    description: "Библиотека типовых формулировок вопросов для RAG/ML-поддоменов (из Template Registry)."
outputs:
  - artifact: "client_questionnaire"
    format: "Markdown + JSON-схема ответов"
    description: "Опросник в человекочитаемом виде + машинная схема для парсинга ответа."
execution_type: LLM
prompt_template_hint: "Сгруппируй вопросы по темам. Каждый вопрос должен: (1) быть понятен не-специалисту, (2) по возможности предлагать варианты ответа, (3) объяснять, ПОЧЕМУ ответ важен (повышает готовность заказчика отвечать). Не более 10 вопросов всего — остальные отложить до следующей итерации."
constraints:
  - "Максимум 10 вопросов в одном опроснике"
  - "Сначала blocking, потом important, потом nice_to_have"
  - "Каждый вопрос — обоснование, почему мы спрашиваем"
  - "Для каждого закрытого вопроса — варианты + 'другое'"
escalation_conditions:
  - "Опросник не укладывается в 10 вопросов даже после приоритезации — эскалация: нужно решение, разбивать ли опрос на серию итераций"
```

```yaml
task_id: BIZ_TO_TZ_602
name: "Формирование точечного вопроса"
type: Executable
description: "Один открытый вопрос по конкретному пробелу. Используется для быстрых уточнений или переспросов."
parent_task: BIZ_TO_TZ_600
spawn_conditions: "Выбрана стратегия точечных вопросов; порождается по одной на каждый gap."
input_requirements:
  - artifact: "gap_list"
    contract: Hard
    description: "Gap-лист (из него берётся один пробел на экземпляр задачи)."
  - artifact: "target_gap_id"
    contract: Hard
    description: "ID конкретного пробела из gap_list, передаваемый при порождении."
outputs:
  - artifact: "point_question"
    format: "JSON {question: str, context: str, expected_format: str}"
    description: "Короткий вопрос с пояснением контекста и ожидаемым форматом ответа."
execution_type: LLM
prompt_template_hint: "Сформулируй ОДИН короткий вопрос по указанному пробелу. Добавь 1-2 предложения контекста (почему спрашиваем). Укажи ожидаемый формат ответа."
constraints:
  - "Ровно один вопрос"
  - "Длина вопроса — не более 2 предложений"
  - "Не копировать слово 'gap' в итоговом тексте — формулировка для человека"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_603
name: "Отправка запроса заказчику через Interruption Gateway"
type: Executable
description: "Передаёт сформированный опросник или вопрос в систему общения с пользователем через Interruption Gateway. Задача переходит в состояние, аналогичное Waiting_for_Children, но ожидает внешнее событие — ответ заказчика."
parent_task: BIZ_TO_TZ_600
spawn_conditions: "После 601 или 602."
input_requirements:
  - artifact: "client_questionnaire"
    contract: Soft
    description: "Если был опросник."
  - artifact: "point_question"
    contract: Soft
    description: "Если был точечный вопрос."
outputs:
  - artifact: "client_response_raw"
    format: "Текст или структура, определяемая системой общения"
    description: "Ответ заказчика в исходном виде."
execution_type: Human
prompt_template_hint: "Задача не вызывает LLM — триггерит отправку через Interruption Gateway и ждёт внешнего события 'response_received'. Таймаут — конфигурируется на уровне проекта."
constraints:
  - "Должен быть передан ровно один артефакт: либо questionnaire, либо point_question"
  - "Ожидание ответа — не блокирует остальной граф; родственные задачи продолжают выполняться"
escalation_conditions:
  - "Превышен таймаут ожидания ответа (по умолчанию 48 часов рабочего времени) — эскалация к менеджеру проекта"
  - "Получен ответ вида 'не знаю' / 'затрудняюсь' на blocking-вопрос — эскалация: заказчик не может ответить на критичный вопрос, нужно решение о продолжении"
```

```yaml
task_id: BIZ_TO_TZ_604
name: "Парсинг ответа заказчика"
type: Executable
description: "Превращает свободный текст ответа в структурированные данные по исходной схеме вопросов."
parent_task: BIZ_TO_TZ_600
spawn_conditions: "После 603 (когда client_response_raw получен)."
input_requirements:
  - artifact: "client_response_raw"
    contract: Hard
    description: "Сырой ответ заказчика."
  - artifact: "client_questionnaire"
    contract: Soft
    description: "Если был опросник — даёт схему для структурирования."
  - artifact: "point_question"
    contract: Soft
    description: "Если был точечный вопрос."
outputs:
  - artifact: "parsed_clarifications"
    format: "JSON [{question_id, answer_text, extracted_fields: {...}}]"
    description: "Ответ, привязанный к исходным вопросам, с извлечёнными полями."
execution_type: LLM
prompt_template_hint: "Для каждого заданного вопроса найди соответствующий фрагмент ответа. Извлеки структурированные поля согласно expected_format. Если на какой-то вопрос не ответили — явно отметь (not_answered=true)."
constraints:
  - "Все вопросы из исходного опросника/точечного вопроса должны быть сопоставлены (хотя бы с пометкой not_answered)"
  - "Запрещено додумывать ответ на неотвеченный вопрос"
escalation_conditions:
  - "Не эскалирует (пустые ответы обрабатываются в 605)"
```

```yaml
task_id: BIZ_TO_TZ_605
name: "Валидация ответа заказчика"
type: Executable
description: "Проверяет, что полученные ответы осмысленны и отвечают на поставленные вопросы. Выявляет противоречия с ранее полученной информацией."
parent_task: BIZ_TO_TZ_600
spawn_conditions: "После 604."
input_requirements:
  - artifact: "parsed_clarifications"
    contract: Hard
    description: "Разобранный ответ."
  - artifact: "extracted_declarations"
    contract: Hard
    description: "Ранее известная информация для проверки противоречий."
outputs:
  - artifact: "clarifications"
    format: "JSON {qa_pairs, contradictions: [str], unanswered_blocking: [str], new_info_fields: {...}}"
    description: "Валидированные ответы + список противоречий + список неотвеченных blocking-вопросов."
execution_type: LLM
prompt_template_hint: "Для каждого ответа: (1) отвечает ли он на заданный вопрос? (2) не противоречит ли ранее известному? (3) что нового добавляет в картину? Собери противоречия и критичные пробелы, оставшиеся без ответа."
constraints:
  - "Противоречия формулируются как пары (ранее_известное, новое_утверждение) — для прозрачности"
  - "Если unanswered_blocking не пуст — это триггер для повторного 600"
escalation_conditions:
  - "Обнаружены противоречия в ответах заказчика, которые нельзя разрешить без уточнения — порождается новый 602 по конкретному противоречию, но не более 2 итераций на один пробел (иначе — эскалация к человеку)"
  - "Все blocking-вопросы остались без ответа после 2-й итерации — эскалация к менеджеру проекта"
```

---

### 2.7. Блок 700 — Работа с данными

```yaml
task_id: BIZ_TO_TZ_700
name: "Работа с данными"
type: Composite
description: "Специализированная обработка всего, что касается данных заказчика: инвентаризация источников, углублённое уточнение их характеристик, оценка достаточности, решение о синтетике."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После 600 (когда есть первая волна уточнений) или параллельно с 600, если упоминания данных уже достаточны."
input_requirements:
  - artifact: "data_mentions"
    contract: Hard
    description: "Из 401."
  - artifact: "clarifications"
    contract: Soft
    description: "Уточнения от заказчика, если были."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Определяет типовые требования к данным."
outputs:
  - artifact: "data_specification"
    format: "JSON"
    description: "Спецификация данных для ТЗ."
possible_children:
  - task_id: BIZ_TO_TZ_701
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_702
    condition: "После 701, если есть упомянутые источники"
  - task_id: BIZ_TO_TZ_703
    condition: "После 702"
  - task_id: BIZ_TO_TZ_704
    condition: "После 703"
```

```yaml
task_id: BIZ_TO_TZ_701
name: "Инвентаризация источников данных"
type: Executable
description: "Из разрозненных упоминаний (data_mentions + ответы заказчика) формирует нормализованный список источников с уникальными ID и первичными характеристиками."
parent_task: BIZ_TO_TZ_700
spawn_conditions: "Первой в блоке 700."
input_requirements:
  - artifact: "data_mentions"
    contract: Hard
    description: "Упоминания данных из 401."
  - artifact: "clarifications"
    contract: Soft
    description: "Что заказчик дополнительно сказал про данные."
outputs:
  - artifact: "data_sources_inventory"
    format: "JSON [{source_id, name, type, known_attributes: {...}, unknown_attributes: [str]}]"
    description: "Нормализованный список источников. Дубликаты объединены, неизвестные поля явно помечены."
execution_type: LLM
prompt_template_hint: "Для RAG типы: document_corpus, wiki, email_archive, codebase. Для ML: tabular_db, log_stream, labeled_dataset, event_stream. Если два упоминания явно про один источник — слить в один. Для каждого источника — список атрибутов, которые НЕ известны (volume, format, quality, access_method, etc)."
constraints:
  - "Каждый источник получает уникальный source_id"
  - "Дедупликация строгая — одно и то же название = один источник"
  - "unknown_attributes обязательно заполнено — служит входом для 702"
escalation_conditions:
  - "Упоминаний данных нет вообще, а задача требует данные (blocking по чеклисту поддомена) — порождается 600 с точечным вопросом 'Какие данные у вас есть?'"
```

```yaml
task_id: BIZ_TO_TZ_702
name: "Уточнение характеристик источников (Dynamic)"
type: Dynamic
description: "Для каждого источника из инвентаря решает: нужно ли уточнять формат/объём, качество/разметку, легальность/доступ. Решение принимается по unknown_attributes."
parent_task: BIZ_TO_TZ_700
spawn_conditions: "После 701 для каждого source_id с непустым unknown_attributes."
input_requirements:
  - artifact: "data_sources_inventory"
    contract: Hard
    description: "Список источников."
  - artifact: "target_source_id"
    contract: Hard
    description: "ID источника, по которому идёт уточнение (передаётся при порождении)."
outputs:
  - artifact: "source_specific_questions"
    format: "JSON [{question, category, target_source_id}]"
    description: "Набор вопросов по конкретному источнику, готовый для отправки в 600."
dynamic_decision_logic: |
  for each unknown_attribute in source:
    if attribute in [format, volume, schema]:
      spawn: 702_A
    if attribute in [quality, labeling, completeness]:
      spawn: 702_B
    if attribute in [legal, access, consent]:
      spawn: 702_C
possible_children:
  - task_id: BIZ_TO_TZ_702_A
    condition: "unknown_attributes содержат format/volume/schema"
  - task_id: BIZ_TO_TZ_702_B
    condition: "unknown_attributes содержат quality/labeling/completeness"
  - task_id: BIZ_TO_TZ_702_C
    condition: "unknown_attributes содержат legal/access/consent"
```

```yaml
task_id: BIZ_TO_TZ_702_A
name: "Формирование вопросов о формате и объёме"
type: Executable
description: "Генерирует вопросы о технических параметрах: формат файлов, схема, объём в записях/байтах, типовой размер одной записи, частота обновления."
parent_task: BIZ_TO_TZ_702
spawn_conditions: "Для каждого источника с пробелами по format/volume/schema."
input_requirements:
  - artifact: "data_sources_inventory"
    contract: Hard
    description: "Источник из инвентаря."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Для RAG важны: формат документов, объём корпуса, язык. Для ML: схема, количество записей, баланс классов."
outputs:
  - artifact: "format_volume_questions"
    format: "JSON [{question, expected_format}]"
    description: "Вопросы о формате и объёме."
execution_type: LLM
prompt_template_hint: "Для RAG: 'В каком формате хранятся документы (PDF, DOCX, HTML, txt)?', 'Сколько документов в корпусе?', 'Средний размер документа?'. Для ML: 'Какова схема таблицы?', 'Сколько строк?', 'Как часто обновляется?'"
constraints:
  - "Вопросы формулируются на языке заказчика, без технического жаргона без необходимости"
escalation_conditions:
  - "Не эскалирует (вопросы уйдут в 600)"
```

```yaml
task_id: BIZ_TO_TZ_702_B
name: "Формирование вопросов о качестве и разметке"
type: Executable
description: "Генерирует вопросы о качестве данных: есть ли пропуски, дубликаты, разметка (для ML), аннотации (для RAG), известные проблемы."
parent_task: BIZ_TO_TZ_702
spawn_conditions: "Для каждого источника с пробелами по quality/labeling."
input_requirements:
  - artifact: "data_sources_inventory"
    contract: Hard
    description: "Источник."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Определяет специфику."
outputs:
  - artifact: "quality_labeling_questions"
    format: "JSON [{question, expected_format}]"
    description: "Вопросы о качестве."
execution_type: LLM
prompt_template_hint: "Для ML-поддомена обязательно: есть ли разметка? кто размечал? какое качество разметки? распределение классов? Для RAG: есть ли метаданные (дата, автор, тип документа)? сколько документов устарело? есть ли дубликаты?"
constraints:
  - "Для ML-задач classification/regression — вопрос о разметке обязателен"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_702_C
name: "Формирование вопросов о легальности и доступе"
type: Executable
description: "Вопросы о правах использования, технической доступности, персональных данных, необходимости анонимизации."
parent_task: BIZ_TO_TZ_702
spawn_conditions: "Для каждого источника с пробелами по legal/access."
input_requirements:
  - artifact: "data_sources_inventory"
    contract: Hard
    description: "Источник."
  - artifact: "constraint_mentions"
    contract: Soft
    description: "Уже извлечённые регуляторные ограничения из 403."
outputs:
  - artifact: "legal_access_questions"
    format: "JSON [{question, expected_format}]"
    description: "Вопросы о легальности."
execution_type: LLM
prompt_template_hint: "Вопросы: 'Содержат ли данные ПД?', 'Нужна ли анонимизация до передачи нам?', 'Как мы получим доступ технически (API/дамп/выгрузка)?', 'Есть ли ограничения по внешнему использованию (облачные LLM-провайдеры)?'"
constraints:
  - "Если в constraint_mentions уже зафиксирован regulatory — этот вопрос не дублировать"
escalation_conditions:
  - "Не эскалирует (но ответы с подтверждением ПД/конфиденциальности влияют на 900 — выбор архитектуры, возможно onprem)"
```

```yaml
task_id: BIZ_TO_TZ_703
name: "Оценка достаточности данных"
type: Executable
description: "Сопоставляет известные характеристики данных с минимальными требованиями для выбранного поддомена. Бинарное решение: достаточно / не достаточно + обоснование."
parent_task: BIZ_TO_TZ_700
spawn_conditions: "После 702 (когда уточнения отработали) или сразу после 701, если пробелов по данным не было."
input_requirements:
  - artifact: "data_sources_inventory"
    contract: Hard
    description: "Итоговый инвентарь (с учётом уточнений)."
  - artifact: "data_sufficiency_heuristics"
    contract: Hard
    description: "Эвристики достаточности для RAG/ML из Template Registry. Для RAG: минимальный объём корпуса, покрытие тематики. Для ML: минимальное число записей, баланс классов, качество разметки."
  - artifact: "need_model"
    contract: Soft
    description: "Для понимания, насколько высоки требования к качеству."
outputs:
  - artifact: "data_sufficiency_verdict"
    format: "JSON {sufficient: bool, gaps: [str], risk_level: enum[low, medium, high]}"
    description: "Вердикт о достаточности + список пробелов + оценка риска."
execution_type: LLM
prompt_template_hint: "Применяй эвристики из registry. Для RAG: менее 50 документов — risk=high. Для ML-классификации: менее 500 записей на класс — risk=high. Для ML-регрессии: менее 1000 записей — risk=medium. Всегда объясняй риски."
constraints:
  - "Вердикт sufficient=true только при risk_level ∈ {low, medium}"
  - "gaps содержит конкретные недостающие параметры"
escalation_conditions:
  - "sufficient=false и нет опции синтетических данных для поддомена — эскалация: продолжение без достаточных данных даст непригодный PoV"
```

```yaml
task_id: BIZ_TO_TZ_704
name: "Решение о синтетических данных"
type: Executable
description: "Если реальных данных недостаточно или доступ к ним ограничен — предлагает использовать синтетические данные для PoV. Решение фиксируется в ТЗ как явное допущение."
parent_task: BIZ_TO_TZ_700
spawn_conditions: "После 703."
input_requirements:
  - artifact: "data_sufficiency_verdict"
    contract: Hard
    description: "Вердикт 703."
  - artifact: "data_sources_inventory"
    contract: Hard
    description: "Что вообще есть."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Для RAG синтетика осмыслена только для запросов пользователей; документы — нет. Для ML — генерация табличных данных применима."
outputs:
  - artifact: "synthetic_data_decision"
    format: "JSON {use_synthetic: bool, rationale: str, synthetic_for: [str], limitations: [str]}"
    description: "Решение + обоснование + список того, для чего именно применяется синтетика + её ограничения."
execution_type: LLM
prompt_template_hint: "Если sufficient=true — use_synthetic=false. Если false из-за недоступа к данным — use_synthetic=true для демонстрации в PoV. Явно укажи limitations: 'синтетика не отражает распределение реальных данных', 'результаты на синтетике не переносятся напрямую на реальные данные'. Для RAG синтезируем только user_queries, не corpus."
constraints:
  - "Если use_synthetic=true, limitations не может быть пустым"
  - "Решение о синтетике требует подтверждения заказчика — порождается 602 с точечным вопросом"
escalation_conditions:
  - "Заказчик в 602 отклонил синтетику, но реальных данных недостаточно — эскалация: тупик, нужно пересогласование scope проекта"
outputs_aggregate:
  - artifact: "data_specification"
    format: "JSON"
    description: "Композитный выход блока 700: inventory + sufficiency + synthetic_decision. Формируется Composite-задачей 700 при завершении всех детей."
```

---

### 2.8. Блок 800 — Формализация требований

```yaml
task_id: BIZ_TO_TZ_800
name: "Формализация требований"
type: Composite
description: "Превращает разрозненные упоминания и ответы заказчика в структурированный набор требований: функциональные, нефункциональные, ограничения, критерии приёмки."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После блоков 600 (завершено, все blocking отвечены) и 700."
input_requirements:
  - artifact: "need_model"
    contract: Hard
    description: "Модель потребности."
  - artifact: "extracted_declarations"
    contract: Hard
    description: "Декларации из 400."
  - artifact: "clarifications"
    contract: Soft
    description: "Ответы заказчика. Soft, т.к. при полном исходном запросе gap_list пуст и блок 600 skip'ается — в этом случае clarifications создаётся пустым объектом Composite-задачей 600."
  - artifact: "data_specification"
    contract: Hard
    description: "Спецификация данных из 700."
outputs:
  - artifact: "formalized_requirements"
    format: "JSON"
    description: "Структурированные требования."
possible_children:
  - task_id: BIZ_TO_TZ_801
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_802
    condition: "Всегда (параллельно с 801)"
  - task_id: BIZ_TO_TZ_803
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_804
    condition: "После 801 и 802"
  - task_id: BIZ_TO_TZ_805
    condition: "После 801, 802, 803, 804"
```

```yaml
task_id: BIZ_TO_TZ_801
name: "Формулирование функциональных требований"
type: Executable
description: "Пишет функциональные требования в формате 'Система должна <действие> при <условии>, результатом <что>'. Каждое требование имеет ID, приоритет (must/should/could) и трассировку к источнику."
parent_task: BIZ_TO_TZ_800
spawn_conditions: "Параллельно с 802, 803."
input_requirements:
  - artifact: "need_model"
    contract: Hard
    description: "Что делает система и зачем."
  - artifact: "clarifications"
    contract: Hard
    description: "Уточнения от заказчика."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Для RAG типовые ФТ: приём запроса пользователя, поиск по корпусу, генерация ответа с указанием источников. Для ML: приём входных признаков, возврат предсказания, логирование."
  - artifact: "functional_requirements_templates"
    contract: Hard
    description: "Шаблонные ФТ для RAG/ML из Template Registry."
outputs:
  - artifact: "functional_requirements"
    format: "JSON [{id: FR-XXX, statement: str, priority: enum[must, should, could], source: str}]"
    description: "Список ФТ с приоритетами и трассировкой."
execution_type: LLM
prompt_template_hint: "Для RAG базовый набор: FR-001 приём запроса, FR-002 поиск релевантных фрагментов, FR-003 формирование ответа, FR-004 указание источников. Для ML: FR-001 приём признаков, FR-002 валидация входа, FR-003 возврат предсказания, FR-004 возврат уверенности. Далее — специфика проекта. source = ссылка на input (quote или QA-пара)."
constraints:
  - "Каждое ФТ имеет уникальный ID вида FR-001, FR-002..."
  - "Формулировка: 'Система должна...' в одном предложении"
  - "Трассировка обязательна — иначе требование не обосновано"
  - "Для MVP-поддоменов минимум 4 базовых ФТ"
escalation_conditions:
  - "Невозможно сформулировать минимальный набор базовых ФТ (признак недоопределённой задачи) — эскалация с возвратом в 500 для перегенерации gap-листа"
```

```yaml
task_id: BIZ_TO_TZ_802
name: "Формулирование нефункциональных требований"
type: Executable
description: "Пишет НФТ: производительность, надёжность, безопасность, удобство сопровождения. Для MVP (из PoV.md) безопасность и производительность не в приоритете — фиксируются как соответствующие."
parent_task: BIZ_TO_TZ_800
spawn_conditions: "Параллельно с 801, 803."
input_requirements:
  - artifact: "metric_mentions"
    contract: Hard
    description: "Из 402 — целевые метрики."
  - artifact: "constraint_mentions"
    contract: Hard
    description: "Из 403 — ограничения."
  - artifact: "clarifications"
    contract: Hard
    description: "Уточнения."
  - artifact: "non_functional_requirements_templates"
    contract: Hard
    description: "Шаблоны НФТ для RAG/ML."
outputs:
  - artifact: "non_functional_requirements"
    format: "JSON [{id: NFR-XXX, category: enum[performance, reliability, maintainability, usability, security], statement, target_value, priority, source}]"
    description: "НФТ с категориями и целевыми значениями."
execution_type: LLM
prompt_template_hint: "Для PoV (MVP) performance, security — обычно 'соответствует демонстрационным требованиям'. Основные НФТ: воспроизводимость (зафиксированный сид, pinned зависимости), трассируемость (логи вызовов LLM), наблюдаемость метрик. Для RAG добавить: faithfulness к источникам. Для ML: устойчивость к входам вне распределения."
constraints:
  - "Для каждого НФТ — target_value или явное 'not applicable for PoV' с обоснованием"
  - "Уникальные ID NFR-XXX"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_803
name: "Фиксация ограничений"
type: Executable
description: "Превращает constraint_mentions и регуляторные требования в формальные Constraint-записи с типом и жёсткостью."
parent_task: BIZ_TO_TZ_800
spawn_conditions: "Параллельно с 801, 802."
input_requirements:
  - artifact: "constraint_mentions"
    contract: Hard
    description: "Ограничения из 403."
  - artifact: "clarifications"
    contract: Hard
    description: "Уточнения."
  - artifact: "data_specification"
    contract: Hard
    description: "Ограничения, вытекающие из данных (ПД → onprem, например)."
outputs:
  - artifact: "project_constraints"
    format: "JSON [{id: CON-XXX, type, statement, is_hard: bool, source}]"
    description: "Формализованные ограничения."
execution_type: LLM
prompt_template_hint: "Объедини упоминания ограничений + следствия из data_specification (например, из наличия ПД → constraint по обработке). is_hard=true для жёстких запретов/обязательств."
constraints:
  - "Уникальные ID CON-XXX"
  - "Если constraint противоречит ФТ — отдельно отметить в metadata для блока 805"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_804
name: "Фиксация критериев приёмки"
type: Executable
description: "Формулирует измеримые критерии, по которым PoV будет считаться принятым. Каждый критерий привязан к ФТ или НФТ и имеет метод проверки."
parent_task: BIZ_TO_TZ_800
spawn_conditions: "После 801 и 802."
input_requirements:
  - artifact: "functional_requirements"
    contract: Hard
    description: "ФТ из 801."
  - artifact: "non_functional_requirements"
    contract: Hard
    description: "НФТ из 802."
  - artifact: "acceptance_mentions"
    contract: Hard
    description: "Упоминания критериев из 404."
  - artifact: "metric_mentions"
    contract: Hard
    description: "Целевые метрики из 402."
outputs:
  - artifact: "acceptance_criteria"
    format: "JSON [{id: AC-XXX, statement, measurable: bool, target, verification_method, linked_requirements: [FR-XXX/NFR-XXX]}]"
    description: "Критерии приёмки с методами проверки."
execution_type: LLM
prompt_template_hint: "Каждый критерий приёмки: что проверяем, как проверяем (скрипт, ручная демонстрация, метрика на тесте), какое значение считается успехом, к каким требованиям относится. measurable=false только если согласовано с заказчиком (редкий случай, требует явного обоснования в statement)."
constraints:
  - "Каждое must-требование обязано иметь хотя бы один критерий приёмки"
  - "verification_method конкретный: 'прогон на отложенной выборке', 'демо-сценарий №3', 'ручная оценка качества 20 ответов'"
  - "Уникальные ID AC-XXX"
escalation_conditions:
  - "Есть must-требования без критериев приёмки и невозможно их сформулировать измеримо — эскалация: нужен диалог с заказчиком для определения, что значит 'работает'"
```

```yaml
task_id: BIZ_TO_TZ_805
name: "Проверка согласованности требований"
type: Executable
description: "Ищет противоречия: между ФТ и НФТ, между ФТ и ограничениями, между критериями приёмки и ограничениями. Формирует список конфликтов для ручного или автоматического разрешения."
parent_task: BIZ_TO_TZ_800
spawn_conditions: "После 801, 802, 803, 804."
input_requirements:
  - artifact: "functional_requirements"
    contract: Hard
    description: "ФТ."
  - artifact: "non_functional_requirements"
    contract: Hard
    description: "НФТ."
  - artifact: "project_constraints"
    contract: Hard
    description: "Ограничения."
  - artifact: "acceptance_criteria"
    contract: Hard
    description: "Критерии приёмки."
outputs:
  - artifact: "formalized_requirements"
    format: "JSON {functional, non_functional, constraints, acceptance_criteria, conflicts: [{type, items: [...], severity}]}"
    description: "Композитный выход блока 800 — все требования плюс обнаруженные конфликты."
execution_type: LLM
prompt_template_hint: "Проверь типичные конфликты: (1) НФТ требует высокую точность, но констрейнт запрещает использовать необходимые ресурсы; (2) ФТ предполагает работу с ПД, но нет ограничения по защите; (3) критерий приёмки недостижим при заявленных ограничениях. severity: critical (блокирует) / warning / info."
constraints:
  - "conflicts может быть пустым — это валидная ситуация"
  - "Каждый конфликт формулируется как 'X требует Y, Z запрещает Y'"
escalation_conditions:
  - "Обнаружены конфликты severity=critical — эскалация к заказчику через 600 с описанием конфликта и предложением вариантов разрешения"
```

---

### 2.9. Блок 900 — Архитектурный анализ

Блок явно заточен под RAG и простой ML. Dynamic-задача 900 выбирает ветку по subdomain_classification и порождает одну из двух профильных подзадач.

```yaml
task_id: BIZ_TO_TZ_900
name: "Архитектурный анализ"
type: Dynamic
description: "Высокоуровневый выбор подхода к решению. Решение о ветке (RAG или ML) принимается на основе subdomain_classification; подзадачи 902 и 903 взаимоисключающие."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После 800."
input_requirements:
  - artifact: "formalized_requirements"
    contract: Hard
    description: "Формализованные требования."
  - artifact: "data_specification"
    contract: Hard
    description: "Спецификация данных."
  - artifact: "need_model"
    contract: Hard
    description: "Модель потребности."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Определяет ветку."
outputs:
  - artifact: "architectural_approach"
    format: "JSON"
    description: "Выбранный подход + обоснование + baseline."
dynamic_decision_logic: |
  always spawn: 901, 904, 905
  if subdomain == RAG: spawn 902
  elif subdomain == ML: spawn 903
possible_children:
  - task_id: BIZ_TO_TZ_901
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_902
    condition: "subdomain == RAG"
  - task_id: BIZ_TO_TZ_903
    condition: "subdomain == ML"
  - task_id: BIZ_TO_TZ_904
    condition: "После 902 или 903"
  - task_id: BIZ_TO_TZ_905
    condition: "После 904"
```

```yaml
task_id: BIZ_TO_TZ_901
name: "Определение класса задачи внутри поддомена"
type: Executable
description: "Для RAG: определяет подтип (Q&A по FAQ, summarization по корпусу, structured extraction, chat-with-docs). Для ML: тип задачи (binary/multiclass classification, regression, anomaly detection)."
parent_task: BIZ_TO_TZ_900
spawn_conditions: "Первой в блоке 900."
input_requirements:
  - artifact: "need_model"
    contract: Hard
    description: "Что делает система."
  - artifact: "functional_requirements"
    contract: Hard
    description: "Детализация функций."
  - artifact: "subdomain_classification"
    contract: Hard
    description: "Верхнеуровневая классификация."
  - artifact: "task_class_taxonomy"
    contract: Hard
    description: "Таксономия классов задач для RAG/ML из Template Registry."
outputs:
  - artifact: "task_class"
    format: "JSON {class_id: str, class_name: str, confidence: float, reasoning: str}"
    description: "Выбранный класс задачи."
execution_type: LLM
prompt_template_hint: "Сопоставь need_model + FR с таксономией. Для RAG: chat_with_docs / faq_qa / structured_extraction / summarization. Для ML: binary_classification / multiclass / regression / anomaly_detection. Обоснуй, почему именно этот класс."
constraints:
  - "class_id строго из taxonomy"
  - "confidence < 0.7 → обязательное указание альтернатив в reasoning"
escalation_conditions:
  - "Класс задачи не укладывается ни в одну позицию taxonomy (признак, что поддомен был определён неверно) — эскалация на пересмотр 103"
```

```yaml
task_id: BIZ_TO_TZ_902
name: "Выбор архитектурного шаблона для RAG"
type: Executable
description: "Выбирает конкретный архитектурный шаблон RAG: Naive RAG / Advanced RAG (с reranking) / Hierarchical RAG / Agentic RAG. Выбор опирается на класс задачи, объём корпуса и требуемое качество."
parent_task: BIZ_TO_TZ_900
spawn_conditions: "subdomain == RAG, после 901."
input_requirements:
  - artifact: "task_class"
    contract: Hard
    description: "Класс задачи."
  - artifact: "data_specification"
    contract: Hard
    description: "Объём корпуса, формат."
  - artifact: "formalized_requirements"
    contract: Hard
    description: "Требования к качеству."
  - artifact: "rag_architecture_patterns"
    contract: Hard
    description: "Библиотека RAG-шаблонов с их применимостью."
outputs:
  - artifact: "rag_architecture_choice"
    format: "JSON {pattern: enum[naive, advanced, hierarchical, agentic], components: {embedder, vector_store, retriever, reranker|null, generator}, rationale: str}"
    description: "Выбранный RAG-шаблон с ключевыми компонентами."
execution_type: LLM
prompt_template_hint: "Правила выбора: Naive RAG — малый корпус (< 1000 документов), низкие требования к качеству. Advanced RAG (+ reranking) — средний корпус, требования к качеству выше. Hierarchical RAG — большой корпус или разнородный контент. Agentic RAG — многошаговые вопросы. Компоненты: для MVP embedder — типовой (тип указывается, модель — на следующий гейт), vector_store — FAISS/Chroma для PoV."
constraints:
  - "Для MVP (PoV) предпочтение простым шаблонам — Naive или Advanced, если это не противоречит требованиям"
  - "Не указывать конкретные версии моделей — это задача следующего гейта (Архитектура)"
  - "rationale ссылается на конкретные FR/NFR"
escalation_conditions:
  - "Ни один шаблон не удовлетворяет формализованным требованиям (например, требуется < 100ms latency при большом корпусе — типовые RAG не подходят) — эскалация с предложением пересмотреть НФТ"
```

```yaml
task_id: BIZ_TO_TZ_903
name: "Выбор архитектурного шаблона для ML"
type: Executable
description: "Выбирает шаблон ML-решения: класс моделей (линейные / ансамблевые / нейросетевые), подход к обучению (supervised / semi-supervised). Для MVP предпочитаются интерпретируемые и быстро обучаемые модели."
parent_task: BIZ_TO_TZ_900
spawn_conditions: "subdomain == ML, после 901."
input_requirements:
  - artifact: "task_class"
    contract: Hard
    description: "Класс задачи."
  - artifact: "data_specification"
    contract: Hard
    description: "Объём данных, качество разметки."
  - artifact: "formalized_requirements"
    contract: Hard
    description: "Требования (интерпретируемость, скорость обучения)."
  - artifact: "ml_architecture_patterns"
    contract: Hard
    description: "Библиотека ML-шаблонов."
outputs:
  - artifact: "ml_architecture_choice"
    format: "JSON {model_family: enum[linear, tree_based, boosting, nn], training_approach: enum[supervised, semi_supervised], features_strategy: str, rationale: str}"
    description: "Выбранный ML-шаблон."
execution_type: LLM
prompt_template_hint: "Правила: малый датасет (< 10k) или требование интерпретируемости → linear или tree_based. Средний датасет, табличные данные → boosting (XGBoost/CatBoost/LightGBM семейство — без указания версии). NN — только если требуется и данных > 100k. features_strategy: 'минимальная обработка, опора на сырые признаки' / 'feature engineering требуется'."
constraints:
  - "Для MVP предпочитать простые интерпретируемые модели"
  - "Без указания конкретных гиперпараметров и версий"
escalation_conditions:
  - "task_class требует подхода, которого нет в ml_architecture_patterns — эскалация на расширение registry"
```

```yaml
task_id: BIZ_TO_TZ_904
name: "Обоснование выбранного подхода"
type: Executable
description: "Формирует текстовое обоснование выбора — связывает архитектурное решение с конкретными требованиями и ограничениями. Это материал для раздела 'Архитектурный подход' в ТЗ."
parent_task: BIZ_TO_TZ_900
spawn_conditions: "После 902 или 903."
input_requirements:
  - artifact: "rag_architecture_choice"
    contract: Soft
    description: "Если RAG."
  - artifact: "ml_architecture_choice"
    contract: Soft
    description: "Если ML."
  - artifact: "formalized_requirements"
    contract: Hard
    description: "Привязка обоснования к требованиям."
outputs:
  - artifact: "architecture_rationale"
    format: "Markdown"
    description: "Обоснование выбора на 1-2 абзаца + таблица 'требование → как удовлетворяется'."
execution_type: LLM
prompt_template_hint: "Формат: (1) абзац об общем подходе, (2) таблица 'FR/NFR → компонент архитектуры, удовлетворяющий требование', (3) абзац о ключевых рисках выбранного подхода."
constraints:
  - "Каждое must-требование должно появиться в таблице сопоставления"
  - "Риски — минимум 2, с описанием как их митигировать"
escalation_conditions:
  - "Не эскалирует"
```

```yaml
task_id: BIZ_TO_TZ_905
name: "Определение baseline-решения"
type: Executable
description: "Для PoV критично определить baseline — простое решение, от которого мы отталкиваемся. Оно же служит точкой сравнения для проверки успеха. Для RAG типовой baseline — Naive RAG. Для ML — простая модель (logreg/decision tree) или эвристика."
parent_task: BIZ_TO_TZ_900
spawn_conditions: "После 904."
input_requirements:
  - artifact: "task_class"
    contract: Hard
    description: "Класс задачи."
  - artifact: "acceptance_criteria"
    contract: Hard
    description: "Baseline должен быть сопоставим по тем же метрикам."
  - artifact: "architecture_rationale"
    contract: Soft
    description: "Чтобы baseline был осмыслен относительно выбранной архитектуры."
outputs:
  - artifact: "baseline_definition"
    format: "JSON {description: str, expected_limitations: [str], comparison_metrics: [str]}"
    description: "Описание baseline + его ожидаемые ограничения + метрики, по которым сравниваем."
execution_type: LLM
prompt_template_hint: "Baseline должен быть проще основного решения и реализуем за 10-20% времени от всего PoV. Для RAG: Naive RAG с дефолтным embedder и без reranking. Для ML: logreg или одно решающее дерево. Зафиксируй, чего именно baseline НЕ умеет — это мотивирует ценность основного решения."
constraints:
  - "Baseline проще основного решения"
  - "comparison_metrics — подмножество acceptance_criteria.measurable"
escalation_conditions:
  - "Невозможно предложить baseline, который измеряется по тем же критериям — эскалация: возможно, критерии приёмки переусложнены для PoV"
outputs_aggregate:
  - artifact: "architectural_approach"
    format: "JSON"
    description: "Композитный выход блока 900: task_class + architecture_choice + rationale + baseline."
```

---

### 2.10. Блок A00 — Сборка документа ТЗ

```yaml
task_id: BIZ_TO_TZ_A00
name: "Сборка документа ТЗ"
type: Composite
description: "Контейнер для посекционного написания ТЗ и последующей компиляции. Каждая секция пишется отдельным LLM-вызовом, что изолирует ошибки и упрощает ревью."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После 900."
input_requirements:
  - artifact: "need_model"
    contract: Hard
    description: "Потребность."
  - artifact: "data_specification"
    contract: Hard
    description: "Данные."
  - artifact: "formalized_requirements"
    contract: Hard
    description: "Требования."
  - artifact: "architectural_approach"
    contract: Hard
    description: "Архитектура."
outputs:
  - artifact: "draft_specification_document"
    format: "Markdown"
    description: "Черновик ТЗ — единый документ."
possible_children:
  - task_id: BIZ_TO_TZ_A01
    condition: "Всегда (параллельно с A02-A06)"
  - task_id: BIZ_TO_TZ_A02
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_A03
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_A04
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_A05
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_A06
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_A07
    condition: "После A01-A06"
```

Шаблоны A01-A06 следуют одной схеме: каждая задача — Executable, пишет один раздел ТЗ. Чтобы не дублировать YAML 6 раз, ниже приведён общий паттерн и отличия для каждого раздела.

**Общий паттерн (A01-A06):**

```yaml
task_id: BIZ_TO_TZ_A0X
type: Executable
parent_task: BIZ_TO_TZ_A00
spawn_conditions: "Параллельно с остальными секциями."
execution_type: LLM
constraints:
  - "Писать ТОЛЬКО указанный раздел — не затрагивать смежные"
  - "Опираться только на артефакты из input_requirements — не изобретать факты"
  - "Каждое утверждение, которое можно трассировать к конкретному требованию/ответу — обязательно с явной ссылкой (FR-XXX, QA-XXX)"
  - "Формат Markdown с заголовком 2 уровня"
  - "Ограничение длины — 500-1500 слов на раздел"
escalation_conditions:
  - "Hard-зависимость отсутствует или пустая (например, раздел 'Данные', а data_specification.data_sources_inventory пуст) — эскалация с диагностикой (как такое допустили предыдущие блоки)"
```

**Специфика по разделам:**

```yaml
# A01 — Контекст и потребность
name: "Раздел: Контекст и потребность"
description: "Пишет вводный раздел: кто заказчик, какую проблему решаем, зачем, какой baseline существует сейчас."
input_requirements:
  - artifact: "need_model"
    contract: Hard
  - artifact: "normalized_request"
    contract: Hard
outputs:
  - artifact: "section_context"
    format: "Markdown"
    description: "Раздел '1. Контекст и потребность'."
prompt_template_hint: "Структура: (1) Контекст заказчика, (2) Проблема, (3) Зачем решать (первопричина), (4) Текущий baseline-процесс, (5) Стейкхолдеры."
```

```yaml
# A02 — Данные
name: "Раздел: Данные"
description: "Описывает источники данных, их характеристики, достаточность, решение о синтетике."
input_requirements:
  - artifact: "data_specification"
    contract: Hard
outputs:
  - artifact: "section_data"
    format: "Markdown"
    description: "Раздел '2. Данные'."
prompt_template_hint: "Для каждого источника — таблица: имя, тип, объём, формат, качество, доступ. Отдельно — блок 'Допущения по данным' (включая synthetic_data_decision, если принят)."
```

```yaml
# A03 — Функциональные и нефункциональные требования
name: "Раздел: Требования"
description: "ФТ + НФТ + ограничения, сведённые в читабельные таблицы."
input_requirements:
  - artifact: "formalized_requirements"
    contract: Hard
outputs:
  - artifact: "section_requirements"
    format: "Markdown"
    description: "Раздел '3. Требования' с подразделами ФТ, НФТ, Ограничения."
prompt_template_hint: "Три таблицы: ФТ (ID, формулировка, приоритет, трассировка), НФТ (ID, категория, формулировка, целевое значение, приоритет), Ограничения (ID, тип, формулировка, жёсткость, источник)."
```

```yaml
# A04 — Архитектурный подход
name: "Раздел: Архитектурный подход"
description: "Описание выбранного шаблона + обоснование + baseline."
input_requirements:
  - artifact: "architectural_approach"
    contract: Hard
outputs:
  - artifact: "section_architecture"
    format: "Markdown"
    description: "Раздел '4. Архитектурный подход'."
prompt_template_hint: "Структура: (1) Выбранный подход, (2) Ключевые компоненты, (3) Обоснование выбора с таблицей 'требование → компонент', (4) Baseline, (5) Риски."
```

```yaml
# A05 — Критерии приёмки
name: "Раздел: Критерии приёмки"
description: "Таблица критериев с методами проверки."
input_requirements:
  - artifact: "formalized_requirements"
    contract: Hard
    description: "acceptance_criteria из formalized_requirements."
outputs:
  - artifact: "section_acceptance"
    format: "Markdown"
    description: "Раздел '5. Критерии приёмки'."
prompt_template_hint: "Таблица: ID, критерий, метрика/значение, метод проверки, связанные требования. Плюс краткий абзац о демо-сценарии приёмки."
```

```yaml
# A06 — Ограничения и допущения
name: "Раздел: Ограничения и допущения"
description: "Явные допущения, сделанные при составлении ТЗ, и границы scope."
input_requirements:
  - artifact: "formalized_requirements"
    contract: Hard
  - artifact: "data_specification"
    contract: Hard
  - artifact: "feasibility_verdict"
    contract: Soft
    description: "Caveats из feasibility, если были."
outputs:
  - artifact: "section_assumptions"
    format: "Markdown"
    description: "Раздел '6. Ограничения и допущения'."
prompt_template_hint: "Раздел фиксирует: (1) что вне scope PoV (для чего оставляем на следующий этап), (2) ключевые допущения, на которых держится решение, (3) caveats из feasibility, (4) риски, не перенесённые в архитектурный раздел."
```

```yaml
task_id: BIZ_TO_TZ_A07
name: "Сборка финального документа"
type: Executable
description: "Соединяет разделы, добавляет титульный блок, оглавление, сквозную нумерацию, ссылки между разделами. Готовит черновик ТЗ к валидации."
parent_task: BIZ_TO_TZ_A00
spawn_conditions: "После A01-A06."
input_requirements:
  - artifact: "section_context"
    contract: Hard
    description: "Раздел 1."
  - artifact: "section_data"
    contract: Hard
    description: "Раздел 2."
  - artifact: "section_requirements"
    contract: Hard
    description: "Раздел 3."
  - artifact: "section_architecture"
    contract: Hard
    description: "Раздел 4."
  - artifact: "section_acceptance"
    contract: Hard
    description: "Раздел 5."
  - artifact: "section_assumptions"
    contract: Hard
    description: "Раздел 6."
outputs:
  - artifact: "draft_specification_document"
    format: "Markdown"
    description: "Единый черновик ТЗ с оглавлением."
execution_type: Tool
prompt_template_hint: "Детерминированный скрипт: объединить Markdown-секции в указанном порядке, сгенерировать оглавление по заголовкам, добавить титульный блок (название проекта, дата, версия, статус: Draft)."
constraints:
  - "Без LLM-вызова — чисто механическая сборка"
  - "Версия — автоматически v0.1 для первого прогона, инкрементируется при повторной сборке после комментариев заказчика"
escalation_conditions:
  - "Любая секция отсутствует или пуста — эскалация с диагностикой, какая именно задача A0X провалилась"
```

---

### 2.11. Блок B00 — Валидация и согласование ТЗ

```yaml
task_id: BIZ_TO_TZ_B00
name: "Валидация и согласование ТЗ"
type: Composite
description: "Финальная проверка черновика (внутренняя + внешнее согласование с заказчиком) и фиксация утверждённой версии."
parent_task: BIZ_TO_TZ_000
spawn_conditions: "После A00."
input_requirements:
  - artifact: "draft_specification_document"
    contract: Hard
    description: "Черновик ТЗ."
outputs:
  - artifact: "approved_technical_specification"
    format: "Markdown + JSON-метаданные"
    description: "Согласованное ТЗ — выход гейта."
possible_children:
  - task_id: BIZ_TO_TZ_B01
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_B02
    condition: "Всегда (параллельно с B01)"
  - task_id: BIZ_TO_TZ_B03
    condition: "Всегда"
  - task_id: BIZ_TO_TZ_B04
    condition: "После B01, B02, B03 с успехом"
  - task_id: BIZ_TO_TZ_B05
    condition: "После B04, если получены комментарии"
  - task_id: BIZ_TO_TZ_B06
    condition: "После B05 (или сразу после B04, если комментариев нет)"
```

```yaml
task_id: BIZ_TO_TZ_B01
name: "Проверка полноты по чеклисту ТЗ"
type: Executable
description: "Формальная проверка: все ли обязательные разделы ТЗ присутствуют и непустые? все ли must-требования имеют критерии приёмки? все ли источники данных описаны?"
parent_task: BIZ_TO_TZ_B00
spawn_conditions: "Первой в блоке B00."
input_requirements:
  - artifact: "draft_specification_document"
    contract: Hard
    description: "Черновик ТЗ."
  - artifact: "tz_completeness_checklist"
    contract: Hard
    description: "Чеклист обязательных разделов и полей ТЗ из Template Registry."
outputs:
  - artifact: "completeness_report"
    format: "JSON {is_complete: bool, missing_items: [{item, severity}]}"
    description: "Отчёт о полноте."
execution_type: Tool
prompt_template_hint: "Детерминированная проверка: пройтись по чеклисту, для каждого пункта проверить наличие и непустоту в draft_specification_document."
constraints:
  - "Без LLM — чисто формальная сверка"
escalation_conditions:
  - "is_complete=false с severity=critical — задача завершается Failed, порождается перепланирование соответствующего блока (например, при отсутствии критериев приёмки → re-spawn 804, предыдущие артефакты маркируются Obsolete)"
```

```yaml
task_id: BIZ_TO_TZ_B02
name: "Проверка внутренней непротиворечивости"
type: Executable
description: "Ищет противоречия ВНУТРИ документа: ФТ противоречит ограничению, критерий приёмки не соответствует НФТ, выбранный архитектурный подход не поддерживает заявленные требования."
parent_task: BIZ_TO_TZ_B00
spawn_conditions: "Параллельно с B01, B03."
input_requirements:
  - artifact: "draft_specification_document"
    contract: Hard
    description: "Черновик ТЗ."
outputs:
  - artifact: "consistency_report"
    format: "JSON {is_consistent: bool, contradictions: [{description, involved_items, severity}]}"
    description: "Отчёт о противоречиях."
execution_type: LLM
prompt_template_hint: "Проверь: (1) каждое must-ФТ покрыто критерием приёмки? (2) выбранная архитектура способна удовлетворить НФТ? (3) ограничения не противоречат ФТ? (4) данные достаточны для архитектуры? severity: blocker / warning."
constraints:
  - "Каждое противоречие описано с упоминанием конкретных ID (FR-XXX, NFR-XXX, CON-XXX)"
  - "Если блок 805 уже обнаружил конфликты — перепроверить, что они явно адресованы в документе"
escalation_conditions:
  - "Обнаружены противоречия severity=blocker — задача Failed, перепланирование (обычно перезапуск 805 и/или 900 с пометкой предыдущих артефактов Obsolete)"
```

```yaml
task_id: BIZ_TO_TZ_B03
name: "Проверка трассируемости к исходному запросу"
type: Executable
description: "Для каждого ключевого элемента ТЗ (ФТ, НФТ, ограничение, критерий) проверяет, что он трассируется либо к исходному запросу, либо к ответу заказчика. Цель — гарантировать, что мы не придумали требований от себя."
parent_task: BIZ_TO_TZ_B00
spawn_conditions: "Параллельно с B01, B02."
input_requirements:
  - artifact: "draft_specification_document"
    contract: Hard
    description: "Черновик ТЗ."
  - artifact: "normalized_request"
    contract: Hard
    description: "Исходный запрос."
  - artifact: "clarifications"
    contract: Hard
    description: "Все ответы заказчика."
outputs:
  - artifact: "traceability_report"
    format: "JSON {traced: int, untraced: [{item_id, item_text}], coverage: float}"
    description: "Отчёт о трассируемости."
execution_type: LLM
prompt_template_hint: "Для каждого ФТ/НФТ/Ограничения/Критерия из ТЗ найди первоисточник: цитата из normalized_request или QA-пара из clarifications. Если найти нельзя — пункт в untraced."
constraints:
  - "Coverage = traced / (traced + untraced)"
  - "Приемлемый порог — coverage >= 0.9 (допустимы 10% элементов, добавленных как типовые для поддомена)"
escalation_conditions:
  - "coverage < 0.9 — эскалация: слишком много 'выдуманных' требований, нужно ревью. Возможна декомпозиция на task 602 для подтверждения заказчиком сомнительных пунктов."
```

```yaml
task_id: BIZ_TO_TZ_B04
name: "Отправка ТЗ заказчику на согласование"
type: Executable
description: "Передаёт черновик ТЗ заказчику через Interruption Gateway. Ожидает один из трёх ответов: approved / approved_with_comments / rejected_with_revision_request."
parent_task: BIZ_TO_TZ_B00
spawn_conditions: "После B01, B02, B03 с успешными результатами (is_complete=true, is_consistent=true, coverage>=0.9)."
input_requirements:
  - artifact: "draft_specification_document"
    contract: Hard
    description: "Черновик ТЗ."
  - artifact: "completeness_report"
    contract: Hard
    description: "Подтверждение внутренней проверки."
  - artifact: "consistency_report"
    contract: Hard
    description: "Подтверждение."
  - artifact: "traceability_report"
    contract: Hard
    description: "Подтверждение."
outputs:
  - artifact: "client_decision"
    format: "JSON {decision: enum[approved, approved_with_comments, rejected], comments_raw: str, timestamp}"
    description: "Решение заказчика + сырые комментарии."
execution_type: Human
prompt_template_hint: "Отправить через Interruption Gateway с сопроводительным письмом (кратко: что сделано, где критичные допущения, куда смотреть особенно внимательно). Ждать ответа."
constraints:
  - "В сопроводительном письме явно подсветить любые допущения (например, решение о синтетике)"
  - "Таймаут ожидания — 5 рабочих дней"
escalation_conditions:
  - "Таймаут ожидания превышен — эскалация к менеджеру проекта"
  - "decision=rejected — задача Failed (не завершается ошибкой — это ожидаемый исход, но требует эскалации к менеджеру: слишком глубокая переделка)"
```

```yaml
task_id: BIZ_TO_TZ_B05
name: "Парсинг и классификация комментариев заказчика"
type: Executable
description: "Если заказчик вернул ТЗ с комментариями, разбирает их: к какому разделу относится каждый комментарий, что конкретно требуется изменить, насколько это критично."
parent_task: BIZ_TO_TZ_B00
spawn_conditions: "После B04 с decision ∈ {approved_with_comments, rejected}."
input_requirements:
  - artifact: "client_decision"
    contract: Hard
    description: "Решение + комментарии."
  - artifact: "draft_specification_document"
    contract: Hard
    description: "Черновик, к которому привязаны комментарии."
outputs:
  - artifact: "classified_comments"
    format: "JSON [{comment_text, target_section, change_type: enum[addition, modification, removal, clarification], criticality, triggered_task_ids: [str]}]"
    description: "Классифицированные комментарии + какие задачи нужно перезапустить."
execution_type: LLM
prompt_template_hint: "Для каждого комментария: (1) к какому разделу/ID он относится, (2) что надо сделать (изменить/добавить/удалить/уточнить), (3) triggered_task_ids — какие задачи из гейта надо перезапустить (например, комментарий по данным → re-spawn 700; комментарий по критерию приёмки → re-spawn 804)."
constraints:
  - "Каждый комментарий привязан к конкретному элементу ТЗ"
  - "triggered_task_ids опирается на маппинг 'раздел ТЗ → задачи, которые его порождают' (хранится в Template Registry)"
escalation_conditions:
  - "Комментарий не удаётся локализовать (непонятно, к чему относится) — порождается 602 с уточняющим вопросом к заказчику"
  - "Комментарии требуют фундаментального пересмотра scope — эскалация: возможно, нужно вернуться на уровень Stage-Gate Manager"
```

```yaml
task_id: BIZ_TO_TZ_B06
name: "Финальная фиксация согласованного ТЗ"
type: Executable
description: "Помечает утверждённую версию ТЗ как approved, фиксирует её в State & Memory Broker с тегом stage_1_output, закрывает задачу BIZ_TO_TZ_000 и сигнализирует Stage-Gate Manager о готовности к переходу."
parent_task: BIZ_TO_TZ_B00
spawn_conditions: "После B05, если triggered_task_ids обработаны и получено новое approved, ИЛИ сразу после B04 с decision=approved."
input_requirements:
  - artifact: "draft_specification_document"
    contract: Hard
    description: "Черновик (возможно, переработанный после комментариев)."
  - artifact: "client_decision"
    contract: Hard
    description: "Финальное approved-решение."
outputs:
  - artifact: "approved_technical_specification"
    format: "Markdown + JSON-метаданные {version, approved_at, approved_by, trace_to_request_id}"
    description: "Финальный артефакт гейта."
execution_type: Tool
prompt_template_hint: "Детерминированный скрипт: пометить версию как v1.0, сохранить в S3 под тегом stage_1_output, записать метаданные в PostgreSQL, отправить событие 'stage_1_completed' в Stage-Gate Manager."
constraints:
  - "Без LLM"
  - "Версия инкрементируется с v0.N до v1.0 при первом approve"
escalation_conditions:
  - "Техническая ошибка при сохранении — эскалация к разработчику платформы (не бизнес-проблема)"
```

---

## Часть 3. Таблица артефактов гейта

Таблица содержит артефакты, циркулирующие внутри Stage Gate 1. Внешние артефакты Template Registry (чеклисты, каталоги паттернов, библиотеки вопросов) перечислены отдельно в конце таблицы — они не создаются внутри гейта, а предоставляются платформой.

### 3.1. Артефакты, создаваемые и потребляемые внутри гейта

| # | Артефакт | Формат | Создаётся задачей | Потребляется задачами |
|---|----------|--------|-------------------|------------------------|
| 1 | `raw_business_request` | Текст + вложения | Внешний вход (Система общения с пользователем) | BIZ_TO_TZ_000, BIZ_TO_TZ_101 |
| 2 | `parsed_request` | JSON | BIZ_TO_TZ_101 | BIZ_TO_TZ_102, BIZ_TO_TZ_103 |
| 3 | `request_completeness_verdict` | JSON | BIZ_TO_TZ_102 | (внутренний — входит в normalized_request) |
| 4 | `subdomain_classification` | JSON | BIZ_TO_TZ_103 | BIZ_TO_TZ_201, 302, 303, 402, 405, 501, 502, 701, 702, 702_A, 702_B, 704, 901, 902/903 (через 900) |
| 5 | `normalized_request` | JSON | BIZ_TO_TZ_100 (агрегирует 101-103) | BIZ_TO_TZ_200, 300, 400, 301, 302, 303, 304, 401-405, B03 |
| 6 | `subdomain_support_check` | JSON | BIZ_TO_TZ_201 | BIZ_TO_TZ_203 |
| 7 | `unfeasibility_flags` | JSON | BIZ_TO_TZ_202 | BIZ_TO_TZ_203 |
| 8 | `feasibility_verdict` | JSON | BIZ_TO_TZ_203 (и блок 200) | BIZ_TO_TZ_000, A06 |
| 9 | `declared_goal` | JSON | BIZ_TO_TZ_301 | BIZ_TO_TZ_302, 303, 304, 305 |
| 10 | `root_cause_hypotheses` | JSON | BIZ_TO_TZ_302 | BIZ_TO_TZ_305 |
| 11 | `baseline_hypotheses` | JSON | BIZ_TO_TZ_303 | BIZ_TO_TZ_305 |
| 12 | `stakeholders_map` | JSON | BIZ_TO_TZ_304 | BIZ_TO_TZ_305 |
| 13 | `need_model` | JSON | BIZ_TO_TZ_305 (и блок 300) | BIZ_TO_TZ_500, 600, 801, 900, 901, A01 |
| 14 | `data_mentions` | JSON | BIZ_TO_TZ_401 | BIZ_TO_TZ_501, 701 |
| 15 | `metric_mentions` | JSON | BIZ_TO_TZ_402 | BIZ_TO_TZ_501, 802, 804 |
| 16 | `constraint_mentions` | JSON | BIZ_TO_TZ_403 | BIZ_TO_TZ_501, 702_C, 802, 803 |
| 17 | `acceptance_mentions` | JSON | BIZ_TO_TZ_404 | BIZ_TO_TZ_501, 804 |
| 18 | `integration_mentions` | JSON | BIZ_TO_TZ_405 | BIZ_TO_TZ_501, 803 |
| 19 | `extracted_declarations` | JSON | BIZ_TO_TZ_400 (агрегирует 401-405) | BIZ_TO_TZ_500, 501, 605, 800 |
| 20 | `raw_gaps` | JSON | BIZ_TO_TZ_501 | BIZ_TO_TZ_502 |
| 21 | `prioritized_gaps` | JSON | BIZ_TO_TZ_502 | BIZ_TO_TZ_503 |
| 22 | `gap_list` | JSON | BIZ_TO_TZ_503 (и блок 500) | BIZ_TO_TZ_600, 601, 602 |
| 23 | `client_questionnaire` | Markdown + JSON-схема | BIZ_TO_TZ_601 | BIZ_TO_TZ_603, 604 |
| 24 | `point_question` | JSON | BIZ_TO_TZ_602 | BIZ_TO_TZ_603, 604 |
| 25 | `client_response_raw` | Текст/структура | BIZ_TO_TZ_603 (Human, через Interruption Gateway) | BIZ_TO_TZ_604 |
| 26 | `parsed_clarifications` | JSON | BIZ_TO_TZ_604 | BIZ_TO_TZ_605 |
| 27 | `clarifications` | JSON | BIZ_TO_TZ_605 (и блок 600) | BIZ_TO_TZ_700, 701, 801, 802, 803, B03 |
| 28 | `data_sources_inventory` | JSON | BIZ_TO_TZ_701 | BIZ_TO_TZ_702, 702_A, 702_B, 702_C, 703, 704 |
| 29 | `source_specific_questions` | JSON | BIZ_TO_TZ_702 (Dynamic, агрегирует 702_A/B/C) | BIZ_TO_TZ_600 (повторный цикл при необходимости) |
| 30 | `format_volume_questions` | JSON | BIZ_TO_TZ_702_A | BIZ_TO_TZ_702 |
| 31 | `quality_labeling_questions` | JSON | BIZ_TO_TZ_702_B | BIZ_TO_TZ_702 |
| 32 | `legal_access_questions` | JSON | BIZ_TO_TZ_702_C | BIZ_TO_TZ_702 |
| 33 | `data_sufficiency_verdict` | JSON | BIZ_TO_TZ_703 | BIZ_TO_TZ_704 |
| 34 | `synthetic_data_decision` | JSON | BIZ_TO_TZ_704 | (входит в data_specification) |
| 35 | `data_specification` | JSON | BIZ_TO_TZ_700 (агрегирует 701-704) | BIZ_TO_TZ_800, 803, 900, 902, 903, A02 |
| 36 | `functional_requirements` | JSON | BIZ_TO_TZ_801 | BIZ_TO_TZ_804, 805, 901 |
| 37 | `non_functional_requirements` | JSON | BIZ_TO_TZ_802 | BIZ_TO_TZ_804, 805 |
| 38 | `project_constraints` | JSON | BIZ_TO_TZ_803 | BIZ_TO_TZ_805 |
| 39 | `acceptance_criteria` | JSON | BIZ_TO_TZ_804 | BIZ_TO_TZ_805, 905 |
| 40 | `formalized_requirements` | JSON | BIZ_TO_TZ_805 (и блок 800) | BIZ_TO_TZ_900, 902, 903, 904, A03, A05, A06 |
| 41 | `task_class` | JSON | BIZ_TO_TZ_901 | BIZ_TO_TZ_902, 903, 905 |
| 42 | `rag_architecture_choice` | JSON | BIZ_TO_TZ_902 | BIZ_TO_TZ_904 |
| 43 | `ml_architecture_choice` | JSON | BIZ_TO_TZ_903 | BIZ_TO_TZ_904 |
| 44 | `architecture_rationale` | Markdown | BIZ_TO_TZ_904 | BIZ_TO_TZ_905, A04 |
| 45 | `baseline_definition` | JSON | BIZ_TO_TZ_905 | (входит в architectural_approach) |
| 46 | `architectural_approach` | JSON | BIZ_TO_TZ_900 (агрегирует 901-905) | BIZ_TO_TZ_A00, A04 |
| 47 | `section_context` | Markdown | BIZ_TO_TZ_A01 | BIZ_TO_TZ_A07 |
| 48 | `section_data` | Markdown | BIZ_TO_TZ_A02 | BIZ_TO_TZ_A07 |
| 49 | `section_requirements` | Markdown | BIZ_TO_TZ_A03 | BIZ_TO_TZ_A07 |
| 50 | `section_architecture` | Markdown | BIZ_TO_TZ_A04 | BIZ_TO_TZ_A07 |
| 51 | `section_acceptance` | Markdown | BIZ_TO_TZ_A05 | BIZ_TO_TZ_A07 |
| 52 | `section_assumptions` | Markdown | BIZ_TO_TZ_A06 | BIZ_TO_TZ_A07 |
| 53 | `draft_specification_document` | Markdown | BIZ_TO_TZ_A07 (и блок A00) | BIZ_TO_TZ_B01, B02, B03, B04, B05, B06 |
| 54 | `completeness_report` | JSON | BIZ_TO_TZ_B01 | BIZ_TO_TZ_B04 |
| 55 | `consistency_report` | JSON | BIZ_TO_TZ_B02 | BIZ_TO_TZ_B04 |
| 56 | `traceability_report` | JSON | BIZ_TO_TZ_B03 | BIZ_TO_TZ_B04 |
| 57 | `client_decision` | JSON | BIZ_TO_TZ_B04 (Human) | BIZ_TO_TZ_B05, B06 |
| 58 | `classified_comments` | JSON | BIZ_TO_TZ_B05 | BIZ_TO_TZ_B06 + триггер re-spawn различных задач |
| 59 | `approved_technical_specification` | Markdown + JSON-метаданные | BIZ_TO_TZ_B06 (и корень 000) | **Выход гейта → вход Stage Gate 2** |

### 3.2. Внешние артефакты из Template Registry (предоставляются платформой)

| Артефакт | Описание | Используется задачами |
|----------|----------|-----------------------|
| `supported_subdomains_registry` | Реестр поддерживаемых поддоменов (RAG, ML) с признаками | BIZ_TO_TZ_103, 201 |
| `unfeasibility_patterns_catalog` | Каталог паттернов нерешаемости | BIZ_TO_TZ_202 |
| `subdomain_checklist_registry` | Чеклисты обязательных полей для RAG/ML | BIZ_TO_TZ_501 |
| `question_templates_library` | Библиотека типовых формулировок вопросов | BIZ_TO_TZ_601 |
| `data_sufficiency_heuristics` | Эвристики достаточности данных | BIZ_TO_TZ_703 |
| `functional_requirements_templates` | Шаблонные ФТ для RAG/ML | BIZ_TO_TZ_801 |
| `non_functional_requirements_templates` | Шаблонные НФТ | BIZ_TO_TZ_802 |
| `task_class_taxonomy` | Таксономия классов задач внутри поддоменов | BIZ_TO_TZ_901 |
| `rag_architecture_patterns` | Библиотека RAG-шаблонов (Naive/Advanced/Hierarchical/Agentic) | BIZ_TO_TZ_902 |
| `ml_architecture_patterns` | Библиотека ML-шаблонов | BIZ_TO_TZ_903 |
| `tz_completeness_checklist` | Чеклист полноты ТЗ | BIZ_TO_TZ_B01 |

---

## Часть 4. Сверка с чеклистом покрытия

Для каждой зоны покрытия из исходного ТЗ — указаны закрывающие её задачи.

| Зона покрытия | Закрывается задачами |
|---------------|----------------------|
| 1. Приём и первичная обработка запроса | BIZ_TO_TZ_100, 101, 102, 103 |
| 2. Понимание потребности | BIZ_TO_TZ_300, 301, 302, 303, 304, 305 |
| 3. Сбор недостающей информации | BIZ_TO_TZ_500 (формирование gap), 600, 601, 602, 603, 604, 605 |
| 4. Работа с данными | BIZ_TO_TZ_401, 700, 701, 702, 702_A/B/C, 703, 704 |
| 5. Определение требований | BIZ_TO_TZ_402, 403, 404, 405, 800, 801, 802, 803, 804, 805 |
| 6. Архитектурный анализ | BIZ_TO_TZ_900, 901, 902, 903, 904, 905 |
| 7. Формирование документа ТЗ | BIZ_TO_TZ_A00, A01-A07 |
| 8. Валидация ТЗ | BIZ_TO_TZ_B00, B01, B02, B03, B04, B05, B06 |

Дополнительно — **ранняя оценка выполнимости** (блок 200) реализует принцип из `PoV.md`: «как определить, что задача невозможна, максимально рано». Это превентивный механизм, не упомянутый в зонах покрытия явно, но вытекающий из раздела «Потенциальные проблемы».

---

## Часть 5. Соответствие принципам PoV.md

| Принцип | Как отражён в спецификации |
|---------|---------------------------|
| **Фокус на потребностях** | Блок 300 (Why-анализ) обязателен — задачи не переходят к требованиям до формирования need_model. Блок 600 снимает `blocking`-пробелы до архитектуры. |
| **Прозрачность** | Каждый артефакт имеет трассировку: ФТ/НФТ имеют поле `source`, задача B03 проверяет coverage трассируемости к исходному запросу (≥ 0.9). Каждое извлечение упоминаний (400) требует `quote`. |
| **Масштабируемость** | Архитектурные решения (902, 903, 901) параметризованы через Template Registry (`rag_architecture_patterns`, `task_class_taxonomy`). Добавление поддомена = добавление записей в registry + новый класс в 103, без изменения шаблонов. |
| **Воспроизводимость** | НФТ по воспроизводимости — обязательная позиция для PoV (802). State & Memory Broker хранит все артефакты + версии. Микро-декомпозиция (один LLM-вызов = одно микро-решение) снижает вариативность между прогонами. |
| **Самоконтроль** | Блок B00 полностью автономен в валидации (B01-B03). Механизмы самокоррекции встроены в escalation_conditions — эскалация происходит только после исчерпания лимитов (например, 2 итерации уточнений в 605). Dynamic-задачи (600, 702, 900) принимают решения без человека. |

