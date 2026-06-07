"""Класс конкуррентности harness — пул слотов на параллельные контейнеры (Ф3).

Отдельный, маленький потолок именно на harness-прогоны (контейнеры тяжёлые),
независимый от общей конкуррентности шагов workflow. Когда свободных слотов нет,
прогон ждёт очереди (бэкпрешер), а не штурмует хост. Интроспекция
(capacity/in_use/available/waiting) — для панели «машинное отделение» (Ф6).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass

from ...common.errors import ConflictError


@dataclass(frozen=True)
class SlotStatus:
    """Снимок занятости пула (для наглядности рантайма)."""

    capacity: int
    in_use: int
    waiting: int

    @property
    def available(self) -> int:
        return max(0, self.capacity - self.in_use)


class HarnessSlotPool:
    """Потокобезопасный пул слотов с очередью ожидания.

    ``acquire`` блокируется до освобождения слота (или таймаута); ``try_acquire``
    — неблокирующая попытка (для пометки «queued» без ожидания). ``slot()`` —
    контекст-менеджер: занимает на время прогона и освобождает в finally.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity пула слотов должен быть >= 1")
        self._capacity = capacity
        self._in_use = 0
        self._waiting = 0
        self._cond = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    def status(self) -> SlotStatus:
        with self._cond:
            return SlotStatus(capacity=self._capacity, in_use=self._in_use, waiting=self._waiting)

    def try_acquire(self) -> bool:
        """Занять слот без ожидания. False — слотов нет (узел в очереди)."""
        with self._cond:
            if self._in_use < self._capacity:
                self._in_use += 1
                return True
            return False

    def acquire(self, timeout: float | None = None) -> bool:
        """Занять слот, дождавшись освобождения. False — не дождались за timeout."""
        with self._cond:
            if self._in_use < self._capacity:
                self._in_use += 1
                return True
            self._waiting += 1
            try:
                ok = self._cond.wait_for(
                    lambda: self._in_use < self._capacity, timeout=timeout
                )
            finally:
                self._waiting -= 1
            if not ok:
                return False
            self._in_use += 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._in_use > 0:
                self._in_use -= 1
            self._cond.notify()

    @contextmanager
    def slot(self, timeout: float | None = None):
        """Занять слот на время блока; освободить в finally."""
        if not self.acquire(timeout=timeout):
            raise ConflictError(
                "Все слоты harness заняты — повторите, когда освободится контейнер."
            )
        try:
            yield
        finally:
            self.release()
