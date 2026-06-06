from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol, runtime_checkable

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
        vocabularies = {}
        objectives = {}
        templates = {}
        artifact_contracts = {}
        domain_packs = {}
        methodology_packs = {}
        quality_gates = {}
        capability_profiles = {}

        for path in sorted((self._root / "vocabularies").glob("*.yaml")):
            raw = self._load_yaml(path)
            vocabulary = parse_vocabulary(raw, path)
            vocabularies[vocabulary.identifier] = vocabulary

        for path in sorted((self._root / "objectives").rglob("*.yaml")):
            raw = self._load_yaml(path)
            objective = parse_objective(raw, path)
            objectives[objective.ref.as_string()] = objective

        for path in sorted((self._root / "tasks").rglob("*.yaml")):
            raw = self._load_yaml(path)
            template = parse_task_template(raw, path)
            templates[template.ref.as_string()] = template

        for path in sorted((self._root / "artifacts").rglob("*.yaml")):
            raw = self._load_yaml(path)
            contract = parse_artifact_contract(raw, path)
            artifact_contracts[contract.ref.as_string()] = contract

        for path in sorted((self._root / "domains").rglob("*.yaml")):
            raw = self._load_yaml(path)
            pack = parse_domain_pack(raw, path)
            domain_packs[pack.ref.as_string()] = pack

        methodologies_root = self._root / "methodologies"
        if methodologies_root.exists():
            for path in sorted(methodologies_root.rglob("*.yaml")):
                raw = self._load_yaml(path)
                methodology = parse_methodology_pack(raw, path)
                methodology_packs[methodology.ref.as_string()] = methodology

        for path in sorted((self._root / "gates").rglob("*.yaml")):
            raw = self._load_yaml(path)
            gate = parse_quality_gate(raw, path)
            quality_gates[gate.ref.as_string()] = gate

        capabilities_root = self._root / "capabilities"
        if capabilities_root.exists():
            for path in sorted(capabilities_root.rglob("*.yaml")):
                raw = self._load_yaml(path)
                profile = parse_capability_profile(raw, path)
                capability_profiles[profile.ref.as_string()] = profile

        _logger.info(
            "реестр загружен",
            objectives=len(objectives),
            tasks=len(templates),
            artifacts=len(artifact_contracts),
            domain_packs=len(domain_packs),
            methodologies=len(methodology_packs),
            gates=len(quality_gates),
            capability_profiles=len(capability_profiles),
            duration_ms=round((time.perf_counter() - _started) * 1000),
        )
        return RegistrySnapshot(
            vocabularies=vocabularies,
            objectives=objectives,
            templates=templates,
            artifact_contracts=artifact_contracts,
            domain_packs=domain_packs,
            methodology_packs=methodology_packs,
            quality_gates=quality_gates,
            capability_profiles=capability_profiles,
        )

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
