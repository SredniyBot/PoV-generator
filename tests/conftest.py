"""Общие ускорители тестов.

Кэш парсинга YAML реестра. ``FilesystemRegistryLoader.load()`` читает и парсит
весь корпус ``templates/`` (~73 файла, ~200мс на вызов) — и почти всё это время
уходит на чтение+``yaml.safe_load`` (сама валидация почти бесплатна). Сотни
тестов делают это повторно для одного и того же неизменного каталога — это
доминирующая часть времени прогона.

Мемоизируем разбор ОДНОГО файла (``_load_yaml``) по ключу (path, mtime, size) в
рамках процесса (в т.ч. каждого xdist-воркера). При этом ``load()`` по-прежнему
пересобирает СВЕЖИЙ ``RegistrySnapshot`` на каждый вызов — поэтому наблюдаемое
поведение (включая «плоский загрузчик отдаёт новый снапшот каждый раз») не
меняется, ломается только повторный парсинг. Правка/добавление файла меняет
mtime/size → промах кэша → честный перепарсинг (важно для тестов, которые
копируют templates во временную папку и меняют их).

Только для тестов (conftest) — прод-код не трогаем.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader

_YAML_CACHE: dict[tuple[str, int, int], dict] = {}
_ORIGINAL_LOAD_YAML = FilesystemRegistryLoader._load_yaml


def _cached_load_yaml(self: FilesystemRegistryLoader, path: Path) -> dict:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return _ORIGINAL_LOAD_YAML(self, path)
    data = _YAML_CACHE.get(key)
    if data is None:
        data = _ORIGINAL_LOAD_YAML(self, path)
        _YAML_CACHE[key] = data
    return data


# Патчим на время тестовой сессии (per-process, в т.ч. в каждом xdist-воркере).
FilesystemRegistryLoader._load_yaml = _cached_load_yaml  # type: ignore[method-assign]
