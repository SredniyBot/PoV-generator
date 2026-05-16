"""Тесты для ProviderSettingsService — высокоуровневое API настроек LLM.

Покрывают:
* Create / read / update / delete для connections.
* Auto-seed routings при создании connection.
* Каталог моделей с расшифровкой connection-имени.
* test_connection / test_model — фейковый провайдер через
  monkeypatch ``LLMProviderRegistry._build_from_connection``.
* ensure_default_settings: bootstrap из env.
* reset_assignments_to_recommended: дефолты.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pov_generator.application.provider_settings_service import (
    KNOWN_MODELS_BY_PROVIDER,
    ProviderSettingsService,
    RECOMMENDED_BY_PURPOSE,
)
from pov_generator.common.errors import ValidationError
from pov_generator.domain.llm_settings import (
    PURPOSE_CLARIFICATION_CE11,
    PURPOSE_EXECUTION_STANDARD,
)
from pov_generator.infrastructure.llm_settings_store import SqliteSettingsStore


def _make_service(tmp_path: Path, monkeypatch) -> ProviderSettingsService:
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    store = SqliteSettingsStore(tmp_path)
    return ProviderSettingsService(store)


# --- Connections CRUD --------------------------------------------------------


def test_add_connection_auto_seeds_routings(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    conn = svc.add_connection(
        provider_type="anthropic",
        display_name="My Anthropic",
        api_key="sk-ant-test",
    )
    assert conn.connection_id
    assert conn.display_name == "My Anthropic"
    assert conn.source == "user"
    assert conn.created_at  # timestamp заполнен

    # Auto-seed: для anthropic — несколько known-моделей.
    catalog = svc.list_models()
    model_names = {entry["model_name"] for entry in catalog}
    expected = set(KNOWN_MODELS_BY_PROVIDER["anthropic"])
    assert expected.issubset(model_names)


def test_second_connection_adds_backup_routings(tmp_path: Path, monkeypatch) -> None:
    """Если уже есть routings для модели — второй connection регистрирует их
    как backup (priority=50), не сбрасывая primary."""
    svc = _make_service(tmp_path, monkeypatch)
    svc.add_connection(provider_type="anthropic", display_name="Primary", api_key="k1")
    svc.add_connection(provider_type="claude_cli", display_name="CLI", api_key=None)

    catalog = {entry["model_name"]: entry for entry in svc.list_models()}
    sonnet_routings = catalog["claude-sonnet-4-5"]["routings"]
    # Два routings: первый priority 100 (primary, anthropic), второй 50 (cli backup).
    assert len(sonnet_routings) == 2
    assert sonnet_routings[0]["priority"] == 100
    assert sonnet_routings[1]["priority"] == 50
    assert sonnet_routings[0]["provider_type"] == "anthropic"
    assert sonnet_routings[1]["provider_type"] == "claude_cli"


def test_update_connection_partial(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    conn = svc.add_connection(
        provider_type="openrouter",
        display_name="OR",
        api_key="sk-or-old",
        seed_default_routings=False,
    )
    updated = svc.update_connection(conn.connection_id, display_name="OR Renamed")
    assert updated.display_name == "OR Renamed"
    # api_key не меняли — старый остался.
    assert updated.credentials.api_key == "sk-or-old"

    # Смена api_key сбрасывает результат теста.
    re_keyed = svc.update_connection(conn.connection_id, api_key="sk-or-new")
    assert re_keyed.credentials.api_key == "sk-or-new"
    assert re_keyed.last_test_status == "untested"


def test_update_connection_empty_api_key_clears_it(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    conn = svc.add_connection(
        provider_type="openrouter", display_name="OR", api_key="x", seed_default_routings=False
    )
    updated = svc.update_connection(conn.connection_id, api_key="")
    assert updated.credentials.api_key is None


# --- Assignments -------------------------------------------------------------


def test_set_assignment_validates_purpose(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    with pytest.raises(ValidationError, match="Неизвестный сценарий"):
        svc.set_assignment(purpose="execution.extra_complex", model_name="x")


def test_reset_to_recommended_uses_available_models(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    svc.add_connection(provider_type="anthropic", display_name="Anthropic", api_key="k")

    applied = svc.reset_assignments_to_recommended()
    purposes_applied = {a.purpose for a in applied}
    # Для всех purposes из RECOMMENDED должна найтись хоть одна модель,
    # т.к. anthropic seed'нул claude-haiku и claude-sonnet (упомянуты во всех).
    assert purposes_applied == set(RECOMMENDED_BY_PURPOSE.keys())

    # Конкретно execution.standard → claude-sonnet-4-5 (первая рекомендация).
    standard = next(a for a in applied if a.purpose == PURPOSE_EXECUTION_STANDARD)
    assert standard.model_name == "claude-sonnet-4-5"


def test_reset_to_recommended_skips_unavailable(tmp_path: Path, monkeypatch) -> None:
    """Если ни одна рекомендованная модель не доступна (пустая БД) — purposes
    остаются не назначены, не падаем."""
    svc = _make_service(tmp_path, monkeypatch)
    applied = svc.reset_assignments_to_recommended()
    assert applied == ()


# --- test_connection ---------------------------------------------------------


def test_test_connection_records_success(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    conn = svc.add_connection(
        provider_type="anthropic", display_name="A", api_key="k", seed_default_routings=True
    )

    # Подменяем _build_from_connection на возврат фейкового провайдера,
    # который отвечает {"reply": "OK"}.
    fake_provider = MagicMock()
    fake_provider.chat_json.return_value = {"reply": "OK"}
    monkeypatch.setattr(
        svc._llm, "_build_from_connection", lambda connection, *, model, complexity: fake_provider
    )

    result = svc.test_connection(conn.connection_id)
    assert result.status == "ok"
    assert "Подключение работает" in result.message
    assert result.sample_response == "OK"
    assert result.tested_at

    # Статус сохранился в connection.
    refreshed = svc.get_connection(conn.connection_id)
    assert refreshed.last_test_status == "ok"
    assert refreshed.last_tested_at == result.tested_at


def test_test_connection_records_failure(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    conn = svc.add_connection(
        provider_type="openrouter", display_name="OR", api_key="bad-key", seed_default_routings=True
    )

    # _build_from_connection возвращает провайдер, у которого chat_json кидает.
    fake_provider = MagicMock()
    fake_provider.chat_json.side_effect = RuntimeError("401 Unauthorized")
    monkeypatch.setattr(
        svc._llm, "_build_from_connection", lambda connection, *, model, complexity: fake_provider
    )

    result = svc.test_connection(conn.connection_id)
    assert result.status == "error"
    assert "401 Unauthorized" in result.message

    refreshed = svc.get_connection(conn.connection_id)
    assert refreshed.last_test_status == "error"


def test_test_connection_missing_id_returns_error(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    result = svc.test_connection("non-existent")
    assert result.status == "error"
    assert "не найден" in result.message


def test_test_model_uses_top_priority_routing(tmp_path: Path, monkeypatch) -> None:
    svc = _make_service(tmp_path, monkeypatch)
    primary = svc.add_connection(provider_type="anthropic", display_name="Primary", api_key="k1")
    backup = svc.add_connection(provider_type="claude_cli", display_name="Backup", api_key=None)

    captured: dict = {}

    def fake_build(connection, *, model, complexity):
        captured["connection_id"] = connection.connection_id
        captured["model"] = model
        fake = MagicMock()
        fake.chat_json.return_value = {"reply": "OK"}
        return fake

    monkeypatch.setattr(svc._llm, "_build_from_connection", fake_build)

    result = svc.test_model(model_name="claude-sonnet-4-5")
    assert result.status == "ok"
    # Primary routing (anthropic, priority 100) должен быть выбран первым.
    assert captured["connection_id"] == primary.connection_id
    assert captured["model"] == "claude-sonnet-4-5"


# --- ensure_default_settings -------------------------------------------------


def test_ensure_default_settings_imports_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    monkeypatch.setenv("POV_OPENROUTER_API_KEY", "sk-or-from-env")
    monkeypatch.setenv("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.delenv("POV_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "openrouter")

    store = SqliteSettingsStore(tmp_path)
    svc = ProviderSettingsService(store)

    created = svc.ensure_default_settings()
    assert len(created) == 1
    assert created[0].provider_type == "openrouter"
    assert created[0].source == "env_bootstrap"
    assert created[0].credentials.api_key == "sk-or-from-env"

    # Auto-import выполнился — повторный вызов ничего не создаёт.
    second = svc.ensure_default_settings()
    assert second == ()


def test_ensure_default_settings_imports_anthropic_and_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    monkeypatch.setenv("POV_ANTHROPIC_API_KEY", "sk-ant-key")
    monkeypatch.delenv("POV_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "claude_subscription")

    store = SqliteSettingsStore(tmp_path)
    svc = ProviderSettingsService(store)
    created = svc.ensure_default_settings()
    types = {c.provider_type for c in created}
    assert types == {"anthropic", "claude_cli"}

    # Должны быть assignments после bootstrap'а.
    assignments = svc.list_assignments()
    assert assignments
    # Для CE11 должна быть назначена sonnet (она в KNOWN_MODELS для anthropic + claude_cli).
    ce11 = next(a for a in assignments if a.purpose == PURPOSE_CLARIFICATION_CE11)
    assert ce11.model_name in ("claude-sonnet-4-5", "claude-haiku-4-5")


def test_ensure_default_settings_noop_if_db_not_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    monkeypatch.setenv("POV_OPENROUTER_API_KEY", "sk-or")

    store = SqliteSettingsStore(tmp_path)
    svc = ProviderSettingsService(store)
    svc.add_connection(
        provider_type="openrouter", display_name="manual", api_key="manual-key",
        seed_default_routings=False,
    )

    created = svc.ensure_default_settings()
    assert created == ()
    # Ручной connection не был перетёрт.
    assert len(svc.list_connections()) == 1
    assert svc.list_connections()[0].display_name == "manual"
