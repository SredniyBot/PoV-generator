"""Сервис настроек harness-исполнителя (Ф7d).

Единая точка чтения/записи активного harness-подключения. Резолв исполнителя
для рантайма (`resolve_runtime_connection`) идёт по приоритету: сохранённое
пользователем → env-bootstrap → дефолт ``stub``. Этот резолвер передаётся в
:class:`HarnessProviderRegistry` как ленивый загрузчик, так что смена настроек
из UI применяется без перезапуска.
"""

from __future__ import annotations

import os

from ..common.errors import ValidationError
from ..common.serialization import utc_now_iso
from ..domain.harness_settings import (
    HARNESS_ENGINES,
    HARNESS_HOST_SECURITY,
    HARNESS_NETWORK,
    HARNESS_PROVIDER_TYPES,
    HarnessConnectionSettings,
)
from ..infrastructure.harness import HarnessConnection, connection_from_env
from ..infrastructure.harness_settings_store import HarnessSettingsStore


class HarnessSettingsService:
    """Чтение/запись активного harness-подключения + резолв для рантайма."""

    def __init__(self, store: HarnessSettingsStore) -> None:
        self._store = store

    def get_connection(self) -> HarnessConnectionSettings:
        """Эффективное подключение для UI: сохранённое или дефолт (env → stub)."""
        stored = self._store.get_connection()
        if stored is not None:
            return stored
        env_conn = connection_from_env()
        source = "env_bootstrap" if os.environ.get("POV_HARNESS_PROVIDER") else "default"
        return HarnessConnectionSettings(
            provider=env_conn.provider,
            image=env_conn.image,
            model=env_conn.model,
            command=env_conn.command,
            default_timeout_s=env_conn.default_timeout_s,
            source=source,
        )

    def set_connection(
        self,
        *,
        provider: str,
        image: str | None = None,
        model: str | None = None,
        command: str | None = None,
        default_timeout_s: int | None = None,
        engine: str = "docker",
        host_security: str = "restricted",
        network: str = "none",
    ) -> HarnessConnectionSettings:
        """Сохранить выбор пользователя. Валидирует тип адаптера и движок."""
        if provider not in HARNESS_PROVIDER_TYPES:
            raise ValidationError(
                f"Неизвестный тип harness: '{provider}'. "
                f"Допустимы: {', '.join(HARNESS_PROVIDER_TYPES)}."
            )
        if engine not in HARNESS_ENGINES:
            raise ValidationError(
                f"Неизвестный движок песочницы: '{engine}'. "
                f"Допустимы: {', '.join(HARNESS_ENGINES)}."
            )
        if host_security not in HARNESS_HOST_SECURITY:
            raise ValidationError(
                f"Неизвестный режим безопасности: '{host_security}'. "
                f"Допустимы: {', '.join(HARNESS_HOST_SECURITY)}."
            )
        if network not in HARNESS_NETWORK:
            raise ValidationError(
                f"Неизвестный сетевой режим: '{network}'. "
                f"Допустимы: {', '.join(HARNESS_NETWORK)}."
            )
        # host-движок переиспользует залогиненную сессию claude CLI — он осмыслен
        # только для адаптера claude_code; для прочих принудительно docker.
        if engine == "host" and provider != "claude_code":
            raise ValidationError(
                "Исполнение на хосте доступно только для адаптера claude_code "
                "(переиспользует залогиненную сессию claude CLI). "
                "Для остальных адаптеров используйте docker."
            )
        settings = HarnessConnectionSettings(
            provider=provider,  # type: ignore[arg-type]  — проверено выше
            image=(image or None),
            model=(model or None),
            command=(command or None),
            default_timeout_s=default_timeout_s,
            engine=engine,  # type: ignore[arg-type]  — проверено выше
            host_security=host_security,  # type: ignore[arg-type]  — проверено выше
            network=network,  # type: ignore[arg-type]  — проверено выше
            source="user",
            updated_at=utc_now_iso(),
        )
        return self._store.set_connection(settings)

    def resolve_runtime_connection(self) -> HarnessConnection:
        """Подключение для реестра: сохранённое → env-bootstrap → stub.

        Передаётся в HarnessProviderRegistry ленивым загрузчиком: каждый прогон
        читает актуальный выбор, смена из UI применяется без перезапуска.
        """
        stored = self._store.get_connection()
        if stored is not None:
            return HarnessConnection(
                provider=stored.provider,
                image=stored.image,
                model=stored.model,
                command=stored.command,
                default_timeout_s=stored.default_timeout_s,
                engine=stored.engine,
                host_security=stored.host_security,
                network=stored.network,
            )
        return connection_from_env()
