"""Проба готовности Docker для harness-агентов (Ф4).

Определяет, доступен ли Docker-демон, без падений: нет docker SDK / не запущен
демон → ``available=False`` с понятной причиной. Используется онбордингом, чтобы
показать пользователю явный индикатор и подсказку, а не «тихую» ошибку.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class DockerStatus:
    """Состояние Docker на машине (для индикатора готовности)."""

    available: bool
    version: str | None = None
    error: str | None = None
    hint: str | None = None
    # Найден демон, но Python-SDK не установлен: образы/прогон требуют SDK.
    sdk_installed: bool = True


_INSTALL_HINT = (
    "Установите Docker (Docker Desktop на Windows/macOS, engine на Linux) "
    "и запустите его, затем повторите проверку."
)
_SDK_HINT = (
    "Docker найден, но не установлен Python-SDK. Для запуска агентов выполните "
    "pip install '.[harness]' в окружении сервиса и перезапустите его."
)


def probe_docker(client_factory: Callable[[], Any] | None = None) -> DockerStatus:
    """Проверить доступность Docker-демона.

    ``client_factory`` — для тестов (подменяет создание клиента). В проде —
    ``docker.from_env`` через ленивый импорт. Если Python-SDK не установлен,
    демон всё равно детектируется через CLI (``docker version``), чтобы индикатор
    не врал «недоступен» при работающем Docker Desktop.
    """
    try:
        client = client_factory() if client_factory is not None else _default_client()
    except ImportError:
        # SDK нет — но Docker Desktop может работать. Детектим демон через CLI.
        return _probe_docker_cli()
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


def _probe_docker_cli() -> DockerStatus:
    """Фоллбек без SDK: детектируем Docker по CLI (кросс-платформенно).

    ``docker version --format {{.Server.Version}}`` отвечает только при живом
    демоне. Если CLI есть и демон отвечает — Docker доступен, но помечаем
    ``sdk_installed=False`` и даём подсказку про установку ``.[harness]``.
    """
    exe = shutil.which("docker")
    if exe is None:
        return DockerStatus(
            available=False,
            error="Python-SDK не установлен и Docker CLI не найден на PATH.",
            hint=_INSTALL_HINT,
            sdk_installed=False,
        )
    try:
        result = subprocess.run(  # noqa: S603 — фикс. аргументы, путь из which
            [exe, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return DockerStatus(
            available=False,
            error=str(exc).strip() or type(exc).__name__,
            hint=_INSTALL_HINT,
            sdk_installed=False,
        )
    version = (result.stdout or "").strip()
    if result.returncode != 0 or not version:
        # CLI есть, но демон не отвечает (не запущен Docker Desktop).
        return DockerStatus(
            available=False,
            error="Docker установлен, но демон не отвечает.",
            hint="Запустите Docker Desktop и повторите проверку.",
            sdk_installed=False,
        )
    return DockerStatus(available=True, version=version, hint=_SDK_HINT, sdk_installed=False)


def _default_client() -> Any:
    import docker  # type: ignore

    return docker.from_env()
