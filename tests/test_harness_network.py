"""Сетевой доступ песочницы для зависимостей (deny-by-default, опт-ин online).

none → egress запрещён (изоляция); online → bridge (агент/сборка ставят
зависимости из реестров). Настройка хранится в подключении и прокидывается в
ResourceLimits.network адаптера.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pov_generator.application.harness_settings_service import HarnessSettingsService
from pov_generator.common.errors import ValidationError
from pov_generator.infrastructure.harness.registry import (
    HarnessConnection,
    HarnessProviderRegistry,
)
from pov_generator.infrastructure.harness.sandbox import StubSandboxRuntime
from pov_generator.infrastructure.harness_settings_store import HarnessSettingsStore


def test_store_round_trips_network(tmp_path: Path) -> None:
    service = HarnessSettingsService(HarnessSettingsStore(tmp_path))
    saved = service.set_connection(provider="aider", network="online")
    assert saved.network == "online"
    again = HarnessSettingsStore(tmp_path).get_connection()
    assert again is not None and again.network == "online"


def test_network_defaults_to_none(tmp_path: Path) -> None:
    service = HarnessSettingsService(HarnessSettingsStore(tmp_path))
    saved = service.set_connection(provider="aider")
    assert saved.network == "none"  # deny-by-default


def test_unknown_network_rejected(tmp_path: Path) -> None:
    service = HarnessSettingsService(HarnessSettingsStore(tmp_path))
    with pytest.raises(ValidationError):
        service.set_connection(provider="aider", network="public")


def test_online_network_gives_bridge_sandbox() -> None:
    online = HarnessProviderRegistry(
        connection=HarnessConnection(provider="aider", image="x", network="online"),
        sandbox=StubSandboxRuntime(),
    ).resolve_default()
    assert online._resource_limits.network == "bridge"

    offline = HarnessProviderRegistry(
        connection=HarnessConnection(provider="aider", image="x", network="none"),
        sandbox=StubSandboxRuntime(),
    ).resolve_default()
    assert offline._resource_limits.network == "none"
