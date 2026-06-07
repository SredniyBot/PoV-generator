from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.cancellation import (
    CancellationError,
    CancellationToken,
    cancellation_scope,
)
from ..common.errors import ConflictError
from ..common.logging import get_logger
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.artifacts import ArtifactMetadata, ArtifactRecord, ArtifactRelations
from ..domain.decisions import SOURCE_IDENTIFICATION, Decision
from ..domain.execution import ExecutionOutput, ExecutionRequest, ExecutionResult, ExecutionTrace
from ..domain.llm_usage import LLMUsageRecord
from ..domain.registry import CapabilityProfileSpec, MethodologyPackSpec, RegistrySnapshot
from ..infrastructure.llm import LLMProvider, LLMProviderRegistry, LLMUsage
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, render_markdown, schema_instruction
from .checkpoint_service import CheckpointService
from .complexity_selector_service import select_complexity
from .context_service import ContextService
from .decision_context_builder import DecisionContextBuilder
from .decision_extraction_service import (
    DecisionExtractionService,
    decisions_instruction,
    decisions_schema,
)
from .decision_identification_service import DecisionIdentificationService
from .harness_execution_service import HarnessExecutionService, HarnessRuntimeStatus
from .merge_strategies import structural_merge
from .methodology_rules import MethodologyEvaluation, evaluate_methodology_rules

logger = get_logger("execution")


def _artifact_document_title(snapshot: RegistrySnapshot, artifact_role: str, fallback: str) -> str:
    """Имя артефакта = название документа (``title`` контракта артефакта),
    а не название задачи-производителя. Контракт ищем по ``artifact_role``
    среди зарегистрированных. Если контракта нет — fallback (имя задачи).
    """
    for contract in snapshot.artifact_contracts.values():
        if contract.artifact_role == artifact_role:
            return contract.title
    return fallback


def _render_capability_pledge(spec: CapabilityProfileSpec) -> str:
    """System-prologue для задачи с executor=agent.

    Фиксирует роль агента, его надёжные способности и пределы (cannot_do).
    Делает executor:agent реальным — генерация специализируется под контракт
    агента, а не «LLM умеет всё». Подмешивается в начало system-prompt.
    """
    caps = (
        "; ".join(
            cap.capability + (f" ({', '.join(cap.tech)})" if cap.tech else "")
            for cap in spec.capabilities
        )
        or "—"
    )
    lines = [
        "<capability_pledge>",
        f"Ты выступаешь как агент сборки {spec.identifier} (роль: {spec.role}).",
        f"Твои надёжные способности: {caps}.",
    ]
    if spec.cannot_do:
        lines.append("Ты НЕ делаешь: " + "; ".join(spec.cannot_do) + ".")
    lines.append(
        "Не обещай того, что вне твоего контракта. Части, выходящие за пределы "
        "твоих способностей, явно помечай как вне зоны ответственности "
        "(out_of_scope), а не выдавай за выполнимые."
    )
    lines.append("</capability_pledge>")
    return "\n".join(lines)


def _render_capability_brief(snapshot: RegistrySnapshot) -> str:
    """Текстовый блок с контрактами способностей агентов для промпта.

    Контракты — объекты реестра (не артефакты), поэтому не проходят через
    обычный artifact-контекст. Эта функция сериализует их в компактный
    ``<capability_profiles>``-блок, который подмешивается в user_prompt
    задачи оценки реализуемости (см. role-gate в ``_build_artifact``).
    Пустой результат, если контрактов нет — блок не добавляется.
    """
    specs = sorted(snapshot.capability_profiles.values(), key=lambda s: s.role)
    if not specs:
        return ""
    lines = [
        "<capability_catalog>",
        "Это ПРОВЕРЕННЫЙ каталог умений — единственный источник того, что мы "
        "реально умеем. Правила оценки (соблюдай строго):",
        "1. По умолчанию требование НЕ реализуемо. «Реализуемо» нужно доказать — "
        "указать конкретное умение из каталога, которое его закрывает.",
        "2. Не выдумывай умения. Нет совпадения в каталоге → статус «пробел» "
        "(возможно, добавим), а НЕ «реализуемо».",
        "3. Проверяй пределы умения (limits): если требование хочет больше "
        "(надёжность/точность/объём/нагрузка) — это «частично» (урезанный "
        "вариант) или «не реализуемо», даже если умение знакомо.",
        "4. Нехватка доступа/данных — НЕ повод для «не реализуемо»: это "
        "предусловие (prerequisites), мы запросим его отдельно.",
        "Статусы: реализуемо | частично | пробел | не реализуемо. Для каждого "
        "пункта заполни covered_by (id умения с версией) и matched_capability, "
        "либо оставь пустыми и поставь «пробел».",
    ]
    for spec in specs:
        lines.append(f"\nПрофиль {spec.identifier} (направление: {spec.role}):")
        for capability in spec.capabilities:
            req = "; ".join(capability.requires) or "—"
            tech = ", ".join(capability.tech) or "—"
            limits = "; ".join(f"{dim}: {val}" for dim, val in capability.limits) or "—"
            lines.append(
                f"  • {capability.capability} [{capability.maturity}] "
                f"tech={tech}; предусловия: {req}; пределы: {limits}"
            )
        if spec.cannot_do:
            lines.append("  не делает: " + "; ".join(spec.cannot_do))
    lines.append("</capability_catalog>")
    return "\n".join(lines)


def _json_safe(value: str) -> str:
    """Эскейпит строку для безопасной подстановки внутрь JSON-литерала."""
    # json.dumps оборачивает в кавычки — нужно их срезать, остаётся
    # корректно эскейпированное содержимое.
    return json.dumps(value, ensure_ascii=False)[1:-1]


@dataclass(frozen=True)
class ExecutionBundle:
    request: ExecutionRequest
    result: ExecutionResult
    traces: tuple[ExecutionTrace, ...]


# =============================================================================
# v3.5 — token usage hotspots (см. ArtifactMetadata.token_usage в UI)
# =============================================================================
#
# Аудит, проведённый параллельно с включением token tracking. Главные
# места «утечки» токенов одного артефакта:
#
# 1. **Системный промпт** (см. ~941–1090 ниже в этом файле): блоки
#    <role>/<writing_principles>/<grounding_rule>/<source_hierarchy>/<style_bans>
#    отправляются ЦЕЛИКОМ на каждый LLM-вызов (single_call ИЛИ N+1 в
#    per_stage_cot). Это 4–5 K tokens × 8–10 вызовов на проект.
#    Оптимизация: prompt caching (Anthropic) либо вынос статики во внешний
#    cached preamble. Эффект: -8…10% от total per project.
#
# 2. **Контекст артефакта** дублируется между выявлением решений и
#    основной генерацией (в per_stage_cot — ещё × N стадий, см. ниже).
#    Большой `context.max_tokens` в task-шаблонах (`requirements_spec_generation`
#    держит 64 K) умножает стоимость. Оптимизация: понизить max_tokens
#    в шаблонах под реалистичные размеры, либо кэшировать `base_user_prompt`.
#    Эффект: -15…20%.
#
# 3. **Per-stage CoT N+1** (см. `_execute_per_stage_cot`): для методологии
#    `lean_jtbd` (4 стадии) фактически 5 LLM-вызовов на ОДИН артефакт.
#    Каждая стадия повторно отправляет system + user-context + previous_outputs
#    (последний растёт линейно). Альтернатива: `single_call` (один вызов на
#    primary+reasoning) — почти всегда хватает на standard-задачах.
#    Эффект: -5…8%, переключается per методологии (см. stage_execution_mode).
#
# Все три можно увидеть на конкретных артефактах через ArtifactDetailPanel →
# «Подробные параметры артефакта» → раздел «Токены» (новая таблица).
# =============================================================================


