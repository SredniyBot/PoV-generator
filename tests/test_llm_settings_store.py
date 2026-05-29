"""Тесты persistence-слоя LLM-настроек.

Покрывают:
* SecretBox: round-trip шифрования + поведение при отсутствии ключа.
* SqliteSettingsStore: CRUD для connections / routings / assignments.
* Что секреты в БД лежат зашифрованными (видим в raw-SQLite, что api_key
  не plaintext).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pov_generator.common.errors import ConflictError, ValidationError
from pov_generator.domain.llm_settings import (
    PURPOSE_EXECUTION_STANDARD,
    ModelAssignment,
    ModelRouting,
    ProviderConnection,
    ProviderCredentials,
)
from pov_generator.infrastructure.llm_settings_store import SqliteSettingsStore
from pov_generator.infrastructure.secret_box import SecretBox

# --- SecretBox ---------------------------------------------------------------


def test_secret_box_roundtrip_with_persistent_keyfile(tmp_path: Path, monkeypatch) -> None:
    """Без env-ключа SecretBox генерирует файл .secret_key и переиспользует его."""
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    box1 = SecretBox(tmp_path)

    ciphertext = box1.encrypt("sk-very-secret")
    assert ciphertext
    assert "sk-very-secret" not in ciphertext  # не plaintext

    # Второй инстанс с той же runtime_root → читает существующий ключ.
    box2 = SecretBox(tmp_path)
    assert box2.decrypt(ciphertext) == "sk-very-secret"

    # Файл ключа создан.
    assert (tmp_path / ".secret_key").exists()


def test_secret_box_uses_env_key_when_provided(tmp_path: Path, monkeypatch) -> None:
    """POV_SECRET_KEY должен иметь приоритет над файлом."""
    from cryptography.fernet import Fernet

    env_key = Fernet.generate_key().decode()
    monkeypatch.setenv("POV_SECRET_KEY", env_key)
    box = SecretBox(tmp_path)

    ciphertext = box.encrypt("env-driven-secret")
    assert box.decrypt(ciphertext) == "env-driven-secret"
    # Файл НЕ должен быть создан — взяли env.
    assert not (tmp_path / ".secret_key").exists()


def test_secret_box_handles_empty_string(tmp_path: Path, monkeypatch) -> None:
    """Пустая строка → пустая строка обратно. Полезно для опциональных полей."""
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    box = SecretBox(tmp_path)
    assert box.encrypt("") == ""
    assert box.decrypt("") == ""


def test_secret_box_invalid_env_key_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("POV_SECRET_KEY", "not-a-valid-fernet-key")
    box = SecretBox(tmp_path)
    with pytest.raises(ConflictError, match="не похожа на корректный Fernet-ключ"):
        box.encrypt("test")


# --- SqliteSettingsStore: connections ----------------------------------------


def _new_store(tmp_path: Path, monkeypatch) -> SqliteSettingsStore:
    monkeypatch.delenv("POV_SECRET_KEY", raising=False)
    return SqliteSettingsStore(tmp_path)


def test_add_and_list_connection_roundtrip(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    conn = ProviderConnection(
        connection_id="conn-1",
        provider_type="openrouter",
        display_name="My OpenRouter",
        credentials=ProviderCredentials(api_key="sk-or-test"),
        extras={"base_url": "https://openrouter.ai/api/v1"},
        source="user",
        created_at="2026-05-15T10:00:00+00:00",
    )
    store.add_connection(conn)

    listed = store.list_connections()
    assert len(listed) == 1
    got = listed[0]
    assert got.connection_id == "conn-1"
    assert got.provider_type == "openrouter"
    assert got.display_name == "My OpenRouter"
    assert got.credentials.api_key == "sk-or-test"  # расшифрован
    assert got.extras == {"base_url": "https://openrouter.ai/api/v1"}
    assert got.source == "user"


def test_api_key_is_encrypted_in_database(tmp_path: Path, monkeypatch) -> None:
    """Сырое содержимое таблицы НЕ должно содержать plaintext api_key."""
    store = _new_store(tmp_path, monkeypatch)
    store.add_connection(
        ProviderConnection(
            connection_id="conn-secret",
            provider_type="anthropic",
            display_name="Anthropic prod",
            credentials=ProviderCredentials(api_key="sk-ant-very-secret-123"),
        )
    )

    raw = sqlite3.connect(tmp_path / "settings.db")
    raw.row_factory = sqlite3.Row
    row = raw.execute(
        "select credentials_api_key_encrypted from provider_connections where connection_id = 'conn-secret'"
    ).fetchone()
    raw.close()

    encrypted = row["credentials_api_key_encrypted"]
    assert encrypted
    assert "sk-ant-very-secret-123" not in encrypted


def test_duplicate_connection_id_raises(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    conn = ProviderConnection(
        connection_id="dup",
        provider_type="openrouter",
        display_name="A",
        credentials=ProviderCredentials(api_key="x"),
    )
    store.add_connection(conn)
    with pytest.raises(ConflictError, match="уже существует"):
        store.add_connection(conn)


def test_update_connection_changes_fields(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    conn = ProviderConnection(
        connection_id="c1",
        provider_type="openrouter",
        display_name="Old name",
        credentials=ProviderCredentials(api_key="old"),
    )
    store.add_connection(conn)

    updated = conn.with_test_result(status="ok", message="alive", tested_at="2026-05-15T10:30:00+00:00")
    # Также меняем display_name.
    updated = ProviderConnection(
        connection_id=updated.connection_id,
        provider_type=updated.provider_type,
        display_name="New name",
        credentials=ProviderCredentials(api_key="new"),
        extras=updated.extras,
        source=updated.source,
        created_at=updated.created_at,
        last_tested_at=updated.last_tested_at,
        last_test_status=updated.last_test_status,
        last_test_message=updated.last_test_message,
    )
    store.update_connection(updated)

    got = store.get_connection("c1")
    assert got is not None
    assert got.display_name == "New name"
    assert got.credentials.api_key == "new"
    assert got.last_test_status == "ok"
    assert got.last_test_message == "alive"


def test_update_missing_connection_raises(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    with pytest.raises(ValidationError, match="не найден"):
        store.update_connection(
            ProviderConnection(
                connection_id="ghost",
                provider_type="openrouter",
                display_name="x",
                credentials=ProviderCredentials(),
            )
        )


def test_delete_connection_also_drops_routings(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    store.add_connection(
        ProviderConnection(
            connection_id="c1",
            provider_type="claude_cli",
            display_name="Local CLI",
            credentials=ProviderCredentials(),
        )
    )
    store.add_routing(ModelRouting(routing_id="r1", connection_id="c1", model_name="claude-sonnet-4-5"))
    store.add_routing(ModelRouting(routing_id="r2", connection_id="c1", model_name="claude-haiku-4-5"))

    assert len(store.list_routings()) == 2
    store.delete_connection("c1")
    assert store.list_routings() == ()
    assert store.get_connection("c1") is None


# --- Routings ----------------------------------------------------------------


def test_routings_filtered_by_model_and_sorted_by_priority(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    store.add_connection(
        ProviderConnection(
            connection_id="c-anthropic",
            provider_type="anthropic",
            display_name="Anthropic",
            credentials=ProviderCredentials(api_key="key"),
        )
    )
    store.add_connection(
        ProviderConnection(
            connection_id="c-cli",
            provider_type="claude_cli",
            display_name="Claude CLI",
            credentials=ProviderCredentials(),
        )
    )
    store.add_routing(
        ModelRouting(routing_id="r-anthropic", connection_id="c-anthropic", model_name="claude-sonnet-4-5", priority=50)
    )
    store.add_routing(
        ModelRouting(routing_id="r-cli", connection_id="c-cli", model_name="claude-sonnet-4-5", priority=100)
    )
    # Disabled routing — не должен попадать.
    store.add_routing(
        ModelRouting(routing_id="r-disabled", connection_id="c-anthropic", model_name="claude-sonnet-4-5",
                     priority=200, enabled=False)
    )

    routings = store.list_routings_for_model("claude-sonnet-4-5")
    assert [r.routing_id for r in routings] == ["r-cli", "r-anthropic"]
    assert routings[0].priority == 100
    assert routings[1].priority == 50


# --- Assignments -------------------------------------------------------------


def test_set_assignment_upsert(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    store.set_assignment(ModelAssignment(purpose=PURPOSE_EXECUTION_STANDARD, model_name="claude-sonnet-4-5"))
    assert store.get_assignment(PURPOSE_EXECUTION_STANDARD).model_name == "claude-sonnet-4-5"

    # Перезапись того же purpose.
    store.set_assignment(ModelAssignment(purpose=PURPOSE_EXECUTION_STANDARD, model_name="claude-opus-4-6"))
    assert store.get_assignment(PURPOSE_EXECUTION_STANDARD).model_name == "claude-opus-4-6"


def test_set_assignment_unknown_purpose_raises(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    with pytest.raises(ValidationError, match="Неизвестный purpose"):
        store.set_assignment(ModelAssignment(purpose="execution.crazy_level", model_name="x"))


def test_list_assignments_sorted(tmp_path: Path, monkeypatch) -> None:
    store = _new_store(tmp_path, monkeypatch)
    store.set_assignment(ModelAssignment(purpose="execution.complex", model_name="opus"))
    store.set_assignment(ModelAssignment(purpose="execution.trivial", model_name="haiku"))
    store.set_assignment(ModelAssignment(purpose="execution.standard", model_name="sonnet"))

    purposes = [a.purpose for a in store.list_assignments()]
    # Сортировка лексикографическая.
    assert purposes == ["execution.complex", "execution.standard", "execution.trivial"]
