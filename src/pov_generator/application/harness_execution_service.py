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
from ..infrastructure.harness import (
    ExpectedArtifact,
    HarnessProviderRegistry,
    HarnessRunSpec,
)
from ..infrastructure.llm.protocol import LLMUsage


@dataclass(frozen=True)
class HarnessOutcome:
    """Результат produce_artifact_payload для ``ExecutionService``."""

    payload: dict[str, Any]
    provider_name: str
    model: str | None
    transcript: str
    usage: LLMUsage | None
    brief: str


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
    """Производит payload артефакта узла через harness-провайдера."""

    def __init__(self, registry: HarnessProviderRegistry | None = None) -> None:
        self._registry = registry or HarnessProviderRegistry()

    def default_provider_name(self) -> str:
        return self._registry.default_provider_name()

    def produce_artifact_payload(
        self,
        *,
        artifact_role: str,
        system_prompt: str,
        user_prompt: str,
        model_hint: str | None = None,
    ) -> HarnessOutcome:
        """Запустить дефолтный harness и собрать структурный payload роли.

        Ф1: один ожидаемый артефакт (структурный JSON), harvest-by-convention.
        Файловые бандлы и несколько артефактов — следующие фазы.
        """
        expected = (ExpectedArtifact(role=artifact_role, fmt="json"),)
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
        )
        provider = self._registry.resolve_default()
        result = provider.run(spec)
        if result.status != "completed":
            raise ConflictError(
                f"harness '{provider.name}' не завершил узел "
                f"(status={result.status}): {result.error or 'без деталей'}."
            )
        harvested = next(
            (a for a in result.artifacts if a.role == artifact_role), None
        )
        if harvested is None or harvested.payload is None:
            raise ConflictError(
                f"harness '{provider.name}' не вернул структурный артефакт "
                f"роли '{artifact_role}'."
            )
        return HarnessOutcome(
            payload=harvested.payload,
            provider_name=f"harness:{provider.name}",
            model=getattr(provider, "model", None) or model_hint,
            transcript=result.transcript,
            usage=result.usage,
            brief=brief,
        )
