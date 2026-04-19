from __future__ import annotations

from dataclasses import dataclass

from pov_generator.domain.registry import RegistryIssue, RegistrySnapshot, ValidationReport
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader


@dataclass(frozen=True)
class RegistrySummary:
    vocabulary_count: int
    template_count: int
    recipe_count: int


class RegistryService:
    def __init__(self, loader: FilesystemRegistryLoader) -> None:
        self._loader = loader

    def load(self) -> RegistrySnapshot:
        return self._loader.load()

    def validate(self) -> tuple[RegistrySnapshot, ValidationReport]:
        snapshot = self.load()
        errors: list[RegistryIssue] = []
        warnings: list[RegistryIssue] = []

        for template in snapshot.templates.values():
            if not snapshot.has_vocabulary_entry("domains", template.domain):
                errors.append(
                    RegistryIssue("error", f"Unknown domain '{template.domain}'.", str(template.source_path))
                )
            if not snapshot.has_vocabulary_entry("template_roles", template.semantics.template_role):
                errors.append(
                    RegistryIssue(
                        "error",
                        f"Unknown template role '{template.semantics.template_role}'.",
                        str(template.source_path),
                    )
                )
            for gap_id in (*template.semantics.closes_gaps, *template.activation.forbidden_open_gaps):
                if not snapshot.has_vocabulary_entry("gap_types", gap_id):
                    errors.append(
                        RegistryIssue("error", f"Unknown gap id '{gap_id}'.", str(template.source_path))
                    )
            for readiness_id in template.activation.required_readiness:
                if not snapshot.has_vocabulary_entry("readiness_dimensions", readiness_id):
                    errors.append(
                        RegistryIssue(
                            "error", f"Unknown readiness dimension '{readiness_id}'.", str(template.source_path)
                        )
                    )
            for raise_spec in template.semantics.raises_readiness:
                if not snapshot.has_vocabulary_entry("readiness_dimensions", raise_spec.dimension):
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Unknown readiness dimension '{raise_spec.dimension}'.",
                            str(template.source_path),
                        )
                    )
            for role in template.outputs.artifact_roles:
                if not snapshot.has_vocabulary_entry("artifact_roles", role):
                    errors.append(
                        RegistryIssue("error", f"Unknown artifact role '{role}'.", str(template.source_path))
                    )

        for recipe in snapshot.recipes.values():
            if not snapshot.has_vocabulary_entry("domains", recipe.domain):
                errors.append(RegistryIssue("error", f"Unknown domain '{recipe.domain}'.", str(recipe.source_path)))
            seen_step_ids: set[str] = set()
            seen_orders: set[int] = set()
            core_steps = 0
            review_after_core = False
            core_seen = False
            for step in recipe.steps:
                if step.identifier in seen_step_ids:
                    errors.append(
                        RegistryIssue("error", f"Duplicate recipe step id '{step.identifier}'.", str(recipe.source_path))
                    )
                seen_step_ids.add(step.identifier)
                if step.order in seen_orders:
                    errors.append(
                        RegistryIssue("error", f"Duplicate recipe step order '{step.order}'.", str(recipe.source_path))
                    )
                seen_orders.add(step.order)
                try:
                    template = snapshot.resolve_template(step.template_ref)
                except Exception as exc:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Recipe step '{step.identifier}' references missing template: {exc}",
                            str(recipe.source_path),
                        )
                    )
                    continue
                if template.semantics.template_role == "core_task":
                    core_steps += 1
                    core_seen = True
                if template.semantics.template_role == "review" and not core_seen:
                    warnings.append(
                        RegistryIssue(
                            "warning",
                            f"Review step '{step.identifier}' appears before the core task.",
                            str(recipe.source_path),
                        )
                    )
                if template.semantics.template_role == "review" and core_seen:
                    review_after_core = True
                for readiness_id in step.completion.readiness:
                    if not snapshot.has_vocabulary_entry("readiness_dimensions", readiness_id):
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Recipe step '{step.identifier}' uses unknown readiness '{readiness_id}'.",
                                str(recipe.source_path),
                            )
                        )
                for artifact_role in step.completion.artifact_roles:
                    if not snapshot.has_vocabulary_entry("artifact_roles", artifact_role):
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Recipe step '{step.identifier}' uses unknown artifact role '{artifact_role}'.",
                                str(recipe.source_path),
                            )
                        )
            if core_steps != 1:
                errors.append(
                    RegistryIssue(
                        "error",
                        f"Recipe '{recipe.identifier}@{recipe.version}' must contain exactly one core task step.",
                        str(recipe.source_path),
                    )
                )
            if core_steps == 1 and not review_after_core:
                warnings.append(
                    RegistryIssue(
                        "warning",
                        f"Recipe '{recipe.identifier}@{recipe.version}' has no review step after the core task.",
                        str(recipe.source_path),
                    )
                )

        return snapshot, ValidationReport(errors=tuple(errors), warnings=tuple(warnings))

    def summary(self, snapshot: RegistrySnapshot) -> RegistrySummary:
        return RegistrySummary(
            vocabulary_count=len(snapshot.vocabularies),
            template_count=len(snapshot.templates),
            recipe_count=len(snapshot.recipes),
        )
