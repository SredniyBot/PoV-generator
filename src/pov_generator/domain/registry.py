from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..common.errors import NotFoundError, ValidationError

TemplateType = Literal["composite", "leaf", "fan_out"]
ExecutorType = Literal["llm", "script", "tool", "human", "hybrid", "system", "harness"]
ComplexityLevel = Literal["trivial", "standard", "complex"]
StageExecutionMode = Literal["single_call", "per_stage_cot"]
QualityGateCheckType = Literal["human_approval", "external_signoff", "automated_review"]


def parse_semver(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValidationError(f"Invalid semantic version: {value}")
    return tuple(int(part) for part in parts)


@dataclass(frozen=True)
class ObjectRef:
    identifier: str
    version: str

    @classmethod
    def parse(cls, value: str) -> "ObjectRef":
        if "@" not in value:
            raise ValidationError(f"Expected object reference '<id>@<version>', got: {value}")
        identifier, version = value.rsplit("@", 1)
        parse_semver(version)
        return cls(identifier=identifier, version=version)

    def as_string(self) -> str:
        return f"{self.identifier}@{self.version}"


@dataclass(frozen=True)
class VocabularyEntry:
    identifier: str
    label: str
    description: str


@dataclass(frozen=True)
class Vocabulary:
    identifier: str
    version: str
    entries: dict[str, VocabularyEntry]
    source_path: Path


@dataclass(frozen=True)
class ObjectiveSpec:
    identifier: str
    version: str
    title: str
    root_task_ref: ObjectRef
    done_artifact_refs: tuple[ObjectRef, ...]
    done_gate_refs: tuple[ObjectRef, ...]
    source_path: Path
    # Опциональная декларация: какую методологию активировать при создании
    # проекта с этим objective. Если ``None``, ``project_service.init_project``
    # подставляет глобальный fallback (``process.lean_jtbd@1.0.0``).
    default_methodology_pack_ref: ObjectRef | None = None
    # Цепочка objective'ов: после завершения этого objective'а в том же
    # workspace можно активировать любой из перечисленных. UX-подсказка,
    # не жёсткий контракт — ``activate_next_objective`` принимает и
    # незадекларированные переходы.
    compatible_next_objectives: tuple[ObjectRef, ...] = ()

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)


@dataclass(frozen=True)
class TaskChildSpec:
    identifier: str
    task_ref: ObjectRef
    required: bool = True


@dataclass(frozen=True)
class TaskSlotSpec:
    identifier: str
    title: str


@dataclass(frozen=True)
class FanOutSpec:
    artifact_role: str
    array_path: str
    key_field: str
    label_field: str


@dataclass(frozen=True)
class TemplateInputs:
    """Что задача требует на вход для исполнения.

    Этап 7.3 добавил декларативный auto-collect:
    ``collect_optional_from_active_domain_packs`` — если ``True``, контекст
    задачи автоматически дополняется всеми артефактами, созданными
    задачами активных доменных паков. Это **снимает** необходимость
    руками перечислять `optional: [...]` для каждого нового домена.
    """

    required_problem_fields: tuple[str, ...] = ()
    required_artifact_roles: tuple[str, ...] = ()
    optional_artifact_roles: tuple[str, ...] = ()
    required_readiness: tuple[str, ...] = ()
    forbidden_open_gaps: tuple[str, ...] = ()
    required_domain_packs: tuple[str, ...] = ()
    collect_optional_from_active_domain_packs: bool = False
    # Роды первоисточника, которые задача-интерпретатор потребляет целиком:
    # "attachments" (текст входных файлов), на будущее — "clarifications" и т.п.
    # Источник истины подаётся неприкосновенно (см. context_assembly): только
    # задачам, которые ОБЪЯВИЛИ потребность, а не всем подряд.
    raw_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemplateOutputs:
    artifact_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadinessRaise:
    dimension: str
    status: str


@dataclass(frozen=True)
class TemplateEffects:
    closes_gaps: tuple[str, ...] = ()
    raises_readiness: tuple[ReadinessRaise, ...] = ()


@dataclass(frozen=True)
class TemplateContextPolicy:
    max_tokens: int | None = None
    overflow: str = "trim_optional"


@dataclass(frozen=True)
class TemplatePlanning:
    priority: int = 0


@dataclass(frozen=True)
class TemplateValidationPolicy:
    requires_review: bool = False
    min_confidence: float | None = None


MergeStrategy = Literal["structural", "synthetic", "hybrid"]
"""Стратегия объединения артефактов merge-задачей (Этап 5 roadmap).

- ``structural`` — детерминированное объединение по правилам контракта,
  без LLM. Для непересекающихся доменов (доменные секции, fan-out
  результаты, сводные индексы).
- ``synthetic`` — интеграция через LLM по объединённому промпту. Для
  пересекающихся артефактов, требующих связного нарратива
  (например, финальный requirements_spec).
- ``hybrid`` — комбинация: скелет детерминирован, отдельные секции
  LLM-заполнены. Зарезервировано на будущее (не реализовано в MVP).
"""

ConflictPolicy = Literal["union", "first_wins", "last_wins", "fail_on_conflict"]
"""Политика разрешения конфликтов при структурном merge.

- ``union`` — для массивов: объединение с дедупликацией.
- ``first_wins`` — берётся значение из первого входного артефакта.
- ``last_wins`` — берётся значение из последнего входного артефакта.
- ``fail_on_conflict`` — поднимается :class:`ConflictError`,
  если входы несогласованы.
"""


