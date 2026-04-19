from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pov_generator.common.errors import NotFoundError, ValidationError


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
class ReadinessRaise:
    dimension: str
    status: str


@dataclass(frozen=True)
class TemplateSemantics:
    template_role: str
    cognitive_role: str
    closes_gaps: tuple[str, ...]
    raises_readiness: tuple[ReadinessRaise, ...]


@dataclass(frozen=True)
class TemplateActivation:
    required_readiness: tuple[str, ...]
    forbidden_open_gaps: tuple[str, ...]


@dataclass(frozen=True)
class TemplatePlanning:
    priority: int


@dataclass(frozen=True)
class TemplateInputs:
    required_problem_fields: tuple[str, ...]


@dataclass(frozen=True)
class TemplateOutputs:
    artifact_roles: tuple[str, ...]


@dataclass(frozen=True)
class TemplateSpec:
    identifier: str
    version: str
    name: str
    template_type: str
    status: str
    domain: str
    semantics: TemplateSemantics
    activation: TemplateActivation
    planning: TemplatePlanning
    inputs: TemplateInputs
    outputs: TemplateOutputs
    framework_summary: str
    source_path: Path

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)


@dataclass(frozen=True)
class StepCompletion:
    readiness: tuple[str, ...]
    artifact_roles: tuple[str, ...]


@dataclass(frozen=True)
class RecipeStep:
    identifier: str
    title: str
    order: int
    template_ref: ObjectRef
    required: bool
    completion: StepCompletion


@dataclass(frozen=True)
class RecipeSpec:
    identifier: str
    version: str
    name: str
    domain: str
    stage_gate: str
    allows_parallel_steps: bool
    steps: tuple[RecipeStep, ...]
    source_path: Path

    @property
    def ref(self) -> ObjectRef:
        return ObjectRef(self.identifier, self.version)


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
    templates: dict[str, TemplateSpec] = field(default_factory=dict)
    recipes: dict[str, RecipeSpec] = field(default_factory=dict)

    def resolve_template(self, reference: str | ObjectRef) -> TemplateSpec:
        object_ref = ObjectRef.parse(reference) if isinstance(reference, str) else reference
        key = object_ref.as_string()
        template = self.templates.get(key)
        if template is None:
            raise NotFoundError(f"Template not found: {key}")
        return template

    def resolve_recipe(self, reference: str | ObjectRef) -> RecipeSpec:
        object_ref = ObjectRef.parse(reference) if isinstance(reference, str) else reference
        key = object_ref.as_string()
        recipe = self.recipes.get(key)
        if recipe is None:
            raise NotFoundError(f"Recipe not found: {key}")
        return recipe

    def has_vocabulary_entry(self, vocabulary_id: str, entry_id: str) -> bool:
        vocabulary = self.vocabularies.get(vocabulary_id)
        return vocabulary is not None and entry_id in vocabulary.entries


def require_mapping(raw: dict[str, Any], key: str, owner: str) -> dict[str, Any]:
    value = raw.get(key)
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


def parse_template(raw: dict[str, Any], source_path: Path) -> TemplateSpec:
    owner = str(source_path)
    semantics_raw = require_mapping(raw, "semantics", owner)
    activation_raw = require_mapping(raw, "activation", owner)
    planning_raw = require_mapping(raw, "planning", owner)
    inputs_raw = require_mapping(raw, "inputs", owner)
    outputs_raw = require_mapping(raw, "outputs", owner)
    raises = tuple(
        ReadinessRaise(
            dimension=require_str(item, "dimension", owner),
            status=require_str(item, "status", owner),
        )
        for item in require_list(semantics_raw, "raises_readiness", owner)
    )
    version = require_str(raw, "version", owner)
    parse_semver(version)
    return TemplateSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        name=require_str(raw, "name", owner),
        template_type=require_str(raw, "type", owner),
        status=require_str(raw, "status", owner),
        domain=require_str(raw, "domain", owner),
        semantics=TemplateSemantics(
            template_role=require_str(semantics_raw, "template_role", owner),
            cognitive_role=require_str(semantics_raw, "cognitive_role", owner),
            closes_gaps=tuple(str(item) for item in require_list(semantics_raw, "closes_gaps", owner)),
            raises_readiness=raises,
        ),
        activation=TemplateActivation(
            required_readiness=tuple(str(item) for item in require_list(activation_raw, "required_readiness", owner)),
            forbidden_open_gaps=tuple(str(item) for item in require_list(activation_raw, "forbidden_open_gaps", owner)),
        ),
        planning=TemplatePlanning(priority=int(planning_raw.get("priority", 0))),
        inputs=TemplateInputs(
            required_problem_fields=tuple(str(item) for item in require_list(inputs_raw, "required_problem_fields", owner))
        ),
        outputs=TemplateOutputs(
            artifact_roles=tuple(str(item) for item in require_list(outputs_raw, "artifact_roles", owner))
        ),
        framework_summary=require_mapping(raw, "framework", owner).get("summary", ""),
        source_path=source_path,
    )


def parse_recipe(raw: dict[str, Any], source_path: Path) -> RecipeSpec:
    owner = str(source_path)
    steps: list[RecipeStep] = []
    for item in require_list(raw, "steps", owner):
        if not isinstance(item, dict):
            raise ValidationError(f"Recipe step in {owner} must be a mapping")
        completion_raw = require_mapping(item, "completion", owner)
        steps.append(
            RecipeStep(
                identifier=require_str(item, "id", owner),
                title=require_str(item, "title", owner),
                order=int(item.get("order", 0)),
                template_ref=ObjectRef.parse(require_str(item, "template", owner)),
                required=bool(item.get("required", True)),
                completion=StepCompletion(
                    readiness=tuple(str(entry) for entry in require_list(completion_raw, "readiness", owner)),
                    artifact_roles=tuple(
                        str(entry) for entry in require_list(completion_raw, "artifact_roles", owner)
                    ),
                ),
            )
        )
    version = require_str(raw, "version", owner)
    parse_semver(version)
    return RecipeSpec(
        identifier=require_str(raw, "id", owner),
        version=version,
        name=require_str(raw, "name", owner),
        domain=require_str(raw, "domain", owner),
        stage_gate=require_str(raw, "stage_gate", owner),
        allows_parallel_steps=bool(raw.get("allows_parallel_steps", False)),
        steps=tuple(sorted(steps, key=lambda step: step.order)),
        source_path=source_path,
    )
