from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import yaml

from ..common.errors import ValidationError
from ..common.logging import get_logger
from ..domain.registry import (
    RegistrySnapshot,
    parse_artifact_contract,
    parse_capability_profile,
    parse_domain_pack,
    parse_methodology_pack,
    parse_objective,
    parse_quality_gate,
    parse_task_template,
    parse_vocabulary,
)

_logger = get_logger("registry")


# Каталог-источника → (парсер, как извлечь ключ снапшота, поле снапшота).
# Единый источник правды для дисетча. Парсинг не зависит от того, откуда
# пришли документы (файлы или закреплённый снимок из БД) — это точка
# расширения (DIP): меняется поставщик документов, не парсинг.
def _by_identifier(spec: object) -> str:
    return spec.identifier  # type: ignore[attr-defined]


def _by_ref(spec: object) -> str:
    return spec.ref.as_string()  # type: ignore[attr-defined]


_KIND_DIRS: dict[str, tuple[Callable, Callable[[object], str], str]] = {
    "vocabularies": (parse_vocabulary, _by_identifier, "vocabularies"),
    "objectives": (parse_objective, _by_ref, "objectives"),
    "tasks": (parse_task_template, _by_ref, "templates"),
    "artifacts": (parse_artifact_contract, _by_ref, "artifact_contracts"),
    "domains": (parse_domain_pack, _by_ref, "domain_packs"),
    "methodologies": (parse_methodology_pack, _by_ref, "methodology_packs"),
    "gates": (parse_quality_gate, _by_ref, "quality_gates"),
    "capabilities": (parse_capability_profile, _by_ref, "capability_profiles"),
}


def _coerce_document(raw: object, source: str) -> dict:
    data = raw or {}
    if not isinstance(data, dict):
        raise ValidationError(f"YAML document must be a mapping: {source}")
    if data.get("kind") is None:
        raise ValidationError(f"Missing 'kind' field in {source}")
    return data


def build_snapshot_from_documents(
    documents: Iterable[tuple[str, str, dict]],
) -> RegistrySnapshot:
    """Собрать :class:`RegistrySnapshot` из набора документов.

    Каждый документ — ``(kind_dir, source, raw)``: ``kind_dir`` — каталог-род
    (tasks/artifacts/...), ``source`` — метка для ошибок (полный путь или
    относительный), ``raw`` — распарсенный YAML. Файлы вне известных родов
    (фикстуры и пр.) игнорируются.
    """
    buckets: dict[str, dict] = {field: {} for (_p, _k, field) in _KIND_DIRS.values()}
    for kind_dir, source, raw in documents:
        entry = _KIND_DIRS.get(kind_dir)
        if entry is None:
            continue
        parser, key_of, field = entry
        spec = parser(_coerce_document(raw, source), Path(source))
        buckets[field][key_of(spec)] = spec
    return RegistrySnapshot(**buckets)


