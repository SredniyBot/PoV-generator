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
from ..domain.registry import RegistrySnapshot, compose_recipe
from ..infrastructure.openrouter_client import OpenRouterClient
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .artifact_contracts import artifact_schema, render_markdown, schema_instruction
from .context_service import ContextService


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
        template = snapshot.resolve_template(f"{task.template_id}@{task.template_version}")
        context_result = self._context_service.build_for_task(workspace, snapshot, task_id)
        context_manifest = context_result.manifest
        composed_recipe = compose_recipe(snapshot, manifest.recipe_ref, tuple(sorted(state.enabled_domain_packs.keys())))
        current_step = next((step for step in composed_recipe.steps if step.identifier == task.recipe_step_id), None)
        if current_step is None:
            raise ConflictError(f"Шаг '{task.recipe_step_id}' отсутствует в composed recipe.")

        artifact_roles = template.outputs.artifact_roles
        if len(artifact_roles) != 1:
            raise ConflictError(f"Сейчас поддерживается ровно один выходной артефакт на шаблон: {template.ref.as_string()}")
        artifact_role = artifact_roles[0]
        active_provider = provider or os.environ.get("POV_EXECUTION_PROVIDER", "stub")
        active_model = model or os.environ.get("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")

        system_prompt, user_prompt = self._build_prompt(
            template_name=template.name,
            framework_summary=template.framework_summary,
            artifact_role=artifact_role,
            domain_pack_refs=tuple(sorted(state.enabled_domain_packs.keys())),
            current_step_title=current_step.title,
            context_manifest=context_manifest,
        )

        if active_provider == "stub":
            payload = self._execute_stub(
                artifact_role=artifact_role,
                context_manifest=context_manifest,
                business_request=state.business_request,
                goal=state.goal,
                domain_pack_refs=tuple(sorted(state.enabled_domain_packs.keys())),
            )
        elif active_provider == "openrouter":
            payload = OpenRouterClient.from_env().chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=artifact_schema(artifact_role, tuple(sorted(state.enabled_domain_packs.keys()))),
            )
        else:
            raise ConflictError(f"Неподдерживаемый provider: {active_provider}")

        artifact_id = str(uuid.uuid4())
        artifact_record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=manifest.project_id,
            artifact_role=artifact_role,
            title=f"{template.name} ({artifact_role})",
            description=f"Артефакт, созданный задачей {task.recipe_step_id}",
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

        proposed_goal = payload.get("clarified_goal") if artifact_role == "clarification_notes" else None
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
        )
        result = ExecutionResult(
            execution_run_id=request.execution_run_id,
            status="succeeded",
            outputs=(ExecutionOutput(artifact_id=artifact_id, artifact_role=artifact_role),),
            trace_ids=tuple(trace.trace_id for trace in traces),
            proposed_goal=proposed_goal,
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
        frontend_enabled = "frontend.web_app_requirements@1.0.0" in domain_pack_refs
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
        if artifact_role == "ui_requirements_outline":
            return {
                "user_roles": ["Клиент", "Оператор"],
                "user_flows": [
                    {"name": "Подача заявки", "steps": ["Открыть форму", "Заполнить поля", "Отправить заявку"]},
                    {"name": "Проверка статуса", "steps": ["Открыть личный кабинет", "Просмотреть статус"]},
                ],
                "screens": [
                    {"name": "Личный кабинет", "purpose": "Просмотр статусов и истории"},
                    {"name": "Форма заявки", "purpose": "Создание новой заявки"},
                ],
                "ux_constraints": ["Адаптивность под мобильные устройства", "Понятные статусы без технического жаргона"],
            }
        if artifact_role == "requirements_spec":
            clarification = self._find_payload(parsed_inputs, "Уточнение бизнес-цели")
            user_story_map = self._find_payload(parsed_inputs, "Анализ user story")
            alternatives = self._find_payload(parsed_inputs, "Сравнение альтернатив")
            ui_outline = self._find_payload(parsed_inputs, "Анализ пользовательских потоков интерфейса")
            spec = {
                "title": "Техническое задание на подготовку решения",
                "business_goal": (
                    clarification.get("clarified_goal")
                    if isinstance(clarification, dict)
                    else goal or f"Подготовить решение по запросу: {business_request}"
                ),
                "success_criteria": clarification.get("success_criteria", []) if isinstance(clarification, dict) else [],
                "actors": [item["name"] for item in user_story_map.get("actors", [])] if isinstance(user_story_map, dict) else [],
                "user_stories": [
                    f"Как {item['actor']}, я хочу {item['story']}, чтобы {item['value']}"
                    for item in user_story_map.get("user_stories", [])
                ]
                if isinstance(user_story_map, dict)
                else [],
                "functional_requirements": [
                    "Система должна фиксировать исходный бизнес-запрос",
                    "Система должна формировать структурированное ТЗ",
                    "Система должна показывать шаги и причины выбора следующего действия",
                ],
                "non_functional_requirements": [
                    "Результат должен быть воспроизводимым",
                    "Все ключевые шаги должны быть трассируемыми",
                ],
                "assumptions": clarification.get("assumptions", []) if isinstance(clarification, dict) else [],
                "risks": [
                    "Неполные входные данные от бизнеса",
                    "Разное понимание целей у стейкхолдеров",
                ],
                "alternatives_considered": [
                    item["name"] for item in alternatives.get("alternatives", [])
                ]
                if isinstance(alternatives, dict)
                else [],
                "acceptance_criteria": [
                    "Документ содержит цели, требования, ограничения и критерии приёмки",
                    "ТЗ пригодно для передачи в проектирование и реализацию",
                ],
                "open_questions": clarification.get("open_questions", []) if isinstance(clarification, dict) else [],
            }
            if frontend_enabled:
                ui_outline = ui_outline if isinstance(ui_outline, dict) else {}
                spec["frontend_requirements"] = {
                    "user_roles": ui_outline.get("user_roles", []),
                    "user_flows": [flow["name"] for flow in ui_outline.get("user_flows", [])],
                    "screens": [screen["name"] for screen in ui_outline.get("screens", [])],
                    "ux_constraints": ui_outline.get("ux_constraints", []),
                }
            return spec
        if artifact_role == "review_report":
            spec_payload = self._find_payload(parsed_inputs, "Подготовка черновика ТЗ")
            issues = []
            if not isinstance(spec_payload, dict) or not spec_payload.get("functional_requirements"):
                issues.append({"severity": "error", "message": "В ТЗ отсутствуют функциональные требования."})
            if frontend_enabled and (
                not isinstance(spec_payload, dict)
                or "frontend_requirements" not in spec_payload
                or not spec_payload["frontend_requirements"].get("screens")
            ):
                issues.append({"severity": "error", "message": "Для frontend-проекта не заполнен раздел frontend_requirements."})
            status = "passed" if not issues else "needs_changes"
            return {
                "overall_status": status,
                "summary": "Черновик ТЗ можно принимать." if status == "passed" else "Черновик ТЗ требует доработки.",
                "strengths": [
                    "Структура документа выдержана",
                    "Есть связь с целями и user story",
                ],
                "issues": issues,
                "recommendations": (
                    ["Можно переходить к следующему этапу."]
                    if status == "passed"
                    else ["Исправить замечания и повторно провести ревью."]
                ),
            }
        raise ConflictError(f"Stub не умеет генерировать артефакт роли '{artifact_role}'.")

    def _find_payload(self, parsed_inputs: dict[str, object], title_prefix: str) -> dict[str, object]:
        for title, payload in parsed_inputs.items():
            if title.startswith(title_prefix) and isinstance(payload, dict):
                return payload
        return {}
