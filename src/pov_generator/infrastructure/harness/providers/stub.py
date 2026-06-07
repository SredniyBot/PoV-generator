"""Детерминированный stub harness-провайдер.

НЕ пользовательский бэкенд, а тест-дубль (как stub-LLM): на каждый ожидаемый
артефакт отдаёт канонический payload из ``templates/harness_fixtures/<role>.json``.
Ничего не запускает, Docker не нужен — основной путь тестов/CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..protocol import (
    HarnessRunResult,
    HarnessRunSpec,
    HarvestedArtifact,
)

# templates/harness_fixtures — рядом с stub_fixtures LLM-стаба. Зависит только от
# расположения репозитория, не от runtime_root. Этот файл лежит на 5 уровней
# ниже корня (src/pov_generator/infrastructure/harness/providers/stub.py).
_FIXTURE_ROOT = Path(__file__).resolve().parents[5] / "templates" / "harness_fixtures"


class StubHarnessProvider:
    """Возвращает фикстуры по роли. Нет фикстуры → результат ``failed``."""

    name = "stub"
    model: str | None = None

    def __init__(self, fixtures_root: Path | None = None) -> None:
        self._root = fixtures_root or _FIXTURE_ROOT

    def run(self, spec: HarnessRunSpec) -> HarnessRunResult:
        harvested: list[HarvestedArtifact] = []
        for expected in spec.expected_artifacts:
            if expected.fmt == "json":
                payload = self._load_fixture(expected.role)
                if payload is None:
                    return self._missing(expected.role, f"{expected.role}.json")
                harvested.append(
                    HarvestedArtifact(role=expected.role, payload=payload, fmt="json")
                )
            else:
                files = self._load_dir_fixture(expected.role)
                if files is None:
                    return self._missing(expected.role, f"{expected.role}/")
                harvested.append(
                    HarvestedArtifact(role=expected.role, files=files, fmt=expected.fmt)
                )
        return HarnessRunResult(
            status="completed",
            artifacts=tuple(harvested),
            transcript=(
                "stub-harness: отдал канонические фикстуры для ролей "
                + ", ".join(a.role for a in harvested)
            ),
        )

    def _missing(self, role: str, name: str) -> HarnessRunResult:
        return HarnessRunResult(
            status="failed",
            transcript=f"stub-harness: нет фикстуры для роли '{role}'",
            error=f"Нет harness-фикстуры '{name}' в {self._root}.",
        )

    def _load_dir_fixture(self, role: str) -> dict[str, bytes] | None:
        """Файловый бандл-фикстура: все файлы из каталога ``<role>/``."""
        directory = self._root / role
        if not directory.is_dir():
            return None
        files: dict[str, bytes] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                files[path.relative_to(directory).as_posix()] = path.read_bytes()
        return files or None

    def _load_fixture(self, role: str) -> dict[str, object] | None:
        path = self._root / f"{role}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
