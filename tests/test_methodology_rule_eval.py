"""Тесты для AST-эвалюатора `if`-выражений правил методологии (W1.1).

Замещают hardcoded `_eval_rule` (3 known rule_id) на универсальный
парсер выражений. Проверяют:
1. Базовая грамматика: literals, names, comparisons, logic, arithmetic.
2. Cross-stage references: `<stage_id>.<field>` и неявная проекция через `.`.
3. Whitelisted functions: max/min/len/sum/count/second/is_null.
4. Маркер `[*]` (для совместимости со spec/02 синтаксисом).
5. Acceptance из BACKLOG #7: рабочее правило `ambiguous_choice`.
6. Отсутствие падений на неполном reasoning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.methodology_rule_eval import evaluate_rule
from pov_generator.application.methodology_rules import evaluate_methodology_rules
from pov_generator.application.registry_service import RegistryService
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

REPO_ROOT = Path(__file__).resolve().parents[1]


def _eval(expr: str, current=None, all_outputs=None) -> bool:
    return evaluate_rule(
        expr,
        current_stage_outputs=current or {},
        all_stage_outputs=all_outputs or {},
    )


# --- 1. Грамматика ----------------------------------------------------------


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("1 == 1", True),
        ("1 == 2", False),
        ("1 < 2 and 2 < 3", True),
        ("1 < 2 or 5 < 3", True),
        ("not (1 == 2)", True),
        ("3 - 1 == 2", True),
        ("0.5 - 0.45 < 0.15", True),
        ("0.5 - 0.1 < 0.15", False),
        ("null == null", True),
        ("true and not false", True),
        ('"approved" == "approved"', True),
    ],
)
def test_literal_grammar(expression: str, expected: bool) -> None:
    assert _eval(expression) is expected


def test_unknown_name_does_not_crash() -> None:
    """Имя, которого нет ни в текущей стадии, ни в общем dict, должно
    приводить к `False` (правило не срабатывает), а не к исключению."""
    assert _eval("nonexistent == null") is False


def test_invalid_syntax_returns_false() -> None:
    """Сломанное выражение — не падение, а `False`."""
    assert _eval("max(((") is False


# --- 2. Cross-stage references --------------------------------------------


def test_current_stage_name_lookup() -> None:
    assert (
        _eval(
            "declared_goal == null",
            current={"declared_goal": None},
        )
        is True
    )
    assert (
        _eval(
            "declared_goal == null",
            current={"declared_goal": "Подготовить ТЗ."},
        )
        is False
    )


def test_cross_stage_path_with_implicit_projection() -> None:
    """`option_generation.options.confidence` — синтаксис BACKLOG #7.
    `option_generation` ищется в all_outputs, `.options` достаёт список,
    `.confidence` неявно проецирует."""
    all_outputs = {
        "option_generation": {
            "options": [
                {"label": "A", "confidence": 0.5},
                {"label": "B", "confidence": 0.45},
            ],
        }
    }
    assert _eval("max(option_generation.options.confidence) >= 0.5", all_outputs=all_outputs) is True


def test_star_projection_marker_is_supported() -> None:
    """`[*]` маркер из spec/02 § методологический пакет — должен
    быть эквивалентен неявной проекции через `.`."""
    all_outputs = {
        "option_generation": {
            "options": [
                {"confidence": 0.6},
                {"confidence": 0.4},
            ],
        }
    }
    expr_star = "max(option_generation.options[*].confidence)"
    expr_plain = "max(option_generation.options.confidence)"
    assert _eval(expr_star, all_outputs=all_outputs) == _eval(expr_plain, all_outputs=all_outputs)


# --- 3. Functions ----------------------------------------------------------


def test_whitelisted_functions() -> None:
    ctx_all = {
        "option_generation": {
            "options": [
                {"confidence": 0.6},
                {"confidence": 0.4},
                {"confidence": 0.2},
            ],
        }
    }
    expressions = {
        "len(option_generation.options) >= 2": True,
        "count(option_generation.options) == 3": True,
        "max(option_generation.options.confidence) == 0.6": True,
        "min(option_generation.options.confidence) == 0.2": True,
        "second(option_generation.options.confidence) == 0.4": True,
        "sum(option_generation.options.confidence) > 1.0": True,
        "is_null(option_generation.options.confidence) == false": True,
    }
    for expr, expected in expressions.items():
        assert _eval(expr, all_outputs=ctx_all) is expected, f"failed: {expr}"


def test_unknown_function_returns_false() -> None:
    assert _eval("__import__('os').system('echo')") is False
    assert _eval("eval('1+1') == 2") is False


# --- 4. BACKLOG #7 acceptance ----------------------------------------------


def test_ambiguous_choice_fires_on_close_confidences_via_real_methodology() -> None:
    """Acceptance #1 из BACKLOG #7: правило `ambiguous_choice` из
    `process.lean_jtbd.yaml` срабатывает, когда у двух variants
    confidences = [0.5, 0.45] (разница < 0.15)."""
    snapshot, _ = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")).validate()
    methodology = snapshot.resolve_methodology_pack("process.lean_jtbd@1.0.0")
    reasoning = {
        "stages": [
            {"stage_id": "goal_framing", "outputs": {"declared_goal": "Цель определена."}},
            {
                "stage_id": "option_generation",
                "outputs": {
                    "options": [
                        {"label": "A", "confidence": 0.5},
                        {"label": "B", "confidence": 0.45},
                    ]
                },
            },
            {"stage_id": "decision", "outputs": {"chosen_option_id": None}},
        ]
    }

    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-1",
    )

    fired_rules = {o.rule_id for o in evaluation.rule_outcomes if o.fired}
    assert "ambiguous_choice" in fired_rules


def test_methodology_rules_silent_on_incomplete_reasoning() -> None:
    """Acceptance #2 из BACKLOG #7: при отсутствующем `option_generation`
    правила не должны падать — просто молча не срабатывают."""
    snapshot, _ = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")).validate()
    methodology = snapshot.resolve_methodology_pack("process.lean_jtbd@1.0.0")
    reasoning = {
        "stages": [
            {"stage_id": "goal_framing", "outputs": {"declared_goal": "Есть цель."}},
            # option_generation полностью отсутствует
            {"stage_id": "decision", "outputs": {"chosen_option_id": None}},
        ]
    }

    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-2",
    )

    # ambiguous_choice / low_overall_confidence не должны сработать,
    # потому что `option_generation` отсутствует в reasoning.
    fired = {o.rule_id for o in evaluation.rule_outcomes if o.fired}
    assert "ambiguous_choice" not in fired
    assert "low_overall_confidence" not in fired


# --- 5. Descriptive decomposition methodology (Stage 2) --------------------


def _load_descriptive_methodology():
    snapshot, _ = RegistryService(FilesystemRegistryLoader(REPO_ROOT / "templates")).validate()
    return snapshot.resolve_methodology_pack("process.descriptive_decomposition@1.0.0")


def test_descriptive_decomposition_missing_entities_fires_when_empty_list() -> None:
    """`missing_entities` срабатывает, когда entity_identification.entities пуст."""
    methodology = _load_descriptive_methodology()
    reasoning = {
        "stages": [
            {"stage_id": "scope_framing", "outputs": {"scope_statement": "Описать модуль X."}},
            {"stage_id": "entity_identification", "outputs": {"entities": []}},
        ]
    }
    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-1",
    )
    fired = {o.rule_id for o in evaluation.rule_outcomes if o.fired}
    assert "missing_entities" in fired


def test_descriptive_decomposition_unconnected_entities_fires_when_no_relationships() -> None:
    """`unconnected_entities` срабатывает, когда relationships пуст, а entities есть."""
    methodology = _load_descriptive_methodology()
    reasoning = {
        "stages": [
            {"stage_id": "scope_framing", "outputs": {"scope_statement": "Описать модуль X."}},
            {
                "stage_id": "entity_identification",
                "outputs": {"entities": [{"id": "a", "label": "A", "kind": "component"}]},
            },
            {"stage_id": "relationship_mapping", "outputs": {"relationships": []}},
        ]
    }
    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-2",
    )
    fired = {o.rule_id for o in evaluation.rule_outcomes if o.fired}
    assert "unconnected_entities" in fired


def test_descriptive_decomposition_low_coverage_fires_below_threshold() -> None:
    """`low_coverage` срабатывает, когда completeness_check.coverage_confidence < 0.5."""
    methodology = _load_descriptive_methodology()
    reasoning = {
        "stages": [
            {"stage_id": "scope_framing", "outputs": {"scope_statement": "Рамка."}},
            {
                "stage_id": "entity_identification",
                "outputs": {"entities": [{"id": "a", "label": "A", "kind": "component"}]},
            },
            {
                "stage_id": "completeness_check",
                "outputs": {"unresolved_questions": ["q1"], "coverage_confidence": 0.3},
            },
        ]
    }
    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-3",
    )
    fired = {o.rule_id for o in evaluation.rule_outcomes if o.fired}
    assert "low_coverage" in fired


def test_descriptive_decomposition_silent_when_stage_absent() -> None:
    """При отсутствующих стадиях правила должны молча не срабатывать."""
    methodology = _load_descriptive_methodology()
    reasoning = {
        "stages": [
            {"stage_id": "scope_framing", "outputs": {"scope_statement": "Есть рамка."}},
            # entity_identification / relationship_mapping / completeness_check отсутствуют
        ]
    }
    evaluation = evaluate_methodology_rules(
        methodology=methodology,
        complexity="standard",
        reasoning=reasoning,
        project_id="proj-1",
        task_id="task-4",
    )
    fired = {o.rule_id for o in evaluation.rule_outcomes if o.fired}
    assert "missing_entities" not in fired
    assert "unconnected_entities" not in fired
    assert "low_coverage" not in fired


def test_descriptive_decomposition_trivial_complexity_skips_completeness_check() -> None:
    """Для trivial-сложности completeness_check выпадает из стадий, low_coverage relaxed."""
    methodology = _load_descriptive_methodology()
    stage_ids = [s.identifier for s in methodology.stages_for_complexity("trivial")]
    assert "completeness_check" not in stage_ids
    assert {"scope_framing", "entity_identification", "relationship_mapping"}.issubset(set(stage_ids))
