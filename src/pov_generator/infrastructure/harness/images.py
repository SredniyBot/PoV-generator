"""Подготовка Docker-образов агентов (Ф4).

Образ (заготовка контейнера) тянется заранее и С ПРОГРЕССОМ, а не «молча» на
первом запуске задачи. ``ImagePreparer`` — абстракция (Docker сейчас, stub в
тестах). Прогресс отдаётся колбэком ``on_progress`` построчно.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ...common.errors import ConflictError

# Колбэк прогресса: словарь как у docker pull (status/progress/id) — пробрасываем
# наверх (онбординг агрегирует в наблюдаемое состояние).
ProgressSink = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class ImageStatus:
    """Готовность образа на машине."""

    image: str
    ready: bool
    error: str | None = None


@runtime_checkable
class ImagePreparer(Protocol):
    def is_ready(self, image: str) -> bool: ...
    def prepare(self, image: str, on_progress: ProgressSink | None = None) -> ImageStatus: ...


class StubImagePreparer:
    """Тест-дубль: образы «готовы» без скачивания; эмулирует прогресс."""

    def __init__(self, ready_images: set[str] | None = None) -> None:
        self._ready: set[str] = set(ready_images or ())
        self.prepared: list[str] = []

    def is_ready(self, image: str) -> bool:
        return image in self._ready

    def prepare(self, image: str, on_progress: ProgressSink | None = None) -> ImageStatus:
        if on_progress:
            on_progress({"status": "pulling", "progress": 50, "id": image})
            on_progress({"status": "ready", "progress": 100, "id": image})
        self._ready.add(image)
        self.prepared.append(image)
        return ImageStatus(image=image, ready=True)


class DockerImagePreparer:
    """Тянет образ через docker-py со стримом прогресса."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _docker_client(self) -> Any:
        if self._client is None:
            try:
                import docker  # type: ignore
            except ImportError as exc:  # noqa: BLE001
                raise ConflictError(
                    "Docker SDK не установлен (pip install '.[harness]')."
                ) from exc
            self._client = docker.from_env()
        return self._client

    def is_ready(self, image: str) -> bool:
        try:
            self._docker_client().images.get(image)
            return True
        except Exception:  # noqa: BLE001 — нет образа / нет демона
            return False

    def prepare(self, image: str, on_progress: ProgressSink | None = None) -> ImageStatus:
        client = self._docker_client()
        try:
            # low-level API даёт построчный прогресс (decode=True → dict'ы).
            for line in client.api.pull(image, stream=True, decode=True):
                if on_progress and isinstance(line, dict):
                    on_progress(line)
        except Exception as exc:  # noqa: BLE001
            return ImageStatus(
                image=image,
                ready=False,
                error=str(exc).strip() or type(exc).__name__,
            )
        return ImageStatus(image=image, ready=self.is_ready(image))
