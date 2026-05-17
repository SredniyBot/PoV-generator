from __future__ import annotations

from typing import Any

from ..common.errors import ValidationError

JSONSchema = dict[str, Any]


def _pack_enabled(domain_pack_refs: tuple[str, ...], pack_prefix: str) -> bool:
    return any(ref.startswith(f"{pack_prefix}@") for ref in domain_pack_refs)


def _string_array_schema() -> JSONSchema:
    return {"type": "array", "items": {"type": "string"}}


def _analysis_meta_properties() -> JSONSchema:
    """Поля, которые есть у любого аналитического артефакта.

    ``confidence`` — мера уверенности модели в результате (0..1). После
    рефакторинга это поле — **метаданные артефакта**, а не часть его
    бизнес-содержимого. LLM по-прежнему может вернуть его в payload (мы
    оставляем optional в схеме для обратной совместимости и потому что
    модель так привыкла), но execution_service вытащит значение из
    payload и положит в ``ArtifactMetadata.overall_confidence`` —
    единственное канонично место для уверенности.
    """
    return {
        "confidence": {"type": "number"},
    }


def _analysis_object(required: list[str], properties: JSONSchema) -> JSONSchema:
    """Сборка схемы для аналитического артефакта.

    ``confidence`` остаётся в ``properties`` (optional), но НЕ
    включается в ``required``. См. ``_analysis_meta_properties``.
    """
    merged = dict(properties)
    merged.update(_analysis_meta_properties())
    return {
        "type": "object",
        "required": list(required),
        "additionalProperties": False,
        "properties": merged,
    }


