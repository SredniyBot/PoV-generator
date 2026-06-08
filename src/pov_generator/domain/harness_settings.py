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

# Движок песочницы: docker (изоляция, креды по API) или host (исполнение на
# хосте — переиспользует залогиненную сессию claude CLI). host имеет смысл только
# для адаптера claude_code.
HarnessEngine = Literal["docker", "host"]
HARNESS_ENGINES: tuple[HarnessEngine, ...] = ("docker", "host")

# Режим безопасности host-исполнения:
#   restricted — claude правит только файлы в изолированном workspace, без
#     хостового shell; сборка/тесты/сервисы — в docker (гейты/образ). Безопасно.
#   full — claude получает полный доступ на хосте (skip-permissions): максимум
#     автономности, но реальной изоляции нет. Осознанный опт-ин.
HarnessHostSecurity = Literal["restricted", "full"]
HARNESS_HOST_SECURITY: tuple[HarnessHostSecurity, ...] = ("restricted", "full")

# Сетевой доступ песочницы:
#   none   — egress запрещён (изоляция, дефолт безопасности). Агент НЕ может
#            ставить зависимости из реестров (pip/npm), сборка офлайн.
#   online — есть сеть (bridge): агент и гейты ставят зависимости из реестров
#            (pip/npm/...), `docker build` тянет пакеты. Осознанный опт-ин.
HarnessNetwork = Literal["none", "online"]
HARNESS_NETWORK: tuple[HarnessNetwork, ...] = ("none", "online")


@dataclass(frozen=True)
class HarnessConnectionSettings:
    """Активное harness-подключение (нечувствительный выбор исполнителя)."""

    provider: HarnessProviderType = "stub"
    image: str | None = None
    model: str | None = None
    command: str | None = None
    default_timeout_s: int | None = None
    # Движок песочницы и режим безопасности host (Ф7e). host — только для
    # claude_code; full-режим — осознанный размен изоляции на автономность.
    engine: HarnessEngine = "docker"
    host_security: HarnessHostSecurity = "restricted"
    # Сетевой доступ docker-песочницы: none (изоляция) или online (зависимости из
    # реестров). Deny-by-default; для host-движка не применимо (сеть хоста).
    network: HarnessNetwork = "none"
    source: HarnessConnectionSource = "default"
    updated_at: str | None = None
