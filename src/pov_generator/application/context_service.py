from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..common.errors import ConflictError
from ..common.serialization import json_dumps, json_loads, utc_now_iso
from ..domain.artifacts import ContextBudget, ContextItem, ContextManifest
from ..domain.positions import REQUISITE_POSITION_PREFIX, Position
from ..domain.project_state import ProjectState
from ..domain.registry import RegistrySnapshot
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .attachment_service import ATTACHMENT_POSITION_PREFIX
from .context_assembly import (
    ContextAuthority,
    ContextCandidate,
    effective_input_budget,
    pack_context,
)


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


def estimate_tokens(content: str) -> int:
    """Грубая оценка числа токенов по тексту.

    BPE-токенизаторы дробят не-ASCII (кириллицу) мельче латиницы: латиница —
    ~4 символа на токен, кириллица/прочий не-ASCII — ~2. Считаем раздельно;
    иначе кириллический контекст недооценивался бы и мог переполнить бюджет
    (оценка — единственный гейт перед жёсткой проверкой бюджета, поэтому лучше
    слегка переоценить, чем недооценить).
    """
    if not content:
        return 1
    ascii_chars = sum(1 for ch in content if ord(ch) < 128)
    other_chars = len(content) - ascii_chars
    return max(1, ascii_chars // 4 + (other_chars + 1) // 2)


@dataclass(frozen=True)
class ContextBuildResult:
    manifest: ContextManifest


class ContextService:
    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    def build_for_task(
        self,
        workspace: Path,
        snapshot: RegistrySnapshot,
        task_id: str,
        *,
        model_context_window: int | None = None,
        input_budget_ceiling: int | None = None,
    ) -> ContextBuildResult:
        """Собрать контекст задачи.

        Принцип сборки (см. ``context_assembly``): источник истины — прямой вход
        заказчика и подтверждённое человеком — и структурно обязательное не
        выкидываются молча; производное укладывается в бюджет по авторитету, а
        выкинутое фиксируется в ``excluded_items``. Первоисточник (вложения)
        подаётся ТОЛЬКО задачам-интерпретаторам, объявившим ``requires.inputs``,
        а не всем подряд.

        ``model_context_window`` — окно активной модели (токены), потолок
        бюджета входа; ``None`` — для не-LLM путей (stub) и тестов.
        ``input_budget_ceiling`` — дополнительный жёсткий потолок входа (напр.
        для провайдера с лимитом окна, где объём токенов выжигает 5-часовое
        окно): срезает лишь ПРОИЗВОДНЫЙ контекст, обязательное не теряется.
        """
        state = self._runtime.load_project_state(workspace)
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)

        candidates: list[ContextCandidate] = []

        def add(item: ContextItem, authority: ContextAuthority, *, pinned: bool) -> None:
            candidates.append(
                ContextCandidate(item, authority, pinned=pinned, display_order=len(candidates))
            )

        # 1. Источник истины — прямой вход заказчика (вложения). Только задачам,
        #    объявившим requires.inputs. Неприкосновенно (pinned, CUSTOMER_INPUT).
        for source_item in self._collect_source_inputs(template, state):
            add(source_item, ContextAuthority.CUSTOMER_INPUT, pinned=True)

        # 2. Прошлая попытка (retry) — производное, можно выкинуть под бюджет.
        previous_attempt_item = self._build_previous_attempt_section(workspace, task)
        if previous_attempt_item is not None:
            add(previous_attempt_item, ContextAuthority.DERIVED, pinned=False)

        # 3. «Что мы уже знаем» — производная сводка (без эха запроса и без
        #    вложений: они подаются источником выше). Можно выкинуть под бюджет.
        project_state_item = self._build_project_state_section(workspace, task, template, state)
        if project_state_item is not None:
            add(project_state_item, ContextAuthority.DERIVED, pinned=False)

        # 4. Обязательные поля состояния (вкл. печатный бизнес-запрос). Pinned.
        for field_name in template.inputs.required_problem_fields:
            value = _resolve_state_field(state, field_name)
            if value in (None, ""):
                raise ConflictError(
                    f"Для задачи '{task.task_id}' отсутствует обязательное "
                    f"поле состояния '{field_name}'."
                )
            content = json_dumps(value) if isinstance(value, (dict, list, tuple)) else str(value)
            field_authority = (
                ContextAuthority.CUSTOMER_INPUT
                if field_name == "business_request"
                else ContextAuthority.REQUIRED
            )
            add(
                ContextItem(
                    item_id=str(uuid.uuid4()),
                    item_type="problem_field",
                    source_ref=f"knowledge:{state.knowledge.version}:{field_name}",
                    title=f"State.{field_name}",
                    content=content,
                    token_estimate=estimate_tokens(content),
                    required=True,
                    priority=100,
                ),
                field_authority,
                pinned=True,
            )

        required_artifact_roles = template.inputs.required_artifact_roles
        optional_artifact_roles = tuple(
            role for role in template.inputs.optional_artifact_roles if role not in required_artifact_roles
        )

        # Этап 7.3: декларативный auto-collect — новый домен попадает в контекст
        # финальной merge-задачи автоматически (origin_kind == domain_contribution).
        if template.inputs.collect_optional_from_active_domain_packs:
            existing = set(required_artifact_roles) | set(optional_artifact_roles)
            for role in self._collect_domain_contribution_roles(workspace):
                if role not in existing:
                    optional_artifact_roles = (*optional_artifact_roles, role)
                    existing.add(role)

        if not required_artifact_roles and not optional_artifact_roles:
            optional_artifact_roles = tuple(
                sorted({artifact.artifact_role for artifact in self._runtime.list_artifacts(workspace)})
            )

        # 5. Обязательные артефакты — структурно необходимы (pinned, REQUIRED).
        for artifact_role in required_artifact_roles:
            artifact = self._runtime.latest_artifact_by_role(workspace, artifact_role)
            if artifact is None:
                raise ConflictError(
                    f"Для задачи '{task.task_id}' отсутствует обязательный входной артефакт роли '{artifact_role}'."
                )
            add(self._make_artifact_item(workspace, artifact, required=True), ContextAuthority.REQUIRED, pinned=True)

        # 5b. Фокус веера: если это fan_out_instance, кладём «свой» элемент из
        # источника (например, конкретный компонент модели). Без этого ребёнок
        # веера не знает, ЗА КАКОЙ элемент он отвечает. Pinned, REQUIRED.
        focus_item = self._fanout_focus_item(workspace, snapshot, task)
        if focus_item is not None:
            add(focus_item, ContextAuthority.REQUIRED, pinned=True)

        # 6. Инструкция задачи — pinned (без неё задача не знает, что делать).
        if template.summary:
            add(
                ContextItem(
                    item_id=str(uuid.uuid4()),
                    item_type="instruction",
                    source_ref=f"template:{template.ref.as_string()}",
                    title="Что должна сделать задача",
                    content=template.summary,
                    token_estimate=estimate_tokens(template.summary),
                    required=True,
                    priority=1000,
                ),
                ContextAuthority.REQUIRED,
                pinned=True,
            )

        # 7. Опциональные артефакты — производное; подтверждённые человеком
        #    (user_verified) приоритетнее неподтверждённых при нехватке бюджета.
        for artifact_role in optional_artifact_roles:
            artifact = self._runtime.latest_artifact_by_role(workspace, artifact_role)
            if artifact is None:
                continue
            authority = (
                ContextAuthority.CONFIRMED
                if getattr(artifact, "user_verified", False)
                else ContextAuthority.DERIVED
            )
            add(self._make_artifact_item(workspace, artifact, required=False), authority, pinned=False)

        # 8. Бюджет: окно модели (потолок) ∩ намерение шаблона ∩ жёсткий потолок.
        template_intent = self._effective_max_tokens(template.context_policy.max_tokens)
        budget = effective_input_budget(
            template_intent, model_context_window, input_budget_ceiling
        )

        # 9. Укладка: pinned всегда внутри; производное — по авторитету до бюджета.
        packed = pack_context(candidates, budget)
        if packed.over_budget:
            # Обязательное + источник истины сами по себе не влезли. Не режем их
            # молча — падаем громко (оператор поднимет бюджет/окно или сократит
            # вход). Верное сжатие источника — будущая точка расширения.
            raise ConflictError(
                f"Обязательный контекст задачи '{task.task_id}' не помещается в "
                f"бюджет: {packed.used_tokens} > {budget}."
            )

        items = list(packed.items)
        source_refs = [item.source_ref for item in items]
        fingerprint = sha256("|".join(sorted(source_refs)).encode("utf-8")).hexdigest()
        manifest_max_tokens = budget if budget is not None else 1_048_576
        context_manifest = ContextManifest(
            manifest_id=str(uuid.uuid4()),
            project_id=state.manifest.project_id,
            task_id=task.task_id,
            template_ref=template.ref.as_string(),
            problem_state_version=state.knowledge.version,
            budget=ContextBudget(
                max_input_tokens=manifest_max_tokens,
                reserved_for_output=min(1200, manifest_max_tokens // 2),
                used_tokens=packed.used_tokens,
            ),
            items=tuple(items),
            excluded_items=packed.excluded,
            input_fingerprint=fingerprint,
            created_at=utc_now_iso(),
            used_position_ids=self.collect_used_position_ids(state),
        )
        self._runtime.record_context_manifest(workspace, context_manifest)
        prompt_text = "\n".join(item.content for item in items)
        self._mark_used_attachments(
            workspace, state, context_manifest.used_position_ids, prompt_text
        )
        return ContextBuildResult(manifest=context_manifest)

    def _collect_source_inputs(self, template, state: ProjectState) -> list[ContextItem]:
        """Первоисточник для задачи-интерпретатора: полный текст входных файлов.

        Подаётся ТОЛЬКО задачам, объявившим соответствующий род в
        ``requires.inputs`` — а не всем подряд. Вложения берутся из положений
        Layer A (``attachment.<id>``) целиком, без обрезки.
        """
        items: list[ContextItem] = []
        wanted = set(template.inputs.raw_inputs)
        if "attachments" in wanted:
            for position in state.knowledge.active():
                if position.type != "fact" or not position.identifier.startswith(
                    ATTACHMENT_POSITION_PREFIX
                ):
                    continue
                text = (position.statement or "").strip()
                if not text:
                    continue
                items.append(
                    ContextItem(
                        item_id=str(uuid.uuid4()),
                        item_type="problem_field",
                        source_ref=f"source:{position.identifier}",
                        title="Входной файл заказчика",
                        content=text,
                        token_estimate=estimate_tokens(text),
                        required=True,
                        priority=200,
                    )
                )
        return items

    def _mark_used_attachments(
        self,
        workspace: Path,
        state: ProjectState,
        used_position_ids: tuple[str, ...],
        prompt_text: str,
    ) -> None:
        """Пометить вложения, чей текст РЕАЛЬНО вошёл в промпт задачи.

        После пометки вложение нельзя удалить (ради воспроизводимости артефакта),
        поэтому маркируем строго: только если полный текст положения
        ``attachment.<id>`` присутствует в собранном контексте. Если бюджет усёк
        текст вложения (или секция состояния не попала в промпт) — НЕ маркируем:
        артефакт этого текста не видел, и запрет удаления был бы ложным.
        """
        from .attachment_service import attachment_id_from_position_id

        statements_by_id = {
            position.identifier: (position.statement or "")
            for position in state.knowledge.active()
        }
        for position_id in used_position_ids:
            attachment_id = attachment_id_from_position_id(position_id)
            if attachment_id is None:
                continue
            statement = statements_by_id.get(position_id, "").strip()
            if statement and statement in prompt_text:
                self._runtime.mark_attachment_used(workspace, attachment_id)

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

    def _fanout_focus_item(
        self, workspace: Path, snapshot: RegistrySnapshot, task
    ) -> ContextItem | None:
        """«Свой» элемент для ребёнка веера (fan_out_instance).

        Берёт fan_out_spec родителя, находит в источнике элемент с
        ``key_field == task.origin_ref`` и кладёт его как закреплённый фокус.
        Без этого per-component задача не знает, за какой компонент отвечает.
        Защитно: любая неувязка → None (контекст собирается без фокуса).
        """
        if getattr(task, "origin_kind", None) != "fan_out_instance" or not task.parent_task_id:
            return None
        try:
            parent = self._runtime.get_task(workspace, task.parent_task_id)
            parent_template = snapshot.resolve_template(parent.template_ref)
        except Exception:
            return None
        spec = parent_template.fan_out_spec
        if spec is None:
            return None
        source = self._runtime.latest_artifact_by_role(workspace, spec.artifact_role)
        if source is None:
            return None
        try:
            content = json_loads(self._runtime.load_artifact_content(workspace, source.artifact_id))
        except Exception:
            return None
        array: object = content
        for part in spec.array_path.split("."):
            array = array.get(part, []) if isinstance(array, dict) else []
        if not isinstance(array, list):
            return None
        item = next(
            (
                entry
                for entry in array
                if isinstance(entry, dict) and str(entry.get(spec.key_field)) == task.origin_ref
            ),
            None,
        )
        if item is None:
            return None
        body = json_dumps(item)
        return ContextItem(
            item_id=str(uuid.uuid4()),
            item_type="fanout_focus",
            source_ref=f"fanout:{spec.artifact_role}:{task.origin_ref}",
            title="Целевой элемент (фокус задачи)",
            content=body,
            token_estimate=estimate_tokens(body),
            required=True,
            priority=100,
        )

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

    # Cap на число положений (допущений/фактов) в секции состояния. Лишние
    # сворачиваются в «… и ещё N».
    _GLOBAL_LIST_LIMIT: int = 20

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
            if position.type in {"assumption", "fact"}:
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

        # Печатный бизнес-запрос здесь НЕ дублируем: он подаётся отдельным
        # обязательным полем целиком (а вложения — источником, см.
        # _collect_source_inputs). Раньше тут была урезанная до 1500 симв. копия —
        # убрали дубль (один каноничный источник вместо двух).

        # Decisions are no longer read from Layer A here. The canonical source
        # is the decisions ledger; ExecutionService appends compact ledger
        # constraints after the generic ContextManifest is built.

        # 🟡 ASSUMPTIONS — рабочие, можно override decision'ом
        assumptions_section = self._format_positions_section(
            positions=state.knowledge.by_type("assumption"),
            title="🟡 Активные допущения системы (можно использовать; решение важнее)",
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
                "или явно отметь пробел как assumption / open_question в "
                "соответствующем разделе артефакта._\n\n"
                + "\n".join(gaps_lines[: self._GLOBAL_LIST_LIMIT])
            )

        # 🟢 Данные, предоставленные пользователем по запросу (реквизиты).
        # Прямой вход заказчика — отделён от извлечённых фактов и от
        # бизнес-запроса. Секреты сюда не попадают (credential/reference не
        # создают value-положение, см. provide_requisite).
        provided_lines = [
            f"- {position.statement}".rstrip()
            for position in state.knowledge.by_type("fact")
            if position.statement
            and position.identifier.startswith(REQUISITE_POSITION_PREFIX)
        ]
        if provided_lines:
            sections.append(
                "## 🟢 Данные, предоставленные пользователем по запросу (реквизиты)\n"
                + "\n".join(provided_lines[: self._GLOBAL_LIST_LIMIT])
            )

        # 🔵 Known facts — extracted база (исключая бизнес-запрос и цель,
        # они уже выведены в отдельные секции выше).
        from ..domain.project_knowledge import GOAL_POSITION_ID

        reserved_fact_ids = {GOAL_POSITION_ID, "project.business_request"}
        # Факты-вложения (attachment.*) и предоставленные реквизиты
        # (requisite.*) сюда НЕ попадают: первые подаются первоисточником
        # задачам-интерпретаторам (см. _collect_source_inputs), вторые выведены
        # отдельной секцией выше.
        facts_lines = [
            f"- {position.statement}".rstrip()
            for position in state.knowledge.by_type("fact")
            if position.statement
            and position.identifier not in reserved_fact_ids
            and not position.identifier.startswith(ATTACHMENT_POSITION_PREFIX)
            and not position.identifier.startswith(REQUISITE_POSITION_PREFIX)
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
        # Без внутренней обрезки по символам. Размер контролирует укладчик
        # (pack_context): эта секция — производное (droppable), и под нехватку
        # бюджета выкидывается ЦЕЛИКОМ ПОСЛЕ источников истины, а не режется
        # вслепую (прежний хардкод 2000 токенов резал ответы заказчика — см.
        # разбор инцидента РТК-копилот).
        return ContextItem(
            item_id=str(uuid.uuid4()),
            item_type="problem_field",
            source_ref=f"project_state:k{state.knowledge.version}/p{state.process.version}",
            title="Контекст проекта",
            content=content,
            token_estimate=estimate_tokens(content),
            required=True,
            priority=1500,
        )

    def _format_positions_section(
        self, *, positions: Iterable[Position], title: str
    ) -> str | None:
        """Форматирует положения (допущения/факты) одним списком, с полным телом.

        После миграции на Decision-модель деления на «релевантные задаче» и
        «глобальные» больше нет: фильтрация по affinity не применяется (Layer A
        уже отвечает реестр Decisions). Показываем все активные положения
        полностью, с capом на количество и явной пометкой остатка.
        """
        lines = [
            self._format_position_line(position)
            for position in positions
            if (position.statement or "").strip()
        ]
        lines = [line for line in lines if line]
        if not lines:
            return None
        head = lines[: self._GLOBAL_LIST_LIMIT]
        remaining = len(lines) - len(head)
        if remaining > 0:
            head.append(f"- … и ещё {remaining}")
        return f"## {title}\n" + "\n".join(head)

    @staticmethod
    def _format_position_line(position: Position) -> str:
        """Форматирует одну строку положения (полное тело) для markdown-секции."""
        statement = (position.statement or "").strip()
        if not statement:
            return ""
        source_label = ""
        if position.source == "clarification":
            source_label = " _(ответ на вопрос пользователя)_"
        elif position.source and position.source != "system":
            source_label = f" _(источник: {position.source})_"
        return f"- {statement.rstrip()}.{source_label}".replace("..", ".")

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