@dataclass(frozen=True)
class MergeConfig:
    """Конфигурация merge-задачи.

    Если задано в шаблоне — execution-слой использует merge-исполнителя
    вместо обычного LLM/stub. См. ``execution_service``.
    """

    strategy: MergeStrategy
    conflict_policy: ConflictPolicy = "union"


@dataclass(frozen=True)
class HarnessGateSpec:
    """Гейт «готово» (DoD) harness-узла, объявленный в шаблоне (Ф5c).

    ``command`` — shell-команда-проверка в песочнице (build/test/lint; сборка
    образа kaniko — частный случай). Успех = exit 0.
    """

    name: str
    command: str
    timeout_s: int | None = None


@dataclass(frozen=True)
class TemplateSpec:
    identifier: str
    version: str
    title: str
    template_type: TemplateType
    status: str
    domain: str
    complexity: ComplexityLevel | None = None
    children: tuple[TaskChildSpec, ...] = ()
    slots: tuple[TaskSlotSpec, ...] = ()
    executor: ExecutorType | None = None
    inputs: TemplateInputs = field(default_factory=TemplateInputs)
    outputs: TemplateOutputs = field(default_factory=TemplateOutputs)
    effects: TemplateEffects = field(default_factory=TemplateEffects)
    planning: TemplatePlanning = field(default_factory=TemplatePlanning)
    context_policy: TemplateContextPolicy = field(default_factory=TemplateContextPolicy)
    validation_policy: TemplateValidationPolicy = field(default_factory=TemplateValidationPolicy)
    instruction: str | None = None
    # Краткое описание задачи (одно-два предложения), которое попадает в
    # контекст исполнителя как "что нужно сделать". Раньше называлось
    # framework_summary, но это было misleading — методологическая часть
    # живёт в methodology_pack (R8/TS9), а это поле — task-specific
    # инструкция. Переименовано в W2.cleanup (C2).
    summary: str = ""
    # Этап 5: если задано, leaf-задача — merge-операция. Execution-слой
    # объединяет входные артефакты по стратегии вместо обычного исполнения.
    merge: MergeConfig | None = None
    # Слой 2: ссылка на профиль умений (kind capability_profile). Если задана,
    # execution-слой подмешивает <capability_pledge> (роль + cannot_do) в
    # system-prompt, специализируя генерацию под контракт. Это ортогональная
    # привязка к обычному executor=llm, а не отдельный механизм исполнения.
    capability_ref: ObjectRef | None = None
    # Methodology mode (Track 5) — определяет, какие стадии reasoning
    # methodology применяются к этой задаче:
    #   full       — все стадии методологии (полный CoT с option_generation
    #                 и decision); подходит для задач выбора варианта,
    #                 сценарного анализа.
    #   minimal    — только базовые стадии (goal_framing + decision);
    #                 подходит для синтетических и описательных задач,
    #                 где «выбор варианта» избыточен.
    #   validation — только decision-stage; подходит для review/QA-задач.
    #   skip       — методология не применяется (чистая экстракция, нет
    #                 решений и нет альтернатив).
    methodology_mode: str = "full"
    # v3.6: включён ли task-level identification «выявление решений»
    # для этого шаблона. По умолчанию True. Ставится False в YAML
    # для pure-transform/review/merge задач, которые ничего нового по
    # проекту не решают — там identification только генерирует мета-шум
    # (см. docs/decision_subsystem_design_v3.6.md).
    decision_identification_enabled: bool = True
    # Ф5: вид выхода harness-узла. "structured" (по умолчанию) — JSON-payload;
    # "bundle" — файловый набор (код/документы/двоичные/БД/образ), сохраняемый
    # как bundle-артефакт. Имеет смысл только для executor=harness.
    harness_output: str | None = None
    # Ф5c: гейты «готово» (DoD) — команды-проверки в песочнице после прогона
    # агента (build/test/lint). Провал любого = узел не выполнен.
    harness_gates: tuple[HarnessGateSpec, ...] = ()
    fan_out_spec: FanOutSpec | None = None
    children_template_ref: str | None = None
    source_path: Path = Path("")

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)

    @property
    def name(self) -> str:
        return self.title


@dataclass(frozen=True)
class ArtifactContractSpec:
    """Контракт артефакта — описание формы результата задачи.

    Этап 7.5: ``unstructured=True`` помечает «честно нет схемы» —
    контракт существует как реф для artifact_role, но не валидирует
    содержимое. Это лучше, чем псевдо-контракт с
    ``additionalProperties: true`` и пустыми полями, который создаёт
    ложное ощущение защищённости.

    Когда контракт становится реальным (описаны ``required`` + поля),
    флаг ``unstructured`` снимается; валидация начинает реально работать.
    """

    identifier: str
    version: str
    title: str
    schema: dict[str, Any]
    source_path: Path
    unstructured: bool = False

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)

    @property
    def artifact_role(self) -> str:
        return self.identifier.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class DomainContributionItem:
    identifier: str
    task_ref: ObjectRef | None = None
    gate_ref: ObjectRef | None = None
    required: bool = True
    when: str | None = None


@dataclass(frozen=True)
class DomainContribution:
    slot_id: str
    items: tuple[DomainContributionItem, ...]


