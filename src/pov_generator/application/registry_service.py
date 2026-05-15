from __future__ import annotations

from dataclasses import dataclass

from ..domain.registry import RegistryIssue, RegistrySnapshot, ValidationReport
from ..infrastructure.filesystem_registry import FilesystemRegistryLoader


@dataclass(frozen=True)
class RegistrySummary:
    vocabulary_count: int
    objective_count: int
    template_count: int
    artifact_contract_count: int
    domain_pack_count: int
    methodology_pack_count: int
    quality_gate_count: int


class RegistryService:
    def __init__(self, loader: FilesystemRegistryLoader) -> None:
        self._loader = loader

    def load(self) -> RegistrySnapshot:
        return self._loader.load()

    def validate(self) -> tuple[RegistrySnapshot, ValidationReport]:
        snapshot = self.load()
        errors: list[RegistryIssue] = []
        warnings: list[RegistryIssue] = []

        for objective in snapshot.objectives.values():
            if objective.root_task_ref.as_string() not in snapshot.templates:
                errors.append(
                    RegistryIssue(
                        "error",
                        f"Цель ссылается на неизвестную корневую задачу '{objective.root_task_ref.as_string()}'.",
                        str(objective.source_path),
                    )
                )
            for artifact_ref in objective.done_artifact_refs:
                if artifact_ref.as_string() not in snapshot.artifact_contracts:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Цель ссылается на неизвестный контракт артефакта '{artifact_ref.as_string()}'.",
                            str(objective.source_path),
                        )
                    )
            for gate_ref in objective.done_gate_refs:
                if gate_ref.as_string() not in snapshot.quality_gates:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Цель ссылается на неизвестную проверку качества '{gate_ref.as_string()}'.",
                            str(objective.source_path),
                        )
                    )

        declared_slots: set[str] = set()
        for template in snapshot.templates.values():
            if template.status != "active":
                warnings.append(
                    RegistryIssue(
                        "warning",
                        f"Шаблон задачи '{template.ref.as_string()}' не активен.",
                        str(template.source_path),
                    )
                )
            if not snapshot.has_vocabulary_entry("domains", template.domain):
                errors.append(
                    RegistryIssue("error", f"Неизвестный домен '{template.domain}'.", str(template.source_path))
                )
            if template.template_type == "composite" and not template.children and not template.slots:
                errors.append(
                    RegistryIssue(
                        "error",
                        f"Композитная задача '{template.ref.as_string()}' должна иметь children или slots.",
                        str(template.source_path),
                    )
                )
            if template.template_type == "leaf":
                if not template.executor:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Листовая задача '{template.ref.as_string()}' должна иметь executor.",
                            str(template.source_path),
                        )
                    )
                if len(template.outputs.artifact_roles) != 1:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Листовая задача '{template.ref.as_string()}' должна производить ровно один основной артефакт.",
                            str(template.source_path),
                        )
                    )
            for child in template.children:
                if child.task_ref.as_string() not in snapshot.templates:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Задача '{template.ref.as_string()}' ссылается на неизвестного child '{child.task_ref.as_string()}'.",
                            str(template.source_path),
                        )
                    )
            for slot in template.slots:
                declared_slots.add(slot.identifier)
            for artifact_role in (*template.inputs.required_artifact_roles, *template.inputs.optional_artifact_roles, *template.outputs.artifact_roles):
                if artifact_role and not any(contract.artifact_role == artifact_role for contract in snapshot.artifact_contracts.values()):
                    warnings.append(
                        RegistryIssue(
                            "warning",
                            f"Для роли артефакта '{artifact_role}' не найден контракт в новом реестре.",
                            str(template.source_path),
                        )
                    )

        for pack in snapshot.domain_packs.values():
            if pack.status != "active":
                warnings.append(
                    RegistryIssue(
                        "warning",
                        f"Доменный пакет '{pack.ref.as_string()}' не активен.",
                        str(pack.source_path),
                    )
                )
            if not snapshot.has_vocabulary_entry("domains", pack.domain):
                errors.append(RegistryIssue("error", f"Неизвестный домен '{pack.domain}'.", str(pack.source_path)))
            for contribution in pack.contributions:
                if contribution.slot_id not in declared_slots:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Доменный пакет '{pack.ref.as_string()}' расширяет неизвестный slot '{contribution.slot_id}'.",
                            str(pack.source_path),
                        )
                    )
                seen_ids: set[str] = set()
                for item in contribution.items:
                    if item.identifier in seen_ids:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Дублирующийся contribution id '{item.identifier}'.",
                                str(pack.source_path),
                            )
                        )
                    seen_ids.add(item.identifier)
                    if item.task_ref and item.task_ref.as_string() not in snapshot.templates:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Contribution '{item.identifier}' ссылается на неизвестную задачу '{item.task_ref.as_string()}'.",
                                str(pack.source_path),
                            )
                        )
                    if item.gate_ref and item.gate_ref.as_string() not in snapshot.quality_gates:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Contribution '{item.identifier}' ссылается на неизвестную проверку '{item.gate_ref.as_string()}'.",
                                str(pack.source_path),
                            )
                        )

        for gate in snapshot.quality_gates.values():
            for artifact_role in gate.required_artifact_roles:
                if not any(contract.artifact_role == artifact_role for contract in snapshot.artifact_contracts.values()):
                    warnings.append(
                        RegistryIssue(
                            "warning",
                            f"Проверка качества '{gate.ref.as_string()}' требует артефакт без контракта: '{artifact_role}'.",
                            str(gate.source_path),
                        )
                    )


        for methodology in snapshot.methodology_packs.values():
            if methodology.status != "active":
                warnings.append(
                    RegistryIssue(
                        "warning",
                        f"Методологический пакет '{methodology.ref.as_string()}' не активен.",
                        str(methodology.source_path),
                    )
                )
            stage_ids = {stage.identifier for stage in methodology.stages}
            seen_field_names: set[str] = set()
            for stage in methodology.stages:
                for produces in stage.produces:
                    if produces.field_name in seen_field_names:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Поле '{produces.field_name}' стадии '{stage.identifier}' дублируется в '{methodology.ref.as_string()}'.",
                                str(methodology.source_path),
                            )
                        )
                    seen_field_names.add(produces.field_name)
            for stage_id in methodology.reasoning_artifact.required_stages:
                if stage_id not in stage_ids:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"reasoning_artifact.required_stages ссылается на неизвестную стадию '{stage_id}' в '{methodology.ref.as_string()}'.",
                            str(methodology.source_path),
                        )
                    )
            for stage_id in methodology.reasoning_artifact.optional_stages:
                if stage_id not in stage_ids:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"reasoning_artifact.optional_stages ссылается на неизвестную стадию '{stage_id}' в '{methodology.ref.as_string()}'.",
                            str(methodology.source_path),
                        )
                    )
            for override in methodology.complexity_overrides:
                for stage_id in override.skip_stages:
                    if stage_id not in stage_ids:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"complexity_overrides[{override.complexity}].skip_stages ссылается на неизвестную стадию '{stage_id}' в '{methodology.ref.as_string()}'.",
                                str(methodology.source_path),
                            )
                        )

        return snapshot, ValidationReport(errors=tuple(errors), warnings=tuple(warnings))

    def summary(self, snapshot: RegistrySnapshot) -> RegistrySummary:
        return RegistrySummary(
            vocabulary_count=len(snapshot.vocabularies),
            objective_count=len(snapshot.objectives),
            template_count=len(snapshot.templates),
            artifact_contract_count=len(snapshot.artifact_contracts),
            domain_pack_count=len(snapshot.domain_packs),
            methodology_pack_count=len(snapshot.methodology_packs),
            quality_gate_count=len(snapshot.quality_gates),
        )
