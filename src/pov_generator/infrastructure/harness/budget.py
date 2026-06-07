"""Бюджеты harness-прогонов (Ф3) — governance-лимиты внутри системы.

Лимиты на ПРОГОН (wall_clock/токены/шаги/стоимость) живут в ``RunLimits``
(см. protocol): wall_clock enforce'ит песочница (таймаут), остальное —
адаптеры/учёт по мере появления реальных данных. ``BudgetTracker`` — накопитель
расхода (токены/стоимость) с опциональными кумулятивными потолками: учёт для
панели рантайма и pre-run governance («дальше нельзя — бюджет исчерпан»).

Это внутренние лимиты управления, НЕ оценки/гарантии, выдаваемые пользователю.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ...common.errors import ConflictError
from ..llm.protocol import LLMUsage


class BudgetExceeded(ConflictError):
    """Кумулятивный бюджет harness исчерпан — дальнейшие прогоны запрещены."""


@dataclass(frozen=True)
class BudgetTotals:
    """Снимок накопленного расхода (для панели/аудита)."""

    runs: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float


class BudgetTracker:
    """Накапливает расход harness-прогонов; стережёт кумулятивные потолки.

    Потолки опциональны: None → без ограничения по этому измерению. Потокобезопасен
    (прогоны идут в параллельных потоках runner'а).
    """

    def __init__(
        self,
        *,
        max_total_tokens: int | None = None,
        max_total_cost_usd: float | None = None,
    ) -> None:
        self._max_tokens = max_total_tokens
        self._max_cost = max_total_cost_usd
        self._runs = 0
        self._input = 0
        self._output = 0
        self._cost = 0.0
        self._lock = threading.Lock()

    def record(self, usage: LLMUsage | None) -> None:
        """Учесть расход одного прогона (None — провайдер не дал данных)."""
        with self._lock:
            self._runs += 1
            if usage is None:
                return
            self._input += usage.input_tokens
            self._output += usage.output_tokens
            if usage.cost_usd is not None:
                self._cost += usage.cost_usd

    def totals(self) -> BudgetTotals:
        with self._lock:
            return BudgetTotals(
                runs=self._runs,
                input_tokens=self._input,
                output_tokens=self._output,
                total_tokens=self._input + self._output,
                cost_usd=round(self._cost, 6),
            )

    def exceeded(self) -> str | None:
        """Причина превышения кумулятивного потолка, иначе None."""
        with self._lock:
            if self._max_tokens is not None and (self._input + self._output) >= self._max_tokens:
                return (
                    f"Достигнут лимит токенов harness ({self._input + self._output} "
                    f">= {self._max_tokens})."
                )
            if self._max_cost is not None and self._cost >= self._max_cost:
                return f"Достигнут лимит стоимости harness ({self._cost:.4f} >= {self._max_cost})."
            return None

    def ensure_within_budget(self) -> None:
        """Бросить :class:`BudgetExceeded`, если кумулятивный потолок исчерпан."""
        reason = self.exceeded()
        if reason is not None:
            raise BudgetExceeded(reason)