@dataclass(frozen=True)
class DomainPackSpec:
    identifier: str
    version: str
    title: str
    description: str
    domain: str
    status: str
    entry_signals: tuple[str, ...]
    contributions: tuple[DomainContribution, ...]
    source_path: Path

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)

    @property
    def name(self) -> str:
        return self.title


@dataclass(frozen=True)
class CapabilityItem:
    capability: str
    tech: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    maturity: str = "reliable"
    produces: str | None = None
    # Пределы гарантии: до какого уровня нагрузки/надёжности/точности/объёма
    # умение даёт результат. Пары (измерение, порог) — например
    # ("reliability", "обычная доступность, без жёсткого SLA"). Именно они ловят
    # «слишком сложное/надёжное/большое»: требование сверх предела не берётся в
    # полном виде, даже если функционально знакомо.
    limits: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CapabilityProfileSpec:
    identifier: str
    version: str
    title: str
    role: str
    capabilities: tuple[CapabilityItem, ...]
    cannot_do: tuple[str, ...]
    source_path: Path
    binds: ObjectRef | None = None
    escalation: str | None = None

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)


@dataclass(frozen=True)
class QualityGateSpec:
    identifier: str
    version: str
    title: str
    required_artifact_roles: tuple[str, ...]
    check_type: QualityGateCheckType
    instruction: str | None
    on_fail: str
    source_path: Path
    approver_role: str | None = None
    decision_modes: tuple[str, ...] = ()
    blocking: bool = True
    timeout_hours: int | None = None
    validator_ref: ObjectRef | None = None
    on_pass: str | None = None
    on_comments: str | None = None

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)


@dataclass(frozen=True)
class MethodologyStageProducesField:
    field_name: str
    field_type: str
    required: bool = False
    nullable: bool = False
    schema: dict[str, Any] | None = None
    item_schema: dict[str, Any] | None = None
    description: str | None = None


@dataclass(frozen=True)
class MethodologyStageRule:
    identifier: str
    if_expression: str | None
    emit_candidate: dict[str, Any]


@dataclass(frozen=True)
class MethodologyStageSpec:
    identifier: str
    title: str
    description: str
    produces: tuple[MethodologyStageProducesField, ...]
    constraints: dict[str, Any]
    rules: tuple[MethodologyStageRule, ...]


@dataclass(frozen=True)
class MethodologyComplexityOverride:
    complexity: ComplexityLevel
    skip_stages: tuple[str, ...]
    relax_rules: tuple[str, ...]


@dataclass(frozen=True)
class MethodologyReasoningArtifactConfig:
    required_stages: tuple[str, ...]
    optional_stages: tuple[str, ...]


@dataclass(frozen=True)
class MethodologyPackSpec:
    identifier: str
    version: str
    title: str
    description: str
    status: str
    stage_execution_mode: StageExecutionMode
    stages: tuple[MethodologyStageSpec, ...]
    reasoning_artifact: MethodologyReasoningArtifactConfig
    complexity_overrides: tuple[MethodologyComplexityOverride, ...]
    clarification_policy: dict[str, Any]
    emit_source_refs: bool
    source_path: Path

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)

    def stages_for(
        self,
        complexity: ComplexityLevel | None,
        methodology_mode: str = "full",
    ) -> tuple[MethodologyStageSpec, ...]:
        """Возвращает активные стадии для задачи с учётом её methodology_mode.

        methodology_mode определяет, какие стадии reasoning'а имеют смысл
        для природы задачи:
        • full       — все стадии (включая option_generation/decision)
        • minimal    — только описательные/синтетические (goal_framing,
                       jtbd_anchor); option_generation и decision-подобные
                       стадии пропускаются как нерелевантные
        • validation — только финальная decision-стадия
        • skip       — methodology не применяется
        """
        if methodology_mode == "skip":
            return ()
        all_stages = self.stages_for_complexity(complexity)
        if methodology_mode == "full":
            return all_stages
        # heuristic для распознавания стадий:
        # • decision-like: identifier == "decision" или содержит "decision"
        # • option-like: identifier содержит "option"
        if methodology_mode == "validation":
            return tuple(s for s in all_stages if "decision" in s.identifier.lower())
        if methodology_mode == "minimal":
            return tuple(
                s for s in all_stages
                if "option" not in s.identifier.lower()
                and "decision" not in s.identifier.lower()
            )
        return all_stages

    def stages_for_complexity(self, complexity: ComplexityLevel | None) -> tuple[MethodologyStageSpec, ...]:
        skip: set[str] = set()
        relax: set[str] = set()
        if complexity is not None:
            for override in self.complexity_overrides:
                if override.complexity == complexity:
                    skip = set(override.skip_stages)
                    relax = set(override.relax_rules)
                    break
        active: list[MethodologyStageSpec] = []
        for stage in self.stages:
            if stage.identifier in skip:
                continue
            if relax:
                rules = tuple(rule for rule in stage.rules if rule.identifier not in relax)
                active.append(
                    MethodologyStageSpec(
                        identifier=stage.identifier,
                        title=stage.title,
                        description=stage.description,
                        produces=stage.produces,
                        constraints=stage.constraints,
                        rules=rules,
                    )
                )
            else:
                active.append(stage)
        return tuple(active)