class ExecutionService:
    def __init__(
        self,
        runtime: SqliteRuntime,
        context_service: ContextService,
        *,
        llm_registry: LLMProviderRegistry | None = None,
        decision_identification_service: DecisionIdentificationService | None = None,
        checkpoint_service: CheckpointService | None = None,
        # Backward-compat alias (старый аргумент). Удалить после миграции
        # внешних вызывателей. Не использовать в новом коде.
        decision_planning_service: DecisionIdentificationService | None = None,
        decision_extraction_service: "DecisionExtractionService | None" = None,
        decision_context_builder: DecisionContextBuilder | None = None,
        # Второй бэкенд исполнения: узлы с executor=harness идут через него
        # (Ф1 — stub-провайдер, без Docker). Опционален — по умолчанию stub.
        harness_service: HarnessExecutionService | None = None,
        # v3.7: phase_gap_analysis_service kwarg сохранён в сигнатуре для
        # backward-compat. Сам сервис удалён — phase-gap признан избыточной
        # абстракцией (см. обсуждение v3.7). Игнорируется.
        phase_gap_analysis_service: Any = None,
    ) -> None:
        self._runtime = runtime
        self._context_service = context_service
        self._llm = llm_registry or LLMProviderRegistry()
        self._harness = harness_service or HarnessExecutionService()
        # v3.6: выявление решений на уровне задачи + post-artifact extraction +
        # checkpoint integration. Опциональны для backward-compat в тестах;
        # в проде инжектируются из api.py.
        # Если decision_identification_service is None — task-level
        # identification пропускается; post-artifact extraction независим.
        self._decision_identification = (
            decision_identification_service or decision_planning_service
        )
        self._checkpoint = checkpoint_service
        self._decision_extraction = decision_extraction_service
        self._decision_context_builder = decision_context_builder or DecisionContextBuilder(runtime)
        # v3.7: phase_gap инстанс игнорируется (см. сигнатуру выше).
        _ = phase_gap_analysis_service

    def harness_runtime_status(self) -> HarnessRuntimeStatus:
        """Живой снимок harness-рантайма (слоты/бюджет) для панели Ф6.

        Делегирует второму бэкенду исполнения — это тот же экземпляр, что
        реально исполняет узлы, поэтому занятость слотов и расход живые.
        """
        return self._harness.runtime_status()

    def execute_task(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        task_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        cancellation: CancellationToken | None = None,
        force_decision_identification: bool = False,
    ) -> ExecutionBundle:
        """Выполнить шаг задачи.

        При переданном ``cancellation`` устанавливается ambient-скоуп отмены
        (провайдер может форсированно оборвать LLM-вызов) и проверяется
        отмена в безопасных точках: на входе и перед коммитом артефакта.

        Контракт отмены: артефакты пишутся одним коммитом в конце; отмена в
        любой момент до него => ``CancellationError`` и НОЛЬ записанных
        результатов (нечего откатывать). Если шаг успел закоммитить
        артефакт — он завершается штатно (без partial-состояния).

        ``force_decision_identification`` — пробрасывается в выявление решений (v3.6).
        """
        with cancellation_scope(cancellation):
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            try:
                return self._execute_task_impl(
                    workspace,
                    snapshot,
                    task_id,
                    provider=provider,
                    model=model,
                    cancellation=cancellation,
                    force_decision_identification=force_decision_identification,
                )
            except CancellationError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Если ошибка возникла на фоне уже запрошенной отмены —
                # трактуем её как отмену (типично: форсированный обрыв
                # LLM-ресурса всплывает как транспортная ошибка), чтобы
                # оркестратор сбросил шаг в ready, а не пометил failed.
                if cancellation is not None and cancellation.is_cancelled:
                    raise CancellationError("Шаг прерван во время исполнения.") from exc
                raise

    def _execute_task_impl(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        task_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        cancellation: CancellationToken | None = None,
        force_decision_identification: bool = False,
    ) -> ExecutionBundle:
        state = self._runtime.load_project_state(workspace)
        manifest = state.manifest
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)

        artifact_roles = template.outputs.artifact_roles
        if len(artifact_roles) != 1:
            raise ConflictError(f"Сейчас поддерживается ровно один выходной артефакт на шаблон: {template.ref.as_string()}")
        artifact_role = artifact_roles[0]
        # `provider` параметр — ЯВНЫЙ override (CLI/тест). env-переменные
        # больше НЕ управляют выбором провайдера для основного workflow —
        # они только bootstrap-помощь для первичного создания connections
        # в `ensure_default_settings`. Это устранило баг, когда UI шлёт
        # provider="" (новая семантика) → env-переменная заставляла идти
        # через legacy путь openrouter и валиться на отсутствующем ключе.
        active_provider = provider
        # W3.2: pre-selector сложности задачи. Default — off, в этом случае
        # возвращает declared `template.complexity`. С `POV_COMPLEXITY_SELECTOR=on`
        # или `=stub` оценка может перезаписать сложность по фактическому
        # контексту проекта (число активных domain packs, бизнес-запрос и т.д.).
        complexity_selection = select_complexity(template=template, state=state, llm_registry=self._llm)
        complexity_value = complexity_selection.complexity

        # Резолв LLM-провайдера. Три пути:
        # 1. ``stub`` или structural-merge — не LLM-вызов, провайдер не нужен.
        # 2. Явное имя провайдера (claude_sdk / openrouter / ...) — legacy
        #    env-based путь, оставлен для тестов и обратной совместимости CLI.
        # 3. Иначе (provider=None) — резолв через settings-store по purpose.
        #    Это **основной** путь: модель и connection определяются
        #    конфигурацией системы (Settings → Default Models).
        # executor=harness — узел исполняет автономный агент (второй бэкенд за
        # тем же контрактом артефакта). LLM-провайдер не нужен; конкретный
        # harness резолвит HarnessExecutionService (Ф1 — stub, без Docker).
        is_harness = template.executor == "harness"
        llm_provider: LLMProvider | None = None
        non_llm_path = is_harness or active_provider == "stub" or (
            template.merge is not None and template.merge.strategy == "structural"
        )
        if is_harness:
            active_provider = self._harness.default_provider_name()
            active_model = model or ""
        elif non_llm_path:
            active_model = model or _fallback_model_for_meta(active_provider or "stub", complexity_value)
        elif active_provider in {"openrouter", "claude_sdk", "claude_subscription"}:
            # Legacy: явное имя провайдера, кредиты из env.
            llm_provider = self._llm.get(
                provider=active_provider,
                model=model,
                complexity=complexity_value,
            )
            active_model = llm_provider.model or ""
        else:
            # Новый путь: модель из settings-store, connection выбирается
            # автоматически по приоритету routings.
            llm_provider = self._llm.resolve_for_purpose(
                "execution",
                complexity=complexity_value,
                override_model=model,
            )
            active_provider = llm_provider.name
            active_model = llm_provider.model or ""

        # Контекст строим ПОСЛЕ резолва модели: бюджет входа ограничен окном
        # активной модели (per-model context window из настроек UI).
        context_result = self._context_service.build_for_task(
            workspace,
            snapshot,
            task_id,
            model_context_window=self._llm.context_limit_for(active_model),
        )
        context_manifest = context_result.manifest

        active_methodology: MethodologyPackSpec | None = None
        for ref in state.process.active_methodology_pack_records.keys():
            try:
                active_methodology = snapshot.resolve_methodology_pack(ref)
                break
            except Exception:
                continue
        active_domain_refs = tuple(sorted(state.process.active_domain_pack_records.keys()))

        logger.info(
            "задача начата",
            task=task.title or task_id,
            role=artifact_role,
            provider=active_provider,
            model=active_model or None,
        )

        system_prompt, user_prompt = self._build_prompt(
            template_name=template.name,
            task_summary=template.summary,
            artifact_role=artifact_role,
            domain_pack_refs=active_domain_refs,
            current_step_title=task.title,
            context_manifest=context_manifest,
        )

        # Слой 2: специализация генерации профилем умений. Если задан
        # capability_ref — префиксим system-prompt контрактом (<capability_pledge>:
        # роль + cannot_do). Это ортогонально executor=llm. .get() — на всякий.
        if template.capability_ref is not None:
            capability_profile = snapshot.capability_profiles.get(
                template.capability_ref.as_string()
            )
            if capability_profile is not None:
                system_prompt = f"{_render_capability_pledge(capability_profile)}\n\n{system_prompt}"

        # Этап ВЫЯВЛЕНИЯ решений до сборки (checkpoint).
        # Skip-условия:
        #   - structural merge: нет LLM-вызова в принципе, нечего выявлять.
        #   - stub-провайдер: stub нужен для быстрых dev/test сценариев без
        #     реальных LLM-вызовов; если бы выявление через fallback
        #     резолвилось в реальный LLM (что оно умеет), stub перестал бы
        #     быть быстрым. `force_decision_identification=True` отключает этот skip —
        #     это test-only flag для интеграционных тестов, которые
        #     инжектят stub-выявление и должны видеть всю логику выявления
        #     без LLM-зависимостей.
        #   - planning/checkpoint сервисы не инжектированы (legacy / тесты
        #     без участия).
        is_structural_merge = (
            template.merge is not None and template.merge.strategy == "structural"
        )
        # v3.6: whitelist на уровне task-template — pure-transform / review /
        # merge задачи не порождают новых проектных решений; identification
        # для них отключаем по флагу шаблона.
        template_allows_identification = getattr(
            template, "decision_identification_enabled", True
        )
        identification_eligible = (
            self._decision_identification is not None
            and self._checkpoint is not None
            and not is_structural_merge
            and not is_harness  # harness-узел не идёт через LLM-выявление решений
            and template_allows_identification
            and (active_provider != "stub" or force_decision_identification)
        )
        applied_decisions: tuple[Decision, ...] = ()
        # v3.5: token usage по стадиям сборки артефакта; identification заполняет
        # ключ decision_identification, основная генерация — primary_generation и
        # (для per_stage_cot) methodology_stage:<id>.
        artifact_token_usage: dict[str, dict[str, int]] = {}
        if identification_eligible:
            identification_context = self._build_decision_identification_context(
                state=state,
                task=task,
                artifact_role=artifact_role,
                template=template,
                context_manifest=context_manifest,
            )
            paused_result = self._run_decision_identification_stage(
                workspace=workspace,
                state=state,
                task=task,
                artifact_role=artifact_role,
                template=template,
                context_text=identification_context,
                execution_run_id_hint=None,  # будет создан ниже при успехе
                token_usage_out=artifact_token_usage,
            )
            if paused_result is not None:
                # Workflow приостанавливается; return до основной генерации.
                # Не пишем артефакт, не делаем LLM-вызов.
                return paused_result

        decision_context = self._decision_context_builder.build_generation_constraints(
            workspace=workspace,
            project_id=manifest.project_id,
            task_id=task.task_id,
            context_manifest=context_manifest,
        )
        if decision_context.text:
            user_prompt = f"{user_prompt}\n\n{decision_context.text}"
            applied_decisions = decision_context.decisions

        # Заземление оценки реализуемости в контрактах способностей агентов.
        # Только для feasibility-задачи: контракты — объекты реестра, не
        # артефакты, поэтому подмешиваем их текстом в промпт (как
        # decision_context выше). Безвредно на stub-пути (stub не читает
        # user_prompt).
        if artifact_role == "feasibility_assessment":
            agent_brief = _render_capability_brief(snapshot)
            if agent_brief:
                user_prompt = f"{user_prompt}\n\n{agent_brief}"

        # Этап 5: если шаблон помечен как merge-задача со strategy=structural,
        # обходим LLM/stub и собираем результат детерминированно из входных
        # артефактов. Strategy=synthetic идёт обычным LLM-путём; metadata
        # отметит, что это была merge-операция. Strategy=hybrid зарезервирована.
        # Учёт токенов: usages всех LLM-вызовов задачи, заполняется ниже
        # (для stub — оценка по длине; для structural-merge — пусто, n/a).
        llm_usages: list[tuple[str | None, LLMUsage | None]] = []
        if is_harness:
            # Второй бэкенд: выход узла даёт harness-агент. Структурный JSON
            # идёт общим downstream (как LLM/stub); файловый бандл — отдельным
            # путём (store_bundle_artifact), к нему json-downstream неприменим.
            bundle_output = template.harness_output == "bundle"
            outcome = self._harness.produce_artifact(
                artifact_role=artifact_role,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                output_kind="bundle" if bundle_output else "structured",
                model_hint=model or None,
                gates=template.harness_gates,
            )
            active_provider = outcome.provider_name
            active_model = outcome.model or active_model
            if bundle_output:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                return self._finish_bundle_execution(
                    workspace=workspace,
                    snapshot=snapshot,
                    manifest=manifest,
                    task=task,
                    template=template,
                    artifact_role=artifact_role,
                    context_manifest=context_manifest,
                    outcome=outcome,
                    complexity_value=complexity_value,
                    active_provider=active_provider,
                    active_model=active_model,
                )
            payload = outcome.payload
            live_reasoning = None
            # Usage harness'а (best-effort). Нет данных → оценка по длине
            # brief+payload, чтобы учёт работал в тестах (source=estimated).
            llm_usages = [
                (
                    None,
                    outcome.usage
                    or LLMUsage.estimated(
                        input_text=outcome.brief,
                        output_text=json_dumps(payload),
                    ),
                )
            ]
        elif template.merge is not None and template.merge.strategy == "structural":
            payload = self._execute_structural_merge(
                workspace=workspace,
                context_manifest=context_manifest,
                merge_config=template.merge,
            )
            live_reasoning = None
        elif template.merge is not None and template.merge.strategy == "hybrid":
            raise ConflictError(
                f"merge.strategy=hybrid не реализован в MVP (template={template.ref.as_string()})"
            )
        elif active_provider == "stub":
            payload = self._execute_stub(
                workspace=workspace,
                artifact_role=artifact_role,
                context_manifest=context_manifest,
                business_request=state.manifest.business_request,
                goal=state.knowledge.goal_statement(),
                domain_pack_refs=active_domain_refs,
            )
            live_reasoning = None
            # Stub не зовёт LLM — usage детерминированно оцениваем по длине
            # (source=estimated), чтобы учёт работал в тестах.
            llm_usages = [
                (
                    None,
                    LLMUsage.estimated(
                        input_text=system_prompt + user_prompt,
                        output_text=json_dumps(payload),
                    ),
                )
            ]
        elif active_provider in ("openrouter", "claude_sdk", "claude_subscription"):
            primary_schema = artifact_schema(artifact_role, active_domain_refs)
            # W3.1: выбор между single_call (один LLM-вызов на primary+reasoning)
            # и per_stage_cot (отдельный вызов на каждую стадию + финальный
            # на primary с накопительным контекстом). Mode читается из
            # methodology_pack; для задач без активной методологии всегда
            # single_call.
            assert llm_provider is not None, "llm_provider должен быть собран для LLM-провайдеров"
            _cot = (
                active_methodology is not None
                and active_methodology.stage_execution_mode == "per_stage_cot"
            )
            # Идея А: на задачах, порождающих решения, модель ВЕРНЁТ принятые
            # ею решения в том же ответе (поле decisions) — без отдельного
            # послесборочного LLM-вызова. Гейт тот же, что у выявления до
            # сборки. persist_self_reported ниже дедуплицирует и сохранит их.
            decisions_schema_def = (
                decisions_schema()
                if (self._decision_extraction is not None and template_allows_identification)
                else None
            )
            # Каждый LLM-вызов логируется LoggingLLMProvider; usage по вызовам
            # (llm_usages) пишется в БД через _record_llm_usages ниже, а наша
            # метадата токенов артефакта строится из них же (см. ниже).
            if _cot:
                payload, live_reasoning, self_reported_decisions, llm_usages = (
                    self._execute_per_stage_cot(
                        llm=llm_provider,
                        base_system_prompt=system_prompt,
                        base_user_prompt=user_prompt,
                        methodology=active_methodology,
                        complexity=complexity_value,
                        primary_schema=primary_schema,
                        decisions_schema_def=decisions_schema_def,
                    )
                )
            else:
                payload, live_reasoning, self_reported_decisions, llm_usages = (
                    self._execute_single_call(
                        llm=llm_provider,
                        base_system_prompt=system_prompt,
                        base_user_prompt=user_prompt,
                        methodology=active_methodology,
                        complexity=complexity_value,
                        primary_schema=primary_schema,
                        decisions_schema_def=decisions_schema_def,
                    )
                )
        else:
            raise ConflictError(f"Неподдерживаемый provider: {active_provider}")
        # self_reported_decisions заполняет только LLM-ветка (идея А);
        # для stub/structural-merge решений модель не самоотчитывает.
        if active_provider not in ("openrouter", "claude_sdk", "claude_subscription"):
            self_reported_decisions = []
        # Метадата токенов артефакта (карточка UI) — из usage каждого вызова;
        # те же usage пишутся в БД через _record_llm_usages ниже.
        stage_token_usage = {
            stage: self._usage_to_token_dict(usage)
            for stage, usage in llm_usages
            if stage and usage is not None
        }
        artifact_token_usage.update(stage_token_usage)

        # Defensive strip устаревших полей: blocking_questions и
        # open_questions_for_user — legacy decision-ledger. Открытые/блокирующие
        # вопросы теперь живут в реестре решений (Decision ledger), отдельного
        # поля документа нет. Если LLM по инерции вернёт их в payload —
        # вычищаем, чтобы legacy не попадало в артефакт и его рендер.
        if isinstance(payload, dict):
            for legacy_field in ("blocking_questions", "open_questions_for_user"):
                payload.pop(legacy_field, None)

        # Этап 1.1: reasoning и methodology trace больше не отдельные
        # артефакты — они становятся метаинформацией primary артефакта.
        # Этап 1.4: input_artifact_ids выводим из context_manifest items
        # типа "artifact"; used_position_ids — placeholder, полностью
        # подключается в Этапе 2 (выборка положений).
        execution_run_id = str(uuid.uuid4())
        reasoning_payload: dict[str, object] = {}
        methodology_trace_payload: dict[str, object] = {}
        methodology_decisions: tuple = ()
        if active_methodology is not None:
            reasoning_payload = self._build_reasoning_payload(
                workspace=workspace,
                task=task,
                methodology=active_methodology,
                complexity=complexity_value,
                live_reasoning=live_reasoning,
                applied_decisions=applied_decisions,
            )
            evaluation = evaluate_methodology_rules(
                methodology=active_methodology,
                complexity=complexity_value,
                reasoning=reasoning_payload,
                project_id=manifest.project_id,
                task_id=task.task_id,
                methodology_mode=getattr(template, "methodology_mode", "full"),
            )
            methodology_trace_payload = self._build_methodology_trace_payload(
                methodology=active_methodology,
                complexity=complexity_value,
                evaluation=evaluation,
            )
            methodology_decisions = evaluation.decision_inputs

        input_artifact_ids = self._extract_input_artifact_ids(context_manifest)

        artifact_id = str(uuid.uuid4())

        # ИСТОЧНИК 2 (идея А, v3.10): самоотчётные эмерджентные решения.
        # Раньше тут был ОТДЕЛЬНЫЙ послесборочный LLM-вызов, перечитывавший
        # готовый артефакт. Теперь модель вернула принятые ею решения прямо
        # в ответе генерации (self_reported_decisions) — мы лишь сохраняем их
        # (дедуп по заголовку относительно реестра, status=accepted_default).
        # Минус один LLM-вызов и повторная отправка всего артефакта на вход.
        # Best-effort: сбой персиста не валит задачу.
        extracted_decisions: tuple[Decision, ...] = ()
        if self._decision_extraction is not None and self_reported_decisions:
            try:
                extracted_decisions = self._decision_extraction.persist_self_reported(
                    workspace,
                    project_id=manifest.project_id,
                    artifact_id=artifact_id,
                    task_id=task.task_id,
                    raw_decisions=self_reported_decisions,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "сохранение самоотчётных решений упало",
                    error=str(exc).strip() or type(exc).__name__,
                    exc_info=False,
                )
        # B4: при retry или повторном создании артефакта того же role
        # текущей задачи — связываем версии через parent_artifact_id и
        # помечаем предыдущую superseded. Это даёт работающую цепочку
        # версий (для L6-6 versions dropdown) и корректный «current» во
        # всех view-методах.
        previous_active = self._runtime.latest_active_artifact_by_role_and_task(
            workspace,
            artifact_role=artifact_role,
            created_by_task_id=task.task_id,
        )
        artifact_record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest.project_id,
            artifact_role=artifact_role,
            # Имя артефакта = название ДОКУМЕНТА (title контракта артефакта),
            # а не задачи. Напр. документ «Карта неоднозначностей и пробелов»
            # вместо задачи «Разобрать неоднозначность запроса». Роль —
            # отдельной мета-меткой в карточке. Fallback на имя задачи, если
            # контракт почему-то не найден.
            title=_artifact_document_title(snapshot, artifact_role, template.name),
            description=f"Артефакт, созданный задачей {task.task_key}",
            artifact_format="json",
            artifact_kind="primary",
            created_by_task_id=task.task_id,
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
            relations=ArtifactRelations(
                parent_artifact_id=previous_active.artifact_id if previous_active else None,
                input_artifact_ids=input_artifact_ids,
            ),
            metadata=ArtifactMetadata(
                template_ref=template.ref.as_string(),
                provider=active_provider,
                model=active_model,
                complexity=complexity_value,
                methodology_pack_ref=(
                    active_methodology.ref.as_string() if active_methodology else None
                ),
                execution_run_id=execution_run_id,
                merge_strategy=(template.merge.strategy if template.merge else None),
                reasoning=reasoning_payload,
                methodology_trace=methodology_trace_payload,
                used_position_ids=context_manifest.used_position_ids,
                # Уверенность вынесена из тела артефакта в метаданные.
                # Берём из payload['confidence'] для backward-compat (LLM
                # часто возвращает там, а task-промпт это всё ещё допускает),
                # клампим в допустимый диапазон [0, 1].
                overall_confidence=_extract_overall_confidence(payload),
                # v3.5: разбивка токенов по стадиям сборки артефакта.
                token_usage=dict(artifact_token_usage),
            ),
        )
        markdown_path = f"artifacts/{artifact_id}.md"
        # Граница отмены = точка коммита. Всё выше (LLM-вызов, сборка payload,
        # reasoning/trace) — in-memory, ничего ещё не записано. Если отмену
        # запросили в любой момент до сюда — сворачиваемся без единого
        # артефакта в БД: результаты обнуляются сами собой, откатывать нечего.
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        self._runtime.store_artifact(workspace, artifact=artifact_record, content=json_dumps(payload))
        logger.info(
            "артефакт сохранён",
            role=artifact_role,
            size=len(json_dumps(payload)),
            versioned=previous_active is not None,
        )
        # v3.0: связываем все Decision-записи этой задачи с свежесозданным
        # артефактом — это даёт UI «решения этого артефакта» (вкладка в
        # ArtifactDetail). Делается после store_artifact, потому что нужен
        # artifact_id. Не falls'ом — если ошибка, просто пропускаем.
        if applied_decisions:
            for d in applied_decisions:
                try:
                    # Re-fetch чтобы не перетереть status/user_action, которые
                    # могли поменяться после нашего snapshot (race protection).
                    fresh = self._runtime.get_decision(workspace, d.decision_id)
                    if artifact_id not in fresh.affected_artifact_ids:
                        updated = replace(
                            fresh,
                            affected_artifact_ids=fresh.affected_artifact_ids + (artifact_id,),
                        )
                        self._runtime.upsert_decision(workspace, updated)
                except Exception:  # noqa: BLE001
                    pass

        # v3.6: привязать extracted-decisions (источник 2) к свежесозданному
        # артефакту. Сама extraction-логика отработала ДО store_artifact —
        # см. блок выше с allocate artifact_id. Здесь — только linking.
        for d in extracted_decisions:
            try:
                fresh = self._runtime.get_decision(workspace, d.decision_id)
                if artifact_id not in fresh.affected_artifact_ids:
                    updated = replace(
                        fresh,
                        affected_artifact_ids=fresh.affected_artifact_ids + (artifact_id,),
                    )
                    self._runtime.upsert_decision(workspace, updated)
            except Exception:  # noqa: BLE001
                pass

        # v3.7: ранее здесь вызывался PhaseGapAnalysisService — он признан
        # избыточной абстракцией. Identification (с реестр-контекстом) +
        # extraction уже покрывают пространство «явные + имплицитные
        # решения». Если LLM ни на одном шаге не подняла развилку — значит
        # она либо не материальна, либо проявится в следующей задаче.
        # Конструирование «синтетических пробелов» через ещё один LLM-вызов
        # на каждую границу фазы давало лишний шум и удваивало стоимость.

        if previous_active is not None:
            # Сначала записываем новый, потом помечаем старый — атомарность
            # не нужна (worst case — оба current, query вернёт latest по
            # created_at). После этого UI и view-методы видят только новый.
            self._runtime.mark_artifact_superseded(workspace, previous_active.artifact_id)
        # Markdown-render может ругаться на неполный payload (часто такое
        # бывает при структурных merge'ах и тестовых сценариях). JSON
        # артефакта валидируется отдельно — поэтому фатально валить
        # задачу из-за визуального рендеринга не стоит. Fallback —
        # минимальный markdown с заголовком и JSON-снимком.
        try:
            markdown_render = render_markdown(artifact_role, payload)
        except (KeyError, TypeError):
            markdown_render = (
                f"# {artifact_role}\n\n"
                "_Минимальный рендер: расширенный шаблон не смог отрендерить "
                "содержимое артефакта (вероятно, не все ожидаемые поля "
                "заполнены). См. JSON-версию артефакта для полного содержимого._\n"
            )
        if markdown_render is None:
            markdown_render = f"# {artifact_role}\n"
        (workspace / markdown_path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / markdown_path).write_text(markdown_render, encoding="utf-8")

        proposed_goal = self._extract_proposed_goal(payload)
        traces = (
            ExecutionTrace(
                trace_id=str(uuid.uuid4()),
                trace_type="prompt_bundle",
                title="Prompt bundle",
                content=json_dumps(
                    {
                        "provider": active_provider,
                        "model": active_model,
                        "complexity": complexity_value,
                        "complexity_source": complexity_selection.source,
                        "complexity_rationale": complexity_selection.rationale,
                        "system_prompt": system_prompt,
                        "user_prompt": user_prompt,
                    }
                ),
            ),
            ExecutionTrace(
                trace_id=str(uuid.uuid4()),
                trace_type="response",
                title="Structured output",
                content=json_dumps(payload),
            ),
        )
        request = ExecutionRequest(
            execution_run_id=execution_run_id,
            project_id=manifest.project_id,
            task_id=task.task_id,
            template_ref=template.ref.as_string(),
            context_manifest_id=context_manifest.manifest_id,
            provider=active_provider,
            model=active_model,
            actor="workflow",
            complexity=complexity_value,
            methodology_pack_ref=active_methodology.ref.as_string() if active_methodology else None,
        )
        outputs: tuple[ExecutionOutput, ...] = (
            ExecutionOutput(artifact_id=artifact_id, artifact_role=artifact_role),
        )
        result = ExecutionResult(
            execution_run_id=request.execution_run_id,
            status="succeeded",
            outputs=outputs,
            trace_ids=tuple(trace.trace_id for trace in traces),
            proposed_goal=proposed_goal,
            methodology_decisions=methodology_decisions,
        )
        self._runtime.record_execution_run(workspace, request=request, result=result, traces=traces)
        self._record_llm_usages(
            workspace,
            project_id=manifest.project_id,
            task_id=task.task_id,
            artifact_id=artifact_id,
            execution_run_id=execution_run_id,
            provider=active_provider,
            model=active_model,
            usages=llm_usages,
        )
        return ExecutionBundle(request=request, result=result, traces=traces)

    def _finish_bundle_execution(
        self,
        *,
        workspace: Path,
        snapshot: RegistrySnapshot,
        manifest,
        task,
        template,
        artifact_role: str,
        context_manifest,
        outcome,
        complexity_value,
        active_provider: str,
        active_model: str,
    ) -> ExecutionBundle:
        """Сохранить файловый бандл harness-узла как bundle-артефакт + run.

        Отдельный путь от json-генерации: бандл (код/документы/двоичные/БД/образ)
        хранится через store_bundle_artifact, к нему json-downstream (markdown/
        решения/confidence) неприменим. Провенанс/учёт/версии — как у обычного
        артефакта (created_by_task_id, parent_artifact_id, execution run, usage).
        """
        execution_run_id = str(uuid.uuid4())
        artifact_id = str(uuid.uuid4())
        files = outcome.files or {}
        input_artifact_ids = self._extract_input_artifact_ids(context_manifest)
        previous_active = self._runtime.latest_active_artifact_by_role_and_task(
            workspace, artifact_role=artifact_role, created_by_task_id=task.task_id
        )
        usage = outcome.usage or LLMUsage.estimated(
            input_text=outcome.brief, output_text=json_dumps(sorted(files))
        )
        artifact_record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest.project_id,
            artifact_role=artifact_role,
            title=_artifact_document_title(snapshot, artifact_role, template.name),
            description=f"Артефакт, созданный задачей {task.task_key}",
            artifact_format="bundle",
            artifact_kind="primary",
            created_by_task_id=task.task_id,
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
            relations=ArtifactRelations(
                parent_artifact_id=previous_active.artifact_id if previous_active else None,
                input_artifact_ids=input_artifact_ids,
            ),
            metadata=ArtifactMetadata(
                template_ref=template.ref.as_string(),
                provider=active_provider,
                model=active_model,
                complexity=complexity_value,
                execution_run_id=execution_run_id,
                token_usage={"primary_generation": self._usage_to_token_dict(usage)},
            ),
        )
        self._runtime.store_bundle_artifact(
            workspace,
            artifact=artifact_record,
            files=files,
            bundle_kind=outcome.bundle_kind,
        )
        logger.info(
            "harness-бандл сохранён",
            role=artifact_role,
            files=len(files),
            versioned=previous_active is not None,
        )
        if previous_active is not None:
            self._runtime.mark_artifact_superseded(workspace, previous_active.artifact_id)
        traces = (
            ExecutionTrace(
                trace_id=str(uuid.uuid4()),
                trace_type="prompt_bundle",
                title="Harness brief",
                content=json_dumps(
                    {"provider": active_provider, "model": active_model, "brief": outcome.brief}
                ),
            ),
            ExecutionTrace(
                trace_id=str(uuid.uuid4()),
                trace_type="response",
                title="Harness transcript",
                content=outcome.transcript or "",
            ),
        )
        request = ExecutionRequest(
            execution_run_id=execution_run_id,
            project_id=manifest.project_id,
            task_id=task.task_id,
            template_ref=template.ref.as_string(),
            context_manifest_id=context_manifest.manifest_id,
            provider=active_provider,
            model=active_model,
            actor="workflow",
            complexity=complexity_value,
            methodology_pack_ref=None,
        )
        result = ExecutionResult(
            execution_run_id=execution_run_id,
            status="succeeded",
            outputs=(ExecutionOutput(artifact_id=artifact_id, artifact_role=artifact_role),),
            trace_ids=tuple(trace.trace_id for trace in traces),
            proposed_goal=None,
            methodology_decisions=(),
        )
        self._runtime.record_execution_run(workspace, request=request, result=result, traces=traces)
        self._record_llm_usages(
            workspace,
            project_id=manifest.project_id,
            task_id=task.task_id,
            artifact_id=artifact_id,
            execution_run_id=execution_run_id,
            provider=active_provider,
            model=active_model,
            usages=[(None, usage)],
        )
        return ExecutionBundle(request=request, result=result, traces=traces)

    # ------------------------------------------------------------------ v3.0
    # Pre-flight checkpoint stage
    # ------------------------------------------------------------------------

    def _run_decision_identification_stage(
        self,
        *,
        workspace: Path,
        state,
        task,
        artifact_role: str,
        template,
        context_text: str,
        execution_run_id_hint: str | None,
        token_usage_out: dict[str, dict[str, int]] | None = None,
    ) -> ExecutionBundle | None:
        """Запустить (или подхватить) выявление решений на запуске задачи.

        Этап выявления решений до сборки артефакта: LLM получает текущий
        реестр проекта и инструкцию не дублировать. Whitelist task-templates
        применяется на уровне
        вызывающего ``execute_task`` через флаг
        ``template.decision_identification_enabled``.

        Возвращает ``ExecutionBundle`` со статусом ``paused_for_checkpoint``
        ТОЛЬКО если задача должна остановиться. Возвращает ``None``, если
        можно продолжать (silent-accept, finalized session, или новых
        решений не возникло).

        Идемпотентен.

        Если передан ``token_usage_out`` — заполняется ключом
        ``decision_identification`` (input/output/total tokens).
        """
        assert self._checkpoint is not None
        assert self._decision_identification is not None

        # Шаг 1: проверяем существующую сессию.
        existing = self._runtime.find_pending_checkpoint_for_task(
            workspace, task.task_id
        )
        if existing is not None:
            return self._make_paused_bundle(
                project_id=state.manifest.project_id,
                task=task,
                template=template,
                checkpoint_session_id=existing.session_id,
            )

        # Шаг 2: ищем finalized session по task_id.
        prior_sessions = self._runtime.list_checkpoint_sessions(
            workspace, project_id=state.manifest.project_id
        )
        prior_for_task = [
            s for s in prior_sessions if s.task_id == task.task_id and s.status == "finalized"
        ]
        if prior_for_task:
            return None

        # Шаг 3: silent-accept без сессии? Для task_id уже есть решения,
        # выявленные на этом этапе (source == identification).
        all_project_decisions = self._runtime.list_decisions(
            workspace, project_id=state.manifest.project_id
        )
        existing_decisions_for_task = [
            d for d in all_project_decisions
            if d.source_task_id == task.task_id and d.source == SOURCE_IDENTIFICATION
        ]
        if existing_decisions_for_task:
            return None

        # Шаг 4: первый запуск — выявление новых проектных решений.
        # v3.6: передаём title'ы существующих решений, чтобы LLM не дублировала.
        existing_titles = tuple(d.title for d in all_project_decisions)

        import logging
        logger = logging.getLogger(__name__)
        # v3.7: identification — best-effort. Раньше LLM-ошибка (flaky CLI,
        # таймаут, парсинг) валила всю задачу — пользователь видел красный
        # workflow и не мог продвинуться. Теперь любая ошибка
        # identification → лог + продолжение задачи без пополнения реестра
        # для этого шага. Это соответствует semantic'у других двух
        # источников реестра (extraction уже best-effort с самого начала).
        try:
            identification = self._decision_identification.identify_for_task(
                project_id=state.manifest.project_id,
                task_id=task.task_id,
                task_title=task.title,
                artifact_role=artifact_role,
                task_summary=template.summary,
                context_text=context_text,
                existing_registry_titles=existing_titles,
            )
        except ConflictError as exc:
            msg = str(exc)
            if "Не назначена модель" in msg or "нет рабочих маршрутов" in msg:
                logger.warning(
                    "Выявление решений skip'нуто для task %s: %s. "
                    "Реестр решений не пополнится. Откройте Settings → "
                    "Default Models, чтобы назначить модель для сценария.",
                    task.task_id, msg,
                )
            else:
                # Транзиентные ошибки (CLI timeout, parse fail, rate limit)
                # — best-effort skip. Пользователь видит warning в логах
                # API; реестр не пополнится для этой задачи, но workflow
                # продолжается.
                logger.warning(
                    "Выявление решений упало для task %s (best-effort skip): %s",
                    task.task_id, msg,
                )
            return None
        except Exception as exc:  # noqa: BLE001
            # Любые неожиданные ошибки тоже soft-fail, чтобы не валить
            # workflow из-за «нерабочего ауксиллиарного сервиса».
            logger.warning(
                "Выявление решений упало (unexpected) для task %s: %s",
                task.task_id, exc,
            )
            return None

        # Фиксируем usage под ключом decision_identification.
        if token_usage_out is not None and identification.token_usage:
            token_usage_out["decision_identification"] = {
                "input_tokens": int(identification.token_usage.get("input_tokens", 0) or 0),
                "output_tokens": int(identification.token_usage.get("output_tokens", 0) or 0),
                "cache_read_tokens": int(identification.token_usage.get("cache_read_tokens", 0) or 0),
                "cache_write_tokens": int(identification.token_usage.get("cache_write_tokens", 0) or 0),
                "total_tokens": int(identification.token_usage.get("total_tokens", 0) or 0),
            }
        planning = identification  # local alias для совместимости со следующим блоком

        # КРИТИЧНО: режим участия перечитываем ЗАНОВО, прямо перед решением
        # «показать vs принять». `state` загружен в начале задачи — ДО
        # (потенциально долгого) LLM-вызова выявления решений. Если за это
        # время пользователь переключил режим (например, на autopilot),
        # `state.process.clarification_mode` устарел: задача всплыла бы с
        # вопросами вопреки autopilot (параллельный прогон, реальный баг).
        # Пост-хок путь (register_decision_inputs) уже читает режим свежим —
        # здесь делаем симметрично.
        try:
            current_mode = self._runtime.load_process_state(workspace).clarification_mode
        except Exception:
            current_mode = state.process.clarification_mode

        result = self._checkpoint.process_planned_decisions(
            workspace,
            project_id=state.manifest.project_id,
            task_id=task.task_id,
            task_title=task.title,
            artifact_role=artifact_role,
            decisions=planning.decisions,
            mode=current_mode,
        )

        if result.session is not None:
            # Создалась сессия → нужна пауза.
            return self._make_paused_bundle(
                project_id=state.manifest.project_id,
                task=task,
                template=template,
                checkpoint_session_id=result.session.session_id,
            )
        # Всё silent → можно продолжать к основной генерации.
        return None

    def _build_decision_identification_context(
        self,
        *,
        state,
        task,
        artifact_role: str,
        template,
        context_manifest,
    ) -> str:
        """Compact context for decision identification, separate from generation prompt."""

        def compact(value: str, limit: int = 1800) -> str:
            text = " ".join((value or "").split())
            if len(text) <= limit:
                return text
            return text[: limit - 1].rstrip() + "..."

        lines: list[str] = [
            "Контекст для выявления новых проектных решений.",
            f"Задача: {task.title}",
            f"Роль артефакта: {artifact_role}",
        ]
        summary = compact(getattr(template, "summary", "") or "")
        if summary:
            lines.append(f"Краткое описание задачи: {summary}")
        business_request = compact(getattr(state.manifest, "business_request", "") or "")
        if business_request:
            lines.append(f"Бизнес-запрос: {business_request}")

        context_lines: list[str] = []
        for item in context_manifest.items:
            if item.item_type == "instruction":
                continue
            content = compact(item.content, limit=1200)
            if not content:
                continue
            context_lines.append(f"- {item.title}: {content}")
            if len(context_lines) >= 8:
                break
        if context_lines:
            lines.append("Краткий task/artifact context:")
            lines.extend(context_lines)

        return "\n".join(lines)

    def _make_paused_bundle(
        self,
        *,
        project_id: str,
        task,
        template,
        checkpoint_session_id: str,
    ) -> ExecutionBundle:
        """Сформировать ExecutionBundle со статусом paused_for_checkpoint.

        НЕ сохраняется в execution_runs (workflow runner это понимает по
        статусу — это псевдо-результат, не настоящий run). Артефакт не
        создаётся. По срабатыванию `submit_answers` пользователь
        ретрайнет задачу — этот код снова отработает, увидит finalized
        session и пойдёт в нормальную генерацию.
        """
        execution_run_id = str(uuid.uuid4())
        request = ExecutionRequest(
            execution_run_id=execution_run_id,
            project_id=project_id,
            task_id=task.task_id,
            template_ref=template.ref.as_string(),
            context_manifest_id="",  # не строим manifest полностью, экономим
            provider="identification",  # помечаем источник (выявление до сборки)
            model="",
            actor="workflow",
        )
        result = ExecutionResult(
            execution_run_id=execution_run_id,
            status="paused_for_checkpoint",
            checkpoint_session_id=checkpoint_session_id,
        )
        return ExecutionBundle(request=request, result=result, traces=())

    def _record_llm_usages(
        self,
        workspace: Path,
        *,
        project_id: str,
        task_id: str,
        artifact_id: str,
        execution_run_id: str,
        provider: str,
        model: str,
        usages: list[tuple[str | None, LLMUsage | None]],
    ) -> None:
        """Записать usage каждого LLM-вызова задачи (best-effort).

        Сбой записи usage не должен ронять исполнение задачи — поэтому
        оборачиваем в try/except и не пробрасываем ошибку наружу.
        """
        for stage, usage in usages:
            if usage is None:
                # Провайдер не дал данных — n/a, строку не пишем (без выдумок).
                continue
            try:
                self._runtime.record_llm_usage(
                    workspace,
                    LLMUsageRecord(
                        usage_id=str(uuid.uuid4()),
                        project_id=project_id,
                        task_id=task_id,
                        artifact_id=artifact_id,
                        execution_run_id=execution_run_id,
                        provider=provider,
                        model=model,
                        stage=stage,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                        cache_tokens=usage.cache_tokens,
                        source=usage.source,
                        cost_usd=usage.cost_usd,
                        created_at=utc_now_iso(),
                    ),
                )
            except Exception:  # noqa: BLE001 — учёт не критичен для исполнения
                pass

    def _execute_structural_merge(
        self,
        *,
        workspace: Path,
        context_manifest,
        merge_config,
    ) -> dict:
        """Структурная merge-стратегия (Этап 5.1, structural).

        Загружает содержимое каждого input-артефакта из ``context_manifest``
        (items типа ``artifact``) и объединяет их в один dict через
        :func:`structural_merge` по политике конфликтов из ``merge_config``.

        Без LLM, детерминированно. Результат должен валидно соответствовать
        контракту выходного артефакта — это проверяет validation-слой.
        """
        prefix = "artifact:"
        inputs: list[dict] = []
        for item in context_manifest.items:
            if item.item_type != "artifact" or not item.source_ref.startswith(prefix):
                continue
            artifact_id = item.source_ref[len(prefix):]
            try:
                content = self._runtime.load_artifact_content(workspace, artifact_id)
            except Exception:
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                inputs.append(payload)
        return structural_merge(inputs, conflict_policy=merge_config.conflict_policy)

    @staticmethod
    def _extract_input_artifact_ids(context_manifest) -> tuple[str, ...]:
        """Вытащить input_artifact_ids из context_manifest для relations.

        Опирается на конвенцию `context_service`: items типа ``artifact``
        имеют ``source_ref`` вида ``artifact:<artifact_id>``.
        """
        ids: list[str] = []
        prefix = "artifact:"
        for item in context_manifest.items:
            if item.item_type != "artifact":
                continue
            if item.source_ref.startswith(prefix):
                ids.append(item.source_ref[len(prefix):])
        # Уникальные значения в порядке появления.
        seen: set[str] = set()
        unique: list[str] = []
        for artifact_id in ids:
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            unique.append(artifact_id)
        return tuple(unique)

    # ---- LLM execution dispatch ------------------------------------------

    @staticmethod
    def _usage_to_token_dict(usage: LLMUsage) -> dict[str, int]:
        """Конвертировать LLMUsage в плоский dict для метадаты артефакта (UI).

        Те же usage пишутся в БД через `_record_llm_usages`; здесь — лёгкая
        копия для карточки артефакта. Cache в LLMUsage хранится одним числом
        (cache_tokens) — кладём его в cache_read, write оставляем 0.
        """
        return {
            "input_tokens": int(usage.input_tokens),
            "output_tokens": int(usage.output_tokens),
            "cache_read_tokens": int(usage.cache_tokens or 0),
            "cache_write_tokens": 0,
            "total_tokens": int(usage.total_tokens),
        }

    def _execute_single_call(
        self,
        *,
        llm: LLMProvider,
        base_system_prompt: str,
        base_user_prompt: str,
        methodology: MethodologyPackSpec | None,
        complexity: str | None,
        primary_schema: dict,
        decisions_schema_def: dict | None = None,
    ) -> tuple[dict, dict | None, list, list[tuple[str | None, LLMUsage | None]]]:
        """`single_call` mode (default): один LLM-вызов.

        Схема ответа: всегда ``primary``; опционально ``reasoning`` (если
        активна методология) и ``decisions`` (самоотчёт о принятых решениях —
        идея А). Если ни reasoning, ни decisions не нужны — схема = голый
        primary.

        Возвращает ``(primary, reasoning|None, decisions_raw, usages)`` где
        usages — список ``(stage, LLMUsage|None)`` для учёта токенов.
        """
        if methodology is None and decisions_schema_def is None:
            result = llm.chat_json(
                system_prompt=base_system_prompt, user_prompt=base_user_prompt, schema=primary_schema,
            )
            return (result.payload, None, [], [("primary_generation", result.usage)])

        properties: dict = {"primary": primary_schema}
        required = ["primary"]
        effective_system = base_system_prompt
        if methodology is not None:
            properties["reasoning"] = self._build_methodology_schema(methodology, complexity)
            required.append("reasoning")
            effective_system += "\n\n" + self._methodology_system_section(methodology, complexity)
        if decisions_schema_def is not None:
            # Опциональное (не в required) — модель вернёт [] если выборов нет.
            properties["decisions"] = decisions_schema_def
            effective_system += "\n\n" + decisions_instruction()

        combined_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }
        result = llm.chat_json(
            system_prompt=effective_system, user_prompt=base_user_prompt, schema=combined_schema,
        )
        full_payload = result.payload
        decisions_raw = full_payload.get("decisions") or []
        return (
            full_payload.get("primary", {}) or {},
            full_payload.get("reasoning"),
            decisions_raw if isinstance(decisions_raw, list) else [],
            [("primary_generation", result.usage)],
        )

    def _execute_per_stage_cot(
        self,
        *,
        llm: LLMProvider,
        base_system_prompt: str,
        base_user_prompt: str,
        methodology: MethodologyPackSpec,
        complexity: str | None,
        primary_schema: dict,
        decisions_schema_def: dict | None = None,
    ) -> tuple[dict, dict, list, list[tuple[str | None, LLMUsage | None]]]:
        """`per_stage_cot` mode (W3.1, vision «После MVP» точка #1): отдельный
        LLM-вызов на каждую активную стадию методологии с накопительным
        контекстом, плюс финальный вызов на primary с собранным reasoning.

        Зачем: single_call упаковывает все стадии в одну схему и один
        промпт — LLM подбирает их одним проходом, без фокуса. Per-stage CoT
        даёт модели по одному вопросу за раз и накапливает структурированный
        контекст, что особенно помогает на сложных задачах со многими
        стадиями (например per-domain reasoning packs)."""
        active_stages = methodology.stages_for_complexity(complexity)
        stage_outputs: dict[str, dict] = {}
        usages: list[tuple[str | None, LLMUsage | None]] = []

        for stage in active_stages:
            stage_schema = self._build_single_stage_schema(stage)
            stage_system = self._stage_system_prompt(methodology, stage, complexity)
            stage_user = self._stage_user_prompt(
                base_user_prompt=base_user_prompt,
                stage=stage,
                previous_outputs=stage_outputs,
            )
            stage_result = llm.chat_json(
                system_prompt=stage_system, user_prompt=stage_user, schema=stage_schema,
            )
            usages.append((f"methodology_stage:{stage.identifier}", stage_result.usage))
            stage_outputs[stage.identifier] = (
                stage_result.payload if isinstance(stage_result.payload, dict) else {}
            )

        # Финальный вызов: primary artifact с reasoning как структурированный контекст.
        primary_system = (
            base_system_prompt
            + "\n\nТы прошёл методологические стадии. Ниже их выводы — используй их как явное "
            "рассуждение для построения основного артефакта. Не возвращай reasoning ещё раз."
        )
        primary_user = (
            base_user_prompt
            + "\n\n### Reasoning через стадии (per-stage CoT):\n"
            + json_dumps(stage_outputs)
        )
        # Финальный вызов может попутно вернуть самоотчётные решения (идея А).
        final_schema = primary_schema
        final_system = primary_system
        if decisions_schema_def is not None:
            final_schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["primary"],
                "properties": {"primary": primary_schema, "decisions": decisions_schema_def},
            }
            final_system = primary_system + "\n\n" + decisions_instruction()
        final_result = llm.chat_json(
            system_prompt=final_system, user_prompt=primary_user, schema=final_schema,
        )
        usages.append(("primary_generation", final_result.usage))
        final_payload = final_result.payload
        if decisions_schema_def is not None:
            decisions_raw = final_payload.get("decisions") or []
            primary_payload = final_payload.get("primary", {}) or {}
        else:
            decisions_raw = []
            primary_payload = final_payload
        return (
            primary_payload,
            stage_outputs,
            decisions_raw if isinstance(decisions_raw, list) else [],
            usages,
        )

    def _build_single_stage_schema(self, stage) -> dict:
        """JSON-schema для одной стадии — только её produces-поля."""
        properties: dict = {}
        required: list[str] = []
        for produces in stage.produces:
            properties[produces.field_name] = self._field_to_schema(produces)
            if produces.required:
                required.append(produces.field_name)
        result: dict = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        }
        if required:
            result["required"] = required
        return result

    def _stage_system_prompt(
        self,
        methodology: MethodologyPackSpec,
        stage,
        complexity: str | None,
    ) -> str:
        active_stages = methodology.stages_for_complexity(complexity)
        stage_index = next(
            (idx for idx, item in enumerate(active_stages) if item.identifier == stage.identifier),
            0,
        )
        return (
            f"Ты выполняешь стадию {stage_index + 1}/{len(active_stages)} методологии "
            f"'{methodology.title}' ({methodology.ref.as_string()}). "
            f"Текущая стадия — '{stage.identifier}': {stage.title}. "
            f"{stage.description.strip() if stage.description else ''} "
            "Сфокусируйся ТОЛЬКО на этой стадии. Заполни её поля по схеме. "
            "Не предугадывай решения следующих стадий и не дублируй уже принятые на предыдущих стадиях. "
            "Пиши только на русском языке. Верни валидный JSON по схеме."
        )

    def _stage_user_prompt(
        self,
        *,
        base_user_prompt: str,
        stage,
        previous_outputs: dict[str, dict],
    ) -> str:
        sections = [base_user_prompt]
        if previous_outputs:
            sections.append(
                "### Уже зафиксированное рассуждение (предыдущие стадии методологии):\n"
                + json_dumps(previous_outputs)
            )
        produces_lines = ", ".join(f"{p.field_name}:{p.field_type}" for p in stage.produces)
        sections.append(f"### Заполни стадию '{stage.identifier}' с полями: {produces_lines}")
        return "\n\n".join(sections)

    # ---- prompt building -------------------------------------------------

    def _build_prompt(
        self,
        *,
        template_name: str,
        task_summary: str,
        artifact_role: str,
        domain_pack_refs: tuple[str, ...],
        current_step_title: str,
        context_manifest,
    ) -> tuple[str, str]:
        # B5: system_prompt — Promp Authority Layer.
        # Без явного указания иерархии источников LLM трактует все данные
        # одинаково и может проигнорировать ответ пользователя. Здесь мы
        # фиксируем: USER DECISIONS — обязательные ограничения, ASSUMPTIONS —
        # рабочие, FACTS — база, GAPS — не утверждать, UPSTREAM — пересмотрим
        # если противоречит decision.
        system_prompt = (
            "<role>\n"
            "Ты — senior-консультант, который готовит коммерческую проектную "
            "документацию (ТЗ, бизнес-кейс, архитектурное предложение). Твоя "
            "аудитория — технический директор и бизнес-заказчик клиента. Они "
            "ценят время, не терпят «воды» и решают, запускать ли проект, по "
            "твоему тексту. Пиши только на русском языке.\n"
            "</role>\n\n"

            "<writing_principles>\n"
            "1. ПИРАМИДА МИНТО. Главный вывод раздела — первым предложением; "
            "затем обоснования и детали.\n\n"
            "2. MECE. Разделы взаимоисключающие и совокупно исчерпывающие; "
            "содержимое не дублируется между секциями.\n\n"
            "3. КОНКРЕТНОЕ — конкретно, абстрактное — абстрактно. Числа, даты, "
            "имена систем и ролей пиши ИЗ КОНТЕКСТА конкретно. Принципы, цели, "
            "обобщения, нефункциональные требования формулируй абстрактно ровно "
            "там, где этого требует природа раздела («Решение устойчиво к отказу "
            "одной из реплик» — нормально). Запрещена не абстракция как таковая, "
            "а абстракция-маскировка нехватки данных: «современный подход "
            "обеспечит эффективность» — это не абстракция, это маркетинг.\n\n"
            "4. АКТИВНЫЙ ЗАЛОГ. «Команда поставляет решение», не «решение "
            "поставляется». Субъект действия виден. Исключение — формулировки "
            "нефункциональных требований, где пассив естественен («данные "
            "шифруются при передаче»).\n\n"
            "5. BURSTINESS. Чередуй короткие предложения (5-8 слов) и "
            "аналитические (20-35 слов). Не пиши 10 предложений одинаковой "
            "длины подряд.\n\n"
            "6. СТРУКТУРНЫЙ РИТМ. Абзац → таблица или список → короткий "
            "абзац-вывод. Не выкладывай 10 одинаковых булет-листов подряд.\n\n"

            "7. ПЛОТНОСТЬ. Соотношение «смысл / слова» должно быть высоким. "
            "Правило: если слово или фраза могут быть удалены без потери "
            "фактического содержания, аргумента, требования или нюанса — "
            "удали их. ВАЖНО: плотность — это сжатие формы, а НЕ урезание "
            "сути. Все факты, числа, имена систем, требования, риски, "
            "пункты списков, оговорки и допущения СОХРАНЯЮТСЯ полностью. "
            "Удаляются только слова-наполнители: усилители без числа, "
            "повторы той же мысли другими словами, мета-комментарии о "
            "тексте, длинные обороты вместо коротких синонимов. "
            "«Команда осуществляет реализацию проекта в течение длительного "
            "промежутка времени» → «Команда ведёт проект долго». Смысл "
            "сохранён, форма ужалась на 60%.\n\n"

            "8. ОДИН АБЗАЦ — ОДНА МЫСЛЬ. Вторая мысль — второй абзац. Если "
            "абзац длиннее 5 предложений, проверь: возможно, в нём смешаны "
            "две темы и его нужно разделить.\n\n"

            "9. ИНФОРМАТИВНЫЕ ЗАГОЛОВКИ. Заголовок раздела или пункта должен "
            "сообщать, ЧТО там, а не быть ярлыком категории. «Кто принимает "
            "решение о запуске» лучше, чем «Стейкхолдеры». «Что считаем "
            "успехом» лучше, чем «KPI». Это применимо к заголовкам внутри "
            "string-полей и к подписям в пунктах списков; названия секций "
            "схемы менять нельзя — они часть контракта.\n\n"

            "10. ПАРАЛЛЕЛИЗМ СПИСКОВ. Внутри одного списка все элементы — "
            "одного грамматического типа: либо все начинаются с глагола "
            "(«внедрить...», «настроить...»), либо все — существительные "
            "(«внедрение...», «настройка...»), либо все — полные клаузы. "
            "Смешение читается как небрежность.\n\n"

            "11. ОДИН ТОН. В одном артефакте не смешивай разговорный и "
            "официальный регистр. Выбери тон под аудиторию (для CTO / "
            "бизнес-заказчика — нейтрально-деловой) и держись его.\n"
            "</writing_principles>\n\n"

            "<grounding_rule>\n"
            "Используй только факты, явно зафиксированные в:\n"
            "  • <context> текущей задачи (бизнес-запрос, upstream-артефакты),\n"
            "  • 🟢 решениях пользователя,\n"
            "  • 🔵 фактах, извлечённых из бизнес-запроса.\n\n"
            "Если факта в контексте нет — НЕ ВЫДУМЫВАЙ. Имена систем, "
            "конкретные числа, регуляторные акты, технологии вендоров — "
            "нельзя подмешивать «по аналогии» из других проектов: это "
            "галлюцинация. Если нужного факта нет, оставь поле незаполненным, "
            "поставь null, или явно отметь как assumption в reasoning. "
            "Абстрактные принципы и обоснованные обобщения, не привязанные "
            "к конкретным фактам, разрешены и нужны (см. writing_principle 3).\n"
            "</grounding_rule>\n\n"

            "<estimates_rule>\n"
            "Деньги и время НЕ оцениваем. Никакой стоимости, экономии, ROI, "
            "бюджета, сроков, длительности, дат, человеко-часов — их считают вне "
            "системы, и любая такая оценка здесь была бы выдумкой. Не упоминай их "
            "как оценки даже приблизительно.\n"
            "Прочие количественные значения (SLA, доступность, нагрузка, "
            "точность, объёмы, KPI, проценты) бери ТОЛЬКО из контекста "
            "(слова заказчика / решения). Если значения в контексте нет — не "
            "придумывай: опусти, поставь null или пометь как требующее уточнения. "
            "Никогда не подавай число как гарантию или обещание, если оно не "
            "подтверждено заказчиком.\n"
            "</estimates_rule>\n\n"

            "<source_hierarchy>\n"
            "В блоке «Контекст проекта» источники маркированы значками:\n"
            "🟢 РЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ — обязательные ограничения; не оспаривай.\n"
            "🟡 ДОПУЩЕНИЯ — рабочие; при конфликте с решением — решение выше.\n"
            "🔵 ФАКТЫ — извлечены из бизнес-запроса, база для рассуждений.\n"
            "⚫ GAPS — пустые области; не утверждай, фиксируй как открытый вопрос.\n"
            "🔻 АРТЕФАКТЫ ПРЕДЫДУЩИХ ШАГОВ — выводы коллег; при конфликте с "
            "решением пользователя — решение перевешивает.\n"
            "</source_hierarchy>\n\n"

            "<questions_to_user>\n"
            "Вопросы к пользователю формируются отдельно — на этапе выявления "
            "решений до сборки и через реестр решений (Decision/CheckpointSession). "
            "Не записывай вопросы в сам артефакт. Если в схеме есть поле "
            "`open_questions` — это нормальный раздел документа для «доуточнить "
            "на этапе детального дизайна», «согласовать с владельцем процесса», "
            "«зависит от выбора инструмента»; реальное ТЗ ВСЕГДА содержит такие "
            "пункты.\n"
            "</questions_to_user>\n\n"

            "<style_bans>\n"
            "Не используй вводные мета-фразы: «стоит отметить», «важно отметить», "
            "«важно понимать», «следует учитывать», «нельзя не заметить», "
            "«хочется отметить», «крайне важно».\n\n"
            "Не используй глаголы-связки вместо «это»: «является» (как связка), "
            "«представляет собой», «выступает в качестве», «служит основой», "
            "«играет ключевую/важную роль».\n\n"
            "Не используй кальки с английского: «на сегодняшний день», "
            "«в современном мире», «в наше время», «в заключение», «подводя итог», "
            "«давайте рассмотрим», «давайте погрузимся», «таким образом, можно "
            "сделать вывод».\n\n"
            "Не используй канцелярит: «осуществление», «реализация», «внедрение» "
            "как пустые отглагольные; «в рамках», «в целях», «посредством»; "
            "«данный/данная/данное» — пиши «этот»; «оказывает влияние» — пиши "
            "«влияет».\n\n"
            "Не используй промо-лексику: «уникальный», «инновационный», "
            "«революционный», «комплексный подход», «синергия», «ведущий», "
            "«передовой».\n\n"
            "Не используй псевдо-параллелизмы: «не просто X, а Y», «не только X, "
            "но и Y», «от стартапов до корпораций».\n\n"
            "Не злоупотребляй em-dash (макс 1 на 5 предложений). Не делай "
            "перечисления всегда из ровно трёх элементов. Не пиши все "
            "предложения одинаковой длины.\n\n"
            "Не используй мета-формулировки про сам процесс ТЗ («система должна "
            "формировать структурированное ТЗ»). Пиши про продукт, не про процесс.\n\n"
            "Не используй стоп-усилители без числа за ними: «очень», «крайне», "
            "«заметно», «существенно», «значительно», «весьма», «достаточно» "
            "(в роли усилителя), «в целом», «по сути», «как правило», "
            "«безусловно», «однозначно». Усилитель допустим только когда "
            "сопровождается величиной («существенно — на 40%»). Без числа — "
            "убирай слово, смысл не пострадает.\n\n"
            "Не используй модальные смягчители без позиции: «возможно», "
            "«вероятно», «при необходимости», «по усмотрению», «в случае "
            "необходимости», «как вариант», «в идеале». Если у тебя есть "
            "позиция — сформулируй её прямо. Если позиции нет — вынеси в "
            "`open_questions` или `assumptions`. Промежуточного «возможно "
            "стоит рассмотреть» в тексте артефакта быть не должно.\n\n"
            "Не используй длинные обороты, когда есть короткий синоним: "
            "«по причине того, что» → «так как»; «в случае если» → «если»; "
            "«в целях обеспечения» → «чтобы»; «несмотря на тот факт, что» → "
            "«хотя»; «принимая во внимание тот факт, что» → «учитывая, что».\n\n"
            "Не повторяй одну мысль дважды разными словами. Если поймал себя "
            "на том, что второе предложение перефразирует первое — удали одно "
            "из них; оставь то, что точнее.\n\n"
            "НЕ ИСПОЛЬЗУЙ ЭМОДЗИ И ПИКТОГРАММЫ. В тексте артефакта (включая "
            "заголовки разделов, маркеры списков, статусы, бейджи) запрещены "
            "🎯 ⚠ ✅ ❌ 🚀 💡 📌 📈 🔥 ✨ и все остальные Unicode-эмодзи / "
            "символы из ranges U+1F300–U+1FAFF, U+2600–U+27BF, U+2700–U+27BF. "
            "Деловой документ — текст и числа, не иконки. Визуальные акценты "
            "только структурой (заголовки, таблицы, списки), не картинками. "
            "Это правило строгое: ни в одном поле артефакта (заголовки, items, "
            "rationale, descriptions) эмодзи быть не должно.\n"
            "</style_bans>\n\n"

            "<final_self_check>\n"
            "Перед тем как вернуть ответ, выполни проверки:\n\n"
            "1. CLICHÉ SCAN. Пройди текст по списку <style_bans>. Если нашёл — "
            "перепиши предложение, сохранив смысл.\n\n"
            "2. CONCRETENESS-OR-ABSTRACTION CHECK. Каждое утверждение либо "
            "подкреплено фактом из контекста, либо является осознанной "
            "абстракцией (принцип, цель, обобщение)? Если ни то ни другое — "
            "удали или подкрепи.\n\n"
            "3. GROUNDING CHECK. Каждый фактический пункт (имя, число, срок) "
            "подкреплён контекстом? Иначе — null / «требует уточнения» / "
            "отметка assumption.\n\n"
            "4. ACTIVE VOICE. Пассивные конструкции переведены в активный залог "
            "там, где это уместно.\n\n"
            "5. EMOJI SCAN. Просканируй все строковые поля артефакта на "
            "Unicode-эмодзи / пиктограммы. Любой найденный — удали без замены "
            "(визуальная функция эмодзи берётся на себя структурой документа). "
            "Это касается заголовков разделов, маркеров, items списков, "
            "статусов — везде только текст.\n\n"
            "6. COMPRESSION TEST. Пройди по каждому абзацу и каждому пункту "
            "списка с одним вопросом: «если я удалю это слово или эту фразу, "
            "пострадает ли смысл, факт, требование, аргумент или нюанс?». "
            "Если нет — удали. Цель — повысить плотность смысла на слово, "
            "НЕ урезать содержание.\n\n"
            "   ЧТО МОЖНО УДАЛЯТЬ: стоп-усилители без числа, модальные "
            "смягчители без позиции, повторы той же мысли другими словами, "
            "мета-комментарии о тексте («ниже мы рассмотрим...»), длинные "
            "обороты вместо коротких синонимов (см. <style_bans>), пустые "
            "связки («следует отметить, что...»), отглагольные существительные "
            "вместо глаголов.\n\n"
            "   ЧТО УДАЛЯТЬ НЕЛЬЗЯ (всё это — содержание, а не форма): "
            "конкретные факты (имена систем, числа, даты, метрики, акты, "
            "роли); требования и их атрибуты; пункты списков с уникальным "
            "смыслом; риски и допущения; оговорки, которые меняют область "
            "применения утверждения; обоснования решений; ссылки на источники "
            "и upstream-артефакты.\n\n"
            "   ПРАВИЛО ОТЛИЧЕНИЯ: если после удаления документ стал "
            "ИНФОРМАЦИОННО беднее (читатель получит меньше фактов или хуже "
            "поймёт, что именно решено) — верни удалённое. Если он стал "
            "только КОРОЧЕ при той же информации — оставь удалённым.\n\n"
            "   НЕ путай compression с пропуском обязательных полей схемы. "
            "Все required-поля должны быть заполнены. Если для поля в "
            "контексте действительно нет данных и оно required — заполни его "
            "осознанной формулировкой принципа или оставь явную пометку "
            "о недостающих данных, как в GROUNDING CHECK.\n"
            "</final_self_check>\n\n"

            "<output_contract>\n"
            "Верни ТОЛЬКО валидный JSON по приложенной схеме. Без markdown-обёрток, "
            "без префиксов «Вот результат:», без комментариев после JSON.\n\n"
            "ПРАВИЛО ДЛИНЫ. Каждое поле и каждый item получает ровно столько "
            "слов, сколько нужно для полноты смысла — ни больше, ни меньше. "
            "Это не про лимит и не про минимум; это про плотность смысла на "
            "слово. Если мысль укладывается в одну точную формулировку — пиши "
            "одну формулировку. Если нужно три предложения, чтобы передать "
            "факт, обоснование и оговорку — пиши три. Запрещено добивать "
            "пункт до «приличного» объёма наполнителями.\n\n"
            "СОХРАНЕНИЕ СОДЕРЖАНИЯ. Сокращай форму, не содержание. Все факты, "
            "числа, имена систем и ролей, требования, риски, оговорки и "
            "допущения сохраняются полностью. Никакие пункты списков, "
            "обоснования или ссылки на источники не выбрасываются ради "
            "краткости. Краткость достигается удалением слов-наполнителей "
            "(см. <writing_principles> пункт 7 и <final_self_check> пункт 6), "
            "а не выбрасыванием информации.\n\n"
            "ФОРМАТ. В string-полях пиши связной прозой. В array<string>-полях "
            "каждый item — самодостаточная формулировка: либо одно ёмкое "
            "предложение с конкретикой, либо короткая цепочка «утверждение → "
            "обоснование → оговорка», если все три части несут информацию. "
            "Телеграфный обрывок без подлежащего и сказуемого («Высокая "
            "нагрузка.») допустим только когда поле явно подразумевает "
            "теги/метки. Иначе — полная формулировка минимальной длины.\n"
            "</output_contract>"
        )
        context_sections = []
        for item in context_manifest.items:
            context_sections.append(f"### {item.title}\n{item.content}")
        prompt_lines = [
            f"Текущий шаг: {current_step_title}",
            f"Тип работы: {template_name}",
        ]
        # B5: task_summary ниже попадает через context_manifest item
        # «Что должна сделать задача» (priority=1000) — здесь его НЕ
        # дублируем, чтобы избежать двойного включения и шум.
        prompt_lines.extend(
            [
                f"Активные доменные пакеты: {', '.join(domain_pack_refs) if domain_pack_refs else 'нет'}",
                schema_instruction(artifact_role, domain_pack_refs),
                "Контекст:",
                *context_sections,
            ]
        )
        user_prompt = "\n\n".join(prompt_lines)
        return system_prompt, user_prompt

    def _execute_stub(
        self,
        *,
        artifact_role: str,
        context_manifest,
        business_request: str,
        goal: str | None,
        domain_pack_refs: tuple[str, ...],
        workspace: Path | None = None,
    ) -> dict[str, object]:
        parsed_inputs: dict[str, object] = {}
        for item in context_manifest.items:
            if item.item_type != "artifact":
                continue
            # Ключуем входы по названию ПОРОДИВШЕЙ задачи, а не по
            # отображаемому имени артефакта: с v3.10 title артефакта = имя
            # документа (контракта), а _find_payload матчит входы по имени
            # задачи-производителя. Это делает стаб устойчивым к
            # переименованию заголовков. Fallback на item.title (юнит-тесты
            # передают синтетические items без workspace).
            key = item.title
            if workspace is not None and item.source_ref.startswith("artifact:"):
                try:
                    art = self._runtime.load_artifact(
                        workspace, item.source_ref[len("artifact:"):]
                    )
                    if art.created_by_task_id:
                        task_title = self._runtime.get_task(
                            workspace, art.created_by_task_id
                        ).title
                        if task_title:
                            key = task_title
                except Exception:  # noqa: BLE001 — стаб не валим из-за lookup
                    pass
            try:
                parsed_inputs[key] = json.loads(item.content)
            except json.JSONDecodeError:
                parsed_inputs[key] = item.content
        frontend_enabled = any(
            ref.startswith("frontend.web_workspace@") or ref.startswith("frontend.web_app_requirements@")
            for ref in domain_pack_refs
        )
        ml_enabled = any(
            ref.startswith("ml.predictive_analytics@") or ref.startswith("ml.predictive_analytics_pov_requirements@")
            for ref in domain_pack_refs
        )
        security_enabled = any(
            ref.startswith("security.enterprise_compliance@") or ref.startswith("security.enterprise_compliance_requirements@")
            for ref in domain_pack_refs
        )
        integration_enabled = any(
            ref.startswith("integration.enterprise_integration@") or ref.startswith("integration.enterprise_delivery_requirements@")
            for ref in domain_pack_refs
        )
        # W3.3: статические payload'ы вынесены в `templates/stub_fixtures/`.
        # Добавление нового task_template со статическим stub'ом теперь — это
        # JSON-файл + новая запись в реестре, без правки этого Python.
        # Compose-payload'ы (requirements_spec, solution_tradeoff_matrix,
        # design_document) — ниже, потому что они зависят от parsed_inputs
        # и domain flags.
        fixture_payload = self._load_stub_fixture(artifact_role, business_request, goal)
        if fixture_payload is not None:
            return fixture_payload

        if artifact_role == "solution_tradeoff_matrix":
            options = [
                {
                    "name": "Узкий PoV с минимальным контуром",
                    "summary": "Сфокусироваться на проверке ключевой гипотезы без лишних обязательств промышленного контура.",
                    "fit_for_pilot": "Хорошо подходит для короткого PoV при высоких ограничениях и неопределённости.",
                    "pros": ["Быстрый старт", "Меньше интеграционных рисков"],
                    "cons": ["Часть будущих требований остаётся за рамкой этапа"],
                    "risks": ["Ограниченная переносимость на промышленный контур"],
                },
                {
                    "name": "Расширенный пилот с учётом будущего масштаба",
                    "summary": "Сразу заложить больше интеграций, защитных ограничений и операционных требований.",
                    "fit_for_pilot": "Подходит, если заказчик готов оплачивать и согласовывать более сложный контур.",
                    "pros": ["Меньше переделок при масштабировании", "Больше уверенности в будущей архитектуре"],
                    "cons": ["Дольше и дороже", "Выше риск упереться в согласования"],
                    "risks": ["Потеря фокуса PoV и смешение этапов"],
                },
            ]
            if ml_enabled:
                options.append(
                    {
                        "name": "ML-пилот с отдельной проверкой реализуемости по данным",
                        "summary": "Сначала формализовать цель предсказания и реализуемость по данным, затем переходить к ТЗ решения.",
                        "fit_for_pilot": "Оптимально для ML/предиктивных инициатив с неясным качеством данных.",
                        "pros": ["Снижает риск ложных ожиданий к модели", "Помогает честно определить рамку этапа"],
                        "cons": ["Требует больше аналитической подготовки"],
                        "risks": ["Может выявить необходимость пересмотра ожиданий бизнеса"],
                    }
                )
            return {
                "options": options,
                "recommended_option": options[-1]["name"] if ml_enabled else options[0]["name"],
                "recommendation_rationale": (
                    "Для сложных корпоративных запросов лучше сначала снять ключевую неопределённость по данным, границам и ограничениям, "
                    "а уже затем фиксировать решение в ТЗ."
                    if ml_enabled
                    else "Для такого типа задач разумно сохранить узкую и проверяемую рамку текущего этапа."
                ),
                "deferred_decisions": [
                    "Полная операционная модель промышленного контура",
                    "Точный стек промышленного внедрения за пределами текущего этапа",
                ],
                "confidence": 0.81,
            }
        if artifact_role == "requirements_spec":
            clarification = self._find_payload(parsed_inputs, "Уточнение бизнес-цели")
            user_story_map = self._find_payload(parsed_inputs, "Анализ user story")
            alternatives = self._find_payload(parsed_inputs, "Сравнение альтернатив")
            ui_outline = self._find_payload(parsed_inputs, "Разобрать пользовательские потоки", "Анализ пользовательских потоков")
            normalized_request = self._find_payload(parsed_inputs, "Разобрать запрос", "Нормализовать запрос", "Нормализация исходного бизнес-запроса")
            business_outcome = self._find_payload(parsed_inputs, "Определить бизнес-результат", "Формализация бизнес-результата")
            scope_boundary = self._find_payload(parsed_inputs, "Зафиксировать границы этапа", "Определение границ этапа")
            stakeholders = self._find_payload(parsed_inputs, "Выделить стейкхолдеров", "Карта стейкхолдеров")
            tradeoff = self._find_payload(parsed_inputs, "Сформировать варианты решения", "Сравнение вариантов решения")
            acceptance = self._find_payload(parsed_inputs, "Сводная модель поставки и приемки")
            implementation_plan = self._find_payload(parsed_inputs, "План реализации и зависимости")
            predictive_definition = self._find_payload(parsed_inputs, "Определить предиктивную задачу", "Определение предиктивной задачи")
            data_assessment = self._find_payload(parsed_inputs, "Оценить данные для аналитики и ML", "Оценка ландшафта данных и реализуемости")
            security_constraints = self._find_payload(parsed_inputs, "Оценить ограничения ИБ и комплаенса", "Оценка ограничений ИБ и комплаенса")
            integration_model = self._find_payload(parsed_inputs, "Описать интеграционную и операционную модель", "Интеграционная и операционная модель")
            spec = {
                "title": "Техническое задание на подготовку решения",
                "business_goal": (
                    business_outcome.get("primary_business_goal")
                    if isinstance(business_outcome, dict) and business_outcome.get("primary_business_goal")
                    else (
                        clarification.get("clarified_goal")
                        if isinstance(clarification, dict) and clarification.get("clarified_goal")
                        else goal or f"Подготовить решение по запросу: {business_request}"
                    )
                ),
                "executive_summary": "Документ фиксирует целевой контур решения, рамку текущего этапа, обязательные ограничения и критерии приемки.",
                "business_context": (
                    normalized_request.get("business_problem")
                    if isinstance(normalized_request, dict) and normalized_request.get("business_problem")
                    else "Исходный бизнес-запрос преобразован в структурированную постановку."
                ),
                "target_outcomes": business_outcome.get("target_kpis", []) if isinstance(business_outcome, dict) else [],
                "scope_in": scope_boundary.get("in_scope", []) if isinstance(scope_boundary, dict) else [],
                "scope_out": scope_boundary.get("out_of_scope", []) if isinstance(scope_boundary, dict) else [],
                "success_criteria": (
                    business_outcome.get("success_metrics", [])
                    if isinstance(business_outcome, dict)
                    else clarification.get("success_criteria", []) if isinstance(clarification, dict) else []
                ),
                "actors": [item["name"] for item in user_story_map.get("actors", [])] if isinstance(user_story_map, dict) else stakeholders.get("primary_users", []) if isinstance(stakeholders, dict) else [],
                "stakeholders": stakeholders.get("decision_owners", []) + stakeholders.get("primary_users", []) if isinstance(stakeholders, dict) else [],
                "operating_model": stakeholders.get("operating_model", []) if isinstance(stakeholders, dict) else [],
                # Сценарии — структурные объекты {actor, goal, steps?}, а не
                # строки (см. схему requirements_spec + _render_user_scenarios).
                # user_story_map даёт {actor, story, value} без шагов, поэтому
                # на этом уровне steps отсутствуют — рендер покажет роль и цель.
                "user_stories": [
                    {
                        "actor": str(item.get("actor", "")).strip(),
                        "goal": (
                            f"{str(item.get('story', '')).strip()} — чтобы {str(item.get('value', '')).strip()}"
                            if str(item.get("value", "")).strip()
                            else str(item.get("story", "")).strip()
                        ),
                    }
                    for item in user_story_map.get("user_stories", [])
                ]
                if isinstance(user_story_map, dict)
                else [
                    {
                        "actor": "Бизнес-заказчик",
                        "goal": "получить прозрачное ТЗ и понятные границы этапа, чтобы запустить следующий шаг проекта",
                    },
                    {
                        "actor": "Команда реализации",
                        "goal": "видеть ограничения, результаты этапа и критерии приёмки, чтобы не строить решение на догадках",
                    },
                ],
                "data_requirements": data_assessment.get("key_features", []) + data_assessment.get("source_systems", []) if isinstance(data_assessment, dict) else [],
                "functional_requirements": [
                    "Система должна фиксировать исходный бизнес-запрос",
                    "Система должна формировать структурированное ТЗ с учётом границ этапа, результатов этапа и зависимостей",
                    "Система должна отражать доменные ограничения и обязательные требования активных пакетов",
                ],
                "non_functional_requirements": [
                    "Результат должен быть воспроизводимым",
                    "Все ключевые шаги должны быть трассируемыми",
                ],
                "integration_requirements": integration_model.get("data_delivery_pattern", []) if isinstance(integration_model, dict) else [],
                "security_requirements": security_constraints.get("mandatory_controls", []) if isinstance(security_constraints, dict) else [],
                "deployment_requirements": security_constraints.get("deployment_constraints", []) if isinstance(security_constraints, dict) else [],
                "delivery_artifacts": (
                    acceptance.get("delivery_items", ["Техническое задание"])
                    if isinstance(acceptance, dict)
                    else ["Техническое задание"]
                ),
                "assumptions": (
                    business_outcome.get("assumptions", [])
                    if isinstance(business_outcome, dict)
                    else clarification.get("assumptions", []) if isinstance(clarification, dict) else []
                ),
                "risks": (
                    implementation_plan.get("project_risks", [])
                    if isinstance(implementation_plan, dict)
                    else ["Неполные входные данные от бизнеса", "Разное понимание целей у стейкхолдеров"]
                ),
                "alternatives_considered": [
                    item["name"] for item in alternatives.get("alternatives", [])
                ]
                if isinstance(alternatives, dict)
                else [item["name"] for item in tradeoff.get("options", [])] if isinstance(tradeoff, dict) else [],
                "acceptance_criteria": (
                    acceptance.get(
                        "acceptance_criteria",
                        [
                            "Документ содержит цели, требования, ограничения и критерии приёмки",
                            "ТЗ пригодно для передачи в проектирование и реализацию",
                        ],
                    )
                    if isinstance(acceptance, dict)
                    else [
                        "Документ содержит цели, требования, ограничения и критерии приёмки",
                        "ТЗ пригодно для передачи в проектирование и реализацию",
                    ]
                ),
                "phased_plan": implementation_plan.get("proposed_timeline", []) if isinstance(implementation_plan, dict) else [],
                "open_questions": (
                    clarification.get("open_questions", []) if isinstance(clarification, dict) else []
                ),
            }
            if frontend_enabled:
                ui_outline = ui_outline if isinstance(ui_outline, dict) else {}
                spec["frontend_requirements"] = {
                    "user_roles": ui_outline.get("user_roles", []),
                    "user_flows": [flow["name"] for flow in ui_outline.get("user_flows", [])],
                    "screens": [screen["name"] for screen in ui_outline.get("screens", [])],
                    "analytics_views": ui_outline.get("analytics_views", []),
                    "decision_support_needs": ui_outline.get("decision_support_needs", []),
                    "ux_constraints": ui_outline.get("ux_constraints", []),
                }
            if ml_enabled:
                predictive_definition = predictive_definition if isinstance(predictive_definition, dict) else {}
                data_assessment = data_assessment if isinstance(data_assessment, dict) else {}
                spec["ml_requirements"] = {
                    "prediction_target": predictive_definition.get("prediction_target", ""),
                    "prediction_horizon": predictive_definition.get("prediction_horizon", ""),
                    "prediction_unit": predictive_definition.get("prediction_unit", ""),
                    "data_sources": data_assessment.get("source_systems", []),
                    "model_outputs": predictive_definition.get("model_outputs", []),
                    "evaluation_metrics": predictive_definition.get("evaluation_metrics", []),
                    "explainability_requirements": predictive_definition.get("explainability_requirements", []),
                }
            if security_enabled:
                security_constraints = security_constraints if isinstance(security_constraints, dict) else {}
                spec["security_constraints_detail"] = {
                    "deployment_constraints": security_constraints.get("deployment_constraints", []),
                    "privacy_constraints": security_constraints.get("privacy_constraints", []),
                    "access_control_constraints": security_constraints.get("access_control_constraints", []),
                    "allowed_ai_usage": security_constraints.get("allowed_ai_usage", []),
                    "mandatory_controls": security_constraints.get("mandatory_controls", []),
                    "compliance_risks": security_constraints.get("compliance_risks", []),
                }
            if integration_enabled:
                integration_model = integration_model if isinstance(integration_model, dict) else {}
                spec["integration_model"] = {
                    "source_systems": integration_model.get("source_integrations", []),
                    "delivery_pattern": integration_model.get("data_delivery_pattern", []),
                    "refresh_model": integration_model.get("refresh_model", ""),
                    "target_surfaces": integration_model.get("target_integrations", []),
                    "operating_roles": integration_model.get("operating_roles", []),
                    "dependency_risks": integration_model.get("dependency_risks", []),
                }
            return spec
        if artifact_role == "design_document":
            system_context = self._find_payload(
                parsed_inputs, "Описать системный контекст", "Системный контекст"
            )
            components = self._find_payload(
                parsed_inputs,
                "Собрать модель компонентов",
                "Модель компонентов",
                # старые заголовки — для закреплённых графом прошлых проектов
                "Выделить компоненты системы",
                "Декомпозиция на компоненты",
            )
            interactions = self._find_payload(
                parsed_inputs, "Описать потоки взаимодействия", "Потоки взаимодействия"
            )
            deployment = self._find_payload(
                parsed_inputs,
                "Собрать карту развёртывания",
                "Карта развёртывания",
                # старые заголовки — для закреплённых графом прошлых проектов
                "Описать топологию развёртывания",
                "Топология развёртывания",
            )
            risk_register = self._find_payload(
                parsed_inputs, "Собрать реестр рисков проекта", "Реестр рисков проекта"
            )

            title = (
                system_context.get("system_name")
                if isinstance(system_context, dict) and system_context.get("system_name")
                else "Архитектурный документ"
            )
            summary_parts: list[str] = []
            if isinstance(system_context, dict) and system_context.get("system_purpose"):
                summary_parts.append(system_context["system_purpose"])
            if isinstance(components, dict) and components.get("components"):
                summary_parts.append(
                    f"Декомпозирована на {len(components['components'])} ключевых компонента(ов)."
                )
            if isinstance(interactions, dict) and interactions.get("flows"):
                summary_parts.append(
                    f"Описано {len(interactions['flows'])} сценариев взаимодействия."
                )
            if not summary_parts:
                snippet = (business_request or "").strip()[:200]
                summary_parts.append(
                    f"Архитектурный документ по запросу: {snippet}." if snippet else "Архитектурный документ."
                )

            result: dict[str, object] = {
                "title": title,
                "executive_summary": " ".join(summary_parts),
                "confidence": 0.78,
            }
            if isinstance(system_context, dict) and system_context:
                # confidence — метаданные upstream-артефакта, не наши.
                result["system_context"] = {
                    k: v for k, v in system_context.items()
                    if k != "confidence"
                }
            if isinstance(components, dict) and components:
                result["components"] = {
                    k: v for k, v in components.items()
                    if k != "confidence"
                }
            if isinstance(interactions, dict) and interactions:
                result["interactions"] = {
                    k: v for k, v in interactions.items()
                    if k != "confidence"
                }
            if isinstance(deployment, dict) and deployment:
                result["deployment"] = deployment
            if isinstance(risk_register, dict) and risk_register.get("risks"):
                result["risks"] = risk_register["risks"]
            return result

        raise ConflictError(f"Stub не умеет генерировать артефакт роли '{artifact_role}'.")



    def _build_methodology_schema(self, methodology: MethodologyPackSpec, complexity: str | None) -> dict:
        active_stages = methodology.stages_for_complexity(complexity)
        properties: dict = {}
        required: list[str] = []
        for stage in active_stages:
            stage_fields: dict = {}
            stage_required: list[str] = []
            for produces in stage.produces:
                schema_fragment = self._field_to_schema(produces)
                stage_fields[produces.field_name] = schema_fragment
                if produces.required:
                    stage_required.append(produces.field_name)
            stage_schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": stage_fields,
            }
            if stage_required:
                stage_schema["required"] = stage_required
            properties[stage.identifier] = stage_schema
            if stage.identifier in methodology.reasoning_artifact.required_stages:
                required.append(stage.identifier)
        result: dict = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        }
        if required:
            result["required"] = required
        return result

    def _field_to_schema(self, produces) -> dict:
        type_value = produces.field_type
        nullable = produces.nullable
        if type_value == "string":
            base = {"type": "string"}
        elif type_value == "number":
            base = {"type": "number"}
        elif type_value == "integer":
            base = {"type": "integer"}
        elif type_value == "boolean":
            base = {"type": "boolean"}
        elif type_value == "array":
            item_schema = produces.item_schema or {}
            if isinstance(item_schema, dict) and item_schema:
                base = {"type": "array", "items": {"type": "object", "properties": {k: {} for k in item_schema}}}
            else:
                base = {"type": "array", "items": {}}
        elif type_value == "object":
            schema_payload = produces.schema or {}
            if isinstance(schema_payload, dict) and schema_payload:
                base = {"type": "object", "properties": {k: {} for k in schema_payload}}
            else:
                base = {"type": "object"}
        else:
            base = {}
        if nullable:
            return {"anyOf": [base, {"type": "null"}]}
        return base

    def _methodology_system_section(self, methodology: MethodologyPackSpec, complexity: str | None) -> str:
        active_stages = methodology.stages_for_complexity(complexity)
        lines = [
            f"Применяй методологию '{methodology.title}' (ref={methodology.ref.as_string()}).",
            "Возвращай два блока: 'primary' (основной артефакт по схеме задачи) и 'reasoning' (по схеме методологии).",
            "Стадии методологии (заполни их выходы в 'reasoning'):",
        ]
        for stage in active_stages:
            fields_desc = ", ".join(f"{p.field_name}:{p.field_type}" for p in stage.produces)
            lines.append(f"- {stage.identifier} — {stage.title}. Поля: {fields_desc}")
            if stage.description:
                lines.append(f"  {stage.description.strip()}")
        return "\n".join(lines)

    def _build_reasoning_payload(
        self,
        *,
        workspace,
        task,
        methodology: MethodologyPackSpec,
        complexity: str | None,
        live_reasoning: dict | None = None,
        applied_decisions: tuple[Decision, ...] = (),
    ) -> dict[str, object]:
        """Собрать reasoning payload для метаинформации primary артефакта.

        Этап 1.1: ранее этот payload писался как отдельный артефакт
        ``reasoning_artifact``. Теперь возвращается как данные для
        :attr:`ArtifactMetadata.reasoning` основного артефакта.
        """
        active_stages = methodology.stages_for_complexity(complexity)
        if isinstance(live_reasoning, dict) and live_reasoning:
            stages_payload = []
            for stage in active_stages:
                stage_fields = live_reasoning.get(stage.identifier)
                if not isinstance(stage_fields, dict):
                    stage_fields = {}
                stages_payload.append(
                    {
                        "stage_id": stage.identifier,
                        "title": stage.title,
                        "outputs": dict(stage_fields),
                        "_source": {
                            "methodology_pack": methodology.ref.as_string(),
                            "stage": stage.identifier,
                        },
                    }
                )
        else:
            stages_payload = []
            for stage in active_stages:
                fields = {}
                for produces in stage.produces:
                    if produces.required:
                        if produces.field_type == "string":
                            fields[produces.field_name] = (
                                f"[stub] результат стадии {stage.identifier}: {task.title}"
                            )
                        elif produces.field_type == "array":
                            fields[produces.field_name] = []
                        elif produces.field_type == "object":
                            fields[produces.field_name] = {}
                        else:
                            fields[produces.field_name] = None
                    else:
                        fields[produces.field_name] = None
                stages_payload.append(
                    {
                        "stage_id": stage.identifier,
                        "title": stage.title,
                        "outputs": fields,
                        "_source": {
                            "methodology_pack": methodology.ref.as_string(),
                            "stage": stage.identifier,
                        },
                    }
                )
        # B5: applied_decisions — ровно те ledger decisions, которые
        # DecisionContextBuilder добавил в generation prompt как compact
        # constraints. Trace больше не читает legacy ProjectKnowledge.
        applied_decision_trace = self._collect_applied_decisions(applied_decisions)
        return {
            "methodology_pack_ref": methodology.ref.as_string(),
            "stages": stages_payload,
            "complexity": complexity,
            "applied_decisions": applied_decision_trace,
        }

    @staticmethod
    def _build_methodology_trace_payload(
        *,
        methodology: MethodologyPackSpec,
        complexity: str | None,
        evaluation: MethodologyEvaluation,
    ) -> dict[str, object]:
        """Собрать methodology trace payload для метаинформации primary артефакта.

        Этап 1.1: ранее писался отдельным артефактом ``methodology_trace``.
        Теперь возвращается как данные для
        :attr:`ArtifactMetadata.methodology_trace`.
        """
        active_stages = methodology.stages_for_complexity(complexity)
        rules_evaluated = [
            {
                "stage_id": outcome.stage_id,
                "rule_id": outcome.rule_id,
                "fired": outcome.fired,
            }
            for outcome in evaluation.rule_outcomes
        ]
        # v3.1: вместо candidate_id фиксируем title (читаемо в audit-логе)
        decisions_emitted = [
            {
                "title": di.title,
                "level": di.level,
                "source_task_id": di.source_task_id,
            }
            for di in evaluation.decision_inputs
        ]
        return {
            "methodology_pack_ref": methodology.ref.as_string(),
            "stage_execution_mode": methodology.stage_execution_mode,
            "complexity": complexity,
            "stages_executed": [stage.identifier for stage in active_stages],
            "stage_outputs": evaluation.stage_outputs,
            "rules_evaluated": rules_evaluated,
            "decisions_emitted": decisions_emitted,
        }

    @staticmethod
    def _collect_applied_decisions(decisions: tuple[Decision, ...]) -> list[dict[str, object]]:
        """Build methodology reasoning trace from canonical ledger decisions."""
        result: list[dict[str, object]] = []
        for decision in decisions:
            signature = decision.signature
            result.append(
                {
                    "decision_id": signature.decision_id,
                    "title": decision.title,
                    "statement": signature.chosen_answer_summary,
                    "category": signature.category,
                    "answer_summary": signature.chosen_answer_summary,
                    "status": signature.status,
                    "source": decision.source,
                    "source_task_id": decision.source_task_id,
                }
            )
        return result

    # W3.3: путь до статических stub-фикстур. Зависит только от расположения
    # репозитория (templates/stub_fixtures/<artifact_role>.json), не привязан
    # к runtime_root. Helper кэширует прочитанные шаблоны.
    _STUB_FIXTURE_ROOT = (
        Path(__file__).resolve().parents[3] / "templates" / "stub_fixtures"
    )
    _STUB_FIXTURE_CACHE: dict[str, str] = {}

    def _load_stub_fixture(
        self,
        artifact_role: str,
        business_request: str,
        goal: str | None,
    ) -> dict[str, object] | None:
        """Читает `templates/stub_fixtures/<artifact_role>.json`, подставляет
        placeholder'ы и возвращает payload. Возвращает None, если фикстуры
        для этой роли нет — тогда вызывающий код упадёт на оставшиеся в
        Python compose-кейсы (requirements_spec, review_report,
        solution_tradeoff_matrix).
        """
        cached = self._STUB_FIXTURE_CACHE.get(artifact_role)
        if cached is None:
            path = self._STUB_FIXTURE_ROOT / f"{artifact_role}.json"
            if not path.exists():
                return None
            cached = path.read_text(encoding="utf-8")
            self._STUB_FIXTURE_CACHE[artifact_role] = cached

        goal_text = goal or f"Подготовить качественное ТЗ по запросу: {business_request}"
        substituted = (
            cached
            .replace("{{goal}}", _json_safe(goal_text))
            .replace("{{business_request_short}}", _json_safe(business_request[:160]))
            .replace("{{business_request}}", _json_safe(business_request))
        )
        try:
            payload = json.loads(substituted)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _find_payload(self, parsed_inputs: dict[str, object], *title_prefixes: str) -> dict[str, object]:
        for title, payload in parsed_inputs.items():
            normalized_title = title.lower()
            for title_prefix in title_prefixes:
                if title_prefix.lower() in normalized_title and isinstance(payload, dict):
                    return payload
        return {}

    def _extract_proposed_goal(self, payload: dict[str, object]) -> str | None:
        for key in ("clarified_goal", "primary_business_goal", "business_goal"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


def _extract_overall_confidence(payload: dict[str, object]) -> float | None:
    """Достать `confidence` из payload и привести к диапазону [0, 1].

    Уверенность — это метаданные артефакта (см. ArtifactMetadata.
    overall_confidence), а не часть бизнес-содержимого. LLM по
    инерции продолжает возвращать число в payload['confidence'],
    и task-промпты допускают это для backward-compat. Тут мы её
    извлекаем, чтобы дальше она жила в единственном месте — в
    метаданных. Если confidence нет / не число / NaN — возвращаем None
    (метаданные допускают отсутствующее значение).
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("confidence")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value:  # NaN check
        return None
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _fallback_model_for_meta(provider: str, complexity: str | None) -> str:
    """Имя модели, которое попадёт в metadata артефакта, если LLM-вызова
    не было (stub, structural merge).

    Для stub / structural — чисто косметическая метка; UI её показывает в
    карточке артефакта. Возвращаем пустую строку или имя провайдера, чтобы
    в UI не висело устаревшее значение реальной модели.
    """
    del complexity  # не используется — модель определяется реальным LLM
    if provider == "stub":
        return "stub"
    return provider