def artifact_schema(artifact_role: str, domain_pack_refs: tuple[str, ...] = ()) -> JSONSchema:
    frontend_enabled = _pack_enabled(domain_pack_refs, "frontend.web_workspace") or _pack_enabled(domain_pack_refs, "frontend.web_app_requirements")
    ml_enabled = _pack_enabled(domain_pack_refs, "ml.predictive_analytics") or _pack_enabled(domain_pack_refs, "ml.predictive_analytics_pov_requirements")
    security_enabled = _pack_enabled(domain_pack_refs, "security.enterprise_compliance") or _pack_enabled(domain_pack_refs, "security.enterprise_compliance_requirements")
    integration_enabled = _pack_enabled(domain_pack_refs, "integration.enterprise_integration") or _pack_enabled(domain_pack_refs, "integration.enterprise_delivery_requirements")

    requirements_spec_properties: JSONSchema = {
        "title": {"type": "string"},
        "business_goal": {"type": "string"},
        "success_criteria": _string_array_schema(),
        "actors": _string_array_schema(),
        "user_stories": _string_array_schema(),
        "functional_requirements": _string_array_schema(),
        "non_functional_requirements": _string_array_schema(),
        "assumptions": _string_array_schema(),
        "risks": _string_array_schema(),
        "alternatives_considered": _string_array_schema(),
        "acceptance_criteria": _string_array_schema(),
        "open_questions": _string_array_schema(),
        "executive_summary": {"type": "string"},
        "business_context": {"type": "string"},
        "target_outcomes": _string_array_schema(),
        "scope_in": _string_array_schema(),
        "scope_out": _string_array_schema(),
        "stakeholders": _string_array_schema(),
        "operating_model": _string_array_schema(),
        "data_requirements": _string_array_schema(),
        "integration_requirements": _string_array_schema(),
        "security_requirements": _string_array_schema(),
        "deployment_requirements": _string_array_schema(),
        "delivery_artifacts": _string_array_schema(),
        "phased_plan": _string_array_schema(),
        # Расширенные разделы — необязательные. Если в upstream активны
        # соответствующие задачи (glossary_drafting / deployment_topology /
        # project_risk_register / privacy_impact_assessment), то их вклад
        # подмешивается в финальное ТЗ как отдельные разделы. Если нет —
        # эти поля просто отсутствуют, и рендерер их опускает.
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["term", "definition"],
                "additionalProperties": False,
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                    "category": {"type": "string"},
                    "synonyms": _string_array_schema(),
                },
            },
        },
        "deployment_topology": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "environments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "scaling": {"type": "string"},
                        },
                    },
                },
                "network_zones": _string_array_schema(),
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "placement", "responsibilities"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "placement": {"type": "string"},
                            "technology": {"type": "string"},
                            "responsibilities": {"type": "string"},
                        },
                    },
                },
                "deployment_flow": {"type": "string"},
            },
        },
        "project_risks_detail": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "category", "description", "probability", "impact", "mitigation"],
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "probability": {"type": "string", "enum": ["low", "medium", "high"]},
                    "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                    "mitigation": {"type": "string"},
                    "trigger": {"type": "string"},
                    "owner": {"type": "string"},
                },
            },
        },
        "privacy_impact": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pii_categories": _string_array_schema(),
                "processing_purposes": _string_array_schema(),
                "lawful_basis": {"type": "string"},
                "data_minimization": _string_array_schema(),
                "controls": _string_array_schema(),
                "data_subject_rights": {"type": "string"},
                "cross_border_transfers": {"type": "string"},
                "retention_policy": {"type": "string"},
                "residual_risks": _string_array_schema(),
            },
        },
    }

    requirements_spec_required = [
        "title",
        "business_goal",
        "success_criteria",
        "actors",
        "user_stories",
        "functional_requirements",
        "non_functional_requirements",
        "assumptions",
        "risks",
        "alternatives_considered",
        "acceptance_criteria",
        "open_questions",
    ]

    if frontend_enabled:
        requirements_spec_properties["frontend_requirements"] = {
            "type": "object",
            "required": ["user_roles", "user_flows", "screens", "ux_constraints"],
            "additionalProperties": False,
            "properties": {
                "user_roles": _string_array_schema(),
                "user_flows": _string_array_schema(),
                "screens": _string_array_schema(),
                "analytics_views": _string_array_schema(),
                "decision_support_needs": _string_array_schema(),
                "ux_constraints": _string_array_schema(),
            },
        }
        requirements_spec_required.append("frontend_requirements")

    if ml_enabled:
        requirements_spec_properties["ml_requirements"] = {
            "type": "object",
            "required": [
                "prediction_target",
                "prediction_horizon",
                "prediction_unit",
                "data_sources",
                "model_outputs",
                "evaluation_metrics",
                "explainability_requirements",
            ],
            "additionalProperties": False,
            "properties": {
                "prediction_target": {"type": "string"},
                "prediction_horizon": {"type": "string"},
                "prediction_unit": {"type": "string"},
                "data_sources": _string_array_schema(),
                "model_outputs": _string_array_schema(),
                "evaluation_metrics": _string_array_schema(),
                "explainability_requirements": _string_array_schema(),
            },
        }
        requirements_spec_required.append("ml_requirements")

    if security_enabled:
        requirements_spec_properties["security_constraints_detail"] = {
            "type": "object",
            "required": [
                "deployment_constraints",
                "privacy_constraints",
                "access_control_constraints",
                "allowed_ai_usage",
                "mandatory_controls",
            ],
            "additionalProperties": False,
            "properties": {
                "deployment_constraints": _string_array_schema(),
                "privacy_constraints": _string_array_schema(),
                "access_control_constraints": _string_array_schema(),
                "allowed_ai_usage": _string_array_schema(),
                "mandatory_controls": _string_array_schema(),
                "compliance_risks": _string_array_schema(),
            },
        }
        requirements_spec_required.append("security_constraints_detail")

    if integration_enabled:
        requirements_spec_properties["integration_model"] = {
            "type": "object",
            "required": [
                "source_systems",
                "delivery_pattern",
                "refresh_model",
                "target_surfaces",
                "operating_roles",
            ],
            "additionalProperties": False,
            "properties": {
                "source_systems": _string_array_schema(),
                "delivery_pattern": _string_array_schema(),
                "refresh_model": {"type": "string"},
                "target_surfaces": _string_array_schema(),
                "operating_roles": _string_array_schema(),
                "dependency_risks": _string_array_schema(),
            },
        }
        requirements_spec_required.append("integration_model")

    schemas: dict[str, JSONSchema] = {
        "clarification_notes": {
            "type": "object",
            "required": ["clarified_goal", "success_criteria", "assumptions", "open_questions"],
            "additionalProperties": False,
            "properties": {
                "clarified_goal": {"type": "string"},
                "success_criteria": _string_array_schema(),
                "assumptions": _string_array_schema(),
                "open_questions": _string_array_schema(),
            },
        },
        "user_story_map": {
            "type": "object",
            "required": ["actors", "user_stories", "edge_cases"],
            "additionalProperties": False,
            "properties": {
                "actors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "needs"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "needs": _string_array_schema(),
                        },
                    },
                },
                "user_stories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["actor", "story", "value"],
                        "additionalProperties": False,
                        "properties": {
                            "actor": {"type": "string"},
                            "story": {"type": "string"},
                            "value": {"type": "string"},
                        },
                    },
                },
                "edge_cases": _string_array_schema(),
            },
        },
        "alternatives_analysis": {
            "type": "object",
            "required": ["alternatives", "recommended_option", "rationale"],
            "additionalProperties": False,
            "properties": {
                "alternatives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "description", "pros", "cons"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "pros": _string_array_schema(),
                            "cons": _string_array_schema(),
                        },
                    },
                },
                "recommended_option": {"type": "string"},
                "rationale": {"type": "string"},
            },
        },
        "normalized_request": _analysis_object(
            ["request_summary", "business_problem", "requested_solution_elements", "explicit_constraints", "implicit_risks", "ambiguous_points"],
            {
                "request_summary": {"type": "string"},
                "business_problem": {"type": "string"},
                "requested_solution_elements": _string_array_schema(),
                "explicit_constraints": _string_array_schema(),
                "implicit_risks": _string_array_schema(),
                "ambiguous_points": _string_array_schema(),
            },
        ),
        "request_fact_sheet": _analysis_object(
            [
                "explicit_facts",
                "named_entities",
                "requested_deliverables",
                "mentioned_systems_and_sources",
                "mentioned_metrics_and_targets",
            ],
            {
                "explicit_facts": _string_array_schema(),
                "named_entities": _string_array_schema(),
                "requested_deliverables": _string_array_schema(),
                "mentioned_systems_and_sources": _string_array_schema(),
                "mentioned_metrics_and_targets": _string_array_schema(),
            },
        ),
        "goal_hypothesis": _analysis_object(
            [
                "hypothesized_goal",
                "expected_effects",
                "project_stage_hypothesis",
                "success_signals",
                "unresolved_goal_points",
            ],
            {
                "hypothesized_goal": {"type": "string"},
                "expected_effects": _string_array_schema(),
                "project_stage_hypothesis": {"type": "string"},
                "success_signals": _string_array_schema(),
                "unresolved_goal_points": _string_array_schema(),
            },
        ),
        "constraint_inventory": _analysis_object(
            [
                "explicit_constraints",
                "inferred_constraints",
                "stage_constraints",
                "environment_constraints",
                "dependency_constraints",
            ],
            {
                "explicit_constraints": _string_array_schema(),
                "inferred_constraints": _string_array_schema(),
                "stage_constraints": _string_array_schema(),
                "environment_constraints": _string_array_schema(),
                "dependency_constraints": _string_array_schema(),
            },
        ),
        "ambiguity_gap_report": _analysis_object(
            [
                "ambiguous_points",
                "conflicting_signals",
                "missing_decisions",
                "safe_assumptions",
                "escalation_candidates",
            ],
            {
                "ambiguous_points": _string_array_schema(),
                "conflicting_signals": _string_array_schema(),
                "missing_decisions": _string_array_schema(),
                "safe_assumptions": _string_array_schema(),
                "escalation_candidates": _string_array_schema(),
            },
        ),
        "business_outcome_model": _analysis_object(
            [
                "primary_business_goal",
                "target_kpis",
                "success_metrics",
                "business_process_impacts",
                "expected_decisions",
                "value_hypotheses",
                "assumptions",
            ],
            {
                "primary_business_goal": {"type": "string"},
                "target_kpis": _string_array_schema(),
                "success_metrics": _string_array_schema(),
                "business_process_impacts": _string_array_schema(),
                "expected_decisions": _string_array_schema(),
                "value_hypotheses": _string_array_schema(),
                "assumptions": _string_array_schema(),
            },
        ),
        "scope_boundary_matrix": _analysis_object(
            [
                "in_scope",
                "out_of_scope",
                "pilot_boundaries",
                "future_phase_candidates",
                "mandatory_deliverables",
                "excluded_deliverables",
            ],
            {
                "in_scope": _string_array_schema(),
                "out_of_scope": _string_array_schema(),
                "pilot_boundaries": _string_array_schema(),
                "future_phase_candidates": _string_array_schema(),
                "mandatory_deliverables": _string_array_schema(),
                "excluded_deliverables": _string_array_schema(),
            },
        ),
        "stakeholder_map": _analysis_object(
            ["stakeholder_groups", "primary_users", "data_owners", "support_teams"],
            {
                "stakeholder_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "role", "influence", "expectations"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "influence": {"type": "string"},
                            "expectations": _string_array_schema(),
                        },
                    },
                },
                "primary_users": _string_array_schema(),
                "data_owners": _string_array_schema(),
                "support_teams": _string_array_schema(),
            },
        ),
        "decision_ownership_matrix": _analysis_object(
            ["decisions", "unowned_decisions", "approval_points"],
            {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "owner", "participants", "timing"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "owner": {"type": "string"},
                            "participants": _string_array_schema(),
                            "timing": {"type": "string"},
                        },
                    },
                },
                "unowned_decisions": _string_array_schema(),
                "approval_points": _string_array_schema(),
            },
        ),
        "operating_model_outline": _analysis_object(
            ["process_flow", "producer_roles", "consumer_roles", "support_roles", "handoff_risks"],
            {
                "process_flow": _string_array_schema(),
                "producer_roles": _string_array_schema(),
                "consumer_roles": _string_array_schema(),
                "support_roles": _string_array_schema(),
                "handoff_risks": _string_array_schema(),
            },
        ),
        "stakeholder_operating_model": _analysis_object(
            [
                "stakeholder_groups",
                "primary_users",
                "decision_owners",
                "operating_model",
                "adoption_constraints",
            ],
            {
                "stakeholder_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "role", "expectations", "responsibilities"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "expectations": _string_array_schema(),
                            "responsibilities": _string_array_schema(),
                        },
                    },
                },
                "primary_users": _string_array_schema(),
                "decision_owners": _string_array_schema(),
                "operating_model": _string_array_schema(),
                "adoption_constraints": _string_array_schema(),
            },
        ),
        "solution_option_inventory": _analysis_object(
            ["options", "comparison_axes"],
            {
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "summary", "boundary_fit", "enabling_conditions"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "summary": {"type": "string"},
                            "boundary_fit": {"type": "string"},
                            "enabling_conditions": _string_array_schema(),
                            "pros": _string_array_schema(),
                            "cons": _string_array_schema(),
                            "fit_score": {"type": "number"},
                        },
                    },
                },
                "comparison_axes": _string_array_schema(),
                # recommended_option и rationale переехали сюда из
                # solution_tradeoff_matrix: задача solution_option_inventory
                # явно выбирает один вариант (см. её prompt). Раньше поле
                # отсутствовало в схеме и рекомендация терялась в массиве.
                "recommended_option": {"type": "string"},
                "recommendation_rationale": {"type": "string"},
            },
        ),
        "solution_tradeoff_matrix": _analysis_object(
            ["options", "recommended_option", "recommendation_rationale", "deferred_decisions"],
            {
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "summary", "fit_for_pilot", "pros", "cons", "risks"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "summary": {"type": "string"},
                            "fit_for_pilot": {"type": "string"},
                            "pros": _string_array_schema(),
                            "cons": _string_array_schema(),
                            "risks": _string_array_schema(),
                        },
                    },
                },
                "recommended_option": {"type": "string"},
                "recommendation_rationale": {"type": "string"},
                "deferred_decisions": _string_array_schema(),
            },
        ),
        "delivery_scope_definition": _analysis_object(
            ["delivery_items", "excluded_items", "demo_expectations", "evidence_artifacts"],
            {
                "delivery_items": _string_array_schema(),
                "excluded_items": _string_array_schema(),
                "demo_expectations": _string_array_schema(),
                "evidence_artifacts": _string_array_schema(),
            },
        ),
        "acceptance_model_definition": _analysis_object(
            [
                "acceptance_criteria",
                "success_evidence",
                "required_customer_inputs",
                "formal_approvals",
                "rejection_conditions",
            ],
            {
                "acceptance_criteria": _string_array_schema(),
                "success_evidence": _string_array_schema(),
                "required_customer_inputs": _string_array_schema(),
                "formal_approvals": _string_array_schema(),
                "rejection_conditions": _string_array_schema(),
            },
        ),
        "delivery_acceptance_plan": _analysis_object(
            [
                "delivery_items",
                "acceptance_criteria",
                "success_evidence",
                "required_customer_inputs",
                "formal_approvals",
                "open_dependencies",
            ],
            {
                "delivery_items": _string_array_schema(),
                "acceptance_criteria": _string_array_schema(),
                "success_evidence": _string_array_schema(),
                "required_customer_inputs": _string_array_schema(),
                "formal_approvals": _string_array_schema(),
                "open_dependencies": _string_array_schema(),
            },
        ),
        "dependency_map": _analysis_object(
            [
                "critical_dependencies",
                "customer_inputs",
                "external_decisions",
                "access_dependencies",
                "stop_conditions",
            ],
            {
                "critical_dependencies": _string_array_schema(),
                "customer_inputs": _string_array_schema(),
                "external_decisions": _string_array_schema(),
                "access_dependencies": _string_array_schema(),
                "stop_conditions": _string_array_schema(),
            },
        ),
        "implementation_dependency_plan": _analysis_object(
            ["phases", "critical_dependencies", "project_risks", "proposed_timeline"],
            {
                "phases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "objectives", "dependencies", "outputs"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "objectives": _string_array_schema(),
                            "dependencies": _string_array_schema(),
                            "outputs": _string_array_schema(),
                        },
                    },
                },
                "critical_dependencies": _string_array_schema(),
                "project_risks": _string_array_schema(),
                "proposed_timeline": _string_array_schema(),
            },
        ),
        # Phase 3+4 additions: glossary, risk register, deployment topology,
        # privacy DPIA. Все они опциональные дополнения к финальному ТЗ.
        "glossary_terms": _analysis_object(
            ["entries"],
            {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["term", "definition"],
                        "additionalProperties": False,
                        "properties": {
                            "term": {"type": "string"},
                            "definition": {"type": "string"},
                            "category": {"type": "string"},
                            "synonyms": _string_array_schema(),
                        },
                    },
                },
                "domain_scope": {"type": "string"},
            },
        ),
        "project_risk_register": _analysis_object(
            ["risks"],
            {
                "risks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["title", "category", "probability", "impact", "mitigation"],
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "category": {"type": "string"},
                            "description": {"type": "string"},
                            "probability": {"type": "string", "enum": ["low", "medium", "high"]},
                            "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                            "mitigation": {"type": "string"},
                            "trigger": {"type": "string"},
                            "owner": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        ),
        "deployment_topology": _analysis_object(
            ["environments", "network_zones", "components"],
            {
                "environments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "scaling": {"type": "string"},
                        },
                    },
                },
                "network_zones": _string_array_schema(),
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "placement", "responsibilities"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "placement": {"type": "string"},
                            "responsibilities": {"type": "string"},
                            "technology": {"type": "string"},
                        },
                    },
                },
                "deployment_flow": {"type": "string"},
            },
        ),
        "privacy_impact_assessment": _analysis_object(
            ["pii_categories", "processing_purposes", "lawful_basis", "controls"],
            {
                "pii_categories": _string_array_schema(),
                "processing_purposes": _string_array_schema(),
                "lawful_basis": {"type": "string"},
                "data_minimization": _string_array_schema(),
                "controls": _string_array_schema(),
                "data_subject_rights": _string_array_schema(),
                "cross_border_transfers": {"type": "string"},
                "retention_policy": {"type": "string"},
                "residual_risks": _string_array_schema(),
            },
        ),
        "predictive_problem_definition": _analysis_object(
            [
                "prediction_target",
                "prediction_horizon",
                "prediction_unit",
                "label_definition",
                "business_actions",
                "model_outputs",
                "evaluation_metrics",
                "baseline_expectations",
                "explainability_requirements",
            ],
            {
                "prediction_target": {"type": "string"},
                "prediction_horizon": {"type": "string"},
                "prediction_unit": {"type": "string"},
                "label_definition": {"type": "string"},
                "business_actions": _string_array_schema(),
                "model_outputs": _string_array_schema(),
                "evaluation_metrics": _string_array_schema(),
                "baseline_expectations": _string_array_schema(),
                "explainability_requirements": _string_array_schema(),
            },
        ),
        "data_landscape_assessment": _analysis_object(
            [
                "source_systems",
                "required_entities",
                "key_features",
                "data_quality_risks",
                "data_gaps",
                "feasibility_assessment",
                "privacy_notes",
            ],
            {
                "source_systems": _string_array_schema(),
                "required_entities": _string_array_schema(),
                "key_features": _string_array_schema(),
                "data_quality_risks": _string_array_schema(),
                "data_gaps": _string_array_schema(),
                "feasibility_assessment": {"type": "string"},
                "privacy_notes": _string_array_schema(),
            },
        ),
        "security_compliance_constraints": _analysis_object(
            [
                "deployment_constraints",
                "privacy_constraints",
                "access_control_constraints",
                "integration_security_constraints",
                "allowed_ai_usage",
                "mandatory_controls",
                "compliance_risks",
            ],
            {
                "deployment_constraints": _string_array_schema(),
                "privacy_constraints": _string_array_schema(),
                "access_control_constraints": _string_array_schema(),
                "integration_security_constraints": _string_array_schema(),
                "allowed_ai_usage": _string_array_schema(),
                "mandatory_controls": _string_array_schema(),
                "compliance_risks": _string_array_schema(),
            },
        ),
        "integration_operating_model": _analysis_object(
            [
                "source_integrations",
                "target_integrations",
                "refresh_model",
                "data_delivery_pattern",
                "operating_roles",
                "support_model",
                "dependency_risks",
            ],
            {
                "source_integrations": _string_array_schema(),
                "target_integrations": _string_array_schema(),
                "refresh_model": {"type": "string"},
                "data_delivery_pattern": _string_array_schema(),
                "operating_roles": _string_array_schema(),
                "support_model": _string_array_schema(),
                "dependency_risks": _string_array_schema(),
            },
        ),
        "ui_requirements_outline": _analysis_object(
            ["user_roles", "user_flows", "screens", "ux_constraints"],
            {
                "user_roles": _string_array_schema(),
                "user_flows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "steps"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "steps": _string_array_schema(),
                        },
                    },
                },
                "screens": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                    },
                },
                "analytics_views": _string_array_schema(),
                "decision_support_needs": _string_array_schema(),
                "ux_constraints": _string_array_schema(),
            },
        ),
        "requirements_spec": {
            "type": "object",
            "required": requirements_spec_required,
            "additionalProperties": False,
            "properties": requirements_spec_properties,
        },
        "review_report": {
            "type": "object",
            "required": ["overall_status", "summary", "strengths", "issues", "recommendations"],
            "additionalProperties": False,
            "properties": {
                "overall_status": {
                    "type": "string",
                    # Расширено: prompt задачи `requirements_spec_review` использует
                    # более выразительные значения. Старые `needs_changes` и
                    # `needs_user_input` остаются для обратной совместимости.
                    "enum": [
                        "passed",
                        "passed_with_remarks",
                        "needs_changes",
                        "needs_user_input",
                        "failed_needs_rework",
                    ],
                },
                "confidence": {"type": "number"},
                "summary": {"type": "string"},
                "strengths": _string_array_schema(),
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["severity", "message"],
                        "additionalProperties": False,
                        "properties": {
                            "area": {"type": "string"},
                            "severity": {"type": "string", "enum": ["info", "warning", "error", "critical"]},
                            "message": {"type": "string"},
                            "requires_user_input": {"type": "boolean"},
                        },
                    },
                },
                "recommendations": _string_array_schema(),
            },
        },
    }
    if artifact_role not in schemas:
        raise ValidationError(f"Неизвестный контракт артефакта: {artifact_role}")
    return schemas[artifact_role]


