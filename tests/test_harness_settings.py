"""Ф7d — хранение активного harness-подключения (без секретов).

Резолв исполнителя: сохранённое пользователем → env-bootstrap → дефолт stub.
Смена применяется без перезапуска (реестр читает резолвер на каждый прогон).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pov_generator.application.harness_settings_service import HarnessSettingsService
from pov_generator.common.errors import ValidationError
from pov_generator.infrastructure.harness import HarnessProviderRegistry
from pov_generator.infrastructure.harness_settings_store import HarnessSettingsStore
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def _service(tmp_path: Path) -> HarnessSettingsService:
    return HarnessSettingsService(HarnessSettingsStore(tmp_path))


def test_default_connection_is_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POV_HARNESS_PROVIDER", raising=False)
    conn = _service(tmp_path).get_connection()
    assert conn.provider == "stub"
    assert conn.source == "default"


def test_set_and_get_roundtrip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.set_connection(
        provider="aider", image="my/aider:1", model="gpt-4o-mini", default_timeout_s=300
    )
    assert saved.provider == "aider"
    assert saved.source == "user"
    assert saved.updated_at is not None

    # Новый экземпляр сервиса над тем же файлом видит сохранённое.
    again = _service(tmp_path).get_connection()
    assert again.provider == "aider"
    assert again.image == "my/aider:1"
    assert again.model == "gpt-4o-mini"
    assert again.default_timeout_s == 300
    assert again.source == "user"


def test_set_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _service(tmp_path).set_connection(provider="nope")


def test_resolve_runtime_connection_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    # Ничего не сохранено, env пуст → stub.
    monkeypatch.delenv("POV_HARNESS_PROVIDER", raising=False)
    assert service.resolve_runtime_connection().provider == "stub"

    # env задан → env-bootstrap.
    monkeypatch.setenv("POV_HARNESS_PROVIDER", "claude_code")
    assert service.resolve_runtime_connection().provider == "claude_code"

    # Сохранённое перекрывает env.
    service.set_connection(provider="aider", image="x")
    assert service.resolve_runtime_connection().provider == "aider"


def test_registry_loader_reflects_live_changes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    registry = HarnessProviderRegistry(
        connection_loader=service.resolve_runtime_connection
    )
    assert registry.default_provider_name() == "stub"
    assert registry.resolve_default().name == "stub"

    # Меняем настройку — реестр без пересоздания видит новый выбор.
    service.set_connection(provider="aider", image="x")
    assert registry.default_provider_name() == "aider"


def test_api_harness_connection_get_and_put(tmp_path: Path) -> None:
    app = create_app(repo_root=REPO_ROOT, runtime_root=tmp_path / "runtime")
    client = TestClient(app)

    got = client.get("/api/harness/connection")
    assert got.status_code == 200
    assert got.json()["provider"] == "stub"

    put = client.put(
        "/api/harness/connection",
        json={"provider": "claude_code", "image": "povgen/claude:1", "model": "claude-opus-4-8"},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["provider"] == "claude_code"
    assert body["source"] == "user"

    # Применилось: активный исполнитель в /adapters и /runtime обновился.
    assert client.get("/api/harness/adapters").json()["active"] == "claude_code"
    assert client.get("/api/harness/runtime").json()["provider_name"] == "claude_code"

    # Невалидный тип → 409 (ValidationError → PovGeneratorError handler).
    bad = client.put("/api/harness/connection", json={"provider": "nope"})
    assert bad.status_code == 409
