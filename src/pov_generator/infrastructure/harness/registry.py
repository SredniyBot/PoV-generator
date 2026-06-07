"""Резолвер harness-провайдеров.

Зеркало ``infrastructure/llm/registry.py``: единственное место, где живёт выбор
конкретного harness-исполнителя. Сервисам отдаёт готовый
:class:`HarnessProvider`.

Ф1 (без Docker, без UI-настроек): единственный зарегистрированный провайдер —
``stub``, он же дефолтный. Резолв из ``SqliteSettingsStore`` (подключения,
образы, дефолт «класс задачи → harness») добавится на Ф4 — структура
``_PROVIDER_BUILDERS`` к этому готова.
"""

from __future__ import annotations

from typing import Callable

from ...common.errors import ConflictError
from .protocol import HarnessProvider
from .providers.stub import StubHarnessProvider

# Имя провайдера в реестре → билдер. Дальше сюда лягут реальные адаптеры
# (claude_code, aider, generic) — каждый строится из настроек подключения.
ProviderBuilder = Callable[[], HarnessProvider]

_PROVIDER_BUILDERS: dict[str, ProviderBuilder] = {
    "stub": StubHarnessProvider,
}

# Дефолтный harness, пока нет настроек выбора (Ф4). Stub — детерминированный,
# без Docker; в проде дефолт сменится на настоящий адаптер.
_DEFAULT_PROVIDER = "stub"


class HarnessProviderRegistry:
    """Резолвит и собирает :class:`HarnessProvider` по имени."""

    @property
    def supported_providers(self) -> tuple[str, ...]:
        return tuple(sorted(_PROVIDER_BUILDERS.keys()))

    def default_provider_name(self) -> str:
        """Имя дефолтного harness (для метаданных/трейса узла)."""
        return _DEFAULT_PROVIDER

    def get(self, provider: str) -> HarnessProvider:
        builder = _PROVIDER_BUILDERS.get(provider)
        if builder is None:
            raise ConflictError(
                f"Неподдерживаемый harness-провайдер: '{provider}'. "
                f"Поддерживаются: {', '.join(self.supported_providers)}."
            )
        return builder()

    def resolve_default(self) -> HarnessProvider:
        """Собрать дефолтный harness (Ф1: всегда stub)."""
        return self.get(self.default_provider_name())
