from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.clarifications import (
    ClarificationCandidate,
    ClarificationMode,
    ClarificationOption,
    ClarificationRequest,
    DecisionOwnerRole,
)
from ..domain.problem_state import ProblemState, SetClarificationModePatch, UpsertAssumptionPatch, UpsertDecisionPatch
from ..infrastructure.claude_sdk_client import ClaudeSdkClient
from ..infrastructure.claude_sdk_client import model_for_complexity as claude_sdk_model_for_complexity
from ..infrastructure.claude_subscription_client import ClaudeSubscriptionClient
from ..infrastructure.claude_subscription_client import (
    model_for_complexity as claude_subscription_model_for_complexity,
)
from ..infrastructure.openrouter_client import OpenRouterClient, OpenRouterConfig
from ..infrastructure.sqlite_runtime import (
    SqliteRuntime,
    _normalize_clarification_question as _normalize_clarification_question_local,
)


@dataclass(frozen=True)
class ClarificationDecision:
    candidate_id: str
    action: str
    request_id: str | None = None
    rationale: str = ""


@dataclass(frozen=True)
class ReevaluationSummary:
    """Результат пере-оценки открытых candidate'ов при смене engagement-режима.
    UI использует counts чтобы показать toast «Закрыто X, отложено Y»."""

    mode: ClarificationMode
    auto_assumed: int
    auto_deferred: int
    kept_open: int


@dataclass(frozen=True)
class ClarificationDraft:
    description: str
    answer_mode: str
    options: tuple[ClarificationOption, ...]
    recommended_option_id: str | None
    min_participation_mode: ClarificationMode
    decision_owner_role: DecisionOwnerRole = "business"


class ClarificationDraftProvider(Protocol):
    def build_draft(
        self,
        *,
        candidate: ClarificationCandidate,
        context: dict[str, object],
        fallback: ClarificationDraft,
    ) -> ClarificationDraft:
        ...


_MODE_RANK: dict[ClarificationMode, int] = {
    "autopilot": 0,
    "balanced": 1,
    "control": 2,
    "expert": 3,
}


# Минимальный режим участия пользователя, начиная с которого вопрос данной роли
# имеет право показаться. Менеджер — бизнес-роль; всё, что не `business/client`,
# по умолчанию ниже его «уровня вовлечённости» и должно быть погашено
# допущением, пока он явно не повысит engagement.
#
# Значение поднимает effective `min_participation_mode` кандидата до пола
# роли (max по `_MODE_RANK`). Существующая логика `_decide_action` дальше
# работает без изменений.
_ROLE_FLOOR: dict[DecisionOwnerRole, ClarificationMode] = {
    "business": "autopilot",
    "client": "autopilot",
    "security": "balanced",
    "methodologist": "control",
    "data_owner": "control",
    "architect": "expert",
}


def _effective_min_mode(
    candidate_min_mode: ClarificationMode,
    role: DecisionOwnerRole,
) -> ClarificationMode:
    floor = _ROLE_FLOOR.get(role, "balanced")
    if _MODE_RANK[floor] > _MODE_RANK[candidate_min_mode]:
        return floor
    return candidate_min_mode


