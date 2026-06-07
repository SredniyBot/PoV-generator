"""Структурный контракт harness-исполнителя (второй бэкенд исполнения).

Зеркало ``infrastructure/llm/protocol.py``: как LLM-провайдер за ``chat_json``
производит структурный ответ, так harness-провайдер за ``run`` производит
артефакт(ы) узла задачи — потенциально многошагово, в песочнице.

Ф1 (без Docker): контракт + stub-провайдер; реальная песочница и файловые
бандлы — следующие фазы. Поэтому ``HarvestedArtifact`` пока несёт ``payload``
(структурный артефакт), а ``files`` зарезервированы под бандлы (Ф5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ..llm.protocol import LLMUsage

HarnessStatus = Literal["completed", "failed", "cancelled", "needs_input", "partial"]
ArtifactFormat = Literal["json", "markdown", "files"]


@dataclass(frozen=True)
class RunLimits:
    """Governance-лимиты прогона (enforce'ятся рантаймом, не агентом)."""

    wall_clock_s: int | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class ExpectedArtifact:
    """Что узел обязан произвести: роль + (опц.) схема + ожидаемый формат."""

    role: str
    schema: dict[str, Any] | None = None
    fmt: ArtifactFormat = "json"


@dataclass(frozen=True)
class HarnessRunSpec:
    """Нормализованная постановка одного прогона узла-агента.

    ``brief`` — самодостаточная постановка (рендерится из ContextManifest +
    методология + контракт + критерии приёмки). ``inputs`` — входные артефакты
    как файлы (имя → содержимое). Песочница/отмена добавятся на Ф2+.
    """

    brief: str
    expected_artifacts: tuple[ExpectedArtifact, ...]
    inputs: dict[str, str] = field(default_factory=dict)
    model_hint: str | None = None
    limits: RunLimits | None = None


@dataclass(frozen=True)
class HarvestedArtifact:
    """Один собранный с прогона артефакт.

    ``payload`` — для структурных (JSON/markdown-данные). ``files`` — для
    файловых бандлов (Ф5). На Ф1 заполняется только ``payload``.
    """

    role: str
    payload: dict[str, Any] | None = None
    files: dict[str, bytes] | None = None
    fmt: ArtifactFormat = "json"


@dataclass(frozen=True)
class HarnessRunResult:
    """Итог прогона: статус + собранные артефакты + транскрипт + usage."""

    status: HarnessStatus
    artifacts: tuple[HarvestedArtifact, ...] = ()
    transcript: str = ""
    usage: LLMUsage | None = None
    error: str | None = None


@runtime_checkable
class HarnessProvider(Protocol):
    """Контракт «дать постановку — получить артефакт(ы) узла».

    Реализации обязаны иметь ``name`` (str) и ``model`` (str | None) и метод
    ``run(spec) -> HarnessRunResult``. Конфигурацию (образ/креды/лимиты)
    провайдер получает при сборке через :class:`HarnessProviderRegistry`,
    а не читает env сам.
    """

    name: str
    model: str | None

    def run(self, spec: HarnessRunSpec) -> HarnessRunResult:
        """Исполнить постановку и вернуть результат с собранными артефактами."""
        ...
