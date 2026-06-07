"""Проба готовности Docker для harness-агентов (Ф4).

Определяет, доступен ли Docker-демон, без падений: нет docker SDK / не запущен
демон → ``available=False`` с понятной причиной. Используется онбордингом, чтобы
показать пользователю явный индикатор и подсказку, а не «тихую» ошибку.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class DockerStatus:
    """Состояние Docker на машине (для индикатора готовности)."""

    available: bool
    version: str | None = None
    error: str | None = None
    hint: str | None = None


_INSTALL_HINT = (
    "Установите Docker (Docker Desktop на Windows/macOS, engine на Linux) "
    "и запустите его, затем повторите проверку."
)


def probe_docker(client_factory: Callable[[], Any] | None = None) -> DockerStatus:
    """Проверить доступность Docker-демона.

    ``client_factory`` — для тестов (подменяет создание клиента). В проде —
    ``docker.from_env`` через ленивый импорт; отсутствие SDK/демона → понятный
    ``DockerStatus(available=False)``.
    """
    try:
        client = client_factory() if client_factory is not None else _default_client()
    except ImportError:
        return DockerStatus(
            available=False,
            error="Docker SDK не установлен (pip install '.[harness]').",
            hint=_INSTALL_HINT,
        )
    except Exception as exc:  # noqa: BLE001 — демон недоступен/не запущен
        return DockerStatus(
            available=False,
            error=str(exc).strip() or type(exc).__name__,
            hint=_INSTALL_HINT,
        )
    try:
        client.ping()
        version_info = client.version()
        version = version_info.get("Version") if isinstance(version_info, dict) else None
        return DockerStatus(available=True, version=version)
    except Exception as exc:  # noqa: BLE001 — демон не отвечает
        return DockerStatus(
            available=False,
            error=str(exc).strip() or type(exc).__name__,
            hint=_INSTALL_HINT,
        )


def _default_client() -> Any:
    import docker  # type: ignore

    return docker.from_env()
