"""Запись расхода токенов на один LLM-вызов (персистентная форма).

Гранулярность — на каждый реальный LLM-вызов; агрегаты до задачи и проекта
считаются запросом, не денормализацией. На одну задачу может приходиться
несколько вызовов: режим ``per_stage_cot`` (по вызову на стадию + финальный) и
retry (каждая попытка — отдельные вызовы, суммируются).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UsageSource = Literal["actual", "estimated"]


@dataclass(frozen=True)
class LLMUsageRecord:
    """Строка таблицы ``llm_usage`` — один LLM-вызов."""

    usage_id: str
    project_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    source: UsageSource
    created_at: str
    task_id: str | None = None
    artifact_id: str | None = None
    execution_run_id: str | None = None
    stage: str | None = None
    cache_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class LLMUsageAggregate:
    """Агрегат расхода токенов (по задаче или проекту)."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    call_count: int
    # Истинно, если хотя бы один вызов в агрегате — оценочный (UI помечает
    # «оценка»). Если вызовов нет вообще — агрегат не строится (n/a).
    has_estimated: bool
    cost_usd: float | None = None