@dataclass(frozen=True)
class RegistryIssue:
    severity: str
    message: str
    location: str


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[RegistryIssue, ...] = ()
    warnings: tuple[RegistryIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class RegistrySnapshot:
    vocabularies: dict[str, Vocabulary] = field(default_factory=dict)
    objectives: dict[str, ObjectiveSpec] = field(default_factory=dict)
    templates: dict[str, TemplateSpec] = field(default_factory=dict)
    artifact_contracts: dict[str, ArtifactContractSpec] = field(default_factory=dict)
    domain_packs: dict[str, DomainPackSpec] = field(default_factory=dict)
    methodology_packs: dict[str, MethodologyPackSpec] = field(default_factory=dict)
    quality_gates: dict[str, QualityGateSpec] = field(default_factory=dict)
    capability_profiles: dict[str, CapabilityProfileSpec] = field(default_factory=dict)

    def resolve_object_ref(self, reference: str | ObjectRef) -> ObjectRef:
        return ObjectRef.parse(reference) if isinstance(reference, str) else reference

    def resolve_objective(self, reference: str | ObjectRef) -> ObjectiveSpec:
        object_ref = self.resolve_object_ref(reference)
        key = object_ref.as_string()
        objective = self.objectives.get(key)
        if objective is None:
            raise NotFoundError(f"Objective not found: {key}")
        return objective

    def resolve_template(self, reference: str | ObjectRef) -> TemplateSpec:
        object_ref = self.resolve_object_ref(reference)
        key = object_ref.as_string()
        template = self.templates.get(key)
        if template is None:
            raise NotFoundError(f"Task template not found: {key}")
        return template

    def resolve_artifact_contract(self, reference: str | ObjectRef) -> ArtifactContractSpec:
        object_ref = self.resolve_object_ref(reference)
        key = object_ref.as_string()
        contract = self.artifact_contracts.get(key)
        if contract is None:
            raise NotFoundError(f"Artifact contract not found: {key}")
        return contract

    def resolve_domain_pack(self, reference: str | ObjectRef) -> DomainPackSpec:
        object_ref = self.resolve_object_ref(reference)
        key = object_ref.as_string()
        pack = self.domain_packs.get(key)
        if pack is None:
            raise NotFoundError(f"Domain pack not found: {key}")
        return pack

    def resolve_methodology_pack(self, reference: str | ObjectRef) -> MethodologyPackSpec:
        object_ref = self.resolve_object_ref(reference)
        key = object_ref.as_string()
        pack = self.methodology_packs.get(key)
        if pack is None:
            raise NotFoundError(f"Methodology pack not found: {key}")
        return pack

    def resolve_quality_gate(self, reference: str | ObjectRef) -> QualityGateSpec:
        object_ref = self.resolve_object_ref(reference)
        key = object_ref.as_string()
        gate = self.quality_gates.get(key)
        if gate is None:
            raise NotFoundError(f"Quality gate not found: {key}")
        return gate

    def resolve_capability_profile(self, reference: str | ObjectRef) -> CapabilityProfileSpec:
        object_ref = self.resolve_object_ref(reference)
        key = object_ref.as_string()
        spec = self.capability_profiles.get(key)
        if spec is None:
            raise NotFoundError(f"Agent capability not found: {key}")
        return spec

    def has_vocabulary_entry(self, vocabulary_id: str, entry_id: str) -> bool:
        vocabulary = self.vocabularies.get(vocabulary_id)
        return vocabulary is not None and entry_id in vocabulary.entries


def require_mapping(raw: dict[str, Any], key: str, owner: str, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = raw.get(key, default if default is not None else {})
    if not isinstance(value, dict):
        raise ValidationError(f"Expected mapping '{key}' in {owner}")
    return value


def require_list(raw: dict[str, Any], key: str, owner: str) -> list[Any]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValidationError(f"Expected list '{key}' in {owner}")
    return value


def require_str(raw: dict[str, Any], key: str, owner: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Expected non-empty string '{key}' in {owner}")
    return value.strip()


def optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def artifact_role_from_ref(reference: str | ObjectRef) -> str:
    object_ref = ObjectRef.parse(reference) if isinstance(reference, str) else reference
    return object_ref.identifier.rsplit(".", 1)[-1]


def parse_vocabulary(raw: dict[str, Any], source_path: Path) -> Vocabulary:
    owner = str(source_path)
    entries: dict[str, VocabularyEntry] = {}
    for item in require_list(raw, "entries", owner):
        if not isinstance(item, dict):
            raise ValidationError(f"Vocabulary entry in {owner} must be a mapping")
        identifier = require_str(item, "id", owner)
        entries[identifier] = VocabularyEntry(
            identifier=identifier,
            label=require_str(item, "label", owner),
            description=require_str(item, "description", owner),
        )
    version = require_str(raw, "version", owner)
    parse_semver(version)
    return Vocabulary(
        identifier=require_str(raw, "id", owner),
        version=version,
        entries=entries,
        source_path=source_path,
    )


def parse_objective(raw: dict[str, Any], source_path: Path) -> ObjectiveSpec:
    owner = str(source_path)
    version = require_str(raw, "version", owner)
    parse_semver(version)
    done_when = require_mapping(raw, "done_when", owner)
    raw_methodology = raw.get("default_methodology_pack")
    if raw_methodology is None:
        default_methodology_pack_ref: ObjectRef | None = None
    elif isinstance(raw_methodology, str) and raw_methodology.strip():
        default_methodology_pack_ref = ObjectRef.parse(raw_methodology.strip())
    else:
        raise ValidationError(
            f"Expected non-empty string 'default_methodology_pack' in {owner}"
        )
    raw_next = raw.get("compatible_next_objectives")
    if raw_next is None:
        compatible_next_objectives: tuple[ObjectRef, ...] = ()
    elif isinstance(raw_next, list):
        parsed: list[ObjectRef] = []
        for item in raw_next:
            if not isinstance(item, str) or not item.strip():
                raise ValidationError(
                    f"Expected non-empty string in 'compatible_next_objectives' of {owner}"
                )
            parsed.append(ObjectRef.parse(item.strip()))
        compatible_next_objectives = tuple(parsed)
    else:
        raise ValidationError(
            f"Expected list of strings for 'compatible_next_objectives' in {owner}"
        )
    return ObjectiveSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        title=require_str(raw, "title", owner),
        root_task_ref=ObjectRef.parse(require_str(raw, "root", owner)),
        done_artifact_refs=tuple(ObjectRef.parse(str(item)) for item in require_list(done_when, "artifacts", owner)),
        done_gate_refs=tuple(ObjectRef.parse(str(item)) for item in require_list(done_when, "gates", owner)),
        source_path=source_path,
        default_methodology_pack_ref=default_methodology_pack_ref,
        compatible_next_objectives=compatible_next_objectives,
    )


_METHODOLOGY_MODES = frozenset({"full", "minimal", "validation", "skip"})


def _parse_methodology_mode(value: Any, owner: str) -> str:
    """Прочитать поле `methodology.mode` из YAML шаблона.

    Допускается как `methodology: minimal` (короткая запись), так и
    `methodology: {mode: minimal}` (полная). По умолчанию — `full`.
    """
    if value is None:
        return "full"
    if isinstance(value, str):
        mode = value
    elif isinstance(value, dict):
        mode = str(value.get("mode", "full"))
    else:
        return "full"
    if mode not in _METHODOLOGY_MODES:
        raise ValidationError(
            f"{owner}: methodology mode {mode!r} должен быть одним из {sorted(_METHODOLOGY_MODES)}"
        )
    return mode


def parse_task_template(raw: dict[str, Any], source_path: Path) -> TemplateSpec:
    owner = str(source_path)
    version = require_str(raw, "version", owner)
    parse_semver(version)
    template_type = require_str(raw, "type", owner)
    if template_type not in {"composite", "leaf", "fan_out"}:
        raise ValidationError(f"Unsupported task template type '{template_type}' in {owner}")

    children = tuple(
        TaskChildSpec(
            identifier=require_str(item, "id", owner),
            task_ref=ObjectRef.parse(require_str(item, "task", owner)),
            required=bool(item.get("required", True)),
        )
        for item in require_list(raw, "children", owner)
        if isinstance(item, dict)
    )
    slots = tuple(
        TaskSlotSpec(
            identifier=require_str(item, "id", owner),
            title=require_str(item, "title", owner),
        )
        for item in require_list(raw, "slots", owner)
        if isinstance(item, dict)
    )

    requires = require_mapping(raw, "requires", owner)
    required_artifacts = require_mapping(requires, "artifacts", owner)
    required_state = tuple(str(item) for item in require_list(requires, "state", owner))
    produces = require_mapping(raw, "produces", owner)
    artifact_ref = optional_str(produces, "artifact")
    effects = require_mapping(raw, "effects", owner)
    readiness_effects = require_list(require_mapping(effects, "readiness", owner), "set", owner)
    closes_gaps = require_list(require_mapping(effects, "gaps", owner), "close", owner)
    context = require_mapping(raw, "context", owner)
    planning = require_mapping(raw, "planning", owner)
    validation = require_mapping(raw, "validation", owner)

    raises_readiness: list[ReadinessRaise] = []
    for item in readiness_effects:
        if isinstance(item, dict):
            raises_readiness.append(
                ReadinessRaise(
                    dimension=require_str(item, "dimension", owner),
                    status=require_str(item, "status", owner),
                )
            )
        else:
            raises_readiness.append(ReadinessRaise(dimension=str(item), status="ready"))

    complexity_raw = raw.get("complexity")
    complexity_value: ComplexityLevel | None = None
    if complexity_raw is not None:
        if complexity_raw not in {"trivial", "standard", "complex"}:
            raise ValidationError(
                f"Поле complexity в {owner} должно быть trivial|standard|complex"
            )
        complexity_value = complexity_raw  # type: ignore[assignment]

    merge_raw = raw.get("merge")
    merge_config: MergeConfig | None = None
    if merge_raw is not None:
        if not isinstance(merge_raw, dict):
            raise ValidationError(
                f"Поле merge в {owner} должно быть mapping со strategy/conflict_policy"
            )
        strategy = merge_raw.get("strategy")
        if strategy not in {"structural", "synthetic", "hybrid"}:
            raise ValidationError(
                f"merge.strategy в {owner} должно быть structural|synthetic|hybrid"
            )
        conflict_policy = merge_raw.get("conflict_policy", "union")
        if conflict_policy not in {"union", "first_wins", "last_wins", "fail_on_conflict"}:
            raise ValidationError(
                f"merge.conflict_policy в {owner} должно быть union|first_wins|last_wins|fail_on_conflict"
            )
        merge_config = MergeConfig(
            strategy=strategy,  # type: ignore[arg-type]
            conflict_policy=conflict_policy,  # type: ignore[arg-type]
        )

    # fan_out fields — validated and parsed here, passed into TemplateSpec below
    fan_out_spec: FanOutSpec | None = None
    children_template_ref_val: str | None = None
    if template_type == "fan_out":
        fan_out_raw = raw.get("fan_out_spec")
        if not isinstance(fan_out_raw, dict):
            raise ValidationError(f"fan_out_spec is required for fan_out template in {owner}")
        fan_out_spec = FanOutSpec(
            artifact_role=require_str(fan_out_raw, "artifact_role", owner),
            array_path=require_str(fan_out_raw, "array_path", owner),
            key_field=require_str(fan_out_raw, "key_field", owner),
            label_field=require_str(fan_out_raw, "label_field", owner),
        )
        children_template_ref_val = raw.get("children_template_ref")
        if not isinstance(children_template_ref_val, str) or not children_template_ref_val.strip():
            raise ValidationError(f"children_template_ref is required for fan_out template in {owner}")
        # Validate it's in <id>@<semver> format
        try:
            ObjectRef.parse(children_template_ref_val)
        except Exception as exc:
            raise ValidationError(
                f"children_template_ref must be in '<id>@<semver>' format in {owner}, got: {children_template_ref_val!r}"
            ) from exc
    else:
        if "fan_out_spec" in raw:
            raise ValidationError(f"fan_out_spec is only allowed for fan_out templates in {owner}")
        if "children_template_ref" in raw:
            raise ValidationError(f"children_template_ref is only allowed for fan_out templates in {owner}")

    return TemplateSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        title=require_str(raw, "title", owner),
        template_type=template_type,
        status=str(raw.get("status", "active")),
        domain=str(raw.get("domain", require_str(raw, "id", owner).split(".", 1)[0])),
        complexity=complexity_value,
        children=children,
        slots=slots,
        executor=optional_str(raw, "executor"),  # type: ignore[arg-type]
        inputs=TemplateInputs(
            required_problem_fields=required_state,
            required_artifact_roles=tuple(artifact_role_from_ref(str(item)) for item in require_list(required_artifacts, "required", owner)),
            optional_artifact_roles=tuple(artifact_role_from_ref(str(item)) for item in require_list(required_artifacts, "optional", owner)),
            required_readiness=tuple(str(item) for item in require_list(requires, "readiness", owner)),
            forbidden_open_gaps=tuple(str(item) for item in require_list(requires, "forbidden_open_gaps", owner)),
            required_domain_packs=tuple(str(item) for item in require_list(requires, "domain_packs", owner)),
            collect_optional_from_active_domain_packs=bool(
                (required_artifacts.get("collect_optional") or {}).get(
                    "from_active_domain_packs", False
                )
            ),
            raw_inputs=tuple(str(item) for item in require_list(requires, "inputs", owner)),
        ),
        outputs=TemplateOutputs(
            artifact_roles=(artifact_role_from_ref(artifact_ref),) if artifact_ref else (),
        ),
        effects=TemplateEffects(
            closes_gaps=tuple(str(item) for item in closes_gaps),
            raises_readiness=tuple(raises_readiness),
        ),
        planning=TemplatePlanning(priority=int(planning.get("priority", 0))),
        context_policy=TemplateContextPolicy(
            max_tokens=int(context["max_tokens"]) if isinstance(context.get("max_tokens"), int) else None,
            overflow=str(context.get("overflow", "trim_optional")),
        ),
        validation_policy=TemplateValidationPolicy(
            requires_review=bool(validation.get("requires_review", False)),
            min_confidence=float(validation["min_confidence"]) if isinstance(validation.get("min_confidence"), (int, float)) else None,
        ),
        instruction=optional_str(raw, "instruction"),
        summary=optional_str(raw, "summary") or "",
        merge=merge_config,
        capability_ref=(ObjectRef.parse(_agent_raw) if (_agent_raw := optional_str(raw, "capability")) else None),
        methodology_mode=_parse_methodology_mode(raw.get("methodology"), owner),
        # v3.6: identification per-template flag. По умолчанию True.
        # Транспорт через YAML: `decision_identification: false` в task-шаблоне.
        decision_identification_enabled=bool(
            raw.get("decision_identification", True)
        ),
        harness_output=optional_str(raw, "harness_output"),
        harness_gates=tuple(
            HarnessGateSpec(
                name=str(item["name"]),
                command=str(item["command"]),
                timeout_s=int(item["timeout_s"]) if isinstance(item.get("timeout_s"), int) else None,
            )
            for item in (raw.get("harness_gates") or [])
            if isinstance(item, dict) and item.get("name") and item.get("command")
        ),
        fan_out_spec=fan_out_spec,
        children_template_ref=children_template_ref_val,
        source_path=source_path,
    )


def parse_artifact_contract(raw: dict[str, Any], source_path: Path) -> ArtifactContractSpec:
    owner = str(source_path)
    version = require_str(raw, "version", owner)
    parse_semver(version)
    return ArtifactContractSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        title=require_str(raw, "title", owner),
        schema=require_mapping(raw, "schema", owner),
        source_path=source_path,
        unstructured=bool(raw.get("unstructured", False)),
    )


