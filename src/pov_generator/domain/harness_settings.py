"""Доменная модель настроек harness-исполнителя (Ф7d).

Один активный harness-connection на систему (как и дефолтный execution-provider):
нечувствительный выбор «каким агентом исполнять harness-узлы» — тип адаптера,
образ, модель, (для generic) команда, таймаут прогона.

ВАЖНО (правило проекта): секреты (креды модели) ЗДЕСЬ НЕ ХРАНЯТСЯ. Они подаются
в песочницу эфемерно в момент прогона. Persisted — только нечувствительный выбор.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Типы harness-адаптеров (зеркало infrastructure/harness/registry).
HarnessProviderType = Literal["stub", "claude_code", "aider", "command"]
HARNESS_PROVIDER_TYPES: tuple[HarnessProviderType, ...] = (
    "stub",
    "claude_code",
    "aider",
    "command",
)

# Откуда взялось подключение: дефолт, выбор пользователя или bootstrap из env.
HarnessConnectionSource = Literal["default", "user", "env_bootstrap"]


@dataclass(frozen=True)
class HarnessConnectionSettings:
    """Активное harness-подключение (нечувствительный выбор исполнителя)."""

    provider: HarnessProviderType = "stub"
    image: str | None = None
    model: str | None = None
    command: str | None = None
    default_timeout_s: int | None = None
    source: HarnessConnectionSource = "default"
    updated_at: str | None = None
