"""Оркестрация исполнения узла harness-агентом (Ф1, без Docker).

Это «второй бэкенд за тем же контрактом артефакта»: узел с ``executor: harness``
получает payload не из ``chat_json``, а от harness-провайдера. Здесь — общая,
провайдер-агностичная обвязка: рендер brief из ContextManifest, запуск
провайдера, сбор результата (harvest). Конкретный провайдер резолвится из
:class:`HarnessProviderRegistry` (Ф1 — stub).

Downstream (персист артефакта, валидация, решения, планировщик) не трогаем:
сервис возвращает только payload + usage, а ``ExecutionService`` собирает
артефакт тем же путём, что и для LLM/stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.errors import ConflictError
from ..domain.registry import HarnessGateSpec
from ..infrastructure.harness import (
    BudgetTotals,
    BudgetTracker,
    ExpectedArtifact,
    GateResult,
    HarnessGate,
    HarnessProviderRegistry,
    HarnessRunSpec,
    HarnessSlotPool,
    RunLimits,
    SlotStatus,
    detect_host_capacity,
)
from ..infrastructure.llm.protocol import LLMUsage


@dataclass(frozen=True)
class HarnessOutcome:
    """Результат прогона узла: структурный payload ЛИБО файловый бандл."""

    provider_name: str
    model: str | None
    transcript: str
    usage: LLMUsage | None
    brief: str
    payload: dict[str, Any] | None = None       # структурный выход (Ф1)
    files: dict[str, bytes] | None = None         # файловый бандл (Ф5)
    bundle_kind: str | None = None
    gates: tuple[GateResult, ...] = ()           # результаты гейтов «готово» (Ф5c)

    @property
    def is_bundle(self) -> bool:
        return self.files is not None

    def trace_payload(self) -> dict[str, Any]:
        """Провенанс прогона (L3/L4) — тем же паттерном, что methodology_trace.

        Самодостаточная «как получен артефакт» сводка узла-агента: адаптер,
        постановка (brief), транскрипт, результаты гейтов и расход. Ложится в
        ``ArtifactMetadata.harness_trace`` и отдаётся drill-down'ом (Ф6).
        """
        usage = (
            {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.input_tokens + self.usage.output_tokens,
                "cost_usd": self.usage.cost_usd,
            }
            if self.usage is not None
            else None
        )
        return {
            "provider": self.provider_name,
            "model": self.model,
            "output_kind": "bundle" if self.is_bundle else "structured",
            "brief": self.brief,
            "transcript": self.transcript,
            "gates": [
                {
                    "name": g.name,
                    "passed": g.passed,
                    "exit_code": g.exit_code,
                    "log": g.log,
                }
                for g in self.gates
            ],
            "usage": usage,
        }


@dataclass(frozen=True)
class HarnessRuntimeStatus:
    """Живой снимок рантайма harness — для панели «машинное отделение» (Ф6).

    Слоты (занятость класса конкуррентности + очередь ожидания), накопленный
    расход прогонов и лимиты одного прогона. Это внутренние governance-показатели
    управления, НЕ оценки/гарантии заказчику (правило проекта).
    """

    provider_name: str
    slots: SlotStatus
    budget: BudgetTotals
    run_limits: RunLimits
    budget_exceeded: str | None = None


def render_harness_brief(
    *,
    artifact_role: str,
    system_prompt: str,
    user_prompt: str,
    expected_artifacts: tuple[ExpectedArtifact, ...],
) -> str:
    """Собрать самодостаточную постановку для агента.

    Методология и контекст уже сведены в ``system_prompt``/``user_prompt``
    (общий путь сборки промпта). Brief добавляет к ним соглашение о выходе:
    куда и в каком формате положить ожидаемые артефакты — чтобы сбор результата
    (harvest-by-convention) был детерминированным независимо от конкретного
    агента. Отдельная функция — чтобы тестировать рендер в изоляции.
    """
    expectations = "\n".join(
        f"- роль `{exp.role}` → файл `.povgen/out/{exp.role}.{exp.fmt}` (формат: {exp.fmt})"
        for exp in expected_artifacts
    )
    return (
        f"{system_prompt}\n\n"
        f"{user_prompt}\n\n"
        "## Что нужно произвести\n"
        f"{expectations or f'- роль `{artifact_role}`'}\n\n"
        "Размести каждый ожидаемый артефакт по указанному пути. Не выходи за рамки "
        "задачи; недостающие данные отметь в содержимом, а не выдумывай."
    )


class HarnessExecutionService:
    """Производит payload артефакта узла через harness-провайдера.

    Ф3: прогон проходит через класс конкуррентности (пул слотов — отдельный
    маленький потолок на параллельные контейнеры, авто-калибровка по ёмкости
    хоста) и под бюджетом (RunLimits → spec; кумулятивный учёт + governance).
    """

    def __init__(
        self,
        registry: HarnessProviderRegistry | None = None,
        *,
        slots: HarnessSlotPool | None = None,
        budget_limits: RunLimits | None = None,
        budget_tracker: BudgetTracker | None = None,
        slot_acquire_timeout: float | None = None,
    ) -> None:
        self._registry = registry or HarnessProviderRegistry()
        capacity = detect_host_capacity()
        # Отдельный класс конкуррентности harness (контейнеры тяжёлые).
        self._slots = slots or HarnessSlotPool(capacity.max_concurrent)
        # Бюджет прогона (wall_clock enforce'ит песочница; остальное — учёт).
        self._budget_limits = budget_limits or capacity.default_budget
        self._budget = budget_tracker or BudgetTracker()
        self._slot_acquire_timeout = slot_acquire_timeout

    def default_provider_name(self) -> str:
        return self._registry.default_provider_name()

    def slot_status(self) -> SlotStatus:
        """Занятость пула слотов — для панели «машинное отделение» (Ф6)."""
        return self._slots.status()

    def budget_totals(self) -> BudgetTotals:
        """Накопленный расход harness-прогонов — для панели/аудита."""
        return self._budget.totals()

    def runtime_status(self) -> HarnessRuntimeStatus:
        """Единый живой снимок рантайма для панели «машинное отделение» (Ф6).

        Сводит дефолтный провайдер, занятость слотов, накопленный расход и
        лимиты прогона. Свободные слоты UI выводит как ``capacity - in_use``.
        """
        return HarnessRuntimeStatus(
            provider_name=self.default_provider_name(),
            slots=self.slot_status(),
            budget=self.budget_totals(),
            run_limits=self._budget_limits,
            budget_exceeded=self._budget.exceeded(),
        )

    def produce_artifact(
        self,
        *,
        artifact_role: str,
        system_prompt: str,
        user_prompt: str,
        output_kind: str = "structured",
        model_hint: str | None = None,
        gates: tuple[HarnessGateSpec, ...] = (),
        build_group: str | None = None,
        inputs: dict[str, str] | None = None,
    ) -> HarnessOutcome:
        """Запустить дефолтный harness и собрать выход роли.

        ``output_kind="structured"`` → структурный JSON-payload (Ф1);
        ``"bundle"`` → файловый бандл (код/документы/двоичные/БД/образ, Ф5).
        ``inputs`` (реквизиты v2, Ф5b) — предоставленные пользователем данные/
        файлы компонента, засеваемые в рабочий каталог узла (``/work/<имя>``),
        чтобы код-узел видел их как реальные файлы. Один ожидаемый артефакт;
        harvest-by-convention.
        """
        # Governance: кумулятивный бюджет исчерпан → не запускаем (fail-loudly).
        self._budget.ensure_within_budget()
        fmt = "files" if output_kind == "bundle" else "json"
        expected = (ExpectedArtifact(role=artifact_role, fmt=fmt),)
        brief = render_harness_brief(
            artifact_role=artifact_role,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            expected_artifacts=expected,
        )
        spec = HarnessRunSpec(
            brief=brief,
            expected_artifacts=expected,
            model_hint=model_hint,
            limits=self._budget_limits,
            gates=tuple(
                HarnessGate(name=g.name, command=g.command, timeout_s=g.timeout_s)
                for g in gates
            ),
            volume=build_group,
            inputs=dict(inputs) if inputs else {},
        )
        provider = self._registry.resolve_default()
        # Класс конкуррентности: занимаем слот на время прогона (бэкпрешер —
        # лишние harness-узлы ждут очереди, а не штурмуют хост).
        with self._slots.slot(timeout=self._slot_acquire_timeout):
            result = provider.run(spec)
        # Учёт расхода (даже неуспешного прогона) — для панели и governance.
        self._budget.record(result.usage)
        if result.status != "completed":
            raise ConflictError(
                f"harness '{provider.name}' не завершил узел "
                f"(status={result.status}): {result.error or 'без деталей'}."
            )
        harvested = next((a for a in result.artifacts if a.role == artifact_role), None)
        if harvested is None:
            raise ConflictError(
                f"harness '{provider.name}' не вернул артефакт роли '{artifact_role}'."
            )
        base = {
            "provider_name": f"harness:{provider.name}",
            "model": getattr(provider, "model", None) or model_hint,
            "transcript": result.transcript,
            "usage": result.usage,
            "brief": brief,
            "gates": result.gates,
        }
        if output_kind == "bundle":
            if not harvested.files:
                raise ConflictError(
                    f"harness '{provider.name}' не вернул файловый бандл роли "
                    f"'{artifact_role}'."
                )
            return HarnessOutcome(**base, files=dict(harvested.files))
        if harvested.payload is None:
            raise ConflictError(
                f"harness '{provider.name}' не вернул структурный артефакт "
                f"роли '{artifact_role}'."
            )
        return HarnessOutcome(**base, payload=harvested.payload)

    def produce_artifact_payload(
        self,
        *,
        artifact_role: str,
        system_prompt: str,
        user_prompt: str,
        model_hint: str | None = None,
    ) -> HarnessOutcome:
        """Совместимость (Ф1/Ф3): структурный выход. Делегирует produce_artifact."""
        return self.produce_artifact(
            artifact_role=artifact_role,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_kind="structured",
            model_hint=model_hint,
        )
