"""Тесты REST API настроек LLM.

Покрывают /api/settings/* эндпойнты:
* CRUD над providers
* test_connection (с моком провайдера, чтобы не делать реальный сетевой вызов)
* CRUD над routings (add_custom_model + update_routing + delete_routing)
* assignments + reset-to-recommended
* purposes catalog
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    # Чтобы ensure_default_settings не создавал лишних connections в тесте,
    # перебиваем env пустыми значениями (delenv недостаточно: create_app
    # вызывает load_repo_env, который дозаливает реальный .env репозитория).
    for var in (
        "POV_OPENROUTER_API_KEY",
        "POV_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "POV_EXECUTION_PROVIDER",
        "POV_OPENROUTER_MODEL",
        "POV_CLARIFICATION_PROVIDER",
        "POV_DOMAIN_PACK_SELECTION_PROVIDER",
    ):
        monkeypatch.setenv(var, "")

    runtime_root = tmp_path / "runtime"
    app = create_app(repo_root=REPO_ROOT, runtime_root=runtime_root)
    return TestClient(app)


def test_list_purposes_returns_canonical_set(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    response = client.get("/api/settings/purposes")
    assert response.status_code == 200
    data = response.json()
    ids = {item["id"] for item in data}
    assert "execution.standard" in ids
    assert "clarification_ce11" in ids
    assert "complexity_selector" in ids
    # Labels — кириллица.
    for item in data:
        assert item["label"]


def test_create_provider_returns_masked_key(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/settings/providers",
        json={
            "provider_type": "openrouter",
            "display_name": "Test OpenRouter",
            "api_key": "sk-or-test-very-long-secret-123456",
        },
    )
    assert response.status_code == 200
    data = response.json()
    # api_key НЕ выводим plaintext.
    assert "api_key" not in data
    assert data["has_api_key"] is True
    assert data["api_key_preview"].startswith("sk-")
    assert "test-very-long-secret-123456" not in data["api_key_preview"]
    assert data["last_test_status"] == "untested"


def test_list_providers_after_create(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/providers",
        json={
            "provider_type": "anthropic",
            "display_name": "Prod Anthropic",
            "api_key": "sk-ant-prod",
        },
    )
    response = client.get("/api/settings/providers")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["display_name"] == "Prod Anthropic"
    assert data[0]["provider_type"] == "anthropic"


def test_update_provider(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    created = client.post(
        "/api/settings/providers",
        json={"provider_type": "openrouter", "display_name": "Old", "api_key": "k1"},
    ).json()
    response = client.put(
        f"/api/settings/providers/{created['connection_id']}",
        json={"display_name": "New name"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "New name"


def test_delete_provider(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    created = client.post(
        "/api/settings/providers",
        json={"provider_type": "openrouter", "display_name": "X", "api_key": "k"},
    ).json()
    response = client.delete(f"/api/settings/providers/{created['connection_id']}")
    assert response.status_code == 200
    assert client.get("/api/settings/providers").json() == []


def test_list_models_shows_seeded_routings(tmp_path: Path, monkeypatch) -> None:
    """После создания anthropic-connection в каталоге появляются routings
    для всех KNOWN_MODELS этого провайдера."""
    client = _build_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/providers",
        json={"provider_type": "anthropic", "display_name": "A", "api_key": "k"},
    )
    response = client.get("/api/settings/models")
    assert response.status_code == 200
    catalog = response.json()
    model_names = {entry["model_name"] for entry in catalog}
    assert "claude-sonnet-4-5" in model_names
    assert "claude-haiku-4-5" in model_names


def test_add_custom_model(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    created = client.post(
        "/api/settings/providers",
        json={"provider_type": "openrouter", "display_name": "OR", "api_key": "k"},
    ).json()
    response = client.post(
        "/api/settings/models",
        json={
            "connection_id": created["connection_id"],
            "model_name": "experimental/secret-model",
            "priority": 150,
        },
    )
    assert response.status_code == 200
    assert response.json()["model_name"] == "experimental/secret-model"
    assert response.json()["priority"] == 150

    catalog = client.get("/api/settings/models").json()
    custom_entry = next(e for e in catalog if e["model_name"] == "experimental/secret-model")
    assert custom_entry["routings"][0]["priority"] == 150


def test_assignments_workflow(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/providers",
        json={"provider_type": "anthropic", "display_name": "A", "api_key": "k"},
    )

    # Reset to recommended должен назначить модели по дефолту.
    response = client.post("/api/settings/assignments/reset-to-recommended")
    assert response.status_code == 200
    applied = response.json()
    assert applied

    assignments = client.get("/api/settings/assignments").json()
    purposes = {a["purpose"] for a in assignments}
    assert "execution.standard" in purposes

    # Custom override.
    client.put(
        "/api/settings/assignments",
        json={"purpose": "execution.standard", "model_name": "claude-opus-4-6"},
    )
    fresh = {a["purpose"]: a["model_name"] for a in client.get("/api/settings/assignments").json()}
    assert fresh["execution.standard"] == "claude-opus-4-6"


def test_assignment_validates_purpose(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    response = client.put(
        "/api/settings/assignments",
        json={"purpose": "execution.crazy", "model_name": "model"},
    )
    assert response.status_code == 400


def test_test_provider_endpoint(tmp_path: Path, monkeypatch) -> None:
    """Test endpoint должен дёрнуть chat_json — мокаем построитель провайдера."""
    client = _build_client(tmp_path, monkeypatch)
    created = client.post(
        "/api/settings/providers",
        json={"provider_type": "anthropic", "display_name": "A", "api_key": "k"},
    ).json()

    fake = MagicMock()
    fake.chat_json.return_value = {"reply": "OK"}

    # Подменяем _build_from_connection у того же registry-инстанса, который
    # сидит в app.state. Идём через provider_settings_service.
    app = client.app
    svc = app.state.provider_settings_service
    monkeypatch.setattr(svc._llm, "_build_from_connection", lambda c, *, model, complexity: fake)

    response = client.post(f"/api/settings/providers/{created['connection_id']}/test", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["sample_response"] == "OK"

    # Статус сохранился — отображается в list.
    listed = client.get("/api/settings/providers").json()
    assert listed[0]["last_test_status"] == "ok"


def test_test_model_endpoint(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/providers",
        json={"provider_type": "anthropic", "display_name": "A", "api_key": "k"},
    )

    fake = MagicMock()
    fake.chat_json.return_value = {"reply": "OK"}
    svc = client.app.state.provider_settings_service
    monkeypatch.setattr(svc._llm, "_build_from_connection", lambda c, *, model, complexity: fake)

    response = client.post("/api/settings/models/claude-sonnet-4-5/test")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_delete_routing(tmp_path: Path, monkeypatch) -> None:
    client = _build_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/providers",
        json={"provider_type": "anthropic", "display_name": "A", "api_key": "k"},
    ).json()
    catalog = client.get("/api/settings/models").json()
    routing_id = catalog[0]["routings"][0]["routing_id"]

    response = client.delete(f"/api/settings/routings/{routing_id}")
    assert response.status_code == 200
