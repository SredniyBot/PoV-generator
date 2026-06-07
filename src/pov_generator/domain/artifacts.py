"""Артефакты как first-class объекты с явным графом связей.

Этап 1 roadmap (см. ``ARCHITECTURE.md``):

- 1.1: ``reasoning`` и ``methodology_trace`` свёрнуты в **метаинформацию**
  основного артефакта; они больше не отдельные ``ArtifactRecord``.
- 1.3: связи артефактов выделены в :class:`ArtifactRelations` —
  ``parent`` (предыдущая версия), ``inputs`` (lineage по контексту),
  ``children`` (для синтезированных композитных).
- 1.4: артефакт **обязан** хранить ссылки на использованные положения
  слоя A (:attr:`ArtifactMetadata.used_position_ids`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ArtifactFormat = Literal["json", "markdown", "text", "bundle"]
"""Формат хранимого содержимого артефакта."""

LOW_CONFIDENCE_THRESHOLD = 0.45
"""Порог уверенности артефакта. Ниже него артефакт помечается «система не
уверена» и просит подтверждения пользователя — НЕ роняя задачу (зеркально
механизму низкоуверенных решений). Единый источник правды: используется и
доменной моделью (`ArtifactRecord.is_low_confidence`), и валидацией."""

ArtifactKind = Literal["primary", "synthesized", "derived", "input"]
"""Класс артефакта.

``input`` — исходные входные данные проекта (например, текстовый запрос при
создании), а не результат задачи. Не участвует в primary-фильтрах
(skeleton/ключевые артефакты), просто доступен для просмотра.

- ``primary`` — результат leaf-задачи по контракту ``produces.artifact``.
- ``synthesized`` — синтезированный артефакт композитной задачи (объединение
  результатов детей; механика слияния — Этап 5 roadmap).
