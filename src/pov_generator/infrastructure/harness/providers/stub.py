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
            payload = self._load_fixture(expected.role)
            if payload is None:
                return HarnessRunResult(
                    status="failed",
                    transcript=f"stub-harness: нет фикстуры для роли '{expected.role}'",
                    error=f"Нет harness-фикстуры '{expected.role}.json' в {self._root}.",
                )
            harvested.append(HarvestedArtifact(role=expected.role, payload=payload, fmt="json"))
        return HarnessRunResult(
            status="completed",
            artifacts=tuple(harvested),
            transcript=(
                "stub-harness: отдал канонические фикстуры для ролей "
                + ", ".join(a.role for a in harvested)
            ),
        )

    def _load_fixture(self, role: str) -> dict[str, object] | None:
        path = self._root / f"{role}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
