from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Protocol
import uuid

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.clarifications import (
    ClarificationCandidate,
    ClarificationMode,
    ClarificationOption,
    ClarificationRequest,
)
from ..domain.problem_state import ProblemState, SetClarificationModePatch, UpsertAssumptionPatch, UpsertDecisionPatch
from ..infrastructure.claude_sdk_client import ClaudeSdkClient
from ..infrastructure.claude_sdk_client import model_for_complexity as claude_sdk_model_for_complexity
from ..infrastructure.claude_subscription_client import ClaudeSubscriptionClient
from ..infrastructure.claude_subscription_client import (
    model_for_complexity as claude_subscription_model_for_complexity,
)
from ..infrastructure.openrouter_client import OpenRouterClient, OpenRouterConfig
from ..infrastructure.sqlite_runtime import SqliteRuntime


@dataclass(frozen=True)
class ClarificationDecision:
    candidate_id: str
    action: str
    request_id: str | None = None
    rationale: str = ""


@dataclass(frozen=True)
class ClarificationDraft:
    description: str
    answer_mode: str
    options: tuple[ClarificationOption, ...]
    recommended_option_id: str | None
    min_participation_mode: ClarificationMode


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
            existing = self._runtime.find_clarification_by_source(
                workspace,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                question=candidate.question,
            )
            if existing is not None:
                decisions.append(
                    ClarificationDecision(
                        candidate_id=candidate.candidate_id,
                        action="reuse_existing",
                        request_id=existing.request_id,
                        rationale="Для этого источника уже есть уточнение.",
                    )
                )
                continue

            action = self._decide_action(candidate, state.clarification_mode)
            request = self._request_from_candidate(candidate, status="assumed" if action == "assume" else "open")
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
        return answered

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
        return accepted

    def set_mode(self, workspace: Path, mode: ClarificationMode) -> None:
        self._runtime.apply_problem_patch(
            workspace,
            SetClarificationModePatch(mode=mode),
            actor="operator",
            reason="clarification mode changed",
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
            "required": ["description", "answer_mode", "options", "recommended_option_id", "min_participation_mode"],
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

        return ClarificationDraft(
            description=description,
            answer_mode=answer_mode,
            options=tuple(options),
            recommended_option_id=recommended_option_id,
            min_participation_mode=min_mode,  # type: ignore[arg-type]
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
        if self._mode_allows(mode, candidate.min_participation_mode):
            return "ask"
        if candidate.blocking_scope != "none" and not candidate.default_assumption:
            return "ask"
        if candidate.default_assumption and candidate.confidence_without_user >= 0.72:
            return "assume"
        return "assume" if mode == "autopilot" and candidate.default_assumption else "ask"

    def _request_from_candidate(self, candidate: ClarificationCandidate, *, status: str) -> ClarificationRequest:
        options = candidate.options or self._default_options_for_candidate(default_assumption=candidate.default_assumption)
        recommended_option_id = self._recommended_option_id(options, candidate.recommended_answer)
        answer_mode = "single" if candidate.answer_mode == "free_text" and options else candidate.answer_mode
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