class ClarificationService:
    def __init__(
        self,
        runtime: SqliteRuntime,
        *,
        provider: str | None = None,
        model: str | None = None,
        draft_provider: ClarificationDraftProvider | None = None,
    ) -> None:
        self._runtime = runtime
        self._provider = provider
        self._model = model
        self._draft_provider = draft_provider

    def register_candidates(
        self,
        workspace: Path,
        candidates: tuple[ClarificationCandidate, ...],
    ) -> tuple[ClarificationDecision, ...]:
        decisions: list[ClarificationDecision] = []
        state = self._runtime.load_problem_state(workspace)
        for candidate in candidates:
            candidate = self._enrich_candidate(workspace, candidate, state)
            candidate = self._runtime.record_clarification_candidate(
                workspace,
                candidate if candidate.created_at else self._with_created_at(candidate),
            )
            # B3 layer 1: двухступенчатый поиск дубля.
            # 1) Точное совпадение по (source_type, source_id, question) —
            #    защищает от re-run одной задачи (тот же источник).
            # 2) Cross-task fallback: ищем ЛЮБОЙ request с тем же
            #    нормализованным текстом вопроса в проекте, даже если он
            #    из другой задачи. Это закрывает системный случай, когда
            #    несколько аналитических задач независимо находят один и
            #    тот же пробел в бизнес-запросе.
            existing = self._runtime.find_clarification_by_source(
                workspace,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                question=candidate.question,
            )
            reuse_reason = "Для этого источника уже есть уточнение."
            if existing is None:
                existing = self._runtime.find_clarification_in_project_by_question(
                    workspace,
                    project_id=candidate.project_id,
                    question=candidate.question,
                )
                if existing is not None:
                    reuse_reason = (
                        "Такой же вопрос уже задан другой задачей — "
                        f"переиспользуем существующее уточнение ({existing.status})."
                    )
            if existing is not None:
                decisions.append(
                    ClarificationDecision(
                        candidate_id=candidate.candidate_id,
                        action="reuse_existing",
                        request_id=existing.request_id,
                        rationale=reuse_reason,
                    )
                )
                continue

            action = self._decide_action(candidate, state.clarification_mode)
            initial_status: str
            if action == "assume":
                initial_status = "assumed"
            elif action == "defer":
                initial_status = "deferred"
            else:
                initial_status = "open"
            request = self._request_from_candidate(candidate, status=initial_status)
            created = self._runtime.create_clarification_request(workspace, request)
            if action == "assume" and candidate.default_assumption:
                self._runtime.apply_problem_patch(
                    workspace,
                    UpsertAssumptionPatch(
                        assumption_id=f"clarification_{created.request_id}",
                        statement=candidate.default_assumption,
                        source=f"clarification:{created.request_id}",
                    ),
                    actor="clarification_coordinator",
                    reason="safe assumption accepted automatically",
                )
            event_type_for_creation = {
                "ask": "created",
                "assume": "assumed_auto",
                "defer": "deferred_auto",
            }.get(action, "created")
            self._emit_event(
                workspace,
                request_id=created.request_id,
                project_id=candidate.project_id,
                event_type=event_type_for_creation,
                payload={
                    "source_type": candidate.source_type,
                    "source_id": candidate.source_id,
                    "decision_owner_role": candidate.decision_owner_role,
                    "min_participation_mode": candidate.min_participation_mode,
                    "default_assumption": candidate.default_assumption,
                },
                actor="clarification_coordinator",
            )
            decisions.append(
                ClarificationDecision(
                    candidate_id=candidate.candidate_id,
                    action=action,
                    request_id=created.request_id,
                    rationale="Кандидат обработан политикой уточнений.",
                )
            )
        return tuple(decisions)

    def answer_clarification(
        self,
        workspace: Path,
        *,
        request_id: str,
        selected_option_ids: tuple[str, ...] = (),
        free_text: str | None = None,
    ) -> ClarificationRequest:
        request = self._runtime.get_clarification_request(workspace, request_id)
        if request.status not in {"open", "deferred"}:
            raise ConflictError("На это уточнение уже был дан ответ или оно закрыто.")
        selected_labels = [option.label for option in request.options if option.option_id in selected_option_ids]
        parts = selected_labels + ([free_text.strip()] if free_text and free_text.strip() else [])
        if not parts:
            raise ConflictError("Ответ на уточнение не может быть пустым.")
        summary = "; ".join(parts)
        answered = self._runtime.answer_clarification_request(
            workspace,
            request_id,
            selected_option_ids=selected_option_ids,
            free_text=free_text.strip() if free_text else None,
            resolution_summary=summary,
        )
        self._runtime.apply_problem_patch(
            workspace,
            UpsertDecisionPatch(
                decision_id=f"clarification_{request_id}",
                statement=f"{request.question} Ответ: {summary}",
                source=f"clarification:{request_id}",
            ),
            actor="operator",
            reason="clarification answered",
        )
        self._emit_event(
            workspace,
            request_id=request_id,
            project_id=request.project_id,
            event_type="answered",
            payload={
                "selected_option_ids": list(selected_option_ids),
                "free_text": free_text.strip() if free_text else None,
                "resolution_summary": summary,
                "previous_status": request.status,
            },
        )
        # B3 layer 2: распространение ответа на дубли.
        # Если в проекте есть другие OPEN requests с тем же вопросом —
        # закрываем их как deferred с reason "resolved_via:{request_id}".
        # Это предотвращает повторное появление вопроса в UI после того
        # как пользователь уже ответил на семантически тот же вопрос в
        # другом источнике. См. B2 жалобу пользователя.
        self._propagate_answer_to_duplicates(
            workspace,
            answered_request=answered,
            resolution_summary=summary,
        )
        return answered

    def _propagate_answer_to_duplicates(
        self,
        workspace: Path,
        *,
        answered_request: ClarificationRequest,
        resolution_summary: str,
    ) -> None:
        """Закрывает все OPEN requests с тем же нормализованным вопросом
        в проекте, помечая их как deferred с указанием источника ответа.
        Audit event пишется для каждого закрытого дубля.
        """
        target_normalized = _normalize_clarification_question_local(
            answered_request.question
        )
        if not target_normalized:
            return
        all_open = self._runtime.list_clarification_requests(
            workspace, statuses=("open",)
        )
        propagation_reason = f"resolved_via:{answered_request.request_id}"
        for other in all_open:
            if other.request_id == answered_request.request_id:
                continue
            if other.project_id != answered_request.project_id:
                continue
            if (
                _normalize_clarification_question_local(other.question)
                != target_normalized
            ):
                continue
            try:
                self._runtime.defer_clarification_request(
                    workspace,
                    other.request_id,
                    reason=propagation_reason,
                )
                self._emit_event(
                    workspace,
                    request_id=other.request_id,
                    project_id=other.project_id,
                    event_type="deferred",
                    payload={
                        "reason": propagation_reason,
                        "previous_status": "open",
                        "resolution_summary": resolution_summary,
                        "via_request_id": answered_request.request_id,
                        "auto": True,
                    },
                )
            except Exception:
                # Best-effort: один сбойный дубль не должен ломать ответ
                # на основной request.
                continue

    def defer_clarification(
        self,
        workspace: Path,
        *,
        request_id: str,
        reason: str | None = None,
    ) -> ClarificationRequest:
        """W5.1: явный 'отложить' — мягкий skip. В отличие от accept_assumption,
        не фиксирует допущение в ProblemState; в отличие от answer, не даёт
        Decision. Просто перевод в `deferred`, чтобы инбокс был чище и
        планировщик мог идти дальше."""
        request = self._runtime.get_clarification_request(workspace, request_id)
        if request.status not in {"open", "answered", "assumed"}:
            raise ConflictError("Это уточнение уже отложено или закрыто.")
        updated = self._runtime.defer_clarification_request(
            workspace, request_id, reason=reason,
        )
        self._emit_event(
            workspace,
            request_id=request_id,
            project_id=request.project_id,
            event_type="deferred",
            payload={"reason": reason, "previous_status": request.status},
        )
        return updated

    def reopen_clarification(
        self,
        workspace: Path,
        *,
        request_id: str,
    ) -> ClarificationRequest:
        """W5.1: отвечавший пользователь хочет пере-ответить. Очищает
        ответ в request'е, но audit-trail (clarification_events) сохраняет
        предыдущий ответ полностью."""
        request = self._runtime.get_clarification_request(workspace, request_id)
        if request.status not in {"answered", "assumed", "deferred"}:
            raise ConflictError("Это уточнение и так открыто.")
        updated = self._runtime.reopen_clarification_request(workspace, request_id)
        self._emit_event(
            workspace,
            request_id=request_id,
            project_id=request.project_id,
            event_type="reopened",
            payload={
                "previous_status": request.status,
                "previous_selected_option_ids": list(request.selected_option_ids),
                "previous_free_text": request.free_text,
                "previous_resolution_summary": request.resolution_summary,
            },
        )
        return updated

    def list_events(self, workspace: Path, request_id: str) -> list[dict]:
        return self._runtime.list_clarification_events(workspace, request_id)

    def _emit_event(
        self,
        workspace: Path,
        *,
        request_id: str,
        project_id: str,
        event_type: str,
        payload: dict,
        actor: str = "operator",
    ) -> None:
        self._runtime.record_clarification_event(
            workspace,
            event_id=str(uuid.uuid4()),
            request_id=request_id,
            project_id=project_id,
            event_type=event_type,
            payload=payload,
            actor=actor,
        )

    def accept_assumption(self, workspace: Path, *, request_id: str) -> ClarificationRequest:
        request = self._runtime.get_clarification_request(workspace, request_id)
        if not request.default_assumption:
            raise ConflictError("У этого уточнения нет предложенного допущения.")
        if request.status not in {"open", "deferred", "assumed"}:
            raise ConflictError("Допущение уже нельзя принять для этого уточнения.")
        accepted = self._runtime.accept_clarification_assumption(
            workspace,
            request_id,
            resolution_summary=request.default_assumption,
        )
        self._runtime.apply_problem_patch(
            workspace,
            UpsertAssumptionPatch(
                assumption_id=f"clarification_{request_id}",
                statement=request.default_assumption,
                source=f"clarification:{request_id}",
            ),
            actor="operator",
            reason="clarification assumption accepted",
        )
        self._emit_event(
            workspace,
            request_id=request_id,
            project_id=request.project_id,
            event_type="assumed",
            payload={
                "default_assumption": request.default_assumption,
                "previous_status": request.status,
            },
        )
        return accepted

    def set_mode(self, workspace: Path, mode: ClarificationMode) -> "ReevaluationSummary":
        """Меняет engagement-режим проекта и **пере-оценивает все открытые
        candidates** против нового mode.

        Раньше смена режима применялась только к НОВЫМ candidate'ам. Если
        пользователь переключался на autopilot, уже-открытые вопросы
        оставались в `open` и блокировали planner — это и есть жалоба
        пользователя в W6 («переключился на autopilot, а вопросы остались»).

        Новое поведение для каждого `open` candidate:
        - есть `default_assumption` + новый mode НЕ allows показ → **assume**
          (тихо принять допущение).
        - нет `default_assumption` + новый mode НЕ allows → **defer**
          (мягкий skip; не блокирует planner, остаётся в инбоксе под
          фильтром «Отложено»).
        - mode allows показ → оставить `open` (ничего не меняем).

        Возвращает summary, сколько каких переходов было — UI показывает
        toast «Автоматически закрыто N вопросов, отложено M».
        """
        # 1. Применяем patch к ProblemState.
        self._runtime.apply_problem_patch(
            workspace,
            SetClarificationModePatch(mode=mode),
            actor="operator",
            reason="clarification mode changed",
        )
        # 2. Пере-оцениваем все открытые candidates против нового mode,
        # используя ту же логику что и `_decide_action` для новых candidate'ов:
        #   - "assume" → accept_assumption (если есть default_assumption);
        #   - "defer"  → defer_clarification (мягкий skip);
        #   - "ask"    → оставить open (бывает для blocking_scope=objective).
        opens = self._runtime.list_clarification_requests(workspace, statuses=("open",))
        auto_assumed = 0
        auto_deferred = 0
        kept_open = 0
        for request in opens:
            # ClarificationRequest и ClarificationCandidate имеют одинаковые
            # поля для решения — превратим request в candidate-shim.
            decision = self._decide_action(
                self._candidate_from_request(request),
                mode,
            )
            if decision == "assume" and request.default_assumption:
                self.accept_assumption(workspace, request_id=request.request_id)
                self._runtime.mark_clarification_auto_resolved(workspace, request.request_id)
                auto_assumed += 1
            elif decision == "defer":
                self.defer_clarification(
                    workspace,
                    request_id=request.request_id,
                    reason=(
                        f"Авто-отложено: смена режима участия на «{mode}». "
                        f"Можно пере-открыть и ответить вручную."
                    ),
                )
                self._runtime.mark_clarification_auto_resolved(workspace, request.request_id)
                auto_deferred += 1
            else:
                # "ask" (например, objective-scope без допущения) — оставляем.
                kept_open += 1
        return ReevaluationSummary(
            mode=mode,
            auto_assumed=auto_assumed,
            auto_deferred=auto_deferred,
            kept_open=kept_open,
        )

    @staticmethod
    def _candidate_from_request(request: ClarificationRequest) -> ClarificationCandidate:
        """Адаптер для пере-оценки: формирует ClarificationCandidate-shim
        из persisted ClarificationRequest. Используется только в set_mode."""
        return ClarificationCandidate(
            candidate_id=request.request_id,
            project_id=request.project_id,
            source_type=request.source_type,
            source_id=request.source_id,
            need="",
            question=request.question,
            description=request.description,
            rationale=request.reason,
            impact=request.impact,
            severity=request.priority,
            confidence_without_user=0.0,  # фиктивное, не используется в _decide_action
            min_participation_mode=request.min_participation_mode,
            default_assumption=request.default_assumption,
            recommended_answer=None,
            answer_mode=request.answer_mode,
            options=request.options,
            affected_task_ids=request.affected_task_ids,
            related_artifact_ids=request.related_artifact_ids,
            blocking_scope=request.blocking_scope,
            decision_owner_role=request.decision_owner_role,
            created_at=request.created_at,
        )

    def candidate_from_question(
        self,
        *,
        project_id: str,
        source_type: str,
        source_id: str,
        question: str,
        affected_task_ids: tuple[str, ...],
        related_artifact_ids: tuple[str, ...],
        severity: str = "high",
        confidence_without_user: float = 0.2,
        default_assumption: str | None = None,
        description: str | None = None,
        rationale: str = "Система не может надежно вывести ответ из доступного контекста.",
        impact: str = "Ответ повлияет на дальнейшую формализацию требований и проверку результата.",
        answer_mode: str = "single",
        options: tuple[ClarificationOption, ...] | None = None,
        min_participation_mode: ClarificationMode | None = None,
        decision_owner_role: DecisionOwnerRole = "business",
    ) -> ClarificationCandidate:
        normalized_options = options or ()
        normalized_mode = "single" if answer_mode == "free_text" and normalized_options else answer_mode
        normalized_min_mode = min_participation_mode or self._min_mode_for_candidate(
            severity=severity,
            confidence_without_user=confidence_without_user,
            default_assumption=default_assumption,
        )
        return ClarificationCandidate(
            candidate_id=str(uuid.uuid4()),
            project_id=project_id,
            source_type=source_type,  # type: ignore[arg-type]
            source_id=source_id,
            need="Получить недостающую информацию для корректного продолжения работы.",
            question=question.strip(),
            description=description or "",
            rationale=rationale,
            impact=impact,
            severity=severity,  # type: ignore[arg-type]
            confidence_without_user=confidence_without_user,
            min_participation_mode=normalized_min_mode,
            default_assumption=default_assumption,
            recommended_answer=None,
            answer_mode=normalized_mode,  # type: ignore[arg-type]
            options=normalized_options,
            affected_task_ids=affected_task_ids,
            related_artifact_ids=related_artifact_ids,
            blocking_scope="task",
            decision_owner_role=decision_owner_role,
            created_at=utc_now_iso(),
        )

    def _enrich_candidate(
        self,
        workspace: Path,
        candidate: ClarificationCandidate,
        state: ProblemState,
    ) -> ClarificationCandidate:
        if not self._needs_llm_draft(candidate):
            return candidate

        fallback = ClarificationDraft(
            description=candidate.description
            or self._compose_description(
                question=candidate.question,
                rationale=candidate.rationale,
                impact=candidate.impact,
                default_assumption=candidate.default_assumption,
            ),
            answer_mode="single" if candidate.answer_mode == "free_text" else candidate.answer_mode,
            options=candidate.options or self._default_options_for_candidate(default_assumption=candidate.default_assumption),
            recommended_option_id=self._recommended_option_id(candidate.options, candidate.recommended_answer),
            min_participation_mode=candidate.min_participation_mode,
            decision_owner_role=candidate.decision_owner_role,
        )
        context = self._clarification_context(workspace, candidate, state)
        draft = self._build_draft(candidate=candidate, context=context, fallback=fallback)
        return ClarificationCandidate(
            **{
                **candidate.__dict__,
                "description": draft.description,
                "answer_mode": draft.answer_mode,
                "options": draft.options,
                "recommended_answer": draft.recommended_option_id,
                "min_participation_mode": draft.min_participation_mode,
                "decision_owner_role": draft.decision_owner_role,
            }
        )

    def _needs_llm_draft(self, candidate: ClarificationCandidate) -> bool:
        if not candidate.description.strip():
            return True
        if not candidate.options:
            return True
        if candidate.answer_mode == "free_text":
            return True
        return any(option.confidence is None for option in candidate.options)

    def _build_draft(
        self,
        *,
        candidate: ClarificationCandidate,
        context: dict[str, object],
        fallback: ClarificationDraft,
    ) -> ClarificationDraft:
        if self._draft_provider is not None:
            return self._normalize_draft_payload(
                self._draft_provider.build_draft(candidate=candidate, context=context, fallback=fallback).__dict__,
                fallback=fallback,
            )
        provider = self._active_provider()
        if provider == "stub":
            return fallback

        system_prompt = self._draft_system_prompt()
        user_prompt = self._draft_user_prompt(candidate=candidate, context=context, fallback=fallback)
        schema = self._draft_schema()

        if provider == "openrouter":
            payload = self._openrouter_client().chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
            )
        elif provider == "claude_sdk":
            payload = ClaudeSdkClient.from_env(
                model=self._active_model_for_provider(provider)
            ).chat_json(system_prompt=system_prompt, user_prompt=user_prompt, schema=schema)
        elif provider == "claude_subscription":
            payload = ClaudeSubscriptionClient.from_env(
                model=self._active_model_for_provider(provider)
            ).chat_json(system_prompt=system_prompt, user_prompt=user_prompt, schema=schema)
        else:
            raise ConflictError(f"Неподдерживаемый provider подготовки уточнений: {provider}")

        return self._normalize_draft_payload(payload, fallback=fallback)

    def _active_provider(self) -> str:
        configured = self._provider or os.environ.get("POV_CLARIFICATION_PROVIDER")
        if configured:
            return configured
        # По умолчанию идём за провайдером исполнения задач (Q4: claude по
        # подписке — основной). Это держит CE11 и leaf-задачи на одной
        # модельной семье и минимизирует расхождения в стиле ответа.
        execution_provider = os.environ.get("POV_EXECUTION_PROVIDER", "stub")
        if execution_provider in {"claude_sdk", "claude_subscription"}:
            return execution_provider
        if execution_provider == "openrouter" and os.environ.get("POV_OPENROUTER_API_KEY"):
            return "openrouter"
        # Историческая совместимость: если ключ openrouter есть, но execution
        # не настроен явно, всё равно используем openrouter (старое поведение).
        if os.environ.get("POV_OPENROUTER_API_KEY"):
            return "openrouter"
        return "stub"

    def _active_model_for_provider(self, provider: str) -> str | None:
        # Явный override (конструктор / env) выигрывает у per-provider дефолтов.
        explicit = self._model or os.environ.get("POV_CLARIFICATION_MODEL")
        if explicit:
            return explicit
        # Подготовка уточнения — задача "standard" сложности: ни trivial, ни complex.
        if provider == "claude_sdk":
            return claude_sdk_model_for_complexity("standard")
        if provider == "claude_subscription":
            return claude_subscription_model_for_complexity("standard")
        return os.environ.get("POV_OPENROUTER_MODEL") or "openai/gpt-4.1-mini"

    def _active_model(self) -> str:
        # Сохранена для обратной совместимости openrouter-ветки.
        return self._active_model_for_provider("openrouter") or "openai/gpt-4.1-mini"

    def _openrouter_client(self) -> OpenRouterClient:
        api_key = os.environ.get("POV_OPENROUTER_API_KEY")
        if not api_key:
            raise ConflictError("Не задан POV_OPENROUTER_API_KEY для подготовки уточнения.")
        return OpenRouterClient(
            OpenRouterConfig(
                api_key=api_key,
                model=self._active_model(),
                base_url=os.environ.get("POV_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            )
        )

    def _clarification_context(
        self,
        workspace: Path,
        candidate: ClarificationCandidate,
        state: ProblemState,
    ) -> dict[str, object]:
        tasks: list[dict[str, object]] = []
        for task_id in candidate.affected_task_ids[:5]:
            try:
                task = self._runtime.get_task(workspace, task_id)
            except Exception:
                continue
            tasks.append(
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "template_ref": task.template_ref,
                    "status": task.status,
                }
            )

        artifacts: list[dict[str, object]] = []
        for artifact_id in candidate.related_artifact_ids[:3]:
            try:
                artifact = self._runtime.load_artifact(workspace, artifact_id)
                content = self._runtime.load_artifact_content(workspace, artifact_id)
            except Exception:
                continue
            artifacts.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "role": artifact.artifact_role,
                    "title": artifact.title,
                    "content_excerpt": content[:4000],
                }
            )

        return {
            "business_request": state.business_request,
            "goal": state.goal,
            "active_domain_packs": tuple(sorted(state.active_domain_pack_records.keys())),
            "known_facts": tuple(item.statement for item in state.known_facts.values())[:12],
            "assumptions": tuple(item.statement for item in state.assumptions.values())[:12],
            "active_gaps": tuple(item.description for item in state.active_gaps.values())[:12],
            "affected_tasks": tasks,
            "related_artifacts": artifacts,
        }

    def _draft_system_prompt(self) -> str:
        return (
            "Ты системный аналитик, который готовит профессиональный запрос на уточнение к пользователю. "
            "Пиши только на русском языке. "
            "Не отвечай на вопрос вместо пользователя. "
            "Твоя задача — сформировать самодостаточное описание ситуации и осмысленные возможные ответы. "
            "Описание должно состоять из 3-10 предложений, без воды и без технического жаргона, если он не нужен. "
            "Варианты ответа должны быть реальными бизнес-вариантами, а не универсальными действиями. "
            "Запрещено добавлять вариант 'другое', 'свой ответ' или аналогичный: свободный ответ всегда есть отдельно в интерфейсе. "
            "Если одновременно может быть верно несколько вариантов, используй answer_mode='multiple'. "
            "confidence у варианта — это уверенность системы, что этот вариант вероятно подойдет на основе доступного контекста. "
            "min_participation_mode выбирай как минимальный режим участия пользователя, начиная с которого вопрос нужно показывать: "
            "autopilot — только критично и небезопасно продолжать без ответа; balanced — блокирует или сильно влияет; "
            "control — важное, но есть безопасное допущение; expert — спорное или низковлияющее. "
            "decision_owner_role описывает, в чьей зоне ответственности находится решение: "
            "business — вопрос про бизнес-цели/KPI/границы проекта (бизнес-менеджер); "
            "client — вопрос требует согласования внешнего заказчика (sign-off, приёмка); "
            "methodologist — методология рассуждения (как мы выбираем варианты, какие гипотезы фиксируем); "
            "architect — архитектурные/технологические развилки реализации; "
            "data_owner — модель данных, источники, качество, владельцы данных; "
            "security — ИБ, приватность, регуляторные ограничения, контур размещения. "
            "Эта классификация управляет показом вопроса бизнес-менеджеру в зависимости от его уровня вовлечённости. "
            "Верни только валидный JSON."
        )

    def _draft_user_prompt(
        self,
        *,
        candidate: ClarificationCandidate,
        context: dict[str, object],
        fallback: ClarificationDraft,
    ) -> str:
        payload = {
            "question": candidate.question,
            "need": candidate.need,
            "rationale": candidate.rationale,
            "impact": candidate.impact,
            "severity": candidate.severity,
            "confidence_without_user": candidate.confidence_without_user,
            "default_assumption": candidate.default_assumption,
            "source_type": candidate.source_type,
            "source_id": candidate.source_id,
            "project_context": context,
            "fallback_do_not_copy_verbatim": {
                "description": fallback.description,
                "options": [option.label for option in fallback.options],
                "min_participation_mode": fallback.min_participation_mode,
            },
        }
        return (
            "Подготовь запрос на уточнение для пользователя по следующему кандидату. "
            "Основной вопрос уже задан в поле question; не превращай его в длинный текст. "
            "Сформируй description, answer_mode, options, recommended_option_id и min_participation_mode.\n\n"
            f"{json_dumps(payload)}"
        )

    def _draft_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "required": [
                "description",
                "answer_mode",
                "options",
                "recommended_option_id",
                "min_participation_mode",
                "decision_owner_role",
            ],
            "additionalProperties": False,
            "properties": {
                "description": {"type": "string"},
                "answer_mode": {"type": "string", "enum": ["single", "multiple"]},
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "required": ["option_id", "label", "description", "effect_preview", "confidence"],
                        "additionalProperties": False,
                        "properties": {
                            "option_id": {"type": "string"},
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                            "effect_preview": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                    },
                },
                "recommended_option_id": {"type": "string"},
                "min_participation_mode": {"type": "string", "enum": ["autopilot", "balanced", "control", "expert"]},
                "decision_owner_role": {
                    "type": "string",
                    "enum": ["business", "client", "methodologist", "architect", "data_owner", "security"],
                },
            },
        }

    def _normalize_draft_payload(self, payload: dict[str, object], *, fallback: ClarificationDraft) -> ClarificationDraft:
        description = str(payload.get("description") or fallback.description).strip()
        if not description:
            description = fallback.description

        answer_mode = str(payload.get("answer_mode") or fallback.answer_mode)
        if answer_mode not in {"single", "multiple"}:
            answer_mode = fallback.answer_mode if fallback.answer_mode in {"single", "multiple"} else "single"

        raw_options = payload.get("options")
        options: list[ClarificationOption] = []
        if isinstance(raw_options, (list, tuple)):
            used_ids: set[str] = set()
            for index, item in enumerate(raw_options, start=1):
                if isinstance(item, ClarificationOption):
                    option_id = self._safe_option_id(item.option_id or item.label, used_ids, index)
                    if self._is_custom_answer_label(item.label):
                        continue
                    options.append(
                        ClarificationOption(
                            option_id=option_id,
                            label=item.label,
                            description=item.description,
                            effect_preview=item.effect_preview,
                            confidence=item.confidence if item.confidence is not None else 0.5,
                        )
                    )
                    used_ids.add(option_id)
                    continue
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "")).strip()
                if not label or self._is_custom_answer_label(label):
                    continue
                option_id = self._safe_option_id(str(item.get("option_id", "")).strip() or label, used_ids, index)
                confidence = self._clamp_confidence(item.get("confidence"))
                options.append(
                    ClarificationOption(
                        option_id=option_id,
                        label=label,
                        description=str(item.get("description", "")).strip(),
                        effect_preview=str(item.get("effect_preview", "")).strip(),
                        confidence=confidence,
                    )
                )
                used_ids.add(option_id)
        if len(options) < 2:
            options = list(fallback.options)

        recommended_option_id = str(payload.get("recommended_option_id") or "").strip() or fallback.recommended_option_id
        if recommended_option_id and recommended_option_id not in {option.option_id for option in options}:
            recommended_option_id = max(options, key=lambda option: option.confidence or 0).option_id if options else None

        min_mode = str(payload.get("min_participation_mode") or fallback.min_participation_mode)
        if min_mode not in _MODE_RANK:
            min_mode = fallback.min_participation_mode

        decision_owner_role = str(payload.get("decision_owner_role") or fallback.decision_owner_role)
        if decision_owner_role not in _ROLE_FLOOR:
            decision_owner_role = fallback.decision_owner_role

        return ClarificationDraft(
            description=description,
            answer_mode=answer_mode,
            options=tuple(options),
            recommended_option_id=recommended_option_id,
            min_participation_mode=min_mode,  # type: ignore[arg-type]
            decision_owner_role=decision_owner_role,  # type: ignore[arg-type]
        )

    def _safe_option_id(self, raw: str, used_ids: set[str], index: int) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_-]+", "_", raw.strip().lower()).strip("_")
        option_id = normalized[:48] or f"option_{index}"
        while option_id in used_ids:
            option_id = f"{option_id}_{index}"
        return option_id

    def _is_custom_answer_label(self, label: str) -> bool:
        normalized = label.strip().lower()
        return any(marker in normalized for marker in ("другое", "свой ответ", "иной ответ", "другой вариант"))

    def _clamp_confidence(self, value: object) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return 0.5
        return max(0.0, min(1.0, float(value)))

    def _with_created_at(self, candidate: ClarificationCandidate) -> ClarificationCandidate:
        return ClarificationCandidate(
            **{**candidate.__dict__, "created_at": utc_now_iso()},
        )

    def _decide_action(self, candidate: ClarificationCandidate, mode: ClarificationMode) -> str:
        # Роль-владелец решения поднимает effective min mode: бизнес-менеджеру
        # на autopilot/balanced нечего делать с архитектурной развилкой, даже
        # если по уверенности она формально подходит.
        effective_min_mode = _effective_min_mode(
            candidate.min_participation_mode, candidate.decision_owner_role
        )
        surface_allowed = self._mode_allows(mode, effective_min_mode)

        # 1. Высокая уверенность системы + есть допущение → тихо принять,
        # не показывать пользователю даже на expert (это «и так очевидно»).
        if candidate.default_assumption and candidate.confidence_without_user >= 0.72:
            return "assume"

        # 2. Mode + role позволяют показать — показываем.
        if surface_allowed:
            return "ask"

        # 3. Mode/role не позволяют показ — есть варианты:
        #   3a. Есть безопасное допущение → принять его.
        if candidate.default_assumption:
            return "assume"
        #   3b. Нет допущения, но scope=objective (блокирует ВЕСЬ проект:
        #       human_approval gate'ы и подобное) — придётся показать.
        #       Менеджер на autopilot всё равно не сможет согласовать клиентскую
        #       подпись автоматически: это явный сигнал «нужно решение».
        if candidate.blocking_scope == "objective":
            return "ask"
        #   3c. Нет допущения и scope меньше objective — это не блокирующий
        #       вопрос. Откладываем (defer): planner идёт дальше, менеджер
        #       видит вопрос на /clarifications во вкладке «Отложенные».
        #       Раньше эта ветка возвращала "ask" — отсюда жалоба, что
        #       autopilot всё равно блокирует.
        return "defer"

    def _request_from_candidate(self, candidate: ClarificationCandidate, *, status: str) -> ClarificationRequest:
        options = candidate.options or self._default_options_for_candidate(default_assumption=candidate.default_assumption)
        recommended_option_id = self._recommended_option_id(options, candidate.recommended_answer)
        answer_mode = "single" if candidate.answer_mode == "free_text" and options else candidate.answer_mode
        # V1: если первоначальный статус не "open" — это auto-решение системы
        # (assumed_auto / deferred_auto в audit log).
        auto_resolved_flag = status in {"assumed", "deferred"}
        return ClarificationRequest(
            request_id=str(uuid.uuid4()),
            project_id=candidate.project_id,
            status=status,  # type: ignore[arg-type]
            priority=candidate.severity,
            title=self._title_from_question(candidate.question),
            question=candidate.question,
            description=candidate.description,
            reason=candidate.rationale,
            impact=candidate.impact,
            answer_mode=answer_mode,
            options=options,
            recommended_option_id=recommended_option_id,
            min_participation_mode=candidate.min_participation_mode,
            default_assumption=candidate.default_assumption,
            affected_task_ids=candidate.affected_task_ids,
            related_artifact_ids=candidate.related_artifact_ids,
            blocking_scope=candidate.blocking_scope,
            decision_owner_role=candidate.decision_owner_role,
            auto_resolved=auto_resolved_flag,
            source_type=candidate.source_type,
            source_id=candidate.source_id,
            created_from_candidate_ids=(candidate.candidate_id,),
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )

    def _default_options_for_candidate(
        self,
        *,
        default_assumption: str | None,
    ) -> tuple[ClarificationOption, ...]:
        assumption_description = (
            default_assumption
            if default_assumption
            else "Система продолжит с явно зафиксированным рабочим допущением."
        )
        return (
            ClarificationOption(
                option_id="include_in_current_project",
                label="Да, учитывать в текущем проекте",
                description="Эта информация важна для текущего PoC/PoV и должна повлиять на дальнейшую работу.",
                effect_preview="Ответ будет сохранен как решение проекта и учтен в следующих задачах.",
                confidence=0.55,
            ),
            ClarificationOption(
                option_id="use_working_assumption",
                label="Продолжить с рабочим допущением",
                description=assumption_description,
                effect_preview="Допущение попадет в состояние проекта и историю решений.",
                confidence=0.45 if default_assumption else 0.35,
            ),
        )

    def _compose_description(
        self,
        *,
        question: str,
        rationale: str,
        impact: str,
        default_assumption: str | None,
    ) -> str:
        parts = [
            rationale.strip(),
            "Система вынесла этот вопрос пользователю, потому что ответ может изменить дальнейшую детализацию требований.",
            f"Основной вопрос: {question.strip()}",
            impact.strip(),
        ]
        if default_assumption:
            parts.append(f"Если пользователь не будет вовлечен, безопасным рабочим допущением считается: {default_assumption}")
        return " ".join(part for part in parts if part)

    def _min_mode_for_candidate(
        self,
        *,
        severity: str,
        confidence_without_user: float,
        default_assumption: str | None,
    ) -> ClarificationMode:
        if severity == "critical":
            return "autopilot"
        if severity == "high" and (confidence_without_user < 0.75 or not default_assumption):
            return "balanced"
        if severity in {"high", "medium"}:
            return "control"
        return "expert"

    def _mode_allows(self, current_mode: ClarificationMode, min_mode: ClarificationMode) -> bool:
        return _MODE_RANK[current_mode] >= _MODE_RANK[min_mode]

    def _title_from_question(self, question: str) -> str:
        normalized = question.strip().rstrip("?!.")
        return normalized[:72] + ("…" if len(normalized) > 72 else "")

    def _recommended_option_id(
        self,
        options: tuple[ClarificationOption, ...],
        recommended_answer: str | None,
    ) -> str | None:
        if not recommended_answer:
            return None
        normalized = recommended_answer.strip().lower()
        for option in options:
            if option.option_id == recommended_answer or option.label.strip().lower() == normalized:
                return option.option_id
        return None