def parse_domain_pack(raw: dict[str, Any], source_path: Path) -> DomainPackSpec:
    owner = str(source_path)
    version = require_str(raw, "version", owner)
    parse_semver(version)
    contributions: list[DomainContribution] = []
    for contribution_raw in require_list(raw, "contributes", owner):
        if not isinstance(contribution_raw, dict):
            raise ValidationError(f"Domain contribution in {owner} must be a mapping")
        items = []
        for item in require_list(contribution_raw, "add", owner):
            if not isinstance(item, dict):
                raise ValidationError(f"Contribution item in {owner} must be a mapping")
            task_ref = optional_str(item, "task")
            gate_ref = optional_str(item, "gate")
            if not task_ref and not gate_ref:
                raise ValidationError(f"Contribution item in {owner} must define task or gate")
            items.append(
                DomainContributionItem(
                    identifier=require_str(item, "id", owner),
                    task_ref=ObjectRef.parse(task_ref) if task_ref else None,
                    gate_ref=ObjectRef.parse(gate_ref) if gate_ref else None,
                    required=bool(item.get("required", True)),
                    when=optional_str(item, "when"),
                )
            )
        contributions.append(
            DomainContribution(
                slot_id=require_str(contribution_raw, "to", owner),
                items=tuple(items),
            )
        )

    detect = require_mapping(raw, "detect", owner)
    return DomainPackSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        title=require_str(raw, "title", owner),
        description=str(raw.get("description", "")),
        domain=str(raw.get("domain", require_str(raw, "id", owner).split(".", 1)[0])),
        status=str(raw.get("status", "active")),
        entry_signals=tuple(str(item) for item in require_list(detect, "signals", owner)),
        contributions=tuple(contributions),
        source_path=source_path,
    )