def validate_json_schema(value: Any, schema: JSONSchema, path: str = "$") -> None:
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            raise ValidationError(f"{path}: ожидался объект")
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ValidationError(f"{path}: отсутствует обязательное поле '{key}'")
        properties = schema.get("properties", {})
        if not schema.get("additionalProperties", True):
            unknown_keys = set(value) - set(properties)
            if unknown_keys:
                raise ValidationError(f"{path}: неизвестные поля {sorted(unknown_keys)}")
        for key, property_schema in properties.items():
            if key in value:
                validate_json_schema(value[key], property_schema, f"{path}.{key}")
        return
    if schema_type == "array":
        if not isinstance(value, list):
            raise ValidationError(f"{path}: ожидался список")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, f"{path}[{index}]")
        return
    if schema_type == "string":
        if not isinstance(value, str):
            raise ValidationError(f"{path}: ожидалась строка")
        allowed = schema.get("enum")
        if allowed and value not in allowed:
            raise ValidationError(f"{path}: недопустимое значение '{value}'")
        return
    if schema_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValidationError(f"{path}: ожидалось число")
        return
    if schema_type == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"{path}: ожидалось логическое значение")
        return
    raise ValidationError(f"{path}: неподдерживаемый тип схемы '{schema_type}'")


def schema_instruction(role: str, domain_pack_refs: tuple[str, ...]) -> str:
    schema = artifact_schema(role, domain_pack_refs)
    return (
        "Верни строго JSON, соответствующий этой схеме.\n"
        "Если данных недостаточно для уверенного вывода, не выдумывай: оставь "
        "поле незаполненным или отметь его как assumption. При желании укажи "
        "`confidence` (0..1) — это поле необязательное и будет вынесено в "
        "метаданные артефакта, поэтому в самом содержании цифру повторять "
        "не нужно. Вопросы к пользователю формируются отдельно через "
        "pre-flight планирование и реестр решений — не записывай их в артефакт.\n"
        f"Роль артефакта: {role}\n"
        f"Схема: {schema}"
    )


def _render_bulleted(lines: list[str], items: list[Any]) -> None:
    """Helper: добавить буллет-список с разнесением многострочных пунктов."""
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        # Если пункт многострочный — преобразуем переводы строк в
        # markdown-перенос (двойной пробел в конце строки), чтобы абзацы
        # внутри одного пункта рендерились нормально.
        text = text.replace("\n", "  \n  ")
        lines.append(f"- {text}")


def _intro_for(label: str, count: int) -> str:
    """Короткая вводная фраза перед буллетным списком, чтобы документ читался
    как текст с логикой, а не как набор отдельных списков. Цель — связность.
    """
    if count == 0:
        return ""
    if count == 1:
        return f"_Ниже фиксируется ключевой пункт по разделу «{label}»._"
    return f"_Ниже зафиксировано {count} пункт(ов) по разделу «{label}»._"


