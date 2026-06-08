# Корректный граф задач гейта «Реализация» (harness)

Статус: проект (design), решения согласованы 2026-06-08. К реализации.

Контекст: гейт `implementation.realize` исполняет код автономными агентами
(harness). Каркасный граф уже есть (`realize_system` → веер
`component_implementation` → `realization_index`), но он некорректен: веер идёт
параллельно в одном общем томе (гонки + порядок-агностично), нет узла-каркаса,
нет интеграции и системного DoD. Этот документ фиксирует корректную модель.

## Согласованные решения

- **Порядок сборки компонентов** — волнами по зависимостям (топо-сортировка по
  `consumed_interfaces[].component`), не плоский параллельный веер.
- **Охват** — полный: добавляем узлы каркаса, интеграции, системной проверки;
  веер пишет в подкаталоги общего тома; `done_when` цели — системный артефакт.
- **Механизм волн (v1)** — топо-приоритет инстансов веера + серийная сборочная
  группа (общий том исполняется конкуррентно=1). Порядок корректен, гонок нет,
  правка планировщика минимальна. Параллель внутри волны — отложена (v2:
  поинстансные зависимости admissibility).

## Как выделять сущности задач

Узел оправдан, когда у него: самодостаточный контракт-вход + машинный DoD; один
владелец-умение; ограниченный радиус взрыва; собираемость/проверяемость в
изоляции против контрактов. По этим критериям нужно пять типов сущностей —
три системного уровня (1×) и одна веерная (N×):

| Сущность | Кратность | Исполнитель | Назначение |
|---|---|---|---|
| Каркас (scaffold) | 1× | harness/bundle | Репо-скелет, стек, манифесты, docker-compose + Dockerfile, общие контракты/типы, конфиг линта/тестов. Пишет в корень общего тома. |
| Компонент | N× веер | harness/bundle | Код одного компонента в `/work/<id>/`, против контрактов каркаса. |
| Интеграция | 1× | harness/bundle | Связать компоненты, общий entrypoint, wiring. |
| Системная проверка | 1× | harness/gates | `docker build`/`compose up` + интеграционные/e2e тесты. Системный DoD. |
| Индекс реализации | 1× | llm | Манифест: что собрано, как запустить, пробелы. |

Гранулярность: компонент из `component_model` — верный размер. Слишком большой
компонент НЕ дробим на узлы, а раскрываем в `build_spec.build_steps` (агент идёт
по ним в одном прогоне). Граф держим плоским.

## Порядок и узлы

```
implementation.realize (objective)
  done_when: implementation.system_check
  └─ realize_system (composite)
       1. scaffold              leaf · harness · bundle        (priority 520)
       2. component_impl_fanout fan_out → component_implementation
                                 (harness · bundle · волнами, подкаталоги)  (400)
       3. system_integration    leaf · harness · bundle        (300)
       4. system_verification   leaf · harness · gates         (200)
       5. realization_index     leaf · llm                     (100)
```

Порядок между узлами composite — через `requires.artifacts` + `planning.priority`:
- scaffold: requires `component_model`, `implementation.build_plan`,
  `architecture.deployment_map`; produces `implementation.project_scaffold`.
- component_implementation: requires `implementation.project_scaffold`,
  `implementation.component_build_spec`, `architecture.component_model`.
- system_integration: requires scaffold + `implementation.component_implementation`
  (веер завершён → обёртка completed).
- system_verification: requires `implementation.system_integration`;
  produces `implementation.system_check`.
- realization_index: requires `implementation.system_check`.

Волны внутри веера: инстансу присваивается приоритет по топо-рангу (компоненты
без зависимостей — ранг 0, далее по DAG `consumed_interfaces`). Сборочная группа
(дети одного веера, общий том по `parent_task_id`) исполняется серийно
(конкуррентно=1), поэтому порядок = топо-ранг.

## Что ещё учтено

- **Гонки/откат** — общие файлы пишут только scaffold/integration; компонент —
  свой подкаталог `/work/<id>/`. Harvest бандла компонента берёт его подкаталог,
  а не весь том.
- **Контракт как шов** — каркас материализует интерфейс-контракты, компоненты
  строятся против них.
- **DoD двух уровней** — per-компонентный (compile/unit) + системный
  (`docker build`/`compose up`/интеграция). `done_when` требует system_check.
- **Сервисы в docker** — контейнеризация в каркасе (compose/Dockerfile) +
  системной проверке (`docker build`/`up` как гейт). Совпадает с host-режимом
  harness (claude на хосте, сервисы в docker).
- **Детерминизм** — stub-harness отдаёт связную фикстуру-систему.
- **Обратное ограничение на «Архитектуру»** — `component_model` обязан нести
  `consumed_interfaces` (DAG), владельца-умение, modules. Уже есть в структурной
  схеме (`artifact_contracts.py`).

## Фазы реализации

- **RG-A** — шаблоны + контракты (scaffold/integration/verification), правки
  realize_system / component_implementation / objective. `registry validate`.
- **RG-B** — топо-приоритет инстансов веера + серийная сборочная группа.
- **RG-C** — скоуп бандла по подкаталогу компонента.
- **RG-D** — системный DoD + сквозной тест на stub + полный прогон + рестарт.

## Статус реализации (2026-06-08)

Сделано (зелёное, закоммичено; `registry validate` + полный pytest):
- **RG-0** — `build_manifest` (контракт + структурная схема: services с
  зоной/стеком/DAG, межсервисные contracts, conventions-устав, build_order) +
  узел `build_manifest_synthesis`.
- **RG-A** — фазовый граф `manifest → scaffold → веер компонентов (в зоны
  сервисов) → integration → system_verification → realization_index`; контракты
  project_scaffold/system_integration/system_check; каркас материализует
  конституцию (AGENTS/CONVENTIONS/STACK + контракты-как-код + compose);
  `done_when = system_check`; stub-фикстуры (граф зелёный на stub-пути).
- **RG-B** — гейт `sibling_fanout_complete` (интеграция/проверка/сводка ждут
  ПОЛНОГО завершения веера) + топо-приоритет инстансов (волны по зависимостям).
- **RG-D (частично)** — страж собираемости (`capability_owner` обязателен).
  Системный DoD — гейты на `system_verification` (docker build/up + e2e).

Осталось:
- **RG-C (ядро)** — РЕАЛЬНЫЙ harvest бандла из рабочего каталога по подкаталогу
  сервиса. Сейчас bundle-harvest — заглушка-соглашение (`.povgen/out/<role>.files`),
  не сбор дерева кода из песочницы. Это **предусловие реального E2E** (код в
  docker) и требует реализации сбора поддерева workspace + zone-скоупа.
- **RG-E** — scoped + полиморфный fan-out (рекурсия до модуля; рецепт по
  capability_owner). Новая возможность планировщика.
- **RG-F** — рецепты сборки по капабилити (UI: дизайн-док + база стиля; ML:
  протокол оценки; …) через `build_recipe` + полиморфный веер.

Зависимость: реальная генерация кода (claude-harness в docker) дополнительно
требует RG-C + прогона на Docker/claude (среда пользователя), не только stub.
