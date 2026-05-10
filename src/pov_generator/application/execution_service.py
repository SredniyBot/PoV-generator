from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import uuid

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.artifacts import ArtifactRecord
from ..domain.execution import ExecutionOutput, ExecutionRequest, ExecutionResult, ExecutionTrace
from ..domain.registry import MethodologyPackSpec, RegistrySnapshot
from ..infrastructure.claude_sdk_client import ClaudeSdkClient, model_for_complexity
from ..infrastructure.claude_subscription_client import (
    ClaudeSubscriptionClient,
    model_for_complexity as model_for_complexity_subscription,
)
from ..infrastructure.openrouter_client import OpenRouterClient
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, render_markdown, schema_instruction
from .context_service import ContextService
from .methodology_rules import MethodologyEvaluation, evaluate_methodology_rules


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
        complexity_value = template.complexity
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
            framework_summary=template.framework_summary,
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
        elif active_provider in ("openrouter", "claude_sdk", "claude_subscription"):
            primary_schema = artifact_schema(artifact_role, tuple(sorted(state.active_domain_pack_records.keys())))
            if active_methodology is not None:
                methodology_schema = self._build_methodology_schema(active_methodology, complexity_value)
                combined_schema = {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["primary", "reasoning"],
                    "properties": {
                        "primary": primary_schema,
                        "reasoning": methodology_schema,
                    },
                }
                effective_system = system_prompt + "\n\n" + self._methodology_system_section(active_methodology, complexity_value)
            else:
                combined_schema = primary_schema
                effective_system = system_prompt
            if active_provider == "openrouter":
                full_payload = OpenRouterClient.from_env().chat_json(
                    system_prompt=effective_system,
                    user_prompt=user_prompt,
                    schema=combined_schema,
                )
            elif active_provider == "claude_sdk":
                full_payload = ClaudeSdkClient.from_env(model=active_model).chat_json(
                    system_prompt=effective_system,
                    user_prompt=user_prompt,
                    schema=combined_schema,
                )
            else:
                full_payload = ClaudeSubscriptionClient.from_env(model=active_model).chat_json(
                    system_prompt=effective_system,
                    user_prompt=user_prompt,
                    schema=combined_schema,
                )
            if active_methodology is not None:
                payload = full_payload.get("primary", {}) or {}
                live_reasoning = full_payload.get("reasoning")
            else:
                payload = full_payload
                live_reasoning = None
        else:
            raise ConflictError(f"Неподдерживаемый provider: {active_provider}")
        if active_provider == "stub":
            live_reasoning = None

        artifact_id = str(uuid.uuid4())
        artifact_record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest.project_id,
            artifact_role=artifact_role,
            title=f"{template.name} ({artifact_role})",
            description=f"Артефакт, созданный задачей {task.task_key}",
            artifact_format="json",
            artifact_kind="primary",
            created_by_task_id=task.task_id,
            parent_artifact_id=None,
            metadata={"template_ref": template.ref.as_string()},
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
        )
        markdown_path = f"artifacts/{artifact_id}.md"
        self._runtime.store_artifact(workspace, artifact=artifact_record, content=json_dumps(payload))
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

    def _build_prompt(
        self,
        *,
        template_name: str,
        framework_summary: str,
        artifact_role: str,
        domain_pack_refs: tuple[str, ...],
        current_step_title: str,
        context_manifest,
    ) -> tuple[str, str]:
        system_prompt = (
            "Ты работаешь как дисциплинированный системный аналитик. "
            "Пиши только на русском языке. "
            "Не придумывай факты, которых нет во входах. "
            "Большинство шагов ты должен выполнять максимально добросовестно на основе имеющейся информации. "
            "Если информации недостаточно для уверенного вывода, не останавливайся сразу: "
            "сделай максимально ответственный анализ, но явно снижай поле `confidence` и заполняй `blocking_questions`. "
            "Эскалация к человеку допустима только если без ответа нельзя продолжать добросовестно. "
            "Верни только валидный JSON без пояснений вне JSON."
        )
        context_sections = []
        for item in context_manifest.items:
            context_sections.append(f"### {item.title}\n{item.content}")
        user_prompt = "\n\n".join(
            [
                f"Текущий шаг: {current_step_title}",
                f"Тип работы: {template_name}",
                f"Методология шага: {framework_summary}",
                f"Активные доменные пакеты: {', '.join(domain_pack_refs) if domain_pack_refs else 'нет'}",
                schema_instruction(artifact_role, domain_pack_refs),
                "Контекст:",
                *context_sections,
            ]
        )
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
        if artifact_role == "clarification_notes":
            return {
                "clarified_goal": goal or f"Подготовить качественное ТЗ по запросу: {business_request}",
                "success_criteria": [
                    "Требования структурированы и непротиворечивы",
                    "Есть критерии приёмки и список рисков",
                ],
                "assumptions": [
                    "Заказчик готов отвечать на уточняющие вопросы",
                    "Исходный бизнес-запрос отражает реальную потребность",
                ],
                "open_questions": [
                    "Есть ли жёсткие ограничения по срокам?",
                    "Нужны ли интеграции с существующими системами?",
                ],
            }
        if artifact_role == "request_fact_sheet":
            return {
                "explicit_facts": [
                    "Инициатива описана как проект по формализации решения и подготовке ТЗ",
                    "В запросе уже присутствуют ожидания к результату, ограничения и рамка этапа",
                ],
                "named_entities": [
                    "Бизнес-заказчик",
                    "Команда реализации",
                    "Корпоративные системы и данные",
                ],
                "requested_deliverables": [
                    "Структурированное техническое задание",
                    "Зафиксированная рамка текущего этапа",
                ],
                "mentioned_systems_and_sources": [
                    "Источники данных и корпоративные системы, если они названы в запросе",
                ],
                "mentioned_metrics_and_targets": [
                    "Целевые метрики и ожидаемый эффект, если они присутствуют в брифе",
                ],
                "confidence": 0.9,
                "blocking_questions": [],
            }
        if artifact_role == "goal_hypothesis":
            return {
                "hypothesized_goal": goal or f"Подготовить пригодное к реализации ТЗ по запросу: {business_request}",
                "expected_effects": [
                    "Снизить неопределенность в постановке задачи",
                    "Создать основу для оценки и запуска следующего этапа проекта",
                ],
                "project_stage_hypothesis": "Текущий этап следует трактовать как формализацию требований и проверку рамки решения, а не как полноценное промышленное внедрение.",
                "success_signals": [
                    "Согласована цель этапа",
                    "Понятны обязательные результаты этапа и критерии приемки",
                ],
                "unresolved_goal_points": [
                    "Не всегда явно указан горизонт ожидаемого эффекта",
                    "Может отсутствовать явное разделение целей бизнеса и целей этапа",
                ],
                "confidence": 0.84,
                "blocking_questions": [],
            }
        if artifact_role == "constraint_inventory":
            return {
                "explicit_constraints": [
                    "Нельзя придумывать отсутствующие факты",
                    "Нужно держать рамку текущего этапа отдельно от будущего масштабирования",
                ],
                "inferred_constraints": [
                    "Часть ограничений может быть зашита в описании желаемого решения",
                    "На проект могут влиять данные, интеграции, безопасность и требования к поставке",
                ],
                "stage_constraints": [
                    "Документ должен быть пригоден для текущего этапа",
                    "Нельзя обещать полноценный промышленный контур без отдельного этапа",
                ],
                "environment_constraints": [
                    "Возможны ограничения на контур размещения, доступ и обработку данных",
                ],
                "dependency_constraints": [
                    "Ключевые решения могут зависеть от подтверждений со стороны заказчика, ИТ или ИБ",
                ],
                "confidence": 0.83,
                "blocking_questions": [],
            }
        if artifact_role == "ambiguity_gap_report":
            return {
                "ambiguous_points": [
                    "В запросе могут быть смешаны бизнес-цель, рамка этапа и детали будущего решения",
                    "Не всегда явно разделены PoV, пилот и промышленный контур",
                ],
                "conflicting_signals": [
                    "Есть риск одновременно ожидать быстрый пилот и слишком широкую рамку проекта",
                ],
                "missing_decisions": [
                    "Кто утверждает границы текущего этапа",
                    "Какие результаты обязательны именно сейчас",
                ],
                "safe_assumptions": [
                    "Текущий бриф следует трактовать как запрос на формализацию решения, а не как готовое ТЗ",
                ],
                "escalation_candidates": [
                    "Неопределенный владелец результата",
                    "Отсутствие решения по критичным ограничениям этапа",
                ],
                "confidence": 0.8,
                "blocking_questions": [],
            }
        if artifact_role == "normalized_request":
            return {
                "request_summary": f"Запрос нормализован как инициатива по проектированию решения: {business_request[:160]}",
                "business_problem": "Нужно формализовать бизнес-проблему, целевой эффект, ограничения и рамку проекта, не смешивая PoV с промышленным внедрением.",
                "requested_solution_elements": [
                    "Подготовить структурированное техническое задание",
                    "Зафиксировать ограничения, ожидания и границы этапа",
                    "Понять, какие доменные требования обязательны",
                ],
                "explicit_constraints": [
                    "Нельзя выдумывать недостающие факты",
                    "Нужно отделять текущий этап от будущего масштабирования",
                ],
                "implicit_risks": [
                    "В запросе могут быть смешаны бизнес-цель, решение и будущий промышленный контур",
                    "Часть важных ограничений может быть названа неявно",
                ],
                "ambiguous_points": [
                    "Не до конца понятно, какие результаты обязательны именно на текущем этапе",
                    "Может отсутствовать ясность по формальному процессу приемки",
                ],
                "confidence": 0.88,
                "blocking_questions": [],
            }
        if artifact_role == "business_outcome_model":
            return {
                "primary_business_goal": goal or f"Сформировать управляемое и пригодное к реализации ТЗ по запросу: {business_request}",
                "target_kpis": [
                    "Сократить неопределённость в постановке задачи",
                    "Снизить риск передать в реализацию неполную постановку",
                ],
                "success_metrics": [
                    "ТЗ покрывает цели, ограничения, результаты этапа и критерии приемки",
                    "Документ можно использовать как основание для оценки и запуска работ",
                ],
                "business_process_impacts": [
                    "Ускорение перехода от сырого брифа к проектируемому решению",
                    "Снижение числа уточнений на поздних этапах проекта",
                ],
                "expected_decisions": [
                    "Можно ли запускать следующий этап проекта",
                    "Какой вариант решения и какая рамка этапа рекомендуются",
                ],
                "value_hypotheses": [
                    "Чёткая постановка снижает стоимость ошибок на этапах проектирования и реализации",
                    "Ранняя фиксация ограничений улучшает предсказуемость проекта",
                ],
                "assumptions": [
                    "Бриф отражает реальную потребность бизнеса",
                    "Заказчик готов уточнять критические пробелы только при реально blocking-вопросах",
                ],
                "confidence": 0.86,
                "blocking_questions": [],
            }
        if artifact_role == "scope_boundary_matrix":
            return {
                "in_scope": [
                    "Подготовка структурированного ТЗ для текущего этапа",
                    "Фиксация ограничений и обязательных входов",
                    "Определение критериев приемки текущего этапа",
                ],
                "out_of_scope": [
                    "Подробный производственный план внедрения",
                    "Полноценное сопровождение промышленной системы за пределами текущего этапа",
                ],
                "pilot_boundaries": [
                    "Текущий этап должен быть ограничен рамкой PoV/PoC/пилота, если это следует из брифа",
                    "Будущее масштабирование фиксируется отдельно как следующая фаза",
                ],
                "future_phase_candidates": [
                    "Промышленное внедрение",
                    "Расширение интеграций и эксплуатационного контура",
                ],
                "mandatory_deliverables": [
                    "Техническое задание",
                    "Фиксация критериев приемки и рисков",
                ],
                "excluded_deliverables": [
                    "Полный производственный бэклог",
                    "Детальный план многолетнего развития",
                ],
                "confidence": 0.82,
                "blocking_questions": [],
            }
        if artifact_role == "stakeholder_map":
            return {
                "stakeholder_groups": [
                    {
                        "name": "Бизнес-заказчик",
                        "role": "Владелец потребности и ожидаемого эффекта",
                        "influence": "Высокое",
                        "expectations": ["Получить полезный результат этапа", "Согласовать рамку проекта"],
                    },
                    {
                        "name": "Команда реализации",
                        "role": "Исполнитель и оценщик реализуемости",
                        "influence": "Высокое",
                        "expectations": ["Получить непротиворечивое ТЗ", "Понять зависимости и ограничения"],
                    },
                ],
                "primary_users": ["Бизнес-заказчик", "Команда реализации"],
                "data_owners": ["Владельцы данных и корпоративных систем"],
                "support_teams": ["ИТ", "Архитектура", "Информационная безопасность"],
                "confidence": 0.81,
                "blocking_questions": [],
            }
        if artifact_role == "decision_ownership_matrix":
            return {
                "decisions": [
                    {
                        "name": "Подтверждение цели и ценности этапа",
                        "owner": "Бизнес-заказчик",
                        "participants": ["Команда реализации"],
                        "timing": "До финализации ТЗ",
                    },
                    {
                        "name": "Подтверждение границ текущего этапа",
                        "owner": "Заказчик этапа",
                        "participants": ["ИТ", "Архитектура"],
                        "timing": "До оценки работ",
                    },
                ],
                "unowned_decisions": [
                    "Часть решений о следующей фазе может требовать отдельного владельца",
                ],
                "approval_points": [
                    "Согласование состава результатов этапа",
                    "Подтверждение критериев приемки",
                ],
                "confidence": 0.8,
                "blocking_questions": [],
            }
        if artifact_role == "operating_model_outline":
            return {
                "process_flow": [
                    "Бизнес формулирует потребность и подтверждает цель",
                    "Команда реализации анализирует ограничения и готовит ТЗ",
                    "Результат проходит приемку и используется для запуска следующего этапа",
                ],
                "producer_roles": ["Бизнес-заказчик", "Владельцы исходных данных и ограничений"],
                "consumer_roles": ["Команда реализации", "Лица, принимающие решение о продолжении проекта"],
                "support_roles": ["ИТ", "Архитектура", "Информационная безопасность"],
                "handoff_risks": [
                    "Неполная передача контекста между бизнесом и реализацией",
                    "Размытая ответственность за внешние согласования",
                ],
                "confidence": 0.79,
                "blocking_questions": [],
            }
        if artifact_role == "stakeholder_operating_model":
            return {
                "stakeholder_groups": [
                    {
                        "name": "Бизнес-заказчик",
                        "role": "Владелец цели и ценности проекта",
                        "expectations": ["Получить результат, влияющий на бизнес-метрики", "Понять границы текущего этапа"],
                        "responsibilities": ["Согласовать цель", "Подтвердить критерии приемки"],
                    },
                    {
                        "name": "Команда реализации",
                        "role": "Исполнитель технического решения",
                        "expectations": ["Получить непротиворечивое ТЗ", "Понять ограничения и зависимости"],
                        "responsibilities": ["Оценить реализуемость", "Построить решение в заданной рамке"],
                    },
                ],
                "primary_users": ["Бизнес-заказчик", "Команда реализации"],
                "decision_owners": ["Заказчик этапа", "Ответственный со стороны ИТ/архитектуры"],
                "operating_model": [
                    "Бизнес формулирует потребность и принимает результат этапа",
                    "Исполнитель готовит решение в согласованной рамке",
                    "Критические пробелы эскалируются только при низкой уверенности или blocking gaps",
                ],
                "adoption_constraints": [
                    "Разные ожидания у бизнеса и ИТ могут размывать границы этапа",
                    "Если owner решения не определён, проект становится плохо управляемым",
                ],
                "confidence": 0.8,
                "blocking_questions": [],
            }
        if artifact_role == "user_story_map":
            return {
                "actors": [
                    {"name": "Бизнес-заказчик", "needs": ["Понять целевой результат", "Согласовать требования"]},
                    {"name": "Команда реализации", "needs": ["Получить ясное и проверяемое ТЗ"]},
                ],
                "user_stories": [
                    {
                        "actor": "Бизнес-заказчик",
                        "story": "получить структурированное ТЗ по исходному запросу",
                        "value": "быстро перейти к следующему этапу проекта",
                    },
                    {
                        "actor": "Команда реализации",
                        "story": "видеть ограничения, риски и критерии приёмки",
                        "value": "реализовывать решение без лишних догадок",
                    },
                ],
                "edge_cases": [
                    "Исходный запрос слишком расплывчат",
                    "У разных стейкхолдеров разные ожидания",
                ],
            }
        if artifact_role == "alternatives_analysis":
            return {
                "alternatives": [
                    {
                        "name": "Быстрое упрощённое ТЗ",
                        "description": "Сделать короткое ТЗ с минимальным набором разделов.",
                        "pros": ["Быстро", "Дешево"],
                        "cons": ["Высокий риск пропустить важные детали"],
                    },
                    {
                        "name": "Полное структурированное ТЗ",
                        "description": "Собрать требования, ограничения, риски и критерии приёмки.",
                        "pros": ["Лучше качество постановки", "Проще передавать в реализацию"],
                        "cons": ["Нужно больше времени на анализ"],
                    },
                ],
                "recommended_option": "Полное структурированное ТЗ",
                "rationale": "Для снижения риска ошибок на следующих этапах нужен более полный и проверяемый документ.",
            }
        if artifact_role == "solution_option_inventory":
            return {
                "options": [
                    {
                        "name": "Узкий проверочный этап",
                        "summary": "Сфокусироваться на проверке ключевой гипотезы и минимальном обязательном контуре.",
                        "boundary_fit": "Хорошо соответствует раннему этапу с высокой неопределенностью.",
                        "enabling_conditions": ["Ясная цель этапа", "Готовность не обещать лишний промышленный функционал"],
                    },
                    {
                        "name": "Расширенный пилотный этап",
                        "summary": "Заложить больше интеграций, требований и операционных ожиданий уже сейчас.",
                        "boundary_fit": "Подходит, если у заказчика есть ресурс и зрелость для более широкого охвата.",
                        "enabling_conditions": ["Подтвержденный бюджет", "Определенные владельцы решений и данных"],
                    },
                ],
                "comparison_axes": [
                    "Соответствие рамке этапа",
                    "Скорость запуска",
                    "Риск пропустить критичные требования",
                    "Сложность внедрения",
                ],
                "confidence": 0.82,
                "blocking_questions": [],
            }
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
        if artifact_role == "delivery_scope_definition":
            return {
                "delivery_items": [
                    "Структурированное техническое задание",
                    "Фиксация ограничений, рисков и открытых вопросов",
                    "Основание для запуска следующего этапа",
                ],
                "excluded_items": [
                    "Полный производственный бэклог",
                    "Эксплуатационная документация промышленного уровня",
                ],
                "demo_expectations": [
                    "Результат должен быть понятен бизнесу и команде реализации",
                    "По документу должно быть видно, что входит в текущий этап, а что нет",
                ],
                "evidence_artifacts": [
                    "Техническое задание",
                    "Ревью-отчёт",
                ],
                "confidence": 0.84,
                "blocking_questions": [],
            }
        if artifact_role == "acceptance_model_definition":
            return {
                "acceptance_criteria": [
                    "Документ покрывает цели, ограничения, требования и критерии приемки",
                    "По документу можно оценивать и запускать следующий этап проекта",
                ],
                "success_evidence": [
                    "Ключевые стейкхолдеры согласны с рамкой этапа",
                    "Команда реализации может использовать документ без критических догадок",
                ],
                "required_customer_inputs": [
                    "Подтверждение бизнес-цели",
                    "Подтверждение границ текущего этапа",
                ],
                "formal_approvals": [
                    "Согласование результатов этапа",
                    "Подтверждение критериев приемки",
                ],
                "rejection_conditions": [
                    "В ТЗ отсутствуют критические ограничения или требования",
                    "Не определены обязательные входы и владельцы решений",
                ],
                "confidence": 0.83,
                "blocking_questions": [],
            }
        if artifact_role == "delivery_acceptance_plan":
            return {
                "delivery_items": [
                    "Структурированное ТЗ",
                    "Явная фиксация ограничений и рисков",
                    "Список открытых вопросов и зависимостей",
                ],
                "acceptance_criteria": [
                    "Документ покрывает бизнес-цель, рамку этапа, результаты этапа и ограничения",
                    "ТЗ можно использовать для оценки и запуска следующего этапа",
                ],
                "success_evidence": [
                    "Стейкхолдеры понимают границы этапа",
                    "Команда реализации может использовать документ без критических догадок",
                ],
                "required_customer_inputs": [
                    "Подтверждение рамки этапа",
                    "Подтверждение критических ограничений и требований",
                ],
                "formal_approvals": [
                    "Согласование бизнес-цели",
                    "Подтверждение критериев приемки",
                ],
                "open_dependencies": [
                    "Скорость ответа заказчика на действительно blocking-вопросы",
                    "Наличие ответственных лиц со стороны бизнеса и ИТ",
                ],
                "confidence": 0.84,
                "blocking_questions": [],
            }
        if artifact_role == "dependency_map":
            return {
                "critical_dependencies": [
                    "Подтверждение цели и рамки этапа со стороны заказчика",
                    "Доступность ответственных лиц по данным, архитектуре и ИБ",
                ],
                "customer_inputs": [
                    "Ответы на критические блокирующие вопросы",
                    "Подтверждение ограничений и границ этапа",
                ],
                "external_decisions": [
                    "Решения по следующей фазе за пределами текущего этапа",
                ],
                "access_dependencies": [
                    "Доступ к исходным материалам, системам и корпоративным ограничениям",
                ],
                "stop_conditions": [
                    "Отсутствует подтверждение ключевой рамки этапа",
                    "Нет владельца обязательного решения или входа",
                ],
                "confidence": 0.8,
                "blocking_questions": [],
            }
        if artifact_role == "implementation_dependency_plan":
            return {
                "phases": [
                    {
                        "name": "Discovery и согласование рамки",
                        "objectives": ["Зафиксировать цель, границы и ключевые ограничения"],
                        "dependencies": ["Доступность заказчика для согласований"],
                        "outputs": ["Подтверждённая рамка этапа"],
                    },
                    {
                        "name": "Подготовка и ревью ТЗ",
                        "objectives": ["Собрать спецификацию", "Проверить полноту и реализуемость"],
                        "dependencies": ["Согласованные входы от предыдущей фазы"],
                        "outputs": ["Черновик ТЗ", "Ревью-отчёт"],
                    },
                ],
                "critical_dependencies": [
                    "Подтверждение ключевых ограничений бизнеса",
                    "Своевременные ответы по действительно блокирующим вопросам",
                ],
                "project_risks": [
                    "Смешение ожиданий текущего этапа и будущего промышленного контура",
                    "Недостаточная вовлечённость владельцев решения",
                ],
                "proposed_timeline": [
                    "Сначала исследование и фиксация рамки",
                    "Затем сборка ТЗ и ревью",
                ],
                "confidence": 0.79,
                "blocking_questions": [],
            }
        if artifact_role == "predictive_problem_definition":
            return {
                "prediction_target": "Вероятность увольнения сотрудника в заданном горизонте",
                "prediction_horizon": "1-3 месяца до потенциального увольнения",
                "prediction_unit": "Отдельный сотрудник розничной сети",
                "label_definition": "Факт увольнения сотрудника в пределах согласованного горизонта",
                "business_actions": [
                    "Выявлять группы риска и приоритизировать управленческие действия",
                    "Поддерживать принятие решений по целевой бизнес-задаче",
                ],
                "model_outputs": [
                    "Оценка вероятности целевого события",
                    "Сегментация риска",
                    "Ключевые факторы риска в объяснимой форме",
                ],
                "evaluation_metrics": [
                    "ROC-AUC",
                    "Precision/Recall на релевантном cut-off",
                    "Пригодность для бизнес-ранжирования и интервенций",
                ],
                "baseline_expectations": [
                    "Качество выше случайного базового уровня",
                    "Модель полезна для приоритизации действий HR",
                ],
                "explainability_requirements": [
                    "Нужны интерпретируемые факторы риска для HR",
                    "Требуется объяснимость достаточная для принятия управленческих решений",
                ],
                "confidence": 0.77,
                "blocking_questions": [],
            }
        if artifact_role == "data_landscape_assessment":
            return {
                "source_systems": ["Названные в запросе системы-источники", "Источники событий и справочников предметной области"],
                "required_entities": ["Объект прогнозирования", "Организационная структура", "Период", "Целевое событие"],
                "key_features": [
                    "Исторические характеристики объекта",
                    "Операционные и результативные показатели",
                    "Активность, комментарии или другие неструктурированные сигналы",
                    "История изменений по объекту",
                ],
                "data_quality_risks": [
                    "Неидеальное качество справочников",
                    "Разнородность и неполнота данных",
                    "Шум в неструктурированных комментариях",
                ],
                "data_gaps": [
                    "Нужна явная договорённость о логике целевой метки",
                    "Может потребоваться уточнение глубины истории и покрытия источников",
                ],
                "feasibility_assessment": "На уровне PoV задача реализуема при наличии исторических данных достаточной глубины и согласованной логики целевой метки.",
                "privacy_notes": [
                    "Данные могут содержать персональную или чувствительную информацию и требуют защищённого контура либо обезличивания",
                    "Текстовые данные требуют отдельной оценки допустимости использования",
                ],
                "confidence": 0.7,
                "blocking_questions": [],
            }
        if artifact_role == "security_compliance_constraints":
            return {
                "deployment_constraints": [
                    "Решение должно работать в локальном контуре или в защищённом облаке",
                    "Передача и хранение данных должны быть защищены",
                ],
                "privacy_constraints": [
                    "Необходимо шифрование и/или обезличивание данных",
                    "Нужно учитывать ограничения на работу с ПДн сотрудников",
                ],
                "access_control_constraints": [
                    "При масштабировании возможны 2FA и ADFS",
                    "Доступ к результатам должен быть ограничен ролями",
                ],
                "integration_security_constraints": [
                    "Интеграции с корпоративными системами должны соответствовать ИБ-политикам",
                ],
                "allowed_ai_usage": [
                    "Внешние LLM допустимы только при явном разрешении и соблюдении ИБ-ограничений",
                    "При отсутствии такого разрешения требуется закрытый контур или отказ от внешних сервисов",
                ],
                "mandatory_controls": [
                    "Шифрование передачи данных",
                    "Контроль доступа и аудит",
                ],
                "compliance_risks": [
                    "Нарушение требований ИБ может заблокировать масштабирование решения",
                ],
                "confidence": 0.74,
                "blocking_questions": [],
            }
        if artifact_role == "integration_operating_model":
            return {
                "source_integrations": ["Системы-источники, названные в запросе", "Корпоративные данные и сервисы предметной области"],
                "target_integrations": ["Пользовательский интерфейс", "Аналитическая витрина или BI-слой", "Внутренние точки потребления результатов"],
                "refresh_model": "Периодическое обновление, например по API или согласованному пакетному процессу",
                "data_delivery_pattern": [
                    "На PoV допустимы выгрузки или ограниченные API",
                    "Для следующего этапа возможен переход к регулярным интеграциям",
                ],
                "operating_roles": [
                    "HR/C&B как основной потребитель результата",
                    "ИТ как владелец интеграций и инфраструктурной поддержки",
                ],
                "support_model": [
                    "На этапе PoV сопровождение ограничено целями проверки гипотезы",
                    "Промышленная эксплуатация требует отдельной операционной модели",
                ],
                "dependency_risks": [
                    "Сложность согласования API и доступа к системам",
                    "Разная скорость готовности источников данных",
                ],
                "confidence": 0.76,
                "blocking_questions": [],
            }
        if artifact_role == "ui_requirements_outline":
            return {
                "user_roles": ["Основной бизнес-пользователь", "Оператор / аналитик"],
                "user_flows": [
                    {"name": "Просмотр приоритетных зон внимания", "steps": ["Открыть дашборд", "Просмотреть сегменты или объекты", "Провалиться в детали"]},
                    {"name": "Разбор конкретного объекта или сегмента", "steps": ["Выбрать объект", "Посмотреть ключевые факторы", "Зафиксировать выводы"]},
                ],
                "screens": [
                    {"name": "Главный аналитический дашборд", "purpose": "Обзор приоритетных сигналов, сегментов и динамики"},
                    {"name": "Карточка объекта или сегмента", "purpose": "Подробный анализ факторов и рекомендаций"},
                ],
                "analytics_views": [
                    "Сегментация сигналов по релевантным группировкам",
                    "Динамика показателей и сравнение периодов",
                ],
                "decision_support_needs": [
                    "Пояснение ключевых факторов и причин",
                    "Рекомендации по интерпретации сигнала и возможным действиям",
                ],
                "ux_constraints": ["Понятные статусы без технического жаргона", "Достаточная объяснимость для бизнес-пользователя"],
                "confidence": 0.78,
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
        reasoning_payload = {
            "methodology_pack_ref": methodology.ref.as_string(),
            "stages": stages_payload,
            "complexity": complexity,
        }
        artifact_id = str(uuid.uuid4())
        record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest_project_id,
            artifact_role="reasoning_artifact",
            title=f"Reasoning ({task.task_key})",
            description=f"Reasoning artifact задачи {task.task_key} по методологии {methodology.ref.as_string()}",
            artifact_format="json",
            artifact_kind="reasoning",
            created_by_task_id=task.task_id,
            parent_artifact_id=None,
            metadata={
                "methodology_pack_ref": methodology.ref.as_string(),
                "complexity": complexity,
            },
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
        )
        self._runtime.store_artifact(workspace, artifact=record, content=json_dumps(reasoning_payload))
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
        record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest_project_id,
            artifact_role="methodology_trace",
            title=f"Methodology trace ({task.task_key})",
            description=f"Трасса исполнения методологии {methodology.ref.as_string()}",
            artifact_format="json",
            artifact_kind="trace",
            created_by_task_id=task.task_id,
            parent_artifact_id=None,
            metadata={
                "methodology_pack_ref": methodology.ref.as_string(),
                "complexity": complexity,
            },
            storage_path=f"artifacts/{artifact_id}.json",
            created_at=utc_now_iso(),
        )
        self._runtime.store_artifact(workspace, artifact=record, content=json_dumps(trace_payload))
        return artifact_id

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
