from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..domain.artifacts import ContextManifest
from ..domain.decisions import (
    SOURCE_IDENTIFICATION,
    Decision,
    normalized_decision_signature,
)
from ..infrastructure.sqlite_runtime import SqliteRuntime


def _estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4)


@dataclass(frozen=True)
class DecisionConstraintBlock:
    """Compact decision context for downstream generation prompts."""

    decisions: tuple[Decision, ...]
    text: str
    token_estimate: int


class DecisionContextBuilder:
    """Builds compact generation constraints from the canonical decisions ledger."""

    CLOSED_STATUSES = frozenset(
        {
            "accepted_default",
            "deferred",
            "user_overridden",
            "locked_in",
        }
    )
    GLOBAL_LEVELS = frozenset({"business", "architecture"})

    def __init__(
        self,
        runtime: SqliteRuntime,
        *,
        max_decisions: int = 20,
        max_tokens: int = 1600,
        max_rationale_chars: int = 220,
    ) -> None:
        self._runtime = runtime
        self._max_decisions = max(1, max_decisions)
        self._max_tokens = max(200, max_tokens)
        self._max_rationale_chars = max(40, max_rationale_chars)

    def build_generation_constraints(
        self,
        *,
        workspace: Path,
        project_id: str,
        task_id: str,
        context_manifest: ContextManifest | None = None,
    ) -> DecisionConstraintBlock:
        """Return compact constraints relevant to the current generation task.

        Relevance is intentionally conservative for this vertical slice:
        business/architecture decisions are project-level constraints, while
        detail decisions are included only when they are tied to the current
        task or to artifacts present in the task context.
        """

        input_artifact_ids = self._input_artifact_ids(context_manifest)
        closed_decisions = [
            decision
            for decision in self._runtime.list_decisions(workspace, project_id=project_id)
            if decision.status in self.CLOSED_STATUSES
        ]
        candidates = self._relevant_with_dependencies(
            closed_decisions,
            task_id=task_id,
            input_artifact_ids=input_artifact_ids,
        )
        ranked = sorted(
            self._dedupe(candidates),
            key=lambda decision: self._rank_key(
                decision,
                task_id=task_id,
                input_artifact_ids=input_artifact_ids,
                dependency_ids=self._dependency_ids(candidates),
            ),
        )
        decisions = self._cap_decisions(tuple(ranked), task_id=task_id)
        text = self._render(decisions, task_id=task_id)
        return DecisionConstraintBlock(
            decisions=decisions,
            text=text,
            token_estimate=_estimate_tokens(text) if text else 0,
        )

    def _relevant_with_dependencies(
        self,
        decisions: list[Decision],
        *,
        task_id: str,
        input_artifact_ids: frozenset[str],
    ) -> list[Decision]:
        by_id = {decision.decision_id: decision for decision in decisions}
        relevant_ids = {
            decision.decision_id
            for decision in decisions
            if self._is_relevant(
                decision,
                task_id=task_id,
                input_artifact_ids=input_artifact_ids,
            )
        }

        queue = list(relevant_ids)
        while queue:
            decision_id = queue.pop()
            decision = by_id.get(decision_id)
            if decision is None:
                continue
            for dependency_id in decision.depends_on_decision_ids:
                if dependency_id in by_id and dependency_id not in relevant_ids:
                    relevant_ids.add(dependency_id)
                    queue.append(dependency_id)

        return [decision for decision in decisions if decision.decision_id in relevant_ids]

    def _is_relevant(
        self,
        decision: Decision,
        *,
        task_id: str,
        input_artifact_ids: frozenset[str],
    ) -> bool:
        if decision.source_task_id == task_id:
            return True
        if input_artifact_ids.intersection(decision.affected_artifact_ids):
            return True
        if decision.effective_level in self.GLOBAL_LEVELS:
            return True
        return False

    def _rank_key(
        self,
        decision: Decision,
        *,
        task_id: str,
        input_artifact_ids: frozenset[str],
        dependency_ids: frozenset[str],
    ) -> tuple[int, float, str]:
        relevance_score = self._relevance_score(
            decision,
            task_id=task_id,
            input_artifact_ids=input_artifact_ids,
            dependency_ids=dependency_ids,
        )
        return (-relevance_score, -self._timestamp_score(decision), decision.decision_id)

    def _relevance_score(
        self,
        decision: Decision,
        *,
        task_id: str,
        input_artifact_ids: frozenset[str],
        dependency_ids: frozenset[str],
    ) -> int:
        artifact_overlap = len(input_artifact_ids.intersection(decision.affected_artifact_ids))
        if decision.source_task_id == task_id:
            score = 100
        elif artifact_overlap > 0:
            score = 80 + min(artifact_overlap, 3)
        elif decision.decision_id in dependency_ids:
            score = 70
        elif decision.effective_level == "business":
            score = 50
        elif decision.effective_level == "architecture":
            score = 40
        else:
            score = 10

        if decision.was_user_modified:
            score += 8
        if decision.is_low_confidence:
            score += 4
        if decision.normalized_category in {"scope", "acceptance", "tech_stack", "data"}:
            score += 2
        return score

    @staticmethod
    def _dependency_ids(decisions: list[Decision]) -> frozenset[str]:
        selected_ids = {decision.decision_id for decision in decisions}
        dependency_ids: set[str] = set()
        for decision in decisions:
            dependency_ids.update(
                dependency_id
                for dependency_id in decision.depends_on_decision_ids
                if dependency_id in selected_ids
            )
        return frozenset(dependency_ids)

    @staticmethod
    def _timestamp_score(decision: Decision) -> float:
        raw_value = decision.updated_at or decision.created_at
        if not raw_value:
            return 0.0
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    def _cap_decisions(self, decisions: tuple[Decision, ...], *, task_id: str) -> tuple[Decision, ...]:
        selected: list[Decision] = []
        for decision in decisions:
            if len(selected) >= self._max_decisions:
                break
            candidate = tuple(selected + [decision])
            if (
                selected
                and _estimate_tokens(self._render(candidate, task_id=task_id))
                > self._max_tokens
            ):
                break
            selected.append(decision)
        return tuple(selected)

    @staticmethod
    def _dedupe(decisions: list[Decision]) -> tuple[Decision, ...]:
        seen: set[tuple[str, str, str, str]] = set()
        result: list[Decision] = []
        for decision in decisions:
            signature = normalized_decision_signature(decision)
            key = (
                signature.normalized_title_key,
                signature.category,
                signature.chosen_answer_summary,
                signature.status,
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(decision)
        return tuple(result)

    @staticmethod
    def _input_artifact_ids(context_manifest: ContextManifest | None) -> frozenset[str]:
        if context_manifest is None:
            return frozenset()
        prefix = "artifact:"
        artifact_ids: set[str] = set()
        for item in context_manifest.items:
            if item.item_type == "artifact" and item.source_ref.startswith(prefix):
                artifact_ids.add(item.source_ref[len(prefix) :])
        return frozenset(artifact_ids)

    def _render(self, decisions: tuple[Decision, ...], *, task_id: str) -> str:
        if not decisions:
            return ""

        lines: list[str] = [
            "<decision_constraints>",
            "Компактные ограничения из реестра решений проекта. Используй их как "
            "обязательные для генерации; не пересматривай без явного override "
            "пользователя.",
            "",
        ]
        for decision in decisions:
            signature = normalized_decision_signature(decision)
            modifier = self._status_modifier(decision)
            lines.append(f"- {decision.title}{modifier}")
            if signature.category:
                lines.append(f"  Категория: {signature.category}")
            lines.append(f"  Решение: {signature.chosen_answer_summary}")

            rationale = self._short_text(decision.rationale)
            if rationale:
                lines.append(f"  Обоснование: {rationale}")

            provenance = self._provenance(decision, task_id=task_id)
            if provenance:
                lines.append(f"  Источник: {provenance}")
            lines.append("")
        lines.append("</decision_constraints>")
        return "\n".join(lines)

    def _short_text(self, value: str, *, limit: int | None = None) -> str:
        text = " ".join((value or "").split())
        if not text:
            return ""
        max_len = limit or self._max_rationale_chars
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "..."

    @staticmethod
    def _status_modifier(decision: Decision) -> str:
        if decision.was_user_modified:
            return " (переопределено пользователем)"
        if decision.status == "deferred":
            return " (отложено; применён дефолт)"
        return ""

    @staticmethod
    def _provenance(decision: Decision, *, task_id: str) -> str:
        pieces: list[str] = []
        if decision.source_task_id and decision.source_task_id != task_id:
            pieces.append(f"задача {decision.source_task_id}")
        if decision.source != SOURCE_IDENTIFICATION:
            pieces.append(decision.source)
        if decision.affected_artifact_ids:
            preview = ", ".join(decision.affected_artifact_ids[:2])
            if len(decision.affected_artifact_ids) > 2:
                preview += ", ..."
            pieces.append(f"артефакты {preview}")
        return "; ".join(pieces)