- ``derived`` — производное представление (markdown-render, summary и т.п.).
"""

ContextItemType = Literal["problem_field", "artifact", "instruction"]


# --- метаинформация артефакта ------------------------------------------------


@dataclass(frozen=True)
class ArtifactMetadata:
    """Метаинформация артефакта.

    Содержит все «как получен» данные одним структурированным объектом,
    а не свободным dict'ом. Поля reasoning и methodology_trace —
    бывшие самостоятельные артефакты (Этап 1.1).

    Инварианты:
        * Если ``methodology_pack_ref`` задан, ``reasoning`` должен
          соответствовать схеме этой методологии (валидируется отдельно).
        * ``overall_confidence`` ∈ [0.0, 1.0], если задан.
        * ``used_position_ids`` — обязательно непустое для primary
          артефактов, опирающихся на положения проекта (см. PS11).
    """

    # --- production provenance ----------------------------------------------

    template_ref: str | None = None
    provider: str | None = None
    model: str | None = None
    complexity: Literal["trivial", "standard", "complex"] | None = None
    methodology_pack_ref: str | None = None
    execution_run_id: str | None = None
    merge_strategy: Literal["structural", "synthetic", "hybrid"] | None = None
    """Этап 5: если артефакт получен merge-задачей, фиксируется выбранная
    стратегия. Используется UI и provenance для отметки «синтез N → 1»."""

    # --- свёрнутые ранее отдельные артефакты (1.1) --------------------------

    reasoning: dict[str, object] = field(default_factory=dict)
    """Reasoning от методологического wrapper'а: стадии, опции, decision и т.д.

    Раньше был отдельным `reasoning_artifact`. Теперь — метаинформация
    primary артефакта. Схема — `reasoning_artifact` активной методологии.
    """

    methodology_trace: dict[str, object] = field(default_factory=dict)
    """Трасса исполнения методологии: пройденные стадии и сработавшие правила.

    Раньше был отдельным `methodology_trace` артефактом. Теперь —
    метаинформация primary артефакта.
    """

    # --- confidence ---------------------------------------------------------

    overall_confidence: float | None = None
    field_confidence: dict[str, float] = field(default_factory=dict)

    # --- ссылки в Layer A (1.4 — обязательно для primary) -------------------

    used_position_ids: tuple[str, ...] = ()
    """Положения слоя A, использованные при создании артефакта.

    Опора для дешёвой выборки положений в контекст потомков и для
    инвалидации зависимых артефактов при оспаривании положения
    (см. ``downstream_closure``).
    """

    # --- v3.5: token usage по стадиям сборки ------------------------------
    #
    # Сколько токенов реально ушло на этот артефакт, с разбивкой по этапам.
    # Ключи стадий — стабильные строковые id; значения — словари с
    # input/output/cache/total. Сумма по стадиям = «полная стоимость» одного
    # артефакта.
    #
    # Используется в UI ArtifactDetail (раздел «Токены»), агрегатах
    # производительности и при отладке («куда уходят токены»). Значения
    # фиксируются на этапе сборки артефакта и не пересчитываются.
    #
    # Ожидаемые стадии:
    #   - `decision_identification` — выявление решений до сборки (1 вызов).
    #   - `primary_generation`  — основная сборка артефакта (1 вызов
    #                              single_call ИЛИ N+1 для per_stage_cot).
    #   - `methodology_stage:<id>` — отдельные стадии per_stage_cot (если есть).
    # При отсутствии данных от провайдера — поле пустое (default {}).
    token_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    # --- free-form расширение ----------------------------------------------

    extras: dict[str, object] = field(default_factory=dict)
    """Расширение для нестандартных полей метаданных без правки контракта."""

    def __post_init__(self) -> None:
        if self.overall_confidence is not None:
            if not 0.0 <= self.overall_confidence <= 1.0:
                raise ValueError(
                    f"overall_confidence must be in [0.0, 1.0], got {self.overall_confidence!r}"
                )


# --- связи артефакта в графе -------------------------------------------------


@dataclass(frozen=True)
class ArtifactRelations:
    """Граф связей артефакта.

    Прямые ссылки:
        * ``parent_artifact_id`` — предыдущая версия (versioning chain).
        * ``input_artifact_ids`` — артефакты, использованные как контекст
          (lineage).
        * ``child_artifact_ids`` — для ``synthesized``: артефакты-источники
          композитного синтеза.

    Обратные ссылки (downstream, «затронутые при изменении») —
    **не хранятся**, вычисляются обратным обходом через индекс.
    """

    parent_artifact_id: str | None = None
    input_artifact_ids: tuple[str, ...] = ()
    child_artifact_ids: tuple[str, ...] = ()


# --- запись артефакта --------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRecord:
    """First-class артефакт проекта.

    Структура (Этап 1 roadmap):
        * содержимое — по ``storage_path`` (валидируется ``artifact_role``);
        * :attr:`metadata` — структурированная метаинформация;
        * :attr:`relations` — явный граф связей.

    На одно исполнение leaf-задачи создаётся **один** primary артефакт
    (Этап 1.1). Reasoning и methodology trace живут в :attr:`metadata`.
    """

    artifact_id: str
    project_id: str
    artifact_role: str
    title: str
    description: str | None
    artifact_format: ArtifactFormat
    artifact_kind: ArtifactKind
    created_by_task_id: str | None
    storage_path: str
    created_at: str
    relations: ArtifactRelations = field(default_factory=ArtifactRelations)
    metadata: ArtifactMetadata = field(default_factory=ArtifactMetadata)
    is_superseded: bool = False
    # Подтверждение низкой уверенности пользователем (аудит-метка, как
    # is_superseded — мутируется на месте, содержимое артефакта не меняет).
    # Симметрично Decision.user_verified: «я просмотрел и согласен» снимает
    # индикатор is_low_confidence.
    user_verified: bool = False
    user_verified_at: str | None = None

    @property
    def is_low_confidence(self) -> bool:
        """Артефакт «подозрительный»: уверенность ниже порога и пользователь
        ещё не подтвердил. Заменяет прежнюю блокирующую ошибку валидации —
        теперь это мягкий сигнал «подтвердите» (зеркально Decision).

        Устаревшие (superseded) версии не подсвечиваем — их заменили.
        """
        confidence = self.metadata.overall_confidence
        if confidence is None or self.user_verified or self.is_superseded:
            return False
        return confidence < LOW_CONFIDENCE_THRESHOLD


# --- context manifest (без изменений в Этапе 1) ------------------------------


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    item_type: ContextItemType
    source_ref: str
    title: str
    content: str
    token_estimate: int
    required: bool
    priority: int


@dataclass(frozen=True)
class ContextBudget:
    max_input_tokens: int
    reserved_for_output: int
    used_tokens: int


@dataclass(frozen=True)
class ContextManifest:
    """Манифест контекста задачи.

    Этап 1.4: ``used_position_ids`` — список идентификаторов положений
    слоя A, которые попали в контекст задачи. На основе этого поля
    исполнение проставляет :attr:`ArtifactMetadata.used_position_ids`,
    что замыкает граф «положение → артефакт» для downstream-вычислений.
    """

    manifest_id: str
    project_id: str
    task_id: str
    template_ref: str
    problem_state_version: int
    budget: ContextBudget
    items: tuple[ContextItem, ...] = field(default_factory=tuple)
    excluded_items: tuple[str, ...] = field(default_factory=tuple)
    input_fingerprint: str = ""
    created_at: str = ""
    used_position_ids: tuple[str, ...] = ()
