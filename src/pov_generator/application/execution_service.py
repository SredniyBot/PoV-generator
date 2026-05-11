from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.artifacts import ArtifactRecord
from ..domain.execution import ExecutionOutput, ExecutionRequest, ExecutionResult, ExecutionTrace
from ..domain.registry import MethodologyPackSpec, RegistrySnapshot
from ..infrastructure.claude_sdk_client import ClaudeSdkClient, model_for_complexity
from ..infrastructure.claude_subscription_client import (
    ClaudeSubscriptionClient,
)
from ..infrastructure.claude_subscription_client import (
    model_for_complexity as model_for_complexity_subscription,
)
from ..infrastructure.openrouter_client import OpenRouterClient
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, render_markdown, schema_instruction
from .complexity_selector_service import select_complexity
from .context_service import ContextService
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
    def __init__(self, runtime: SqliteRuntime, context_service: ContextService) -> None:
        self._runtime = runtime
        self._context_service = context_service

    def execute_task(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        task_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> ExecutionBundle:
        manifest = self._runtime.load_manifest(workspace)
        state = self._runtime.load_problem_state(workspace)
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)
        context_result = self._context_service.build_for_task(workspace, snapshot, task_id)
        context_manifest = context_result.manifest

        artifact_roles = template.outputs.artifact_roles
        if len(artifact_roles) != 1:
            raise ConflictError(f"Сейчас поддерживается ровно один выходной артефакт на шаблон: {template.ref.as_string()}")
        artifact_role = artifact_roles[0]
        active_provider = provider or os.environ.get("POV_EXECUTION_PROVIDER", "stub")
        # W3.2: pre-selector сложности задачи. Default — off, в этом случае
        # возвращает declared `template.complexity`. С `POV_COMPLEXITY_SELECTOR=on`
        # или `=stub` оценка может перезаписать сложность по фактическому
        # контексту проекта (число активных domain packs, бизнес-запрос и т.д.).
        complexity_selection = select_complexity(template=template, state=state)
        complexity_value = complexity_selection.complexity
        if active_provider == "claude_sdk":
            active_model = model or model_for_complexity(complexity_value)
        elif active_provider == "claude_subscription":
            active_model = model or model_for_complexity_subscription(complexity_value)
        else:
            active_model = model or os.environ.get("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        active_methodology: MethodologyPackSpec | None = None
        for ref in state.active_methodology_pack_records.keys():
            try:
                active_methodology = snapshot.resolve_methodology_pack(ref)
                break
            except Exception:
                continue

        system_prompt, user_prompt = self._build_prompt(
            template_name=template.name,
            task_summary=template.summary,
            artifact_role=artifact_role,
            domain_pack_refs=tuple(sorted(state.active_domain_pack_records.keys())),
            current_step_title=task.title,
            context_manifest=context_manifest,
        )

        if active_provider == "stub":
            payload = self._execute_stub(
                artifact_role=artifact_role,
                context_manifest=context_manifest,
                business_request=state.business_request,
                goal=state.goal,
                domain_pack_refs=tuple(sorted(state.active_domain_pack_records.keys())),
            )
            live_reasoning = None
        elif active_provider in ("openrouter", "claude_sdk", "claude_subscription"):
            primary_schema = artifact_schema(artifact_role, tuple(sorted(state.active_domain_pack_records.keys())))
            # W3.1: выбор между single_call (один LLM-вызов на primary+reasoning)
            # и per_stage_cot (отдельный вызов на каждую стадию + финальный
            # на primary с накопительным контекстом). Mode читается из
            # methodology_pack; для задач без активной методологии всегда
            # single_call.
            if (
                active_methodology is not None
                and active_methodology.stage_execution_mode == "per_stage_cot"
            ):
                payload, live_reasoning = self._execute_per_stage_cot(
                    provider=active_provider,
                    model=active_model,
                    base_system_prompt=system_prompt,
                    base_user_prompt=user_prompt,
                    methodology=active_methodology,
                    complexity=complexity_value,
                    primary_schema=primary_schema,
                )
            else:
                payload, live_reasoning = self._execute_single_call(
                    provider=active_provider,
                    model=active_model,
                    base_system_prompt=system_prompt,
                    base_user_prompt=user_prompt,
                    methodology=active_methodology,
                    complexity=complexity_value,
                    primary_schema=primary_schema,
                )
        else:
            raise ConflictError(f"Неподдерживаемый provider: {active_provider}")

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
            title=f"{template.name} ({artifact_role})",
            description=f"Артефакт, созданный задачей {task.task_key}",
            artifact_format="json",
            artifact_kind="primary",
            created_by_task_id=task.task_id,
            parent_artifact_id=previous_active.artifact_id if previous_active else None,
            metadata={"template_ref": template.ref.as_string()},
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
        )
        markdown_path = f"artifacts/{artifact_id}.md"
        self._runtime.store_artifact(workspace, artifact=artifact_record, content=json_dumps(payload))
        if previous_active is not None:
            # Сначала записываем новый, потом помечаем старый — атомарность
            # не нужна (worst case — оба current, query вернёт latest по
            # created_at). После этого UI и view-методы видят только новый.
            self._runtime.mark_artifact_superseded(workspace, previous_active.artifact_id)
        markdown_render = render_markdown(artifact_role, payload)
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
            execution_run_id=str(uuid.uuid4()),
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
        outputs: list[ExecutionOutput] = [ExecutionOutput(artifact_id=artifact_id, artifact_role=artifact_role)]
        methodology_candidates: tuple = ()
        if active_methodology is not None:
            reasoning_id, reasoning_payload = self._attach_reasoning_artifact(
                workspace=workspace,
                manifest_project_id=manifest.project_id,
                task=task,
                methodology=active_methodology,
                complexity=complexity_value,
                primary_payload=payload,
                live_reasoning=live_reasoning,
            )
            evaluation = evaluate_methodology_rules(
                methodology=active_methodology,
                complexity=complexity_value,
                reasoning=reasoning_payload,
                project_id=manifest.project_id,
                task_id=task.task_id,
            )
            trace_id = self._attach_methodology_trace(
                workspace=workspace,
                manifest_project_id=manifest.project_id,
                task=task,
                methodology=active_methodology,
                complexity=complexity_value,
                evaluation=evaluation,
            )
            outputs.append(ExecutionOutput(artifact_id=reasoning_id, artifact_role="reasoning_artifact", kind="reasoning"))
            outputs.append(ExecutionOutput(artifact_id=trace_id, artifact_role="methodology_trace", kind="trace"))
            methodology_candidates = evaluation.candidates
        result = ExecutionResult(
            execution_run_id=request.execution_run_id,
            status="succeeded",
            outputs=tuple(outputs),
            trace_ids=tuple(trace.trace_id for trace in traces),
            proposed_goal=proposed_goal,
            methodology_candidates=methodology_candidates,
        )
        self._runtime.record_execution_run(workspace, request=request, result=result, traces=traces)
        return ExecutionBundle(request=request, result=result, traces=traces)

    # ---- LLM execution dispatch ------------------------------------------

    def _chat_json(
        self,
        *,
        provider: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
    ) -> dict:
        """Единая точка вызова LLM, чтобы execute_task / per-stage / primary
        ходили одной дорогой и не плодили switch'ей по провайдеру."""
        if provider == "openrouter":
            return OpenRouterClient.from_env().chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
            )
        if provider == "claude_sdk":
            return ClaudeSdkClient.from_env(model=model).chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
            )
        if provider == "claude_subscription":
            return ClaudeSubscriptionClient.from_env(model=model).chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
            )
        raise ConflictError(f"Неподдерживаемый provider: {provider}")

    def _execute_single_call(
        self,
        *,
        provider: str,
        model: str,
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
            full_payload = self._chat_json(
                provider=provider, model=model,
                system_prompt=effective_system, user_prompt=base_user_prompt, schema=combined_schema,
            )
            return (full_payload.get("primary", {}) or {}, full_payload.get("reasoning"))
        full_payload = self._chat_json(
            provider=provider, model=model,
            system_prompt=base_system_prompt, user_prompt=base_user_prompt, schema=primary_schema,
        )
        return (full_payload, None)

    def _execute_per_stage_cot(
        self,
        *,
        provider: str,
        model: str,
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
            stage_result = self._chat_json(
                provider=provider, model=model,
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
        primary_payload = self._chat_json(
            provider=provider, model=model,
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
            "Ты работаешь как дисциплинированный системный аналитик. "
            "Пиши только на русском языке. "
            "Не придумывай факты, которых нет во входах. "
            "Большинство шагов ты должен выполнять максимально добросовестно на основе имеющейся информации. "
            "Если информации недостаточно для уверенного вывода, не останавливайся сразу: "
            "сделай максимально ответственный анализ, но явно снижай поле `confidence` и заполняй `blocking_questions`. "
            "Эскалация к человеку допустима только если без ответа нельзя продолжать добросовестно. "
            "\n\n"
            "ВАЖНО — ИЕРАРХИЯ ИСТОЧНИКОВ ЗНАНИЯ.\n"
            "В блоке «Контекст проекта» источники маркированы значками. Уважай иерархию:\n"
            "🟢 РЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ (USER DECISIONS) — обязательные ограничения. "
            "Они зафиксированы пользователем и НЕ ПОДЛЕЖАТ пересмотру тобой. "
            "Если твой вывод противоречит решению — перепиши вывод. "
            "Если без нарушения решения задачу не выполнить — добавь конкретный пункт в blocking_questions, не молчи.\n"
            "🟡 ДОПУЩЕНИЯ СИСТЕМЫ (ASSUMPTIONS) — рабочие предположения. "
            "Используй, но при конфликте с решением пользователя выбирай решение, а допущение явно отмечай как устаревшее в reasoning.\n"
            "🔵 ИЗВЕСТНЫЕ ФАКТЫ — извлечены из исходного запроса, можно использовать как базу.\n"
            "⚫ ОТКРЫТЫЕ ПРОБЕЛЫ (GAPS) — это области, где данных нет. "
            "НЕ УТВЕРЖДАЙ ничего в этих областях; либо опирайся на default, либо явно укажи нехватку в blocking_questions.\n"
            "🔻 АРТЕФАКТЫ ПРЕДЫДУЩИХ ШАГОВ — выводы других задач. Опирайся, но при конфликте с решением пользователя — решение важнее.\n"
            "\n"
            "Не задавай в `blocking_questions` то, на что УЖЕ есть ответ в «Решениях пользователя». "
            "Если в схеме твоего ответа есть `reasoning_steps` или поле для логики решения — "
            "явно отмечай какие решения пользователя ты учёл (по их формулировке).\n"
            "\n"
            "Верни только валидный JSON без пояснений вне JSON."
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
        # Compose-payload'ы (requirements_spec, review_report,
        # solution_tradeoff_matrix) — ниже, потому что они зависят от
        # parsed_inputs и domain flags.
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
        if artifact_role == "review_report":
            spec_payload = self._find_payload(parsed_inputs, "Подготовить структурированное ТЗ", "Подготовка структурированного ТЗ")
            if not spec_payload:
                spec_payload = self._find_payload(parsed_inputs, "Подготовка черновика ТЗ")
            issues = []
            blocking_questions: list[str] = []
            if not isinstance(spec_payload, dict) or not spec_payload.get("functional_requirements"):
                issues.append({"severity": "error", "message": "В ТЗ отсутствуют функциональные требования."})
            if frontend_enabled and (
                not isinstance(spec_payload, dict)
                or "frontend_requirements" not in spec_payload
                or not spec_payload["frontend_requirements"].get("screens")
            ):
                issues.append({"severity": "error", "message": "Для проекта с интерфейсом не заполнен раздел требований к интерфейсу."})
            if ml_enabled and (not isinstance(spec_payload, dict) or not spec_payload.get("ml_requirements")):
                issues.append({"severity": "critical", "message": "Для проекта с аналитикой и ML в ТЗ отсутствует раздел требований к модели и данным.", "area": "ml", "requires_user_input": False})
            if security_enabled and (not isinstance(spec_payload, dict) or not spec_payload.get("security_constraints_detail")):
                issues.append({"severity": "critical", "message": "Для проекта с ограничениями ИБ в ТЗ отсутствует раздел безопасности и приватности.", "area": "security", "requires_user_input": False})
            if integration_enabled and (not isinstance(spec_payload, dict) or not spec_payload.get("integration_model")):
                issues.append({"severity": "critical", "message": "Для проекта с интеграциями в ТЗ отсутствует раздел интеграционной модели.", "area": "integration", "requires_user_input": False})
            if isinstance(spec_payload, dict) and not spec_payload.get("open_questions"):
                blocking_questions = []
            status = "passed" if not issues else "needs_changes"
            return {
                "overall_status": status,
                "summary": "Черновик ТЗ можно принимать." if status == "passed" else "Черновик ТЗ требует доработки.",
                "confidence": 0.9 if status == "passed" else 0.62,
                "strengths": [
                    "Структура документа выдержана",
                    "Есть связь с целями, рамкой этапа и требованиями к результату",
                ],
                "issues": issues,
                "blocking_questions": blocking_questions,
                "recommendations": (
                    ["Можно переходить к следующему этапу."]
                    if status == "passed"
                    else ["Исправить замечания и повторно провести ревью."]
                ),
            }
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

    def _attach_reasoning_artifact(
        self,
        *,
        workspace,
        manifest_project_id: str,
        task,
        methodology: MethodologyPackSpec,
        complexity: str | None,
        primary_payload: dict,
        live_reasoning: dict | None = None,
    ) -> tuple[str, dict]:
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
        reasoning_payload = {
            "methodology_pack_ref": methodology.ref.as_string(),
            "stages": stages_payload,
            "complexity": complexity,
            "applied_decisions": applied_decisions,
        }
        artifact_id = str(uuid.uuid4())
        # B4: при retry задачи прошлый reasoning_artifact помечается
        # superseded, и новый ссылается на него через parent_artifact_id.
        # Это даёт чистую историю reasoning per attempt.
        previous_reasoning = self._runtime.latest_active_artifact_by_role_and_task(
            workspace,
            artifact_role="reasoning_artifact",
            created_by_task_id=task.task_id,
        )
        record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest_project_id,
            artifact_role="reasoning_artifact",
            title=f"Reasoning ({task.task_key})",
            description=f"Reasoning artifact задачи {task.task_key} по методологии {methodology.ref.as_string()}",
            artifact_format="json",
            artifact_kind="reasoning",
            created_by_task_id=task.task_id,
            parent_artifact_id=previous_reasoning.artifact_id if previous_reasoning else None,
            metadata={
                "methodology_pack_ref": methodology.ref.as_string(),
                "complexity": complexity,
            },
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
        )
        self._runtime.store_artifact(workspace, artifact=record, content=json_dumps(reasoning_payload))
        if previous_reasoning is not None:
            self._runtime.mark_artifact_superseded(workspace, previous_reasoning.artifact_id)
        return artifact_id, reasoning_payload

    def _attach_methodology_trace(
        self,
        *,
        workspace,
        manifest_project_id: str,
        task,
        methodology: MethodologyPackSpec,
        complexity: str | None,
        evaluation: MethodologyEvaluation,
    ) -> str:
        active_stages = methodology.stages_for_complexity(complexity)
        # Список правил по стадиям из методологии — нужен, чтобы не потерять
        # записи о правилах для стадий, которые не попали в evaluation
        # (на случай рассинхрона). evaluation.rule_outcomes — основной источник.
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
        trace_payload = {
            "methodology_pack_ref": methodology.ref.as_string(),
            "stage_execution_mode": methodology.stage_execution_mode,
            "complexity": complexity,
            "stages_executed": [stage.identifier for stage in active_stages],
            "stage_outputs": evaluation.stage_outputs,
            "rules_evaluated": rules_evaluated,
            "candidates_emitted": candidates_emitted,
        }
        artifact_id = str(uuid.uuid4())
        # B4: при retry задачи прошлый methodology_trace помечается
        # superseded; новый ссылается на него через parent_artifact_id.
        previous_trace = self._runtime.latest_active_artifact_by_role_and_task(
            workspace,
            artifact_role="methodology_trace",
            created_by_task_id=task.task_id,
        )
        record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest_project_id,
            artifact_role="methodology_trace",
            title=f"Methodology trace ({task.task_key})",
            description=f"Трасса исполнения методологии {methodology.ref.as_string()}",
            artifact_format="json",
            artifact_kind="trace",
            created_by_task_id=task.task_id,
            parent_artifact_id=previous_trace.artifact_id if previous_trace else None,
            metadata={
                "methodology_pack_ref": methodology.ref.as_string(),
                "complexity": complexity,
            },
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
        )
        self._runtime.store_artifact(workspace, artifact=record, content=json_dumps(trace_payload))
        if previous_trace is not None:
            self._runtime.mark_artifact_superseded(workspace, previous_trace.artifact_id)
        return artifact_id

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
            state = self._runtime.load_problem_state(workspace)
        except Exception:
            return []
        result: list[dict[str, object]] = []
        for decision_id, fact in state.decisions.items():
            source = (fact.source or "").strip()
            relevant = True
            request_id: str | None = None
            if source.startswith("clarification:"):
                request_id = source.split(":", 1)[1]
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
                    "decision_id": decision_id,
                    "statement": fact.statement,
                    "source": source,
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
