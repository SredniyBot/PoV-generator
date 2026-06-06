"""Закреплённый граф задач за проектом.

У каждого проекта — свой замороженный снимок реестра. Резолв шаблонов проекта
идёт из снимка, а не из живого ``templates/``, поэтому правки графа не ломают
прошлые проекты: их можно смотреть, перезапускать и дорабатывать на исходном
графе. Новые проекты берут текущий реестр.

Принцип захвата — «при первом обращении» (capture-on-first-use): если снимок
ещё не закреплён (старый проект или первый запуск), закрепляем текущий реестр и
дальше работаем из него. Это не требует менять создание проекта и охватывает
уже существующие проекты при первом же обращении.

SOLID: единственная ответственность — отдать снимок для проекта (SRP). Зависит
от абстракций (хранилище-рантайм + корень реестра), парсинг переиспользуется из
``filesystem_registry`` (DRY/DIP).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.registry import RegistrySnapshot
from ..infrastructure.filesystem_registry import read_registry_texts, snapshot_from_texts


class _PinStore(Protocol):
    """Минимальный контракт хранилища закреплённого снимка (рантайм проекта)."""

    def load_pinned_registry(self, workspace: Path) -> dict[str, str] | None: ...

    def pin_registry(
        self, workspace: Path, files: dict[str, str], fingerprint: str = ""
    ) -> None: ...


class ProjectRegistryResolver:
    """Отдаёт :class:`RegistrySnapshot` для конкретного проекта (его граф)."""

    def __init__(self, store: _PinStore, registry_root: Path) -> None:
        self._store = store
        self._registry_root = registry_root
        # Снимок проекта заморожен, поэтому кешируем по workspace на процесс.
        self._cache: dict[str, RegistrySnapshot] = {}

    def snapshot_for(self, workspace: Path) -> RegistrySnapshot:
        key = str(workspace)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        files = self._store.load_pinned_registry(workspace)
        if files is None:
            # Старый проект / первый запуск — замораживаем текущий реестр.
            files = read_registry_texts(self._registry_root)
            self._store.pin_registry(workspace, files)
        snapshot = snapshot_from_texts(files)
        self._cache[key] = snapshot
        return snapshot
