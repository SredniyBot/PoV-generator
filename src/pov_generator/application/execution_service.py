from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.artifacts import ArtifactMetadata, ArtifactRecord, ArtifactRelations
from ..domain.execution import ExecutionOutput, ExecutionRequest, ExecutionResult, ExecutionTrace
from ..domain.registry import MethodologyPackSpec, RegistrySnapshot
from ..infrastructure.llm import LLMProvider, LLMProviderRegistry
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, render_markdown, schema_instruction
from .complexity_selector_service import select_complexity
from .context_service import ContextService
from .merge_strategies import structural_merge
from .methodology_rules import MethodologyEvaluation, evaluate_methodology_rules


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


class ExecutionService:
    def __init__(
        self,
        runtime: SqliteRuntime,
        context_service: ContextService,
        *,
        llm_registry: LLMProviderRegistry | None = None,
    ) -> None:
        self._runtime = runtime
        self._context_service = context_service
        self._llm = llm_registry or LLMProviderRegistry()

    def execute_task(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        task_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> ExecutionBundle:
        state = self._runtime.load_project_state(workspace)
        manifest = state.manifest
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)
        context_result = self._context_service.build_for_task(workspace, snapshot, task_id)
        context_manifest = context_result.manifest

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
        llm_provider: LLMProvider | None = None
        non_llm_path = active_provider == "stub" or (
            template.merge is not None and template.merge.strategy == "structural"
        )
        if non_llm_path:
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
        active_methodology: MethodologyPackSpec | None = None
        for ref in state.process.active_methodology_pack_records.keys():
            try:
                active_methodology = snapshot.resolve_methodology_pack(ref)
                break
            except Exception:
                continue
        active_domain_refs = tuple(sorted(state.process.active_domain_pack_records.keys()))

        system_prompt, user_prompt = self._build_prompt(
            template_name=template.name,
            task_summary=template.summary,
            artifact_role=artifact_role,
            domain_pack_refs=active_domain_refs,
            current_step_title=task.title,
            context_manifest=context_manifest,
        )

        # Этап 5: если шаблон помечен как merge-задача со strategy=structural,
        # обходим LLM/stub и собираем результат детерминированно из входных
        # артефактов. Strategy=synthetic идёт обычным LLM-путём; metadata
        # отметит, что это была merge-операция. Strategy=hybrid зарезервирована.
        if template.merge is not None and template.merge.strategy == "structural":
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
                artifact_role=artifact_role,
                context_manifest=context_manifest,
                business_request=state.manifest.business_request,
                goal=state.knowledge.goal_statement(),
                domain_pack_refs=active_domain_refs,
            )
            live_reasoning = None
        elif active_provider in ("openrouter", "claude_sdk", "claude_subscription"):
            primary_schema = artifact_schema(artifact_role, active_domain_refs)
            # W3.1: выбор между single_call (один LLM-вызов на primary+reasoning)
            # и per_stage_cot (отдельный вызов на каждую стадию + финальный
            # на primary с накопительным контекстом). Mode читается из
            # methodology_pack; для задач без активной методологии всегда
            # single_call.
            assert llm_provider is not None, "llm_provider должен быть собран для LLM-провайдеров"
            if (
                active_methodology is not None
                and active_methodology.stage_execution_mode == "per_stage_cot"
            ):
                payload, live_reasoning = self._execute_per_stage_cot(
                    llm=llm_provider,
                    base_system_prompt=system_prompt,
                    base_user_prompt=user_prompt,
                    methodology=active_methodology,
                    complexity=complexity_value,
                    primary_schema=primary_schema,
                )
            else:
                payload, live_reasoning = self._execute_single_call(
                    llm=llm_provider,
                    base_system_prompt=system_prompt,
                    base_user_prompt=user_prompt,
                    methodology=active_methodology,
                    complexity=complexity_value,
                    primary_schema=primary_schema,
                )
        else:
            raise ConflictError(f"Неподдерживаемый provider: {active_provider}")

        # Этап 1.1: reasoning и methodology trace больше не отдельные
        # артефакты — они становятся метаинформацией primary артефакта.
        # Этап 1.4: input_artifact_ids выводим из context_manifest items
        # типа "artifact"; used_position_ids — placeholder, полностью
        # подключается в Этапе 2 (выборка положений).
        execution_run_id = str(uuid.uuid4())
        reasoning_payload: dict[str, object] = {}
        methodology_trace_payload: dict[str, object] = {}
        methodology_candidates: tuple = ()
        if active_methodology is not None:
            reasoning_payload = self._build_reasoning_payload(
                workspace=workspace,
                task=task,
                methodology=active_methodology,
                complexity=complexity_value,
                live_reasoning=live_reasoning,
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
            methodology_candidates = evaluation.candidates

        input_artifact_ids = self._extract_input_artifact_ids(context_manifest)

        artifact_id = str(uuid.uuid4())
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
            # Раньше склеивали `<template.name> (<role_id>)` — техническое id
            # роли в скобках захламляло список артефактов в UI и не несло
            # информации для пользователя. Теперь title = чистое название
            # шаблона задачи; роль артефакта показывается отдельной мета-меткой
            # в карточке.
            title=template.name,
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
            ),
        )
        markdown_path = f"artifacts/{artifact_id}.md"
        self._runtime.store_artifact(workspace, artifact=artifact_record, content=json_dumps(payload))
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
            methodology_candidates=methodology_candidates,
        )
        self._runtime.record_execution_run(workspace, request=request, result=result, traces=traces)
        return ExecutionBundle(request=request, result=result, traces=traces)

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

    def _execute_single_call(
        self,
        *,
        llm: LLMProvider,
        base_system_prompt: str,
        base_user_prompt: str,
        methodology: MethodologyPackSpec | None,
        complexity: str | None,
        primary_schema: dict,
    ) -> tuple[dict, dict | None]:
        """`single_call` mode (default): один LLM-вызов, объединённая схема
        `primary + reasoning` (если есть активная методология) либо просто
        `primary` (если методология не наложена)."""
        if methodology is not None:
            methodology_schema = self._build_methodology_schema(methodology, complexity)
            combined_schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["primary", "reasoning"],
                "properties": {"primary": primary_schema, "reasoning": methodology_schema},
            }
            effective_system = (
                base_system_prompt
                + "\n\n"
                + self._methodology_system_section(methodology, complexity)
            )
            full_payload = llm.chat_json(
                system_prompt=effective_system, user_prompt=base_user_prompt, schema=combined_schema,
            )
            return (full_payload.get("primary", {}) or {}, full_payload.get("reasoning"))
        full_payload = llm.chat_json(
            system_prompt=base_system_prompt, user_prompt=base_user_prompt, schema=primary_schema,
        )
        return (full_payload, None)

    def _execute_per_stage_cot(
        self,
        *,
        llm: LLMProvider,
        base_system_prompt: str,
        base_user_prompt: str,
        methodology: MethodologyPackSpec,
        complexity: str | None,
        primary_schema: dict,
    ) -> tuple[dict, dict]:
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
            stage_outputs[stage.identifier] = stage_result if isinstance(stage_result, dict) else {}

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
        primary_payload = llm.chat_json(
            system_prompt=primary_system, user_prompt=primary_user, schema=primary_schema,
        )
        return (primary_payload, stage_outputs)

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

            "<source_hierarchy>\n"
            "В блоке «Контекст проекта» источники маркированы значками:\n"
            "🟢 РЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ — обязательные ограничения; не оспаривай.\n"
            "🟡 ДОПУЩЕНИЯ — рабочие; при конфликте с решением — решение выше.\n"
            "🔵 ФАКТЫ — извлечены из бизнес-запроса, база для рассуждений.\n"
            "⚫ GAPS — пустые области; не утверждай, фиксируй как открытый вопрос.\n"
            "🔻 АРТЕФАКТЫ ПРЕДЫДУЩИХ ШАГОВ — выводы коллег; при конфликте с "
            "решением пользователя — решение перевешивает.\n"
            "</source_hierarchy>\n\n"

            "<two_uncertainty_channels>\n"
            "1. `open_questions` ВНУТРИ АРТЕФАКТА (если такое поле есть в схеме) — "
            "нормальный раздел документа. Сюда идёт всё, что «доуточнить на "
            "этапе детального дизайна», «согласовать с владельцем процесса», "
            "«зависит от выбора инструмента». Реальное ТЗ ВСЕГДА содержит "
            "такие пункты.\n\n"
            "2. `blocking_questions` В МЕТАДАННЫХ — стоп-сигнал «без ответа не "
            "могу произвести артефакт вообще». Используй ТОЛЬКО когда бизнес-цель "
            "или скоуп фундаментально неоднозначны, либо данные в контексте "
            "противоречат друг другу и без выбора артефакт будет вредить.\n\n"
            "Детали реализации, выбор инструментов, конкретные пороги метрик — "
            "НЕ blocking_questions. Это open_questions или assumptions.\n\n"
            "Пустой blocking_questions — нормальное и желаемое состояние.\n"
            "</two_uncertainty_channels>\n\n"

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

            "<pre_flight_check>\n"
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
            "</pre_flight_check>\n\n"

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
            "(см. <writing_principles> пункт 7 и <pre_flight_check> пункт 6), "
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
    ) -> dict[str, object]:
        parsed_inputs: dict[str, object] = {}
        for item in context_manifest.items:
            if item.item_type == "artifact":
                try:
                    parsed_inputs[item.title] = json.loads(item.content)
                except json.JSONDecodeError:
                    parsed_inputs[item.title] = item.content
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
                "blocking_questions": [],
            }
        if artifact_role == "requirements_spec":
            clarification = self._find_payload(parsed_inputs, "Уточнение бизнес-цели")
            user_story_map = self._find_payload(parsed_inputs, "Анализ user story")
            alternatives = self._find_payload(parsed_inputs, "Сравнение альтернатив")
            ui_outline = self._find_payload(parsed_inputs, "Разобрать пользовательские потоки", "Анализ пользовательских потоков")
            normalized_request = self._find_payload(parsed_inputs, "Нормализовать запрос", "Нормализация исходного бизнес-запроса")
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
                "user_stories": [
                    f"Как {item['actor']}, я хочу {item['story']}, чтобы {item['value']}"
                    for item in user_story_map.get("user_stories", [])
                ]
                if isinstance(user_story_map, dict)
                else (
                    [
                        "Как бизнес-заказчик, я хочу получить прозрачное ТЗ и понятные границы этапа, чтобы запустить следующий шаг проекта.",
                        "Как команда реализации, я хочу видеть ограничения, результаты этапа и критерии приемки, чтобы не строить решение на догадках.",
                    ]
                ),
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
                parsed_inputs, "Выделить компоненты системы", "Декомпозиция на компоненты"
            )
            interactions = self._find_payload(
                parsed_inputs, "Описать потоки взаимодействия", "Потоки взаимодействия"
            )
            deployment = self._find_payload(
                parsed_inputs, "Описать топологию развёртывания", "Топология развёртывания"
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
                "blocking_questions": [],
            }
            if isinstance(system_context, dict) and system_context:
                # Очищаем blocking_questions/confidence — они метаданные
                # upstream-артефакта, не наши.
                result["system_context"] = {
                    k: v for k, v in system_context.items()
                    if k not in {"blocking_questions", "confidence"}
                }
            if isinstance(components, dict) and components:
                result["components"] = {
                    k: v for k, v in components.items()
                    if k not in {"blocking_questions", "confidence"}
                }
            if isinstance(interactions, dict) and interactions:
                result["interactions"] = {
                    k: v for k, v in interactions.items()
                    if k not in {"blocking_questions", "confidence"}
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
        # B5: applied_decisions — список decision_id, доступных на момент
        # исполнения и потенциально учтённых этой задачей. Это даёт явную
        # трассировку: пользователь видит «вот эти ответы повлияли на
        # вывод». Заполняем системно по релевантности (LLM не обязан
        # ничего знать про id'ы — мы их матчим сами по state.decisions
        # и affected_task_ids источника).
        applied_decisions = self._collect_applied_decisions(workspace, task.task_id)
        return {
            "methodology_pack_ref": methodology.ref.as_string(),
            "stages": stages_payload,
            "complexity": complexity,
            "applied_decisions": applied_decisions,
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
                **({"candidate_id": outcome.candidate_id} if outcome.candidate_id else {}),
            }
            for outcome in evaluation.rule_outcomes
        ]
        candidates_emitted = [
            {
                "candidate_id": candidate.candidate_id,
                "source_id": candidate.source_id,
                "severity": candidate.severity,
                "blocking_scope": candidate.blocking_scope,
            }
            for candidate in evaluation.candidates
        ]
        return {
            "methodology_pack_ref": methodology.ref.as_string(),
            "stage_execution_mode": methodology.stage_execution_mode,
            "complexity": complexity,
            "stages_executed": [stage.identifier for stage in active_stages],
            "stage_outputs": evaluation.stage_outputs,
            "rules_evaluated": rules_evaluated,
            "candidates_emitted": candidates_emitted,
        }

    def _collect_applied_decisions(
        self, workspace: Path, task_id: str
    ) -> list[dict[str, object]]:
        """B5: формирует список decisions, релевантных текущей задаче.

        Семантика: если decision из ClarificationRequest, чей
        `affected_task_ids` содержит task_id — он применим. Также сюда
        попадают global decisions (не привязанные к конкретной задаче).

        Используется в reasoning_artifact как трассировка: пользователь
        видит «вот эти ответы повлияли на этот reasoning». Closes M-J6.
        """
        try:
            knowledge = self._runtime.load_knowledge(workspace)
        except Exception:
            return []
        result: list[dict[str, object]] = []
        clarification_prefix = "clarification."
        for position in knowledge.by_type("decision"):
            relevant = True
            request_id: str | None = None
            if (
                position.source == "clarification"
                and position.identifier.startswith(clarification_prefix)
            ):
                request_id = position.identifier[len(clarification_prefix):]
                try:
                    request = self._runtime.get_clarification_request(
                        workspace, request_id
                    )
                except Exception:
                    request = None
                if request is not None:
                    affected = request.affected_task_ids or ()
                    if affected and task_id not in affected:
                        relevant = False
            if not relevant:
                continue
            result.append(
                {
                    "decision_id": position.identifier,
                    "statement": position.statement,
                    "source": position.source,
                    "via_clarification_id": request_id,
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