_CAPABILITY_ROLES = frozenset({"backend", "ui", "ml", "data", "integration"})
_CAPABILITY_MATURITIES = frozenset({"reliable", "experimental"})


def parse_capability_profile(raw: dict[str, Any], source_path: Path) -> CapabilityProfileSpec:
    owner = str(source_path)
    version = require_str(raw, "version", owner)
    parse_semver(version)
    role = require_str(raw, "role", owner)
    if role not in _CAPABILITY_ROLES:
        raise ValidationError(f"Поле role в {owner} должно быть одним из {sorted(_CAPABILITY_ROLES)}")
    items: list[CapabilityItem] = []
    for item in require_list(raw, "capabilities", owner):
        if not isinstance(item, dict):
            raise ValidationError(f"Элемент capabilities в {owner} должен быть mapping")
        maturity = str(item.get("maturity", "reliable"))
        if maturity not in _CAPABILITY_MATURITIES:
            raise ValidationError(f"Поле maturity в {owner} должно быть reliable|experimental")
        limits_raw = item.get("limits") or {}
        if not isinstance(limits_raw, dict):
            raise ValidationError(f"Поле limits в {owner} должно быть mapping")
        limits = tuple((str(k), str(v)) for k, v in limits_raw.items())
        items.append(
            CapabilityItem(
                capability=require_str(item, "capability", owner),
                tech=tuple(str(t) for t in require_list(item, "tech", owner)),
                requires=tuple(str(r) for r in require_list(item, "requires", owner)),
                maturity=maturity,
                produces=optional_str(item, "produces"),
                limits=limits,
            )
        )
    binds_raw = optional_str(raw, "binds")
    return CapabilityProfileSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        title=require_str(raw, "title", owner),
        role=role,
        capabilities=tuple(items),
        cannot_do=tuple(str(c) for c in require_list(raw, "cannot_do", owner)),
        binds=ObjectRef.parse(binds_raw) if binds_raw else None,
        escalation=optional_str(raw, "escalation"),
        source_path=source_path,
    )


