"""Резолв LLM-подключения для harness: исключение claude_cli + пустая
архитектура падает по схеме (а не уходит в пустую сборку).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from pov_generator.application.artifact_contracts import (
    artifact_schema,
    validate_json_schema,
)
from pov_generator.common.errors import ValidationError
from pov_generator.domain.llm_settings import ProviderConnection, ProviderCredentials
from pov_generator.infrastructure.llm.registry import LLMProviderRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _Assignment:
    model_name: str


@dataclass
class _Routing:
    connection_id: str


class _FakeStore:
    def __init__(self, conns, routings, assignment):
        self._conns = conns
        self._routings = routings
        self._assignment = assignment

    def get_assignment(self, _key):  # noqa: ANN001
        return self._assignment

    def list_routings_for_model(self, model):  # noqa: ANN001
        return self._routings.get(model, [])

    def get_connection(self, cid):  # noqa: ANN001
        return self._conns.get(cid)

    def get_context_limit(self, _model):  # noqa: ANN001
        return None


def _conn(cid: str, ptype: str, key: str | None = "k") -> ProviderConnection:
    return ProviderConnection(
        connection_id=cid,
        provider_type=ptype,  # type: ignore[arg-type]
        display_name=cid,
        credentials=ProviderCredentials(api_key=key),
        extras={},
    )


def test_harness_resolution_skips_claude_cli() -> None:
    # Модель маршрутизирована и на claude_cli (приоритетнее), и на openrouter.
    store = _FakeStore(
        conns={"cli": _conn("cli", "claude_cli", key=None), "or": _conn("or", "openrouter")},
        routings={"m": [_Routing("cli"), _Routing("or")]},
        assignment=_Assignment("m"),
    )
    reg = LLMProviderRegistry(settings_store=store)

    # Для harness в docker claude_cli исключаем → берём openrouter.
    conn, model = reg.resolve_connection_for_purpose(
        "execution", complexity="complex", exclude_provider_types=("claude_cli",)
    )
    assert conn is not None and conn.provider_type == "openrouter"
    assert model == "m"

    # Без исключения — первый по приоритету (claude_cli).
    conn2, _ = reg.resolve_connection_for_purpose("execution", complexity="complex")
    assert conn2 is not None and conn2.provider_type == "claude_cli"


def test_only_claude_cli_yields_no_harness_connection() -> None:
    store = _FakeStore(
        conns={"cli": _conn("cli", "claude_cli", key=None)},
        routings={"m": [_Routing("cli")]},
        assignment=_Assignment("m"),
    )
    reg = LLMProviderRegistry(settings_store=store)
    conn, _ = reg.resolve_connection_for_purpose(
        "execution", complexity="complex", exclude_provider_types=("claude_cli",)
    )
    assert conn is None  # docker-агенту нечего подать → UI предупредит


def test_empty_component_model_fails_schema() -> None:
    # Пустая декомпозиция (components: []) — блокирующая schema-ошибка: гейт
    # архитектуры падает с понятной причиной, не уходит в пустую реализацию.
    payload = json.loads(
        (REPO_ROOT / "templates" / "stub_fixtures" / "component_model.json").read_text(
            encoding="utf-8"
        )
    )
    payload["components"] = []
    with pytest.raises(ValidationError):
        validate_json_schema(payload, artifact_schema("component_model"))


def test_nonempty_component_model_passes_schema() -> None:
    payload = json.loads(
        (REPO_ROOT / "templates" / "stub_fixtures" / "component_model.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(payload["components"]) >= 1
    validate_json_schema(payload, artifact_schema("component_model"))  # не бросает