def read_registry_texts(root: Path) -> dict[str, str]:
    """Прочитать дерево реестра как ``{относительный_путь: текст YAML}``.

    Для закрепления графа за проектом: сырой текст переживает любые правки
    шаблонов и не требует сериализатора доменных объектов.
    """
    texts: dict[str, str] = {}
    for path in sorted(root.rglob("*.yaml")):
        texts[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return texts


def snapshot_from_texts(texts: Mapping[str, str]) -> RegistrySnapshot:
    """Построить снимок из закреплённых текстов (см. ``read_registry_texts``)."""
    documents: list[tuple[str, str, dict]] = []
    for relpath, text in texts.items():
        if not relpath.endswith(".yaml"):
            continue
        kind_dir = relpath.replace("\\", "/").split("/", 1)[0]
        documents.append((kind_dir, relpath, yaml.safe_load(text) or {}))
    return build_snapshot_from_documents(documents)


@runtime_checkable
class RegistryLoader(Protocol):
    """Порт загрузки реестра шаблонов.

    Абстрагирует источник реестра от потребителей (``RegistryService`` и
    кеширующего декоратора). ``fingerprint`` — дешёвый токен изменения
    исходников: по нему кеширующий слой решает, нужно ли перепарсивать
    реестр, не читая содержимое файлов.
    """

    def load(self) -> RegistrySnapshot: ...

    def fingerprint(self) -> object: ...


class FilesystemRegistryLoader:
    """Загрузчик реестра из дерева YAML под ``root`` (templates/)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self) -> RegistrySnapshot:
        _started = time.perf_counter()
        documents: list[tuple[str, str, dict]] = []
        for path in sorted(self._root.rglob("*.yaml")):
            rel = path.relative_to(self._root).as_posix()
            kind_dir = rel.split("/", 1)[0]
            if kind_dir not in _KIND_DIRS:
                continue
            documents.append((kind_dir, str(path), self._load_yaml(path)))
        snapshot = build_snapshot_from_documents(documents)
        _logger.info(
            "реестр загружен",
            objectives=len(snapshot.objectives),
            tasks=len(snapshot.templates),
            artifacts=len(snapshot.artifact_contracts),
            domain_packs=len(snapshot.domain_packs),
            methodologies=len(snapshot.methodology_packs),
            gates=len(snapshot.quality_gates),
            capability_profiles=len(snapshot.capability_profiles),
            duration_ms=round((time.perf_counter() - _started) * 1000),
        )
        return snapshot

    def _load_yaml(self, path: Path) -> dict:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValidationError(f"YAML document must be a mapping: {path}")
        kind = data.get("kind")
        if kind is None:
            raise ValidationError(f"Missing 'kind' field in {path}")
        return data

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        """Дешёвый токен изменения исходников реестра.

        Кортеж ``(path, mtime_ns, size)`` по всем YAML под корнем. Сравнение
        этого токена позволяет кешу понять, изменился ли реестр, не читая и
        не парся содержимое. Чувствителен к правке, добавлению и удалению
        любого файла (путь входит в токен, а полный набор путей — в кортеж).

        Стоимость — один ``stat`` на файл (~доли мс на дерево), на порядки
        дешевле полного парсинга YAML.
        """
        entries: list[tuple[str, int, int]] = []
        for path in sorted(self._root.rglob("*.yaml")):
            try:
                stat = path.stat()
            except OSError:
                # Файл исчез между rglob и stat — пропускаем; следующий
                # fingerprint это отразит и вызовет переинициализацию кеша.
                continue
            entries.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)


class CachingRegistryLoader:
    """Кеширующий декоратор над :class:`RegistryLoader`.

    Хранит распарсенный снапшот и переиспользует его, пока ``fingerprint``
    исходников не изменится. Инвалидация — по контенту (mtime + size), а не
    по TTL: нет окна устаревания, правка YAML подхватывается при следующем
    ``load()``. Дорогой парсинг выполняется только при реальном изменении.

    Дизайн: SRP — декоратор отвечает только за политику кеширования, парсинг
    остаётся заботой обёрнутого лоадера; OCP — поведение добавлено без правки
    парсера; DIP — работает с любым ``RegistryLoader``.

    Важно для мемоизации в ``RegistryService``: пока исходники не менялись,
    ``load()`` возвращает РОВНО ТОТ ЖЕ объект снапшота, поэтому потребители
    могут кешировать производные данные по identity снапшота.
    """

    def __init__(self, inner: RegistryLoader) -> None:
        self._inner = inner
        self._snapshot: RegistrySnapshot | None = None
        self._fingerprint: object = None

    def load(self) -> RegistrySnapshot:
        current = self._inner.fingerprint()
        if self._snapshot is None or current != self._fingerprint:
            self._snapshot = self._inner.load()
            self._fingerprint = current
        return self._snapshot

    def fingerprint(self) -> object:
        return self._inner.fingerprint()
