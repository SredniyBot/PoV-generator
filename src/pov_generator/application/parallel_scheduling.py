"""Чистая логика параллельного шедулинга шагов workflow.

Вынесена из runner'а отдельным модулем без I/O — чтобы решения «сколько
параллелить» и «какие задачи можно запускать одновременно» были детерминир.
и юнит-тестируемыми в изоляции (SRP). Сам runner отвечает за потоки, запись
в БД и жизненный цикл run'а.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

# --- провайдер-зависимая конкурентность ---------------------------------------
#
# Разные провайдеры по-разному переносят параллельные вызовы:
#   - claude_subscription — локальный CLI: каждый вызов спавнит процесс, есть
#     контеншн сессий подписки и rate-limit → держим умеренно (3).
#   - claude_sdk / openrouter — прямой API: параллелятся хорошо.
#   - stub — без сети, можно агрессивно.
# provider=None означает резолв по purpose в рантайме (может оказаться чем
# угодно) → берём умеренный безопасный дефолт.
_PROVIDER_MAX_CONCURRENCY: dict[str, int] = {
    "claude_subscription": 3,
    "claude_sdk": 5,
    "openrouter": 5,
    "stub": 8,
}
_DEFAULT_MAX_CONCURRENCY = 3
_ENV_OVERRIDE = "POV_WORKFLOW_MAX_CONCURRENCY"


def max_concurrency_for(provider: str | None) -> int:
    """Сколько шагов запускать параллельно для данного провайдера.

    Глобальный потолок ``POV_WORKFLOW_MAX_CONCURRENCY`` (если задан и валиден)
    переопределяет всё — удобно для отладки и для прода с известными лимитами.
    """
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        try:
            value = int(override)
            if value >= 1:
                return value
        except ValueError:
            pass
    if provider in _PROVIDER_MAX_CONCURRENCY:
        return _PROVIDER_MAX_CONCURRENCY[provider]
    return _DEFAULT_MAX_CONCURRENCY


# --- write-set задачи (conflict-aware co-scheduling) --------------------------


def task_write_set(template) -> frozenset[str]:
    """«Ресурсы записи» задачи из её декларированных эффектов.

    Две задачи с пересекающимися write-set'ами нельзя запускать одновременно:
    они конкурируют за один и тот же логический ресурс (один artifact_role →
    дуэль версий артефакта; один gap / readiness-dimension → семантический
    клинч). Per-workspace лок гарантирует атомарность записи, а этот фильтр —
    логическую корректность (не даём двум задачам писать одно и то же).

    Источник — только ДЕКЛАРАТИВНЫЕ поля template (никакого хардкода):
    ``outputs.artifact_roles``, ``effects.closes_gaps``,
    ``effects.raises_readiness``.
    """
    items: set[str] = set()
    for role in template.outputs.artifact_roles:
        items.add(f"artifact:{role}")
    for gap_id in template.effects.closes_gaps:
        items.add(f"gap:{gap_id}")
    for readiness in template.effects.raises_readiness:
        items.add(f"readiness:{readiness.dimension}")
    return frozenset(items)


# --- выбор задач к запуску ----------------------------------------------------


@dataclass(frozen=True)
class DispatchPlan:
    """Результат раунда выбора: какие задачи запустить и сколько слотов занято."""

    selected: tuple[str, ...]  # task_id к запуску в этом раунде


def select_dispatchable(
    candidates: Sequence,
    *,
    write_set_of: Callable[[object], frozenset[str]],
    in_flight_task_ids: Iterable[str],
    in_flight_write_sets: Iterable[frozenset[str]],
    free_slots: int,
) -> list:
    """Выбрать подмножество admissible-кандидатов к параллельному запуску.

    Жадно по уже отсортированному порядку (приоритет), пропуская:
      * задачи, уже находящиеся in-flight (по task_id);
      * задачи, чей write-set пересекается с занятыми (in-flight) или с уже
        выбранными в этом раунде.
    Не больше ``free_slots`` штук.

    Возвращает список выбранных кандидатов (в исходном порядке).
    """
    if free_slots <= 0:
        return []
    running = set(in_flight_task_ids)
    reserved: set[str] = set()
    for ws in in_flight_write_sets:
        reserved |= ws

    chosen: list = []
    for candidate in candidates:
        if len(chosen) >= free_slots:
            break
        if candidate.task_id in running:
            continue
        ws = write_set_of(candidate)
        if ws & reserved:
            continue
        chosen.append(candidate)
        reserved |= ws
    return chosen
