# Реестр (`templates/`)

Декларативный слой системы: цели, шаблоны задач, контракты артефактов,
доменные паки, методологические паки, проверки качества и словари.
Каждый файл YAML — один объект одного `kind`.

> **Источник истины контрактов:** [`specs/`](../specs/). Этот README —
> карта местности для быстрой навигации, не нормативный документ.

## Структура

```
templates/
├── objectives/        # верхняя цель проекта (что значит «готово»)
├── tasks/             # шаблоны задач (composite + leaf)
│   ├── common/        # core pipeline
│   ├── ml/, security/, integration/, frontend/  # доменные расширения
├── artifacts/         # контракты артефактов (форма результата задачи)
├── domains/           # доменные паки — расширения через slot'ы
├── methodologies/     # методологии рассуждения (как мы думаем)
├── gates/             # quality gates — точки внешнего согласования
├── vocabularies/      # общие словари (роли, домены, dimension'ы)
└── stub_fixtures/     # фикстуры для stub-провайдера
```

## Как добавить расширение

### Новый доменный пак (быстрый путь)

1. **Domain pack:** `templates/domains/<area>/<name>.yaml`
   ```yaml
   kind: domain_pack
   id: healthcare.clinical_data
   version: 1.0.0
   domain: healthcare
   detect:
     signals: [медицин, клинич, диагноз]
   contributes:
     - to: solution.evaluation        # slot из core composite
       add:
         - id: clinical_data
           task: healthcare.clinical_data_assessment@1.0.0
   ```

2. **Задача:** `templates/tasks/healthcare/clinical_data_assessment.yaml`
   ```yaml
   kind: task_template
   id: healthcare.clinical_data_assessment
   type: leaf
   executor: llm
   requires:
     state: [business_request]
     artifacts:
       required: [common.normalized_request@1.0.0]
   produces:
     artifact: healthcare.clinical_data_assessment@1.0.0
   ```

3. **Контракт артефакта:** `templates/artifacts/healthcare/clinical_data_assessment.yaml`
   ```yaml
   kind: artifact_contract
   id: healthcare.clinical_data_assessment
   version: 1.0.0
   title: Оценка клинических данных
   # Этап 7.5: если схема не описана — честно помечаем unstructured.
   unstructured: true
   schema:
     type: object
     additionalProperties: true
   ```

4. **Словарь:** добавить запись в `templates/vocabularies/artifact_roles.yaml`.

Финальная задача (`requirements_spec_generation`) **не правится** —
она использует `collect_optional.from_active_domain_packs: true`
(Этап 7.3), артефакты нового домена попадают в контекст автоматически.

### Новая методология

`templates/methodologies/<id>.yaml` — стадии, правила, complexity overrides.
Активная методология не зависит от шаблонов задач (A14): wrapper накладывается
на каждое исполнение leaf-задачи извне.

## Что точно НЕ делать

| Не делай | Почему |
|---|---|
| Описывать стадии рассуждения внутри `task_template` | R8/TS9: это работа `methodology_pack` |
| Использовать `recipe` / `recipe_fragment` | Историческая терминология, удалена |
| Псевдо-контракт (`additionalProperties: true` без обязательных полей) **без** `unstructured: true` | Этап 7.5: либо контракт реальный, либо честно ничего |
| Перечислять руками optional-артефакты доменов в финальной задаче | Этап 7.3: `collect_optional.from_active_domain_packs: true` |

## Версии и совместимость

* Опубликованные ссылки — всегда `<id>@<semver>` (без `latest`).
* Контракт не меняется задним числом: правка → новая `version`.
* Тесты `test_foundation.py::test_registry_validation_passes_for_task_graph_corpus`
  проверяют целостность реестра при каждом коммите.

## См. также

* [`specs/02_registry_dsl.md`](../specs/02_registry_dsl.md) — нормативная DSL.
* [`specs/04_task_template_semantics.md`](../specs/04_task_template_semantics.md) — семантика шаблонов.
* [`specs/09_domain_packs.md`](../specs/09_domain_packs.md) — доменные паки.
* [`ARCHITECTURE.md`](../ARCHITECTURE.md) — общая карта.
