"""Lightweight pre-selector сложности leaf-задачи (W3.2).

Цель — перед запуском leaf-задачи отдельным дешёвым LLM-вызовом (haiku)
оценить сложность по фактическому контексту и, если она расходится с
declared `template.complexity`, переопределить выбор модели.

Без selector'а сложность зашита автором шаблона и не меняется от
конкретного бизнес-запроса (16 leaf-задач c `complexity: standard` —
независимо от того, простой это PoC или enterprise multi-domain
сценарий). Это противоречит исходному принципу «выбор модели по
сложности» из vision (00).

## Контракт

`select_complexity(...)` возвращает `ComplexitySelection`:
- `complexity`: одна из `trivial | standard | complex`
- `rationale`: короткое объяснение, попадает в execution trace
- `source`: `"template"` (без вызова), `"stub"`, `"llm"` — кто решал

## Активация

Default: **off** (selector не вызывается, используется
`template.complexity`).

Включение через env `POV_COMPLEXITY_SELECTOR=on` (или `=stub` для
тестов). Если LLM-вызов не удался — fallback на `template.complexity`,
никогда не блокирует workflow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from ..common.errors import ConflictError
from ..common.serialization import json_dumps
from ..domain.project_state import ProjectState
from ..domain.registry import TemplateSpec
from ..infrastructure.llm import LLMProviderRegistry

ComplexityLevel = Literal["trivial", "standard", "complex"]


@dataclass(frozen=True)
class ComplexitySelection:
    complexity: ComplexityLevel
    rationale: str
    source: Literal["template", "stub", "llm"]


@dataclass(frozen=True)
class ComplexitySelectorContext:
    """Минимальный snapshot контекста для оценки сложности.

    Не таскает полные артефакты — selector должен быть дешёвым. Только
    структурный summary, который влияет на сложность."""

    business_request: str
    goal: str | None
    active_domain_packs: tuple[str, ...]
    active_gap_count: int
    open_clarification_count: int


def select_complexity(
    *,
    template: TemplateSpec,
    state: ProjectState,
    open_clarification_count: int = 0,
    llm_registry: "LLMProviderRegistry | None" = None,
) -> ComplexitySelection:
    """Главная точка входа. Решает, нужно ли вообще звать selector,
    и какой провайдер использовать.

    ``llm_registry`` опционально — если передан и в нём есть привязка к
    settings-store, selector пойдёт через ``resolve_for_purpose
    ("complexity_selector")``. Без store будет fallback на env.
    """
    declared = _coerce_complexity(template.complexity) or "standard"
    mode = (os.environ.get("POV_COMPLEXITY_SELECTOR") or "off").strip().lower()
    if mode in {"off", "false", "0", ""}:
        return ComplexitySelection(
            complexity=declared,
            rationale=f"Selector выключен (POV_COMPLEXITY_SELECTOR={mode!r}); используется declared template.complexity.",
            source="template",
        )

    selector_context = ComplexitySelectorContext(
        business_request=state.manifest.business_request or "",
        goal=state.knowledge.goal_statement(),
        active_domain_packs=tuple(sorted(state.process.active_domain_pack_records.keys())),
        active_gap_count=len(state.process.active_gaps),
        open_clarification_count=open_clarification_count,
    )

    if mode == "stub":
        return _stub_select(template=template, declared=declared, context=selector_context)

    # mode == "on" (или любой непустой провайдерный override)
    try:
        return _llm_select(
            template=template,
            declared=declared,
            context=selector_context,
            mode=mode,
            llm_registry=llm_registry,
        )
    except ConflictError:
        # LLM-провайдер недоступен — fallback на declared.
        return ComplexitySelection(
            complexity=declared,
            rationale="Не удалось вызвать complexity-selector (fallback на declared).",
            source="template",
        )


def _stub_select(
    *,
    template: TemplateSpec,
    declared: ComplexityLevel,
    context: ComplexitySelectorContext,
) -> ComplexitySelection:
    """Детерминированный фоллбэк: повышает declared->complex, если активных
    domain packs много, или declared->trivial если контекст пустой.
    Нужен для тестов (POV_COMPLEXITY_SELECTOR=stub) и для случая отсутствия
    LLM-провайдера."""
    if len(context.active_domain_packs) >= 3 or context.open_clarification_count >= 3:
        if declared != "complex":
            return ComplexitySelection(
                complexity="complex",
                rationale=(
                    f"Stub-selector поднял сложность до complex: "
                    f"{len(context.active_domain_packs)} domain packs, "
                    f"{context.open_clarification_count} открытых уточнений."
                ),
                source="stub",
            )
    if not context.active_domain_packs and len(context.business_request) < 80:
        if declared == "complex":
            return ComplexitySelection(
                complexity="standard",
                rationale="Stub-selector опустил сложность: контекст лёгкий, доменов нет.",
                source="stub",
            )
    return ComplexitySelection(
        complexity=declared,
        rationale="Stub-selector подтвердил declared template.complexity.",
        source="stub",
    )


def _llm_select(
    *,
    template: TemplateSpec,
    declared: ComplexityLevel,
    context: ComplexitySelectorContext,
    mode: str,
    llm_registry: "LLMProviderRegistry | None" = None,
) -> ComplexitySelection:
    """Зовёт LLM (предпочтительно haiku — самая дешёвая модель). Возвращает
    структурированный JSON или ConflictError, если провайдер недоступен."""
    provider = _select_provider(mode)
    system_prompt = (
        "Ты — pre-selector сложности задачи в системе генерации проектных артефактов. "
        "Твоя задача — оценить, насколько leaf-задача требует мощной модели для качественного результата. "
        "Trivial — простой структурный шаг (нормализация запроса, извлечение фактов). "
        "Standard — типичный аналитический шаг (требование, ограничение, сравнение). "
        "Complex — задача с многими активными domain packs, противоречивыми входами, неопределённостями. "
        "Верни строго JSON: {complexity, rationale}. Пиши rationale на русском, кратко."
    )
    user_prompt = json_dumps(
        {
            "task_template": template.ref.as_string(),
            "task_title": template.name,
            "task_summary": template.summary,
            "declared_complexity": declared,
            "context": {
                "business_request_excerpt": context.business_request[:400],
                "goal": context.goal,
                "active_domain_packs": list(context.active_domain_packs),
                "active_gap_count": context.active_gap_count,
                "open_clarification_count": context.open_clarification_count,
            },
        }
    )
    schema = {
        "type": "object",
        "required": ["complexity", "rationale"],
        "additionalProperties": False,
        "properties": {
            "complexity": {"type": "string", "enum": ["trivial", "standard", "complex"]},
            "rationale": {"type": "string"},
        },
    }

    # Резолв провайдера:
    # * если есть явный override провайдера (POV_COMPLEXITY_SELECTOR_PROVIDER)
    #   — legacy путь через registry.get с env-кредитами;
    # * иначе — resolve_for_purpose через settings-store (если registry
    #   привязан к store), иначе env.
    registry = llm_registry or LLMProviderRegistry()
    explicit_model = os.environ.get("POV_COMPLEXITY_SELECTOR_MODEL")
    explicit_provider = os.environ.get("POV_COMPLEXITY_SELECTOR_PROVIDER")
    if explicit_provider:
        llm = registry.get(
            provider=provider,
            model=explicit_model,
            complexity="trivial",
        )
    else:
        llm = registry.resolve_for_purpose(
            "complexity_selector",
            complexity="trivial",
            override_model=explicit_model,
        )
    payload = llm.chat_json(system_prompt=system_prompt, user_prompt=user_prompt, schema=schema).payload

    raw_complexity = str(payload.get("complexity") or declared)
    chosen = _coerce_complexity(raw_complexity) or declared
    rationale = str(payload.get("rationale") or "").strip() or "LLM не указал rationale."
    return ComplexitySelection(complexity=chosen, rationale=rationale, source="llm")


def _select_provider(mode: str) -> str:
    """Какой LLM-клиент использовать для selector. По умолчанию следует за
    POV_EXECUTION_PROVIDER (как CE11 в W0.3), но можно override через
    POV_COMPLEXITY_SELECTOR_PROVIDER."""
    explicit = os.environ.get("POV_COMPLEXITY_SELECTOR_PROVIDER")
    if explicit:
        return explicit
    if mode in {"openrouter", "claude_sdk", "claude_subscription"}:
        return mode
    execution_provider = os.environ.get("POV_EXECUTION_PROVIDER", "stub")
    if execution_provider in {"openrouter", "claude_sdk", "claude_subscription"}:
        return execution_provider
    return "openrouter"


def _coerce_complexity(value: str | None) -> ComplexityLevel | None:
    if value in ("trivial", "standard", "complex"):
        return value  # type: ignore[return-value]
    return None
