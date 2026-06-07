"""Ф3 harness-рантайма: ёмкость хоста, класс конкуррентности (слоты), бюджеты.

Без Docker. Проверяем калибровку, пул слотов (включая очередь под нагрузкой в
потоках), учёт бюджета и интеграцию в HarnessExecutionService (слот на прогон +
лимиты в spec + кумулятивный governance).
"""

from __future__ import annotations

import threading

import pytest

from pov_generator.application.harness_execution_service import HarnessExecutionService
from pov_generator.infrastructure.harness import (
    BudgetExceeded,
    BudgetTracker,
    HarnessSlotPool,
    HarvestedArtifact,
    RunLimits,
    detect_host_capacity,
    recommend_capacity,
)
from pov_generator.infrastructure.harness.protocol import HarnessRunResult, HarnessRunSpec
from pov_generator.infrastructure.llm.protocol import LLMUsage

# --- калибровка ёмкости ------------------------------------------------------


def test_recommend_capacity_clamps_and_scales() -> None:
    big = recommend_capacity(cpu_count=8, total_memory_mb=16384)
    assert 1 <= big.max_concurrent <= 4
    assert big.per_run_limits.cpus is not None and big.per_run_limits.cpus >= 1.0
    assert big.per_run_limits.memory_mb is not None and big.per_run_limits.memory_mb >= 1024
    assert big.per_run_limits.network == "none"
    assert big.default_budget.wall_clock_s is not None

    tiny = recommend_capacity(cpu_count=1)
    assert tiny.max_concurrent == 1  # минимум один слот

    no_ram = recommend_capacity(cpu_count=16)  # без RAM → cpu-only, потолок 4
    assert no_ram.max_concurrent == 4


def test_detect_host_capacity_is_valid() -> None:
    cap = detect_host_capacity()
    assert cap.max_concurrent >= 1
    assert cap.per_run_limits.cpus is not None


# --- пул слотов --------------------------------------------------------------


def test_slot_pool_capacity_and_try_acquire() -> None:
    pool = HarnessSlotPool(2)
    assert pool.try_acquire() is True
    assert pool.try_acquire() is True
    assert pool.try_acquire() is False  # занят
    assert pool.status().in_use == 2
    assert pool.status().available == 0
    pool.release()
    assert pool.status().available == 1


def test_slot_pool_acquire_timeout_when_full() -> None:
    pool = HarnessSlotPool(1)
    assert pool.acquire() is True
    assert pool.acquire(timeout=0.05) is False  # не дождались


def test_slot_pool_blocks_until_release_across_threads() -> None:
    pool = HarnessSlotPool(1)
    assert pool.acquire() is True  # держим единственный слот

    entered = threading.Event()
    acquired_second = threading.Event()

    def worker() -> None:
        entered.set()
        pool.acquire()  # должен блокироваться, пока не освободим
        acquired_second.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert entered.wait(1.0)
    # Пока слот занят — второй поток ждёт (в очереди), слот не получен.
    assert not acquired_second.wait(0.1)
    assert pool.status().waiting == 1
    pool.release()
    assert acquired_second.wait(1.0)
    t.join(1.0)


# --- бюджет ------------------------------------------------------------------


def test_budget_tracker_accumulates_and_enforces() -> None:
    tracker = BudgetTracker(max_total_tokens=100)
    tracker.record(LLMUsage(input_tokens=30, output_tokens=20, total_tokens=50, source="actual"))
    totals = tracker.totals()
    assert totals.runs == 1
    assert totals.total_tokens == 50
    assert tracker.exceeded() is None
    tracker.ensure_within_budget()  # ещё в пределах

    tracker.record(LLMUsage(input_tokens=40, output_tokens=30, total_tokens=70, source="actual"))
    assert tracker.exceeded() is not None
    with pytest.raises(BudgetExceeded):
        tracker.ensure_within_budget()


def test_budget_tracker_handles_none_usage() -> None:
    tracker = BudgetTracker()
    tracker.record(None)
    assert tracker.totals().runs == 1
    assert tracker.totals().total_tokens == 0


# --- интеграция в HarnessExecutionService -----------------------------------


class _CapturingProvider:
    name = "stub"
    model = None

    def __init__(self, *, usage: LLMUsage | None = None, on_run=None) -> None:
        self.specs: list[HarnessRunSpec] = []
        self._usage = usage
        self._on_run = on_run

    def run(self, spec: HarnessRunSpec) -> HarnessRunResult:
        self.specs.append(spec)
        if self._on_run is not None:
            self._on_run()
        role = spec.expected_artifacts[0].role
        return HarnessRunResult(
            status="completed",
            artifacts=(HarvestedArtifact(role=role, payload={"ok": True}, fmt="json"),),
            usage=self._usage,
        )


class _FakeRegistry:
    def __init__(self, provider: _CapturingProvider) -> None:
        self._provider = provider

    def default_provider_name(self) -> str:
        return "stub"

    def resolve_default(self) -> _CapturingProvider:
        return self._provider


def test_service_passes_budget_limits_into_spec() -> None:
    provider = _CapturingProvider()
    service = HarnessExecutionService(
        registry=_FakeRegistry(provider),
        budget_limits=RunLimits(wall_clock_s=123, max_steps=7),
    )
    service.produce_artifact_payload(
        artifact_role="demo_output", system_prompt="S", user_prompt="U"
    )
    assert len(provider.specs) == 1
    assert provider.specs[0].limits is not None
    assert provider.specs[0].limits.wall_clock_s == 123
    assert provider.specs[0].limits.max_steps == 7


def test_service_serializes_runs_through_slot_pool() -> None:
    in_run = threading.Event()
    release = threading.Event()
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    def on_run() -> None:
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        in_run.set()
        release.wait(1.0)
        with lock:
            concurrent["now"] -= 1

    provider = _CapturingProvider(on_run=on_run)
    service = HarnessExecutionService(
        registry=_FakeRegistry(provider),
        slots=HarnessSlotPool(1),  # потолок 1 → прогоны сериализуются
    )

    def call() -> None:
        service.produce_artifact_payload(
            artifact_role="demo_output", system_prompt="S", user_prompt="U"
        )

    t1 = threading.Thread(target=call, daemon=True)
    t2 = threading.Thread(target=call, daemon=True)
    t1.start()
    assert in_run.wait(1.0)  # первый внутри прогона
    t2.start()
    # Пока первый держит слот — второй не должен войти в run.
    assert service.slot_status().in_use == 1
    release.set()
    t1.join(2.0)
    t2.join(2.0)
    # Слот один → одновременно не больше одного прогона.
    assert concurrent["max"] == 1
    assert provider.specs.__len__() == 2


def test_service_refuses_when_cumulative_budget_exhausted() -> None:
    usage = LLMUsage(input_tokens=80, output_tokens=40, total_tokens=120, source="actual")
    provider = _CapturingProvider(usage=usage)
    service = HarnessExecutionService(
        registry=_FakeRegistry(provider),
        budget_tracker=BudgetTracker(max_total_tokens=100),
    )
    # Первый прогон проходит и записывает 120 токенов (> лимита).
    service.produce_artifact_payload(
        artifact_role="demo_output", system_prompt="S", user_prompt="U"
    )
    # Второй — отказ до запуска (governance).
    with pytest.raises(BudgetExceeded):
        service.produce_artifact_payload(
            artifact_role="demo_output", system_prompt="S", user_prompt="U"
        )
