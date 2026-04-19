from __future__ import annotations

from dataclasses import dataclass

from ..domain.registry import RegistryIssue, RegistrySnapshot, ValidationReport
from ..infrastructure.filesystem_registry import FilesystemRegistryLoader


@dataclass(frozen=True)
class RegistrySummary:
    vocabulary_count: int
    template_count: int
    recipe_count: int
    recipe_fragment_count: int
    domain_pack_count: int


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
                    RegistryIssue("error", f"Неизвестный домен '{template.domain}'.", str(template.source_path))
                )
            if not snapshot.has_vocabulary_entry("template_roles", template.semantics.template_role):
                errors.append(
                    RegistryIssue(
                        "error",
                        f"Неизвестная роль шаблона '{template.semantics.template_role}'.",
                        str(template.source_path),
                    )
                )
            for gap_id in (*template.semantics.closes_gaps, *template.activation.forbidden_open_gaps):
                if not snapshot.has_vocabulary_entry("gap_types", gap_id):
                    errors.append(
                        RegistryIssue("error", f"Неизвестный gap '{gap_id}'.", str(template.source_path))
                    )
            for readiness_id in template.activation.required_readiness:
                if not snapshot.has_vocabulary_entry("readiness_dimensions", readiness_id):
                    errors.append(
                        RegistryIssue(
                            "error", f"Неизвестная readiness-ось '{readiness_id}'.", str(template.source_path)
                        )
                    )
            for raise_spec in template.semantics.raises_readiness:
                if not snapshot.has_vocabulary_entry("readiness_dimensions", raise_spec.dimension):
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Неизвестная readiness-ось '{raise_spec.dimension}'.",
                            str(template.source_path),
                        )
                    )
            for role in template.outputs.artifact_roles:
                if not snapshot.has_vocabulary_entry("artifact_roles", role):
                    errors.append(
                        RegistryIssue("error", f"Неизвестная роль артефакта '{role}'.", str(template.source_path))
                    )

        for recipe in snapshot.recipes.values():
            if not snapshot.has_vocabulary_entry("domains", recipe.domain):
                errors.append(RegistryIssue("error", f"Неизвестный домен '{recipe.domain}'.", str(recipe.source_path)))
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

        for fragment in snapshot.recipe_fragments.values():
            if not snapshot.has_vocabulary_entry("domains", fragment.domain):
                errors.append(
                    RegistryIssue("error", f"Unknown domain '{fragment.domain}'.", str(fragment.source_path))
                )
            if fragment.status != "active":
                warnings.append(
                    RegistryIssue(
                        "warning",
                        f"Recipe fragment '{fragment.identifier}@{fragment.version}' is not active.",
                        str(fragment.source_path),
                    )
                )
            if not fragment.target_recipe_refs:
                errors.append(
                    RegistryIssue(
                        "error",
                        f"Recipe fragment '{fragment.identifier}@{fragment.version}' must target at least one recipe.",
                        str(fragment.source_path),
                    )
                )
            for target_ref in fragment.target_recipe_refs:
                try:
                    recipe = snapshot.resolve_recipe(target_ref)
                except Exception as exc:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Recipe fragment '{fragment.identifier}' references missing target recipe: {exc}",
                            str(fragment.source_path),
                        )
                    )
                    continue
                recipe_step_ids = {step.identifier for step in recipe.steps}
                seen_step_ids: set[str] = set()
                for step in fragment.steps:
                    if step.identifier in seen_step_ids or step.identifier in recipe_step_ids:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Recipe fragment step id '{step.identifier}' collides inside target recipe "
                                f"'{recipe.identifier}@{recipe.version}'.",
                                str(fragment.source_path),
                            )
                        )
                    seen_step_ids.add(step.identifier)
                    if step.anchor_step_id not in recipe_step_ids:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Recipe fragment step '{step.identifier}' references unknown anchor "
                                f"'{step.anchor_step_id}'.",
                                str(fragment.source_path),
                            )
                        )
                    try:
                        template = snapshot.resolve_template(step.template_ref)
                    except Exception as exc:
                        errors.append(
                            RegistryIssue(
                                "error",
                                f"Recipe fragment step '{step.identifier}' references missing template: {exc}",
                                str(fragment.source_path),
                            )
                        )
                        continue
                    if template.domain not in {fragment.domain, "common"}:
                        warnings.append(
                            RegistryIssue(
                                "warning",
                                f"Template '{template.identifier}@{template.version}' has domain '{template.domain}' "
                                f"which does not match fragment domain '{fragment.domain}'.",
                                str(fragment.source_path),
                            )
                        )
                    for readiness_id in step.completion.readiness:
                        if not snapshot.has_vocabulary_entry("readiness_dimensions", readiness_id):
                            errors.append(
                                RegistryIssue(
                                    "error",
                                    f"Recipe fragment step '{step.identifier}' uses unknown readiness "
                                    f"'{readiness_id}'.",
                                    str(fragment.source_path),
                                )
                            )
                    for artifact_role in step.completion.artifact_roles:
                        if not snapshot.has_vocabulary_entry("artifact_roles", artifact_role):
                            errors.append(
                                RegistryIssue(
                                    "error",
                                    f"Recipe fragment step '{step.identifier}' uses unknown artifact role "
                                    f"'{artifact_role}'.",
                                    str(fragment.source_path),
                                )
                            )

        for domain_pack in snapshot.domain_packs.values():
            if not snapshot.has_vocabulary_entry("domains", domain_pack.domain):
                errors.append(
                    RegistryIssue(
                        "error",
                        f"Unknown domain '{domain_pack.domain}'.",
                        str(domain_pack.source_path),
                    )
                )
            if domain_pack.status != "active":
                warnings.append(
                    RegistryIssue(
                        "warning",
                        f"Domain pack '{domain_pack.identifier}@{domain_pack.version}' is not active.",
                        str(domain_pack.source_path),
                    )
                )
            for template_ref in domain_pack.template_refs:
                try:
                    template = snapshot.resolve_template(template_ref)
                except Exception as exc:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Domain pack '{domain_pack.identifier}' references missing template: {exc}",
                            str(domain_pack.source_path),
                        )
                    )
                    continue
                if template.domain not in {domain_pack.domain, "common"}:
                    warnings.append(
                        RegistryIssue(
                            "warning",
                            f"Template '{template.identifier}@{template.version}' has domain '{template.domain}' "
                            f"which does not match pack domain '{domain_pack.domain}'.",
                            str(domain_pack.source_path),
                        )
                    )
            for fragment_ref in domain_pack.recipe_fragment_refs:
                try:
                    fragment = snapshot.resolve_recipe_fragment(fragment_ref)
                except Exception as exc:
                    errors.append(
                        RegistryIssue(
                            "error",
                            f"Domain pack '{domain_pack.identifier}' references missing recipe fragment: {exc}",
                            str(domain_pack.source_path),
                        )
                    )
                    continue
                if fragment.domain != domain_pack.domain:
                    warnings.append(
                        RegistryIssue(
                            "warning",
                            f"Recipe fragment '{fragment.identifier}@{fragment.version}' has domain "
                            f"'{fragment.domain}', expected '{domain_pack.domain}'.",
                            str(domain_pack.source_path),
                        )
                    )

        return snapshot, ValidationReport(errors=tuple(errors), warnings=tuple(warnings))

    def summary(self, snapshot: RegistrySnapshot) -> RegistrySummary:
        return RegistrySummary(
            vocabulary_count=len(snapshot.vocabularies),
            template_count=len(snapshot.templates),
            recipe_count=len(snapshot.recipes),
            recipe_fragment_count=len(snapshot.recipe_fragments),
            domain_pack_count=len(snapshot.domain_packs),
        )
