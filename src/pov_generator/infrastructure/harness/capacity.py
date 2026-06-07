"""Авто-калибровка ёмкости хоста под harness-прогоны (Ф3).

Контейнер с агентом тяжёлый (CPU+RAM+диск) — нельзя лить их десятками, как
LLM-вызовы. По числу ядер и объёму RAM рекомендуем: сколько контейнеров держать
параллельно (класс конкуррентности) и какие per-container лимиты ставить.
Чистая функция ``recommend_capacity`` — детерминированная и юнит-тестируемая;
``detect_host_capacity`` — best-effort определение ресурсов без тяжёлых
зависимостей (psutil не требуется), при неудаче деградирует к CPU-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .protocol import RunLimits
from .sandbox import ResourceLimits

# Эвристики (консервативные, безопасные дефолты).
_MEM_PER_CONTAINER_MB = 2048   # ~2 ГБ на контейнер при расчёте по RAM
_MAX_CONCURRENT_CEILING = 4    # потолок параллельных контейнеров
_DEFAULT_WALL_CLOCK_S = 1800   # 30 мин на прогон по умолчанию


@dataclass(frozen=True)
class HarnessCapacity:
    """Рекомендация по ёмкости: параллелизм + лимиты контейнера + бюджет прогона."""

    max_concurrent: int
    per_run_limits: ResourceLimits
    default_budget: RunLimits


def recommend_capacity(
    *, cpu_count: int, total_memory_mb: int | None = None
) -> HarnessCapacity:
    """Рекомендуемая ёмкость по ресурсам хоста (чистая функция).

    Параллелизм = по более узкому ресурсу (CPU vs RAM), с потолком безопасности.
    per-container лимиты подбираются так, чтобы N контейнеров уживались на хосте.
    """
    cpu_count = max(1, cpu_count)
    cpu_cap = max(1, cpu_count // 2)  # половину ядер оставляем системе/API
    if total_memory_mb:
        mem_cap = max(1, total_memory_mb // _MEM_PER_CONTAINER_MB)
        max_concurrent = min(cpu_cap, mem_cap)
    else:
        max_concurrent = cpu_cap
    max_concurrent = max(1, min(max_concurrent, _MAX_CONCURRENT_CEILING))

    per_cpus = round(min(2.0, max(1.0, cpu_count / max_concurrent)), 1)
    if total_memory_mb:
        # 70% RAM делим между контейнерами; зажимаем в [1 ГБ, 4 ГБ].
        per_mem = int(min(4096, max(1024, int(total_memory_mb * 0.7) // max_concurrent)))
    else:
        per_mem = _MEM_PER_CONTAINER_MB

    return HarnessCapacity(
        max_concurrent=max_concurrent,
        per_run_limits=ResourceLimits(
            cpus=per_cpus, memory_mb=per_mem, pids=512, network="none"
        ),
        default_budget=RunLimits(wall_clock_s=_DEFAULT_WALL_CLOCK_S),
    )


def detect_host_capacity() -> HarnessCapacity:
    """Определить ресурсы хоста (best-effort) и вернуть рекомендацию."""
    cpu_count = os.cpu_count() or 1
    return recommend_capacity(cpu_count=cpu_count, total_memory_mb=_detect_total_memory_mb())


def _detect_total_memory_mb() -> int | None:
    """Общая RAM в МБ. Кросс-платформенно без зависимостей; None при неудаче."""
    # POSIX (Linux; на части систем доступно и в macOS).
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
        phys_pages = os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
        if page_size > 0 and phys_pages > 0:
            return int(page_size * phys_pages // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass
    # Windows через GlobalMemoryStatusEx.
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.ullTotalPhys // (1024 * 1024))
    except Exception:  # noqa: BLE001 — нет Windows/ctypes → деградируем
        pass
    return None