def _render_requirements_spec(payload: dict[str, Any]) -> str:
    """Рендерит ТЗ как связный документ, а не как набор плоских списков.

    Принципы:
    • Документ начинается с краткого резюме (Executive Summary) — главное
      сообщение читателю.
    • Каждый раздел имеет НАРРАТИВНУЮ ВСТАВКУ перед буллет-списком,
      чтобы читатель понимал, ЗАЧЕМ этот раздел и что в нём искать.
    • Парные блоки (что входит / что не входит) даются рядом с явным
      контрастом.
    • Риски — таблицей, а не списком: даёт сразу видимую структуру.
    • Доменные расширения (frontend / ml / security / integration) идут в
      собственных подразделах с горизонтальной чертой-разделителем.
    • Маркеры разделов (🎯, 🛡, 🔌, 💡) — лёгкая визуальная навигация.
    """
    lines: list[str] = []

    title = (payload.get("title") or "Техническое задание").strip()
    lines.append(f"# {title}")
    lines.append("")

    # ----- Введение / резюме --------------------------------------------------
    summary = (payload.get("executive_summary") or "").strip()
    if summary:
        # Резюме идёт «цитатой» — визуально выделяется в начале документа.
        lines.append("> " + summary.replace("\n", "\n> "))
        lines.append("")

    business_context = (payload.get("business_context") or "").strip()
    if business_context:
        lines.append("## Контекст и постановка задачи")
        lines.append("")
        lines.append(business_context)
        lines.append("")

    business_goal = (payload.get("business_goal") or "").strip()
    if business_goal:
        lines.append("## 🎯 Бизнес-цель")
        lines.append("")
        lines.append(business_goal)
        lines.append("")

    target_outcomes = payload.get("target_outcomes") or []
    if target_outcomes:
        lines.append("### Целевые результаты")
        lines.append("")
        lines.append(_intro_for("целевые результаты", len(target_outcomes)))
        lines.append("")
        _render_bulleted(lines, target_outcomes)
        lines.append("")

    # ----- Границы проекта ----------------------------------------------------
    scope_in = payload.get("scope_in") or []
    scope_out = payload.get("scope_out") or []
    if scope_in or scope_out:
        lines.append("---")
        lines.append("")
        lines.append("## Границы текущего этапа")
        lines.append("")
        lines.append(
            "Раздел фиксирует, какие результаты PoV команда обязуется поставить, "
            "и какие пункты сознательно вынесены за рамки этапа. Это защита от "
            "scope creep и явная точка для последующих обсуждений."
        )
        lines.append("")
        if scope_in:
            lines.append("**✅ Входит в этап**")
            lines.append("")
            _render_bulleted(lines, scope_in)
            lines.append("")
        if scope_out:
            lines.append("**⛔️ Не входит в этап (отнесено к следующим этапам)**")
            lines.append("")
            _render_bulleted(lines, scope_out)
            lines.append("")

    # ----- Стейкхолдеры -------------------------------------------------------
    stakeholders = payload.get("stakeholders") or payload.get("actors") or []
    if stakeholders:
        lines.append("## 👥 Стейкхолдеры и роли")
        lines.append("")
        lines.append(
            "Здесь перечислены ключевые роли, влияющие на проект: владелец, "
            "согласующие, ответственные за данные и инфраструктуру, конечные "
            "пользователи."
        )
        lines.append("")
        _render_bulleted(lines, stakeholders)
        lines.append("")

    operating_model = payload.get("operating_model") or []
    if operating_model:
        lines.append("### Операционная модель")
        lines.append("")
        _render_bulleted(lines, operating_model)
        lines.append("")

    # ----- Пользовательские сценарии -----------------------------------------
    user_stories = payload.get("user_stories") or []
    if user_stories:
        lines.append("---")
        lines.append("")
        lines.append("## 📋 Пользовательские сценарии")
        lines.append("")
        lines.append(
            "Сценарии описывают типовые пути использования решения "
            "конкретными ролями. Они служат основой для проектирования "
            "интерфейса и приёмки."
        )
        lines.append("")
        _render_bulleted(lines, user_stories)
        lines.append("")

    # ----- Требования ---------------------------------------------------------
    data_reqs = payload.get("data_requirements") or []
    func_reqs = payload.get("functional_requirements") or []
    non_func_reqs = payload.get("non_functional_requirements") or []
    integration_reqs = payload.get("integration_requirements") or []
    security_reqs = payload.get("security_requirements") or []
    deployment_reqs = payload.get("deployment_requirements") or []

    if any([data_reqs, func_reqs, non_func_reqs, integration_reqs, security_reqs, deployment_reqs]):
        lines.append("---")
        lines.append("")
        lines.append("## Требования к решению")
        lines.append("")
        lines.append(
            "Требования сгруппированы по типу — от функциональных до "
            "инфраструктурных. Каждый пункт должен быть проверяем на "
            "финальной приёмке."
        )
        lines.append("")

        if func_reqs:
            lines.append("### Функциональные требования")
            lines.append("")
            _render_bulleted(lines, func_reqs)
            lines.append("")

        if non_func_reqs:
            lines.append("### Нефункциональные требования")
            lines.append("")
            _render_bulleted(lines, non_func_reqs)
            lines.append("")

        if data_reqs:
            lines.append("### Требования к данным")
            lines.append("")
            _render_bulleted(lines, data_reqs)
            lines.append("")

        if integration_reqs:
            lines.append("### 🔌 Интеграционные требования")
            lines.append("")
            _render_bulleted(lines, integration_reqs)
            lines.append("")

        if security_reqs:
            lines.append("### 🛡 Требования ИБ и комплаенса")
            lines.append("")
            _render_bulleted(lines, security_reqs)
            lines.append("")

        if deployment_reqs:
            lines.append("### Требования к развёртыванию")
            lines.append("")
            _render_bulleted(lines, deployment_reqs)
            lines.append("")

    # ----- Доменные расширения ------------------------------------------------
    frontend = payload.get("frontend_requirements")
    if frontend:
        lines.append("---")
        lines.append("")
        lines.append("## 🖥 Требования к интерфейсу")
        lines.append("")
        lines.append(
            "Этот блок описывает пользовательскую часть решения: кто будет "
            "работать с системой, какие сценарии он проходит, какие экраны и "
            "представления видит."
        )
        lines.append("")
        if frontend.get("user_roles"):
            lines.append("### Пользовательские роли")
            lines.append("")
            _render_bulleted(lines, frontend["user_roles"])
            lines.append("")
        if frontend.get("user_flows"):
            lines.append("### Пользовательские потоки")
            lines.append("")
            _render_bulleted(lines, frontend["user_flows"])
            lines.append("")
        if frontend.get("screens"):
            lines.append("### Экраны")
            lines.append("")
            _render_bulleted(lines, frontend["screens"])
            lines.append("")
        if frontend.get("analytics_views"):
            lines.append("### Аналитические представления")
            lines.append("")
            _render_bulleted(lines, frontend["analytics_views"])
            lines.append("")
        if frontend.get("decision_support_needs"):
            lines.append("### Поддержка принятия решений")
            lines.append("")
            _render_bulleted(lines, frontend["decision_support_needs"])
            lines.append("")
        if frontend.get("ux_constraints"):
            lines.append("### UX-ограничения и принципы")
            lines.append("")
            _render_bulleted(lines, frontend["ux_constraints"])
            lines.append("")

    ml_requirements = payload.get("ml_requirements")
    if ml_requirements:
        lines.append("---")
        lines.append("")
        lines.append("## 🤖 ML-задача и данные")
        lines.append("")
        prediction_target = (ml_requirements.get("prediction_target") or "").strip()
        prediction_horizon = (ml_requirements.get("prediction_horizon") or "").strip()
        prediction_unit = (ml_requirements.get("prediction_unit") or "").strip()
        if prediction_target:
            lines.append(f"**Цель предсказания.** {prediction_target}")
            lines.append("")
        if prediction_horizon:
            lines.append(f"**Горизонт прогноза.** {prediction_horizon}")
            lines.append("")
        if prediction_unit:
            lines.append(f"**Единица предсказания.** {prediction_unit}")
            lines.append("")
        if ml_requirements.get("data_sources"):
            lines.append("### Источники данных")
            lines.append("")
            _render_bulleted(lines, ml_requirements["data_sources"])
            lines.append("")
        if ml_requirements.get("model_outputs"):
            lines.append("### Выходы модели")
            lines.append("")
            _render_bulleted(lines, ml_requirements["model_outputs"])
            lines.append("")
        if ml_requirements.get("evaluation_metrics"):
            lines.append("### Метрики качества")
            lines.append("")
            _render_bulleted(lines, ml_requirements["evaluation_metrics"])
            lines.append("")
        if ml_requirements.get("explainability_requirements"):
            lines.append("### Требования к интерпретируемости")
            lines.append("")
            _render_bulleted(lines, ml_requirements["explainability_requirements"])
            lines.append("")

    security_detail = payload.get("security_constraints_detail")
    if security_detail:
        lines.append("---")
        lines.append("")
        lines.append("## 🔒 Детальные ограничения ИБ и комплаенса")
        lines.append("")
        lines.append(
            "Раздел раскрывает требования к контуру решения: где живут данные, "
            "как защищены, как контролируется доступ, что допустимо в работе с ИИ."
        )
        lines.append("")
        if security_detail.get("deployment_constraints"):
            lines.append("### Контур и развёртывание")
            lines.append("")
            _render_bulleted(lines, security_detail["deployment_constraints"])
            lines.append("")
        if security_detail.get("privacy_constraints"):
            lines.append("### Приватность данных")
            lines.append("")
            _render_bulleted(lines, security_detail["privacy_constraints"])
            lines.append("")
        if security_detail.get("access_control_constraints"):
            lines.append("### Контроль доступа")
            lines.append("")
            _render_bulleted(lines, security_detail["access_control_constraints"])
            lines.append("")
        if security_detail.get("allowed_ai_usage"):
            lines.append("### Допустимое использование ИИ")
            lines.append("")
            _render_bulleted(lines, security_detail["allowed_ai_usage"])
            lines.append("")
        if security_detail.get("mandatory_controls"):
            lines.append("### Обязательные меры контроля")
            lines.append("")
            _render_bulleted(lines, security_detail["mandatory_controls"])
            lines.append("")

    integration_model = payload.get("integration_model")
    if integration_model:
        lines.append("---")
        lines.append("")
        lines.append("## 🔌 Интеграционная модель")
        lines.append("")
        lines.append(
            "Описание того, откуда поступают данные, в каком виде, как часто, "
            "куда уходит результат, и кто обеспечивает эксплуатацию связей."
        )
        lines.append("")
        if integration_model.get("source_systems"):
            lines.append("### Системы-источники")
            lines.append("")
            _render_bulleted(lines, integration_model["source_systems"])
            lines.append("")
        if integration_model.get("delivery_pattern"):
            lines.append("### Способ доставки данных")
            lines.append("")
            _render_bulleted(lines, integration_model["delivery_pattern"])
            lines.append("")
        refresh = (integration_model.get("refresh_model") or "").strip()
        if refresh:
            lines.append(f"**Модель обновления.** {refresh}")
            lines.append("")
        if integration_model.get("target_surfaces"):
            lines.append("### Точки потребления результата")
            lines.append("")
            _render_bulleted(lines, integration_model["target_surfaces"])
            lines.append("")
        if integration_model.get("operating_roles"):
            lines.append("### Операционные роли")
            lines.append("")
            _render_bulleted(lines, integration_model["operating_roles"])
            lines.append("")

    # ----- Топология развёртывания -------------------------------------------
    deployment_topology = payload.get("deployment_topology") or {}
    if deployment_topology:
        lines.append("---")
        lines.append("")
        lines.append("## 🏗 Топология развёртывания")
        lines.append("")
        lines.append(
            "Раздел описывает, как и где физически разворачивается решение: "
            "среды, сетевые контуры, где живут ключевые компоненты, как "
            "выглядит цикл поставки и обновлений."
        )
        lines.append("")
        environments = deployment_topology.get("environments") or []
        if environments:
            lines.append("### Среды")
            lines.append("")
            lines.append("| Среда | Назначение | Масштаб |")
            lines.append("|---|---|---|")
            for env in environments:
                if not isinstance(env, dict):
                    continue
                name = (env.get("name") or "").strip() or "—"
                purpose = (env.get("purpose") or "").strip() or "—"
                scaling = (env.get("scaling") or "").strip() or "—"
                lines.append(f"| {name} | {purpose} | {scaling} |")
            lines.append("")
        network_zones = deployment_topology.get("network_zones") or []
        if network_zones:
            lines.append("### Сетевые контуры")
            lines.append("")
            _render_bulleted(lines, network_zones)
            lines.append("")
        components = deployment_topology.get("components") or []
        if components:
            lines.append("### Размещение компонентов")
            lines.append("")
            lines.append("| Компонент | Размещение | Технология | Назначение |")
            lines.append("|---|---|---|---|")
            for comp in components:
                if not isinstance(comp, dict):
                    continue
                name = (comp.get("name") or "").strip() or "—"
                placement = (comp.get("placement") or "").strip() or "—"
                technology = (comp.get("technology") or "").strip() or "—"
                resp = (comp.get("responsibilities") or "").strip() or "—"
                lines.append(f"| {name} | {placement} | {technology} | {resp} |")
            lines.append("")
        deployment_flow = (deployment_topology.get("deployment_flow") or "").strip()
        if deployment_flow:
            lines.append("### Цикл поставки и обновлений")
            lines.append("")
            lines.append(deployment_flow)
            lines.append("")

    # ----- DPIA / Оценка воздействия на персональные данные ------------------
    privacy_impact = payload.get("privacy_impact") or {}
    if privacy_impact:
        lines.append("---")
        lines.append("")
        lines.append("## 🔐 Оценка воздействия на персональные данные (DPIA)")
        lines.append("")
        lines.append(
            "Формальный раздел для DPO/ИБ заказчика: какие категории ПДн "
            "обрабатываются, на каком основании, какими мерами защищены и "
            "какие остаточные риски остаются."
        )
        lines.append("")
        pii_categories = privacy_impact.get("pii_categories") or []
        if pii_categories:
            lines.append("**Категории ПДн.**")
            lines.append("")
            _render_bulleted(lines, pii_categories)
            lines.append("")
        else:
            lines.append(
                "В рамках PoV персональные данные не обрабатываются "
                "(используются обезличенные или синтетические данные)."
            )
            lines.append("")
        processing_purposes = privacy_impact.get("processing_purposes") or []
        if processing_purposes:
            lines.append("**Цели обработки.**")
            lines.append("")
            _render_bulleted(lines, processing_purposes)
            lines.append("")
        lawful_basis = (privacy_impact.get("lawful_basis") or "").strip()
        if lawful_basis:
            lines.append(f"**Правовое основание.** {lawful_basis}")
            lines.append("")
        data_min = privacy_impact.get("data_minimization") or []
        if data_min:
            lines.append("**Минимизация данных.**")
            lines.append("")
            _render_bulleted(lines, data_min)
            lines.append("")
        controls = privacy_impact.get("controls") or []
        if controls:
            lines.append("**Меры контроля.**")
            lines.append("")
            _render_bulleted(lines, controls)
            lines.append("")
        dsr = (privacy_impact.get("data_subject_rights") or "").strip()
        if dsr:
            lines.append(f"**Права субъектов ПДн.** {dsr}")
            lines.append("")
        cross_border = (privacy_impact.get("cross_border_transfers") or "").strip()
        if cross_border:
            lines.append(f"**Трансграничная передача.** {cross_border}")
            lines.append("")
        retention = (privacy_impact.get("retention_policy") or "").strip()
        if retention:
            lines.append(f"**Хранение и ретенция.** {retention}")
            lines.append("")
        residual = privacy_impact.get("residual_risks") or []
        if residual:
            lines.append("**Остаточные риски.**")
            lines.append("")
            _render_bulleted(lines, residual)
            lines.append("")

    # ----- Результаты и критерии приёмки -------------------------------------
    delivery_artifacts = payload.get("delivery_artifacts") or []
    acceptance = payload.get("acceptance_criteria") or []
    success = payload.get("success_criteria") or []
    if delivery_artifacts or acceptance or success:
        lines.append("---")
        lines.append("")
        lines.append("## ✅ Результаты и приёмка")
        lines.append("")
        lines.append(
            "Конкретные результаты этапа и измеримые критерии, по которым "
            "результат принимается заказчиком."
        )
        lines.append("")
        if delivery_artifacts:
            lines.append("### Поставляемые результаты")
            lines.append("")
            _render_bulleted(lines, delivery_artifacts)
            lines.append("")
        if success:
            lines.append("### Критерии успеха")
            lines.append("")
            _render_bulleted(lines, success)
            lines.append("")
        if acceptance:
            lines.append("### Критерии приёмки")
            lines.append("")
            _render_bulleted(lines, acceptance)
            lines.append("")

    # ----- План этапов --------------------------------------------------------
    phased = payload.get("phased_plan") or []
    if phased:
        lines.append("---")
        lines.append("")
        lines.append("## 🗓 Этапы реализации")
        lines.append("")
        lines.append(
            "Крупная декомпозиция работы по фазам. Конкретные сроки и "
            "состав работ внутри фаз уточняются на этапе подготовки контракта."
        )
        lines.append("")
        _render_bulleted(lines, phased)
        lines.append("")

    # ----- Альтернативы, допущения, риски, открытые вопросы ------------------
    alternatives = payload.get("alternatives_considered") or []
    assumptions = payload.get("assumptions") or []
    risks = payload.get("risks") or []
    open_questions = payload.get("open_questions") or []

    if alternatives:
        lines.append("## 🧭 Рассмотренные альтернативы")
        lines.append("")
        lines.append(
            "Альтернативные варианты архитектуры, между которыми сделан "
            "осознанный выбор. Зафиксированы здесь для прозрачности и для "
            "повторного обсуждения, если контекст изменится."
        )
        lines.append("")
        _render_bulleted(lines, alternatives)
        lines.append("")

    if assumptions:
        lines.append("## 💡 Допущения")
        lines.append("")
        lines.append(
            "Рабочие предположения, на которых строится решение. При "
            "несоответствии реальности — пересмотр соответствующих разделов."
        )
        lines.append("")
        _render_bulleted(lines, assumptions)
        lines.append("")

    risks_detail = payload.get("project_risks_detail") or []
    if risks_detail:
        lines.append("## ⚠️ Реестр рисков")
        lines.append("")
        lines.append(
            "Структурированный реестр известных рисков PoV с оценкой "
            "вероятности и влияния, митигацией, триггером эскалации и "
            "владельцем. Пополняется по ходу реализации."
        )
        lines.append("")
        lines.append("| # | Риск | Категория | Вероятность | Влияние | Митигация | Триггер | Владелец |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for idx, risk in enumerate(risks_detail, start=1):
            if not isinstance(risk, dict):
                continue
            title_risk = (risk.get("title") or "").strip().replace("|", "\\|") or "—"
            category = (risk.get("category") or "").strip().replace("|", "\\|") or "—"
            prob = (risk.get("probability") or "").strip() or "—"
            impact = (risk.get("impact") or "").strip() or "—"
            mitigation = (risk.get("mitigation") or "").strip().replace("|", "\\|") or "—"
            trigger = (risk.get("trigger") or "").strip().replace("|", "\\|") or "—"
            owner = (risk.get("owner") or "").strip().replace("|", "\\|") or "—"
            lines.append(
                f"| {idx} | {title_risk} | {category} | {prob} | {impact} | "
                f"{mitigation} | {trigger} | {owner} |"
            )
        lines.append("")
        # Дополнительно — короткие описания, чтобы таблица не оставалась без
        # контекста, если описание риска длиннее одной строки.
        descriptions = [
            (r.get("title", "").strip(), (r.get("description") or "").strip())
            for r in risks_detail
            if isinstance(r, dict) and (r.get("description") or "").strip()
        ]
        if descriptions:
            lines.append("**Расшифровки рисков.**")
            lines.append("")
            for title_risk, desc in descriptions:
                if not title_risk:
                    continue
                lines.append(f"- **{title_risk}.** {desc}")
            lines.append("")
    elif risks:
        lines.append("## ⚠️ Риски")
        lines.append("")
        lines.append(
            "Известные риски проекта с указанием митигации. Список не "
            "исчерпывающий и пополняется по ходу реализации."
        )
        lines.append("")
        _render_bulleted(lines, risks)
        lines.append("")

    if open_questions:
        lines.append("## ❓ Открытые вопросы")
        lines.append("")
        lines.append(
            "Вопросы, которые остаются на согласование с заказчиком. Их "
            "наличие — нормальная часть PoV-документации: они отражают "
            "осознанные точки уточнения, а не пробелы в проработке."
        )
        lines.append("")
        _render_bulleted(lines, open_questions)
        lines.append("")

    # ----- Глоссарий (приложение) --------------------------------------------
    glossary = payload.get("glossary") or []
    if glossary:
        lines.append("---")
        lines.append("")
        lines.append("## 📖 Глоссарий")
        lines.append("")
        lines.append(
            "Словарь ключевых терминов, ролей, систем и метрик, упомянутых "
            "в документе, — для общего понимания заказчиком и подрядчиком."
        )
        lines.append("")
        for entry in glossary:
            if not isinstance(entry, dict):
                continue
            term = (entry.get("term") or "").strip()
            definition = (entry.get("definition") or "").strip()
            if not term or not definition:
                continue
            category = (entry.get("category") or "").strip()
            synonyms = entry.get("synonyms") or []
            suffix = ""
            if synonyms:
                suffix = f" _(синонимы: {', '.join(str(s).strip() for s in synonyms if str(s).strip())})_"
            tag = f" _[{category}]_" if category else ""
            lines.append(f"- **{term}**{tag} — {definition}{suffix}")
        lines.append("")

    # Финальная пустая строка для аккуратности.
    if lines and lines[-1] != "":
        lines.append("")
    return "\n".join(lines)


def render_markdown(artifact_role: str, payload: dict[str, Any]) -> str:
    if artifact_role == "clarification_notes":
        sections = [
            "# Уточнение бизнес-цели",
            f"## Уточнённая цель\n{payload['clarified_goal']}",
            "## Критерии успеха\n" + "\n".join(f"- {item}" for item in payload["success_criteria"]),
            "## Допущения\n" + "\n".join(f"- {item}" for item in payload["assumptions"]),
            "## Открытые вопросы\n" + "\n".join(f"- {item}" for item in payload["open_questions"]),
        ]
        return "\n\n".join(sections)

    if artifact_role == "user_story_map":
        lines = ["# Карта user story", "## Роли"]
        lines.extend(f"- {actor['name']}: {', '.join(actor['needs'])}" for actor in payload["actors"])
        lines.append("\n## User story")
        lines.extend(
            f"- Как {item['actor']}, я хочу {item['story']}, чтобы {item['value']}"
            for item in payload["user_stories"]
        )
        lines.append("\n## Граничные случаи")
        lines.extend(f"- {item}" for item in payload["edge_cases"])
        return "\n".join(lines)

    if artifact_role == "alternatives_analysis":
        lines = [
            "# Анализ альтернатив",
            f"## Рекомендованный вариант\n{payload['recommended_option']}",
            "## Обоснование",
            payload["rationale"],
            "## Варианты",
        ]
        for item in payload["alternatives"]:
            lines.append(f"### {item['name']}")
            lines.append(item["description"])
            lines.append("Плюсы:")
            lines.extend(f"- {entry}" for entry in item["pros"])
            lines.append("Минусы:")
            lines.extend(f"- {entry}" for entry in item["cons"])
        return "\n".join(lines)

    if artifact_role == "normalized_request":
        return "\n".join(
            [
                "# Нормализованный запрос",
                f"## Краткое резюме\n{payload['request_summary']}",
                f"## Бизнес-проблема\n{payload['business_problem']}",
                "## Запрошенные элементы решения",
                *[f"- {item}" for item in payload["requested_solution_elements"]],
                "\n## Явные ограничения",
                *[f"- {item}" for item in payload["explicit_constraints"]],
                "\n## Неявные риски",
                *[f"- {item}" for item in payload["implicit_risks"]],
                "\n## Неоднозначности",
                *[f"- {item}" for item in payload["ambiguous_points"]],
            ]
        )

    if artifact_role == "request_fact_sheet":
        return "\n".join(
            [
                "# Факты исходного запроса",
                "## Явно названные факты",
                *[f"- {item}" for item in payload["explicit_facts"]],
                "\n## Названные сущности",
                *[f"- {item}" for item in payload["named_entities"]],
                "\n## Упомянутые результаты и поставка",
                *[f"- {item}" for item in payload["requested_deliverables"]],
                "\n## Упомянутые системы и источники",
                *[f"- {item}" for item in payload["mentioned_systems_and_sources"]],
                "\n## Упомянутые метрики и целевые значения",
                *[f"- {item}" for item in payload["mentioned_metrics_and_targets"]],
            ]
        )

    if artifact_role == "goal_hypothesis":
        return "\n".join(
            [
                "# Гипотеза цели проекта",
                f"## Рабочая формулировка цели\n{payload['hypothesized_goal']}",
                "\n## Ожидаемые эффекты",
                *[f"- {item}" for item in payload["expected_effects"]],
                f"\n## Гипотеза о стадии проекта\n{payload['project_stage_hypothesis']}",
                "\n## Сигналы успеха",
                *[f"- {item}" for item in payload["success_signals"]],
                "\n## Непрояснённые части цели",
                *[f"- {item}" for item in payload["unresolved_goal_points"]],
            ]
        )

    if artifact_role == "constraint_inventory":
        return "\n".join(
            [
                "# Инвентаризация ограничений",
                "## Явные ограничения",
                *[f"- {item}" for item in payload["explicit_constraints"]],
                "\n## Подразумеваемые ограничения",
                *[f"- {item}" for item in payload["inferred_constraints"]],
                "\n## Ограничения текущего этапа",
                *[f"- {item}" for item in payload["stage_constraints"]],
                "\n## Ограничения среды и контура",
                *[f"- {item}" for item in payload["environment_constraints"]],
                "\n## Зависимости и внешние условия",
                *[f"- {item}" for item in payload["dependency_constraints"]],
            ]
        )

    if artifact_role == "ambiguity_gap_report":
        return "\n".join(
            [
                "# Неоднозначности и пробелы запроса",
                "## Неоднозначные места",
                *[f"- {item}" for item in payload["ambiguous_points"]],
                "\n## Конфликтующие сигналы",
                *[f"- {item}" for item in payload["conflicting_signals"]],
                "\n## Недостающие решения",
                *[f"- {item}" for item in payload["missing_decisions"]],
                "\n## Безопасные рабочие допущения",
                *[f"- {item}" for item in payload["safe_assumptions"]],
                "\n## Кандидаты на эскалацию",
                *[f"- {item}" for item in payload["escalation_candidates"]],
            ]
        )

    if artifact_role == "business_outcome_model":
        return "\n".join(
            [
                "# Модель бизнес-результата",
                f"## Основная цель\n{payload['primary_business_goal']}",
                "## KPI",
                *[f"- {item}" for item in payload["target_kpis"]],
                "\n## Метрики успеха",
                *[f"- {item}" for item in payload["success_metrics"]],
                "\n## Влияние на процессы",
                *[f"- {item}" for item in payload["business_process_impacts"]],
                "\n## Какие решения должен поддержать результат",
                *[f"- {item}" for item in payload["expected_decisions"]],
                "\n## Гипотезы ценности",
                *[f"- {item}" for item in payload["value_hypotheses"]],
                "\n## Допущения",
                *[f"- {item}" for item in payload["assumptions"]],
            ]
        )

    if artifact_role == "scope_boundary_matrix":
        return "\n".join(
            [
                "# Границы и рамка этапа",
                "## Входит в текущий этап",
                *[f"- {item}" for item in payload["in_scope"]],
                "\n## Не входит в текущий этап",
                *[f"- {item}" for item in payload["out_of_scope"]],
                "\n## Границы пилота",
                *[f"- {item}" for item in payload["pilot_boundaries"]],
                "\n## Кандидаты на следующие фазы",
                *[f"- {item}" for item in payload["future_phase_candidates"]],
                "\n## Обязательные результаты этапа",
                *[f"- {item}" for item in payload["mandatory_deliverables"]],
                "\n## Исключённые результаты этапа",
                *[f"- {item}" for item in payload["excluded_deliverables"]],
            ]
        )

    if artifact_role == "stakeholder_map":
        lines = ["# Карта стейкхолдеров", "## Группы стейкхолдеров"]
        for item in payload["stakeholder_groups"]:
            lines.append(f"### {item['name']}")
            lines.append(f"Роль: {item['role']}")
            lines.append(f"Влияние: {item['influence']}")
            lines.append("Ожидания:")
            lines.extend(f"- {entry}" for entry in item["expectations"])
        lines.extend(
            [
                "\n## Основные пользователи",
                *[f"- {item}" for item in payload["primary_users"]],
                "\n## Владельцы данных",
                *[f"- {item}" for item in payload["data_owners"]],
                "\n## Поддерживающие команды",
                *[f"- {item}" for item in payload["support_teams"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "decision_ownership_matrix":
        lines = ["# Владельцы решений и согласований", "## Решения"]
        for item in payload["decisions"]:
            lines.append(f"### {item['name']}")
            lines.append(f"Владелец: {item['owner']}")
            lines.append("Участники:")
            lines.extend(f"- {entry}" for entry in item["participants"])
            lines.append(f"Когда принимается: {item['timing']}")
        lines.extend(
            [
                "\n## Решения без владельца",
                *[f"- {item}" for item in payload["unowned_decisions"]],
                "\n## Точки согласования",
                *[f"- {item}" for item in payload["approval_points"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "operating_model_outline":
        return "\n".join(
            [
                "# Черновой контур операционной модели",
                "## Ход процесса",
                *[f"- {item}" for item in payload["process_flow"]],
                "\n## Роли-поставщики",
                *[f"- {item}" for item in payload["producer_roles"]],
                "\n## Роли-получатели результата",
                *[f"- {item}" for item in payload["consumer_roles"]],
                "\n## Роли поддержки",
                *[f"- {item}" for item in payload["support_roles"]],
                "\n## Риски передачи ответственности",
                *[f"- {item}" for item in payload["handoff_risks"]],
            ]
        )

    if artifact_role == "stakeholder_operating_model":
        lines = ["# Стейкхолдеры и операционная модель", "## Группы стейкхолдеров"]
        for item in payload["stakeholder_groups"]:
            lines.append(f"### {item['name']}")
            lines.append(f"Роль: {item['role']}")
            lines.append("Ожидания:")
            lines.extend(f"- {entry}" for entry in item["expectations"])
            lines.append("Ответственность:")
            lines.extend(f"- {entry}" for entry in item["responsibilities"])
        lines.extend(
            [
                "\n## Основные пользователи",
                *[f"- {item}" for item in payload["primary_users"]],
                "\n## Владельцы решений",
                *[f"- {item}" for item in payload["decision_owners"]],
                "\n## Операционная модель",
                *[f"- {item}" for item in payload["operating_model"]],
                "\n## Ограничения внедрения",
                *[f"- {item}" for item in payload["adoption_constraints"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "solution_option_inventory":
        lines = ["# Набор вариантов решения", "## Варианты"]
        for item in payload["options"]:
            lines.append(f"### {item['name']}")
            lines.append(item["summary"])
            lines.append(f"Соответствие рамке этапа: {item['boundary_fit']}")
            lines.append("Условия применимости:")
            lines.extend(f"- {entry}" for entry in item["enabling_conditions"])
        lines.extend(
            [
                "\n## Оси сравнения",
                *[f"- {item}" for item in payload["comparison_axes"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "solution_tradeoff_matrix":
        lines = ["# Матрица вариантов решения", "## Варианты"]
        for item in payload["options"]:
            lines.append(f"### {item['name']}")
            lines.append(item["summary"])
            lines.append(f"Пригодность для этапа: {item['fit_for_pilot']}")
            lines.append("Плюсы:")
            lines.extend(f"- {entry}" for entry in item["pros"])
            lines.append("Минусы:")
            lines.extend(f"- {entry}" for entry in item["cons"])
            lines.append("Риски:")
            lines.extend(f"- {entry}" for entry in item["risks"])
        lines.extend(
            [
                f"\n## Рекомендуемый вариант\n{payload['recommended_option']}",
                "\n## Обоснование",
                payload["recommendation_rationale"],
                "\n## Отложенные решения",
                *[f"- {item}" for item in payload["deferred_decisions"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "delivery_scope_definition":
        return "\n".join(
            [
                "# Состав поставки этапа",
                "## Что должно быть поставлено",
                *[f"- {item}" for item in payload["delivery_items"]],
                "\n## Что не обещается на этом этапе",
                *[f"- {item}" for item in payload["excluded_items"]],
                "\n## Ожидания к демонстрации результата",
                *[f"- {item}" for item in payload["demo_expectations"]],
                "\n## Артефакты-доказательства",
                *[f"- {item}" for item in payload["evidence_artifacts"]],
            ]
        )

    if artifact_role == "acceptance_model_definition":
        return "\n".join(
            [
                "# Модель приемки этапа",
                "## Критерии приемки",
                *[f"- {item}" for item in payload["acceptance_criteria"]],
                "\n## Подтверждающие доказательства",
                *[f"- {item}" for item in payload["success_evidence"]],
                "\n## Входы от заказчика",
                *[f"- {item}" for item in payload["required_customer_inputs"]],
                "\n## Формальные согласования",
                *[f"- {item}" for item in payload["formal_approvals"]],
                "\n## Основания для отклонения результата",
                *[f"- {item}" for item in payload["rejection_conditions"]],
            ]
        )

    if artifact_role == "delivery_acceptance_plan":
        return "\n".join(
            [
                "# Модель поставки и приемки",
                "## Результаты этапа",
                *[f"- {item}" for item in payload["delivery_items"]],
                "\n## Критерии приемки",
                *[f"- {item}" for item in payload["acceptance_criteria"]],
                "\n## Подтверждающие доказательства",
                *[f"- {item}" for item in payload["success_evidence"]],
                "\n## Входы от заказчика",
                *[f"- {item}" for item in payload["required_customer_inputs"]],
                "\n## Формальные согласования",
                *[f"- {item}" for item in payload["formal_approvals"]],
                "\n## Открытые зависимости",
                *[f"- {item}" for item in payload["open_dependencies"]],
            ]
        )

    if artifact_role == "dependency_map":
        return "\n".join(
            [
                "# Критические зависимости и входы",
                "## Критические зависимости",
                *[f"- {item}" for item in payload["critical_dependencies"]],
                "\n## Входы от заказчика",
                *[f"- {item}" for item in payload["customer_inputs"]],
                "\n## Внешние решения",
                *[f"- {item}" for item in payload["external_decisions"]],
                "\n## Доступы и разрешения",
                *[f"- {item}" for item in payload["access_dependencies"]],
                "\n## Условия остановки этапа",
                *[f"- {item}" for item in payload["stop_conditions"]],
            ]
        )

    if artifact_role == "implementation_dependency_plan":
        lines = ["# План реализации и зависимости", "## Фазы"]
        for phase in payload["phases"]:
            lines.append(f"### {phase['name']}")
            lines.append("Цели:")
            lines.extend(f"- {item}" for item in phase["objectives"])
            lines.append("Зависимости:")
            lines.extend(f"- {item}" for item in phase["dependencies"])
            lines.append("Выходы:")
            lines.extend(f"- {item}" for item in phase["outputs"])
        lines.extend(
            [
                "\n## Критические зависимости",
                *[f"- {item}" for item in payload["critical_dependencies"]],
                "\n## Риски проекта",
                *[f"- {item}" for item in payload["project_risks"]],
                "\n## Предлагаемый график",
                *[f"- {item}" for item in payload["proposed_timeline"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "predictive_problem_definition":
        return "\n".join(
            [
                "# Определение предиктивной задачи",
                f"## Цель предсказания\n{payload['prediction_target']}",
                f"## Горизонт прогноза\n{payload['prediction_horizon']}",
                f"## Единица предсказания\n{payload['prediction_unit']}",
                f"## Определение события\n{payload['label_definition']}",
                "\n## Бизнес-действия по результату",
                *[f"- {item}" for item in payload["business_actions"]],
                "\n## Выходы модели",
                *[f"- {item}" for item in payload["model_outputs"]],
                "\n## Метрики оценки",
                *[f"- {item}" for item in payload["evaluation_metrics"]],
                "\n## Базовые ожидания",
                *[f"- {item}" for item in payload["baseline_expectations"]],
                "\n## Требования к интерпретируемости",
                *[f"- {item}" for item in payload["explainability_requirements"]],
            ]
        )

    if artifact_role == "data_landscape_assessment":
        return "\n".join(
            [
                "# Оценка данных и реализуемости",
                "## Источники",
                *[f"- {item}" for item in payload["source_systems"]],
                "\n## Сущности",
                *[f"- {item}" for item in payload["required_entities"]],
                "\n## Ключевые признаки",
                *[f"- {item}" for item in payload["key_features"]],
                "\n## Риски качества данных",
                *[f"- {item}" for item in payload["data_quality_risks"]],
                "\n## Пробелы в данных",
                *[f"- {item}" for item in payload["data_gaps"]],
                f"\n## Оценка реализуемости\n{payload['feasibility_assessment']}",
                "\n## Замечания по приватности данных",
                *[f"- {item}" for item in payload["privacy_notes"]],
            ]
        )

    if artifact_role == "security_compliance_constraints":
        return "\n".join(
            [
                "# Ограничения ИБ и комплаенса",
                "## Ограничения развертывания",
                *[f"- {item}" for item in payload["deployment_constraints"]],
                "\n## Ограничения по приватности данных",
                *[f"- {item}" for item in payload["privacy_constraints"]],
                "\n## Ограничения контроля доступа",
                *[f"- {item}" for item in payload["access_control_constraints"]],
                "\n## Ограничения безопасности интеграций",
                *[f"- {item}" for item in payload["integration_security_constraints"]],
                "\n## Допустимое использование ИИ",
                *[f"- {item}" for item in payload["allowed_ai_usage"]],
                "\n## Обязательные меры контроля",
                *[f"- {item}" for item in payload["mandatory_controls"]],
                "\n## Комплаенс-риски",
                *[f"- {item}" for item in payload["compliance_risks"]],
            ]
        )

    if artifact_role == "integration_operating_model":
        return "\n".join(
            [
                "# Интеграционная и операционная модель",
                "## Источники и входящие интеграции",
                *[f"- {item}" for item in payload["source_integrations"]],
                "\n## Целевые точки потребления",
                *[f"- {item}" for item in payload["target_integrations"]],
                f"\n## Модель обновления\n{payload['refresh_model']}",
                "\n## Способ доставки данных",
                *[f"- {item}" for item in payload["data_delivery_pattern"]],
                "\n## Операционные роли",
                *[f"- {item}" for item in payload["operating_roles"]],
                "\n## Модель поддержки",
                *[f"- {item}" for item in payload["support_model"]],
                "\n## Риски зависимостей",
                *[f"- {item}" for item in payload["dependency_risks"]],
            ]
        )

    if artifact_role == "ui_requirements_outline":
        lines = ["# Контур UI/BI-требований", "## Роли пользователей"]
        lines.extend(f"- {item}" for item in payload["user_roles"])
        lines.append("\n## Пользовательские потоки")
        for flow in payload["user_flows"]:
            lines.append(f"### {flow['name']}")
            lines.extend(f"- {step}" for step in flow["steps"])
        lines.append("\n## Экраны")
        for screen in payload["screens"]:
            lines.append(f"- {screen['name']}: {screen['purpose']}")
        if payload.get("analytics_views"):
            lines.append("\n## Аналитические представления")
            lines.extend(f"- {item}" for item in payload["analytics_views"])
        if payload.get("decision_support_needs"):
            lines.append("\n## Сценарии поддержки решений")
            lines.extend(f"- {item}" for item in payload["decision_support_needs"])
        lines.extend(
            [
                "\n## UX-ограничения",
                *[f"- {item}" for item in payload["ux_constraints"]],
            ]
        )
        return "\n".join(lines)

    if artifact_role == "requirements_spec":
        return _render_requirements_spec(payload)

    if artifact_role == "review_report":
        lines = [
            "# Отчёт ревью",
            f"## Статус\n{payload['overall_status']}",
            f"## Резюме\n{payload['summary']}",
        ]
        # Уверенность вынесена в метаданные артефакта (overall_confidence)
        # и отображается отдельно в UI — в markdown-теле её не дублируем.
        lines.extend(["\n## Сильные стороны", *[f"- {item}" for item in payload["strengths"]], "\n## Замечания"])
        for issue in payload["issues"]:
            area = issue.get("area")
            prefix = f"[{area}] " if area else ""
            user_flag = " (нужен ввод пользователя)" if issue.get("requires_user_input") else ""
            lines.append(f"- [{issue['severity']}] {prefix}{issue['message']}{user_flag}")
        lines.append("\n## Рекомендации")
        lines.extend(f"- {item}" for item in payload["recommendations"])
        return "\n".join(lines)

    if artifact_role == "glossary_terms":
        entries = payload.get("entries") or []
        lines = ["# Глоссарий"]
        if payload.get("domain_scope"):
            lines.append(payload["domain_scope"])
            lines.append("")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            term = str(entry.get("term") or "").strip()
            definition = str(entry.get("definition") or "").strip()
            if not term:
                continue
            line = f"**{term}** — {definition}"
            if entry.get("category"):
                line += f" _({entry['category']})_"
            lines.append(line)
            syns = entry.get("synonyms") or []
            if syns:
                lines.append(f"  Синонимы: {', '.join(syns)}")
        return "\n\n".join(lines)

    if artifact_role == "project_risk_register":
        risks = payload.get("risks") or []
        lines = ["# Реестр рисков проекта"]
        if payload.get("summary"):
            lines.append(payload["summary"])
        lines.append("\n| # | Риск | Категория | Вероятность | Влияние | Митигация |")
        lines.append("|---|------|-----------|-------------|---------|-----------|")
        for i, r in enumerate(risks, 1):
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "—")
            cat = str(r.get("category") or "—")
            prob = str(r.get("probability") or "—")
            imp = str(r.get("impact") or "—")
            mit = str(r.get("mitigation") or "—").replace("\n", " ")
            lines.append(f"| {i} | {title} | {cat} | {prob} | {imp} | {mit} |")
        # Детали рисков с описанием/триггером/владельцем
        details = []
        for i, r in enumerate(risks, 1):
            if not isinstance(r, dict):
                continue
            description = (r.get("description") or "").strip()
            trigger = (r.get("trigger") or "").strip()
            owner = (r.get("owner") or "").strip()
            if any([description, trigger, owner]):
                block = [f"### Риск {i}: {r.get('title', '—')}"]
                if description:
                    block.append(description)
                if trigger:
                    block.append(f"**Триггер останова:** {trigger}")
                if owner:
                    block.append(f"**Ответственный:** {owner}")
                details.append("\n".join(block))
        if details:
            lines.append("\n## Детали критичных рисков\n")
            lines.append("\n\n".join(details))
        return "\n".join(lines)

    if artifact_role == "deployment_topology":
        lines = ["# Топология развёртывания"]
        envs = payload.get("environments") or []
        if envs:
            lines.append("\n## Среды")
            for env in envs:
                if not isinstance(env, dict):
                    continue
                name = env.get("name", "—")
                purpose = env.get("purpose", "")
                scaling = env.get("scaling", "")
                line = f"**{name}** — {purpose}"
                if scaling:
                    line += f" _(масштабирование: {scaling})_"
                lines.append(line)
        zones = payload.get("network_zones") or []
        if zones:
            lines.append("\n## Сетевые зоны")
            lines.extend(f"- {z}" for z in zones)
        components = payload.get("components") or []
        if components:
            lines.append("\n## Размещение компонентов")
            lines.append("\n| Компонент | Среда / зона | Технология | Зона ответственности |")
            lines.append("|-----------|--------------|------------|-----------------------|")
            for c in components:
                if not isinstance(c, dict):
                    continue
                name = c.get("name", "—")
                placement = c.get("placement", "—")
                tech = c.get("technology", "—")
                resp = c.get("responsibilities", "—")
                lines.append(f"| {name} | {placement} | {tech} | {resp} |")
        if payload.get("deployment_flow"):
            lines.append("\n## Процесс развёртывания")
            lines.append(payload["deployment_flow"])
        return "\n".join(lines)

    if artifact_role == "privacy_impact_assessment":
        lines = ["# Оценка воздействия на персональные данные (DPIA)"]
        sections = [
            ("Категории ПДн", payload.get("pii_categories")),
            ("Цели обработки", payload.get("processing_purposes")),
            ("Минимизация данных", payload.get("data_minimization")),
            ("Меры контроля", payload.get("controls")),
            ("Права субъектов ПДн", payload.get("data_subject_rights")),
            ("Остаточные риски", payload.get("residual_risks")),
        ]
        if payload.get("lawful_basis"):
            lines.append(f"\n**Правовое основание обработки:** {payload['lawful_basis']}")
        for title, items in sections:
            if items:
                lines.append(f"\n## {title}")
                lines.extend(f"- {item}" for item in items)
        if payload.get("cross_border_transfers"):
            lines.append("\n## Трансграничная передача данных")
            lines.append(payload["cross_border_transfers"])
        if payload.get("retention_policy"):
            lines.append("\n## Хранение данных")
            lines.append(payload["retention_policy"])
        return "\n".join(lines)

    raise ValidationError(f"Неизвестный рендерер артефакта: {artifact_role}")