def parse_quality_gate(raw: dict[str, Any], source_path: Path) -> QualityGateSpec:
    owner = str(source_path)
    version = require_str(raw, "version", owner)
    parse_semver(version)
    requires = require_mapping(raw, "requires", owner)
    check = require_mapping(raw, "check", owner)
    raw_check_type = str(check.get("type", "automated_review"))
    legacy_aliases = {"llm_review": "automated_review", "schema": "automated_review"}
    normalized_check_type = legacy_aliases.get(raw_check_type, raw_check_type)
    if normalized_check_type not in {"human_approval", "external_signoff", "automated_review"}:
        raise ValidationError(
            f"Поле check.type в {owner} должно быть human_approval|external_signoff|automated_review"
        )
    decision_modes_raw = check.get("decision_modes", [])
    if not isinstance(decision_modes_raw, list):
        raise ValidationError(f"check.decision_modes в {owner} должно быть списком строк")
    decision_modes = tuple(str(item) for item in decision_modes_raw)
    if normalized_check_type in {"human_approval", "external_signoff"} and not decision_modes:
        decision_modes = ("approved", "approved_with_comments", "rejected")
    timeout_hours_raw = check.get("timeout_hours")
    timeout_hours: int | None = None
    if isinstance(timeout_hours_raw, int):
        timeout_hours = timeout_hours_raw
    validator_ref_raw = optional_str(check, "validator_ref")
    validator_ref = ObjectRef.parse(validator_ref_raw) if validator_ref_raw else None
    return QualityGateSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        title=require_str(raw, "title", owner),
        required_artifact_roles=tuple(artifact_role_from_ref(str(item)) for item in require_list(requires, "artifacts", owner)),
        check_type=normalized_check_type,  # type: ignore[arg-type]
        instruction=optional_str(check, "instruction"),
        on_fail=str(raw.get("on_fail", "fail")),
        source_path=source_path,
        approver_role=optional_str(check, "approver_role"),
        decision_modes=decision_modes,
        blocking=bool(check.get("blocking", True)),
        timeout_hours=timeout_hours,
        validator_ref=validator_ref,
        on_pass=optional_str(raw, "on_pass"),
        on_comments=optional_str(raw, "on_comments"),
    )


