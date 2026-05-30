"""Тесты кеширования реестра: декоратор CachingRegistryLoader, fingerprint
файловой системы и мемоизация RegistryService.validate().

Мотивация: GET /api/projects при 35 проектах перезагружал реестр (73 YAML)
~105 раз за запрос. Кеш с инвалидацией по fingerprint и мемоизация валидации
сводят это к однократному парсингу/валидации на версию реестра.
"""

from __future__ import annotations

from pathlib import Path

from pov_generator.application.registry_service import RegistryService
from pov_generator.infrastructure.filesystem_registry import (
    CachingRegistryLoader,
    FilesystemRegistryLoader,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"


class _FakeLoader:
    """Лоадер-заглушка со счётчиком load() и управляемым fingerprint."""

    def __init__(self) -> None:
        self.load_calls = 0
        self._fingerprint: tuple = (("file.yaml", 1, 10),)
        self.snapshot = object()

    def load(self) -> object:  # type: ignore[override]
        self.load_calls += 1
        return self.snapshot

    def fingerprint(self) -> object:
        return self._fingerprint

    def mutate(self) -> None:
        """Сымитировать правку исходников: новый fingerprint + новый снапшот."""
        self._fingerprint = (("file.yaml", 2, 12),)
        self.snapshot = object()


def test_caching_loader_parses_once_until_fingerprint_changes() -> None:
    inner = _FakeLoader()
    cache = CachingRegistryLoader(inner)

    first = cache.load()
    second = cache.load()

    # Парсинг выполнен один раз; возвращается тот же объект снапшота.
    assert inner.load_calls == 1
    assert first is second is inner.snapshot


def test_caching_loader_reparses_after_fingerprint_change() -> None:
    inner = _FakeLoader()
    cache = CachingRegistryLoader(inner)

    before = cache.load()
    assert inner.load_calls == 1

    inner.mutate()
    after = cache.load()

    # Изменился fingerprint → перепарсинг и новый объект снапшота.
    assert inner.load_calls == 2
    assert after is inner.snapshot
    assert after is not before


def test_filesystem_fingerprint_changes_on_edit(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    root.mkdir()
    yaml_file = root / "thing.yaml"
    yaml_file.write_text("kind: vocabulary\nid: x\n", encoding="utf-8")

    loader = FilesystemRegistryLoader(root)
    fp_before = loader.fingerprint()

    # Правка содержимого меняет size и/или mtime → fingerprint другой.
    yaml_file.write_text("kind: vocabulary\nid: x\nextra: value\n", encoding="utf-8")
    fp_after = loader.fingerprint()
    assert fp_before != fp_after

    # Добавление нового файла тоже отражается.
    (root / "another.yaml").write_text("kind: vocabulary\nid: y\n", encoding="utf-8")
    assert loader.fingerprint() != fp_after


def test_registry_service_memoizes_validate_with_cache() -> None:
    service = RegistryService(CachingRegistryLoader(FilesystemRegistryLoader(TEMPLATES)))

    snap1, report1 = service.validate()
    snap2, report2 = service.validate()

    # Реестр валиден и валидация мемоизирована: тот же снапшот и тот же
    # объект отчёта без повторного прохода проверок.
    assert report1.is_valid
    assert snap1 is snap2
    assert report1 is report2


def test_plain_loader_returns_fresh_snapshot_each_call() -> None:
    # Контраст: без кеша каждый load() даёт новый объект снапшота — именно
    # это раньше мешало мемоизации и заставляло перевалидировать реестр.
    service = RegistryService(FilesystemRegistryLoader(TEMPLATES))
    snap1, _ = service.validate()
    snap2, _ = service.validate()
    assert snap1 is not snap2
