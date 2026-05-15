from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, utc_now_iso
from ..domain.artifacts import ContextBudget, ContextItem, ContextManifest
from ..domain.positions import Position
from ..domain.project_state import ProjectState
from ..domain.registry import RegistrySnapshot
from ..infrastructure.sqlite_runtime import SqliteRuntime


def _resolve_state_field(state: ProjectState, field_name: str) -> object | None:
    """Получить значение требуемого «поля состояния».

    Транслирует имена полей старого ProblemState в актуальные источники:

    - ``business_request`` — manifest;
    - ``goal`` — формулировка положения ``project.goal`` в Layer A.
    """
    if field_name == "business_request":
        return state.manifest.business_request
    if field_name == "goal":
        return state.knowledge.goal_statement()
    return None


def _clarification_request_id_from_position(position: Position) -> str | None:
    """Извлечь request_id из identifier'а положения, привязанного к уточнению.

    Положения, рождённые координатором уточнений, имеют стабильный префикс
    ``clarification.`` в identifier'е. Это позволяет связать положение
    обратно с источником без хранения дополнительного поля.
    """
    if position.source != "clarification":
        return None
    prefix = "clarification."
    if not position.identifier.startswith(prefix):
        return None
    return position.identifier[len(prefix):]


def estimate_tokens(content: str) -> int:
    return max(1, len(content) // 4)


@dataclass(frozen=True)
class ContextBuildResult:
    manifest: ContextManifest


class ContextService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def build_for_task(self, workspace: Path, snapshot: RegistrySnapshot, task_id: str) -> ContextBuildResult:
        """B4: ContextManifest = три слоя.

        Слой 1 — Project state context (всегда): goal, business_request,
        decisions, assumptions, gaps, known_facts. Без этого LLM не имеет
        доступа к ответам пользователя через clarifications и работает «без
        scope». См. USERS_AND_JTBD §5B C1/C4.

        Слой 2 — Task inputs (по template): required_problem_fields,
        required_artifact_roles, optional_artifact_roles, instruction.

        Слой 3 — Previous attempt context (только при retry): что было в
        прошлой попытке + почему она забракована. Даёт LLM continuity, не
        повторяет ту же ошибку.
        """
        state = self._runtime.load_project_state(workspace)
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)

        items: list[ContextItem] = []
        source_refs: list[str] = []

        # Слой 1 — Project state (всегда первым, priority=1500 — выше
        # инструкции, чтобы LLM видел контекст ДО постановки задачи).
        project_state_item = self._build_project_state_section(
            workspace, task, template, state
        )
        if project_state_item is not None:
            items.append(project_state_item)
            source_refs.append(project_state_item.source_ref)

        # Слой 3 — Previous attempt (если retry). Делаем рано, чтобы LLM
        # сразу видел «прошлая попытка не вышла потому что …», прежде чем
        # читать те же входы заново.
        previous_attempt_item = self._build_previous_attempt_section(workspace, task)
        if previous_attempt_item is not None:
            items.append(previous_attempt_item)
            source_refs.append(previous_attempt_item.source_ref)

        # Слой 2 — Task inputs (template-declared, как раньше).
        for field_name in template.inputs.required_problem_fields:
            value = _resolve_state_field(state, field_name)
            if value in (None, ""):
                raise ConflictError(
                    f"Для задачи '{task.task_id}' отсутствует обязательное "
                    f"поле состояния '{field_name}'."
                )
            content = json_dumps(value) if isinstance(value, (dict, list, tuple)) else str(value)
            item = ContextItem(
                item_id=str(uuid.uuid4()),
                item_type="problem_field",
                source_ref=f"knowledge:{state.knowledge.version}:{field_name}",
                title=f"State.{field_name}",
                content=content,
                token_estimate=estimate_tokens(content),
                required=True,
                priority=100,
            )
            items.append(item)
            source_refs.append(item.source_ref)

        required_artifact_roles = template.inputs.required_artifact_roles
        optional_artifact_roles = tuple(
            role for role in template.inputs.optional_artifact_roles if role not in required_artifact_roles
        )

        # Этап 7.3: декларативный auto-collect. Когда шаблон поднял флаг
        # `collect_optional_from_active_domain_packs`, добавляем в optional
        # все артефакты, созданные задачами активных доменных паков
        # (origin_kind == "domain_contribution"). Это снимает hand-coded
        # список optional из финальной merge-задачи: новый домен попадает
        # в контекст автоматически.
        if template.inputs.collect_optional_from_active_domain_packs:
            existing = set(required_artifact_roles) | set(optional_artifact_roles)
            for role in self._collect_domain_contribution_roles(workspace):
                if role not in existing:
                    optional_artifact_roles = (*optional_artifact_roles, role)
                    existing.add(role)

        if not required_artifact_roles and not optional_artifact_roles:
            optional_artifact_roles = tuple(sorted({artifact.artifact_role for artifact in self._runtime.list_artifacts(workspace)}))

        for artifact_role in required_artifact_roles:
            artifact = self._runtime.latest_artifact_by_role(workspace, artifact_role)
            if artifact is None:
                raise ConflictError(
                    f"Для задачи '{task.task_id}' отсутствует обязательный входной артефакт роли '{artifact_role}'."
                )
            self._append_artifact_item(workspace, items, source_refs, artifact, required=True)

        # Краткое описание задачи (R8/TS9: методологическая часть приходит
        # из methodology_pack wrapper'а, здесь — только task-specific guidance).
        if template.summary:
            instruction = ContextItem(
                item_id=str(uuid.uuid4()),
                item_type="instruction",
                source_ref=f"template:{template.ref.as_string()}",
                title="Что должна сделать задача",
                content=template.summary,
                token_estimate=estimate_tokens(template.summary),
                required=True,
                priority=1000,
            )
            items.append(instruction)
            source_refs.append(instruction.source_ref)

        max_tokens = self._effective_max_tokens(template.context_policy.max_tokens)

        for artifact_role in optional_artifact_roles:
            artifact = self._runtime.latest_artifact_by_role(workspace, artifact_role)
            if artifact is None:
                continue
            candidate_item = self._make_artifact_item(workspace, artifact, required=False)
            if max_tokens is not None and (
                sum(item.token_estimate for item in items) + candidate_item.token_estimate > max_tokens
            ):
                continue
            items.append(candidate_item)
            source_refs.append(candidate_item.source_ref)

        used_tokens = sum(item.token_estimate for item in items)
        if max_tokens is not None and used_tokens > max_tokens:
            raise ConflictError(
                f"Контекст задачи '{task.task_id}' не помещается в budget: {used_tokens} > {max_tokens}."
            )

        fingerprint = sha256("|".join(sorted(source_refs)).encode("utf-8")).hexdigest()
        manifest_max_tokens = max_tokens if max_tokens is not None else 1_048_576
        context_manifest = ContextManifest(
            manifest_id=str(uuid.uuid4()),
            project_id=state.manifest.project_id,
            task_id=task.task_id,
            template_ref=template.ref.as_string(),
            problem_state_version=state.knowledge.version,
            budget=ContextBudget(
                max_input_tokens=manifest_max_tokens,
                reserved_for_output=min(1200, manifest_max_tokens // 2),
                used_tokens=used_tokens,
            ),
            items=tuple(items),
            excluded_items=(),
            input_fingerprint=fingerprint,
            created_at=utc_now_iso(),
            used_position_ids=self.collect_used_position_ids(state),
        )
        self._runtime.record_context_manifest(workspace, context_manifest)
        return ContextBuildResult(manifest=context_manifest)

    def _effective_max_tokens(self, template_max_tokens: int) -> int | None:
        raw_disable = os.environ.get("POV_DISABLE_TEMPLATE_CONTEXT_BUDGET", "").strip().lower()
        if raw_disable in {"1", "true", "yes", "on"}:
            return None

        raw_override = os.environ.get("POV_TEMPLATE_CONTEXT_MAX_TOKENS", "").strip()
        if raw_override:
            try:
                override = int(raw_override)
            except ValueError:
                override = template_max_tokens
            else:
                if override <= 0:
                    return None
                return override

        return template_max_tokens

    def _append_artifact_item(
        self,
        workspace: Path,
        items: list[ContextItem],
        source_refs: list[str],
        artifact,
        *,
        required: bool,
    ) -> None:
        item = self._make_artifact_item(workspace, artifact, required=required)
        items.append(item)
        source_refs.append(item.source_ref)

    def _make_artifact_item(self, workspace: Path, artifact, *, required: bool) -> ContextItem:
        content = self._runtime.load_artifact_content(workspace, artifact.artifact_id)
        return ContextItem(
            item_id=str(uuid.uuid4()),
            item_type="artifact",
            source_ref=f"artifact:{artifact.artifact_id}",
            title=artifact.title,
            content=content,
            token_estimate=estimate_tokens(content),
            required=required,
            priority=80,
        )

    # ------------------------------------------------------------------
    # B4: Project state context (всегда добавляется к prompt)
    # ------------------------------------------------------------------

    # Внутри одной задачи decisions/assumptions делятся на «relevant к этой
    # задаче» (полный текст) и «глобальные» (компактный список). Это даёт
    # фокус на нужном без потери видимости общего.
    _RELEVANT_FULL_LIMIT: int = 12   # сколько relevant items с полным телом
    _GLOBAL_LIST_LIMIT: int = 20     # сколько global items в коротком списке
    # B5: жёсткий cap на размер project_state section (в estimated tokens).
    # Если контекст разрастается — relevant items сохраняются полностью,
    # global секции обрезаются. Это защита от outsize project state,
    # который мог бы вытолкнуть upstream артефакты из budget.
    _PROJECT_STATE_TOKEN_HARD_CAP: int = 2000

    def _collect_domain_contribution_roles(self, workspace: Path) -> tuple[str, ...]:
        """Роли артефактов, рождённых задачами активных доменных паков.

        Этап 7.3: пробегаем граф задач workspace, отбираем те, у которых
        ``origin_kind == "domain_contribution"`` (созданы при раскрытии
        слотов доменных паков). Их primary-артефакты — кандидаты для
        автоматического подмешивания в optional-контекст финального merge.
        """
        domain_task_ids: set[str] = set()
        for task in self._runtime.list_tasks(workspace):
            if task.origin_kind == "domain_contribution":
                domain_task_ids.add(task.task_id)
        if not domain_task_ids:
            return ()
        roles: list[str] = []
        seen: set[str] = set()
        for artifact in self._runtime.list_artifacts(workspace):
            if artifact.created_by_task_id not in domain_task_ids:
                continue
            if artifact.artifact_role in seen:
                continue
            seen.add(artifact.artifact_role)
            roles.append(artifact.artifact_role)
        return tuple(sorted(roles))

    def collect_used_position_ids(self, state: ProjectState) -> tuple[str, ...]:
        """Идентификаторы положений Layer A, которые попали в контекст задачи.

        Этап 1.4: пока подаём все активные положения, реально попавшие
        в ``_build_project_state_section`` (decisions/assumptions/facts).
        Полноценная фильтрация по релевантности — Этап 2 roadmap.
        """
        ids: list[str] = []
        for position in state.knowledge.active():
            if position.type in {"decision", "assumption", "fact"}:
                ids.append(position.identifier)
        return tuple(ids)

    def _build_project_state_section(
        self,
        workspace: Path,
        task,
        template,
        state: ProjectState,
    ) -> ContextItem | None:
        """Собирает компактный markdown-снимок «что мы уже знаем о проекте».

        Зачем: задача не должна повторно изобретать колесо или
        противоречить уже принятым решениям/допущениям. Без этой секции
        LLM работает «без scope»: видит только входные артефакты, но не
        знает что пользователь уже ответил на вопросы.

        Источники теперь — Layer A (knowledge) и Layer B (process).
        """
        # B5: контекст с явной иерархией источников через visual markers.
        # См. system_prompt — он опирается на эти маркеры для приоритезации.
        sections: list[str] = []

        # 🎯 Goal (главное что задача держит в голове)
        goal_statement = state.knowledge.goal_statement()
        if goal_statement:
            sections.append("## 🎯 Цель проекта\n" + goal_statement.strip())

        # 📥 Business request (исходный raw input)
        business_request = (state.manifest.business_request or "").strip()
        if business_request:
            if len(business_request) > 1500:
                business_request = business_request[:1500].rstrip() + " …"
            sections.append(
                "## 📥 Исходный бизнес-запрос (база — отсюда выведены остальные знания)\n"
                + business_request
            )

        # 🟢 USER DECISIONS — обязательные ограничения.
        #
        # always_full=True: решения пользователя ВСЕГДА показываются полным
        # текстом, даже в global-секции. Раньше global-список обрезал
        # statement до 120 символов; для решений вида
        # «<длинный вопрос>? Ответ: <короткий ответ>» это резало именно
        # часть «Ответ: …», и LLM видел только вопрос без ответа. Из-за
        # этого модель эмитила blocking_questions с пометкой «Решение
        # пользователя не найдено в контексте», что плодило дубликаты
        # уточнений и блокировало pipeline.
        decisions_section = self._format_positions_section(
            workspace=workspace,
            task=task,
            template=template,
            positions=state.knowledge.by_type("decision"),
            relevant_title="🟢 Решения пользователя — ОБЯЗАТЕЛЬНО учитывай в выводе",
            global_title="🟢 Другие принятые решения проекта (тоже учитывай)",
            always_full=True,
        )
        if decisions_section:
            sections.append(decisions_section)

        # 🟡 ASSUMPTIONS — рабочие, можно override decision'ом
        assumptions_section = self._format_positions_section(
            workspace=workspace,
            task=task,
            template=template,
            positions=state.knowledge.by_type("assumption"),
            relevant_title="🟡 Допущения системы для этой задачи (можно использовать, decision важнее)",
            global_title="🟡 Другие активные допущения проекта",
        )
        if assumptions_section:
            sections.append(assumptions_section)

        # ⚫ Open gaps — НЕ утверждать
        gaps_lines = [
            f"- **{gap.title}** ({gap.severity}): {gap.description}".rstrip()
            for gap in state.process.active_gaps.values()
            if gap.closed_at is None
        ]
        if gaps_lines:
            sections.append(
                "## ⚫ Открытые пробелы — НЕ утверждай ничего в этих областях\n"
                "_Если задача требует ответа в этих областях — снижай confidence "
                "или добавь конкретный вопрос в blocking_questions._\n\n"
                + "\n".join(gaps_lines[: self._GLOBAL_LIST_LIMIT])
            )

        # 🔵 Known facts — extracted база (исключая бизнес-запрос и цель,
        # они уже выведены в отдельные секции выше).
        from ..domain.project_knowledge import GOAL_POSITION_ID

        reserved_fact_ids = {GOAL_POSITION_ID, "project.business_request"}
        facts_lines = [
            f"- {position.statement}".rstrip()
            for position in state.knowledge.by_type("fact")
            if position.statement and position.identifier not in reserved_fact_ids
        ]
        if facts_lines:
            sections.append(
                "## 🔵 Известные факты (извлечены из бизнес-запроса)\n"
                + "\n".join(facts_lines[: self._GLOBAL_LIST_LIMIT])
            )

        # Active methodology (лензa рассуждения)
        methodology_records = state.process.active_methodology_pack_records
        if methodology_records:
            method_ref = next(iter(methodology_records.values())).ref
            sections.append(f"## 📐 Активная методология рассуждения\n{method_ref}")

        if not sections:
            return None

        intro = (
            "# Контекст проекта\n\n"
            "_Это общее знание о проекте, накопленное к моменту запуска задачи. "
            "Источники маркированы значками — соблюдай иерархию приоритетов "
            "(подробности в system-инструкции):_\n"
            "- 🟢 = решения пользователя (обязательные ограничения)\n"
            "- 🟡 = допущения системы (можно override решением)\n"
            "- 🔵 = факты из запроса\n"
            "- ⚫ = пробелы (не утверждай)\n"
        )
        content = intro + "\n\n".join(sections)
        # B5: жёсткий cap. Если состояние проекта раздулось — truncate
        # по символам (с явной пометкой), сохраняя intro и начало.
        token_estimate = estimate_tokens(content)
        if token_estimate > self._PROJECT_STATE_TOKEN_HARD_CAP:
            # 1 token ≈ 4 char (см. estimate_tokens) → допустимо ~8000 char
            max_chars = self._PROJECT_STATE_TOKEN_HARD_CAP * 4
            content = (
                content[:max_chars].rstrip()
                + "\n\n_… [контекст проекта обрезан для соблюдения token budget — "
                "показаны самые приоритетные секции]_"
            )
            token_estimate = estimate_tokens(content)
        return ContextItem(
            item_id=str(uuid.uuid4()),
            item_type="problem_field",
            source_ref=f"project_state:k{state.knowledge.version}/p{state.process.version}",
            title="Контекст проекта",
            content=content,
            token_estimate=token_estimate,
            required=True,
            priority=1500,
        )

    def _format_positions_section(
        self,
        *,
        workspace: Path,
        task,
        template,
        positions: Iterable[Position],
        relevant_title: str,
        global_title: str,
        always_full: bool = False,
    ) -> str | None:
        """Разбивает положения на relevant и global, форматирует markdown.

        Relevant — положения, релевантные конкретной задаче:
        - пришли из clarification, чей affected_task_ids содержит task.task_id, ИЛИ
        - related_artifact_ids уточнения пересекаются с required/optional ролями задачи.

        Остальные — global, выводятся компактным списком.

        ``always_full``: даже в global-секции показывать положения целиком.
        Используется для decisions: их обрезка приводила к тому, что LLM
        видел вопрос без ответа («Ответ: Ма…») и переспрашивал.
        """
        positions_list = [p for p in positions if (p.statement or "").strip()]
        if not positions_list:
            return None

        template_roles = set(template.inputs.required_artifact_roles) | set(
            template.inputs.optional_artifact_roles
        )
        relevant: list[Position] = []
        global_items: list[Position] = []
        for position in positions_list:
            if self._is_position_relevant_to_task(
                workspace, position, task, template_roles
            ):
                relevant.append(position)
            else:
                global_items.append(position)

        chunks: list[str] = []
        if relevant:
            relevant_lines = [
                self._format_position_line(position, full=True)
                for position in relevant[: self._RELEVANT_FULL_LIMIT]
            ]
            chunks.append(f"## {relevant_title}\n" + "\n".join(relevant_lines))
        if global_items:
            head = global_items[: self._GLOBAL_LIST_LIMIT]
            remaining = len(global_items) - len(head)
            global_lines = [
                self._format_position_line(position, full=always_full)
                for position in head
            ]
            if remaining > 0:
                global_lines.append(f"- … и ещё {remaining}")
            chunks.append(f"## {global_title}\n" + "\n".join(global_lines))
        return "\n\n".join(chunks) if chunks else None

    @staticmethod
    def _format_position_line(position: Position, *, full: bool) -> str:
        """Форматирует одну строку положения для markdown-секции."""
        statement = (position.statement or "").strip()
        if not statement:
            return ""
        source_label = ""
        if position.source == "clarification":
            source_label = " _(ответ на вопрос пользователя)_"
        elif position.source and position.source != "system":
            source_label = f" _(источник: {position.source})_"
        if full:
            return f"- {statement.rstrip()}.{source_label}".replace("..", ".")
        compact = statement if len(statement) <= 120 else statement[:117] + "…"
        return f"- {compact}".rstrip()

    def _is_position_relevant_to_task(
        self,
        workspace: Path,
        position: Position,
        task,
        template_roles: set[str],
    ) -> bool:
        """Положение считается relevant к task если:
        - источник — clarification, чей affected_task_ids содержит task.task_id, ИЛИ
        - related_artifact_ids этого clarification пересекаются с ролями задачи.

        Для прочих источников — relevant по умолчанию, чтобы случайно не
        отфильтровать важное.
        """
        request_id = _clarification_request_id_from_position(position)
        if request_id is None:
            return True
        try:
            request = self._runtime.get_clarification_request(workspace, request_id)
        except Exception:
            return True
        if task.task_id in (request.affected_task_ids or ()):
            return True
        if template_roles and set(request.related_artifact_ids or ()) & template_roles:
            return True
        return False

    # ------------------------------------------------------------------
    # B4: Previous attempt context (для retry задач)
    # ------------------------------------------------------------------

    def _build_previous_attempt_section(
        self, workspace: Path, task
    ) -> ContextItem | None:
        """Если задача исполняется повторно — добавляем секцию о прошлой
        попытке: artifact + validation findings. Это даёт LLM continuity
        и помогает не повторять ту же ошибку.
        """
        attempt = getattr(task, "attempt", 0) or 0
        if attempt <= 1:
            # Первая попытка либо attempt не отслеживается — нечего показывать.
            return None
        # Ищем последний primary артефакт, созданный этой задачей.
        previous_artifact = None
        try:
            all_artifacts = self._runtime.list_artifacts(workspace)
        except Exception:
            return None
        for art in sorted(
            (a for a in all_artifacts if a.created_by_task_id == task.task_id and a.artifact_kind == "primary"),
            key=lambda a: a.created_at or "",
            reverse=True,
        ):
            previous_artifact = art
            break
        if previous_artifact is None:
            return None
        # Загружаем содержимое и validation findings последнего validation_run.
        previous_content = ""
        try:
            previous_content = self._runtime.load_artifact_content(
                workspace, previous_artifact.artifact_id
            )
        except Exception:
            previous_content = ""
        validation_summary = self._collect_previous_validation_findings(
            workspace, task
        )
        if not previous_content and not validation_summary:
            return None
        sections = [
            "# Прошлая попытка этой задачи",
            "",
            f"_Это попытка №{attempt}. Ниже — что было сделано в прошлый раз и почему это не приняли._",
            "",
        ]
        if validation_summary:
            sections.append("## Что не приняли в прошлый раз")
            sections.append(validation_summary)
            sections.append("")
        if previous_content:
            # Компактный preview, чтобы не дублировать всё тело — пусть LLM
            # видит что было, и сделает иначе.
            preview = previous_content
            if len(preview) > 1500:
                preview = preview[:1500].rstrip() + "\n…"
            sections.append(f"## Прошлый результат ({previous_artifact.artifact_role})")
            sections.append("```json")
            sections.append(preview)
            sections.append("```")
        content = "\n".join(sections)
        return ContextItem(
            item_id=str(uuid.uuid4()),
            item_type="instruction",
            source_ref=f"previous_attempt:{task.task_id}:{attempt}",
            title="Прошлая попытка",
            content=content,
            token_estimate=estimate_tokens(content),
            required=True,
            priority=1400,
        )

    def _collect_previous_validation_findings(self, workspace: Path, task) -> str:
        try:
            runs = self._runtime.list_validation_runs(workspace)
        except Exception:
            return ""
        # Берём последний validation_run по этой задаче.
        task_runs = [r for r in runs if getattr(r, "task_id", None) == task.task_id]
        if not task_runs:
            return ""
        latest = max(task_runs, key=lambda r: getattr(r, "created_at", "") or "")
        findings = getattr(latest, "findings", ()) or ()
        if not findings:
            return ""
        lines = [
            f"- **{getattr(f, 'finding_type', 'finding')}** "
            f"({getattr(f, 'severity', 'info')}): {getattr(f, 'message', '')}"
            for f in findings
        ]
        return "\n".join(lines)