def parse_methodology_pack(raw: dict[str, Any], source_path: Path) -> MethodologyPackSpec:
    owner = str(source_path)
    version = require_str(raw, "version", owner)
    parse_semver(version)
    mode_raw = raw.get("stage_execution_mode", "single_call")
    if mode_raw not in {"single_call", "per_stage_cot"}:
        raise ValidationError(
            f"stage_execution_mode в {owner} должно быть single_call или per_stage_cot"
        )
    stages: list[MethodologyStageSpec] = []
    for stage_raw in require_list(raw, "stages", owner):
        if not isinstance(stage_raw, dict):
            raise ValidationError(f"Стадия методологии в {owner} должна быть mapping")
        produces: list[MethodologyStageProducesField] = []
        for field_raw in require_list(stage_raw, "produces", owner):
            if not isinstance(field_raw, dict):
                raise ValidationError(f"produces в стадии {owner} должно быть mapping")
            produces.append(
                MethodologyStageProducesField(
                    field_name=require_str(field_raw, "field", owner),
                    field_type=require_str(field_raw, "type", owner),
                    required=bool(field_raw.get("required", False)),
                    nullable=bool(field_raw.get("nullable", False)),
                    schema=field_raw.get("schema") if isinstance(field_raw.get("schema"), dict) else None,
                    item_schema=field_raw.get("item_schema") if isinstance(field_raw.get("item_schema"), dict) else None,
                    description=optional_str(field_raw, "description"),
                )
            )
        rules: list[MethodologyStageRule] = []
        for rule_raw in stage_raw.get("rules", []) or []:
            if not isinstance(rule_raw, dict):
                raise ValidationError(f"Правило стадии в {owner} должно быть mapping")
            emit_raw = rule_raw.get("emit_candidate", {})
            if not isinstance(emit_raw, dict):
                raise ValidationError(f"emit_candidate в {owner} должно быть mapping")
            rules.append(
                MethodologyStageRule(
                    identifier=require_str(rule_raw, "id", owner),
                    if_expression=optional_str(rule_raw, "if"),
                    emit_candidate=dict(emit_raw),
                )
            )
        constraints_raw = stage_raw.get("constraints", {})
        if not isinstance(constraints_raw, dict):
            raise ValidationError(f"constraints в стадии {owner} должно быть mapping")
        stages.append(
            MethodologyStageSpec(
                identifier=require_str(stage_raw, "id", owner),
                title=require_str(stage_raw, "title", owner),
                description=str(stage_raw.get("description", "")),
                produces=tuple(produces),
                constraints=dict(constraints_raw),
                rules=tuple(rules),
            )
        )

    reasoning_raw = require_mapping(raw, "reasoning_artifact", owner)
    required_stages = tuple(str(item) for item in reasoning_raw.get("required_stages", []) or [])
    optional_stages = tuple(str(item) for item in reasoning_raw.get("optional_stages", []) or [])

    overrides_raw = raw.get("complexity_overrides", {}) or {}
    if not isinstance(overrides_raw, dict):
        raise ValidationError(f"complexity_overrides в {owner} должно быть mapping")
    overrides: list[MethodologyComplexityOverride] = []
    for level, payload in overrides_raw.items():
        if level not in {"trivial", "standard", "complex"}:
            raise ValidationError(
                f"complexity_overrides в {owner}: уровень {level!r} должен быть trivial|standard|complex"
            )
        if not isinstance(payload, dict):
            raise ValidationError(f"complexity_overrides[{level}] в {owner} должно быть mapping")
        overrides.append(
            MethodologyComplexityOverride(
                complexity=level,  # type: ignore[arg-type]
                skip_stages=tuple(str(item) for item in payload.get("skip_stages", []) or []),
                relax_rules=tuple(str(item) for item in payload.get("relax_rules", []) or []),
            )
        )

    clarification_policy_raw = raw.get("clarification_policy", {}) or {}
    if not isinstance(clarification_policy_raw, dict):
        raise ValidationError(f"clarification_policy в {owner} должно быть mapping")

    provenance_raw = raw.get("provenance", {}) or {}
    emit_source_refs = bool(provenance_raw.get("emit_source_refs", True)) if isinstance(provenance_raw, dict) else True

    return MethodologyPackSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        title=require_str(raw, "title", owner),
        description=str(raw.get("description", "")),
        status=str(raw.get("status", "active")),
        stage_execution_mode=mode_raw,  # type: ignore[arg-type]
        stages=tuple(stages),
        reasoning_artifact=MethodologyReasoningArtifactConfig(
            required_stages=required_stages,
            optional_stages=optional_stages,
        ),
        complexity_overrides=tuple(overrides),
        clarification_policy=dict(clarification_policy_raw),
        emit_source_refs=emit_source_refs,
        source_path=source_path,
    )
