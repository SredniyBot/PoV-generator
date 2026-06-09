"""Persistence-слой для настроек LLM-провайдеров.

Settings — **system-wide**, не per-workspace. Один файл БД на runtime_root:
``<runtime_root>/settings.db``. Все workspace'ы видят одни и те же
connections / routings / assignments.

Шифрование секретов: api_key хранится в БД через :class:`SecretBox`.
При записи — encrypt, при чтении — decrypt прозрачно для вызывающего.

Round-trip контракт: ``add_connection(conn) → list_connections() →`` вернёт
тот же объект (с тем же ``credentials.api_key``), модулем точно того, что
сохранили.

Транзакции: каждая операция CRUD атомарна (single-row write). Multi-row
операций пока нет.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..common.errors import ConflictError, ValidationError
from ..domain.llm_settings import (
    ALL_PURPOSES,
    MIN_MODEL_CONTEXT_LIMIT,
    ModelAssignment,
    ModelContextLimit,
    ModelRouting,
    ProviderConnection,
    ProviderCredentials,
)
from .secret_box import SecretBox

_DB_FILENAME = "settings.db"


class SqliteSettingsStore:
    """CRUD-репозиторий для llm-настроек.

    Args:
        runtime_root: корневая директория runtime (родитель settings.db).
        secret_box: шифровальщик секретов. Если None — создаётся с тем же
            runtime_root (общий ключ).
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        secret_box: SecretBox | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._db_path = runtime_root / _DB_FILENAME
        self._secret_box = secret_box or SecretBox(runtime_root)
        self._schema_ensured = False

    # --- Connections ---------------------------------------------------------

    def add_connection(self, connection: ProviderConnection) -> ProviderConnection:
        """Сохранить новый connection. Уникальность по ``connection_id``.

        Returns: тот же объект (для удобства цепочек). API не меняется.
        """
        encrypted_api_key = self._secret_box.encrypt(connection.credentials.api_key or "")
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    insert into provider_connections(
                        connection_id, provider_type, display_name,
                        credentials_api_key_encrypted, extras_json,
                        source, created_at,
                        last_tested_at, last_test_status, last_test_message
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection.connection_id,
                        connection.provider_type,
                        connection.display_name,
                        encrypted_api_key,
                        json.dumps(connection.extras, ensure_ascii=False),
                        connection.source,
                        connection.created_at,
                        connection.last_tested_at,
                        connection.last_test_status,
                        connection.last_test_message,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"Connection с id '{connection.connection_id}' уже существует."
                ) from exc
        return connection

    def update_connection(self, connection: ProviderConnection) -> ProviderConnection:
        """Перезаписать существующий connection (по connection_id)."""
        encrypted_api_key = self._secret_box.encrypt(connection.credentials.api_key or "")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update provider_connections set
                    provider_type = ?, display_name = ?,
                    credentials_api_key_encrypted = ?, extras_json = ?,
                    source = ?,
                    last_tested_at = ?, last_test_status = ?, last_test_message = ?
                where connection_id = ?
                """,
                (
                    connection.provider_type,
                    connection.display_name,
                    encrypted_api_key,
                    json.dumps(connection.extras, ensure_ascii=False),
                    connection.source,
                    connection.last_tested_at,
                    connection.last_test_status,
                    connection.last_test_message,
                    connection.connection_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValidationError(
                    f"Connection '{connection.connection_id}' не найден — обновлять нечего."
                )
            conn.commit()
        return connection

    def delete_connection(self, connection_id: str) -> None:
        """Удалить connection. CASCADE на routings: они тоже удаляются."""
        with self._connect() as conn:
            conn.execute(
                "delete from model_routings where connection_id = ?",
                (connection_id,),
            )
            cursor = conn.execute(
                "delete from provider_connections where connection_id = ?",
                (connection_id,),
            )
            if cursor.rowcount == 0:
                raise ValidationError(
                    f"Connection '{connection_id}' не найден — удалять нечего."
                )
            conn.commit()

    def list_connections(self) -> tuple[ProviderConnection, ...]:
        """Все connections, отсортированные по created_at (старые сверху)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                select connection_id, provider_type, display_name,
                       credentials_api_key_encrypted, extras_json,
                       source, created_at,
                       last_tested_at, last_test_status, last_test_message
                from provider_connections
                order by created_at, connection_id
                """
            ).fetchall()
        return tuple(self._connection_from_row(row) for row in rows)

    def get_connection(self, connection_id: str) -> ProviderConnection | None:
        """Один connection или None."""
        with self._connect() as conn:
            row = conn.execute(
                """
                select connection_id, provider_type, display_name,
                       credentials_api_key_encrypted, extras_json,
                       source, created_at,
                       last_tested_at, last_test_status, last_test_message
                from provider_connections where connection_id = ?
                """,
                (connection_id,),
            ).fetchone()
        if row is None:
            return None
        return self._connection_from_row(row)

    # --- Model routings ------------------------------------------------------

    def add_routing(self, routing: ModelRouting) -> ModelRouting:
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    insert into model_routings(
                        routing_id, connection_id, model_name, priority, enabled
                    ) values (?, ?, ?, ?, ?)
                    """,
                    (
                        routing.routing_id,
                        routing.connection_id,
                        routing.model_name,
                        routing.priority,
                        1 if routing.enabled else 0,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"Routing с id '{routing.routing_id}' уже существует "
                    "или ссылается на несуществующий connection."
                ) from exc
        return routing

    def update_routing(self, routing: ModelRouting) -> ModelRouting:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update model_routings set
                    connection_id = ?, model_name = ?, priority = ?, enabled = ?
                where routing_id = ?
                """,
                (
                    routing.connection_id,
                    routing.model_name,
                    routing.priority,
                    1 if routing.enabled else 0,
                    routing.routing_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValidationError(f"Routing '{routing.routing_id}' не найден.")
            conn.commit()
        return routing

    def delete_routing(self, routing_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "delete from model_routings where routing_id = ?",
                (routing_id,),
            )
            if cursor.rowcount == 0:
                raise ValidationError(f"Routing '{routing_id}' не найден.")
            conn.commit()

    def list_routings(self) -> tuple[ModelRouting, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select routing_id, connection_id, model_name, priority, enabled
                from model_routings
                order by model_name, priority desc, routing_id
                """
            ).fetchall()
        return tuple(
            ModelRouting(
                routing_id=row["routing_id"],
                connection_id=row["connection_id"],
                model_name=row["model_name"],
                priority=int(row["priority"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        )

    def list_routings_for_model(self, model_name: str) -> tuple[ModelRouting, ...]:
        """Все enabled routings одной модели, отсортированные по приоритету
        (primary первым)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                select routing_id, connection_id, model_name, priority, enabled
                from model_routings
                where model_name = ? and enabled = 1
                order by priority desc, routing_id
                """,
                (model_name,),
            ).fetchall()
        return tuple(
            ModelRouting(
                routing_id=row["routing_id"],
                connection_id=row["connection_id"],
                model_name=row["model_name"],
                priority=int(row["priority"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        )

    # --- Assignments ---------------------------------------------------------

    def set_assignment(self, assignment: ModelAssignment) -> ModelAssignment:
        """UPSERT: ``purpose`` — первичный ключ."""
        if assignment.purpose not in ALL_PURPOSES:
            raise ValidationError(
                f"Неизвестный purpose '{assignment.purpose}'. "
                f"Допустимые: {', '.join(ALL_PURPOSES)}."
            )
        with self._connect() as conn:
            conn.execute(
                """
                insert into model_assignments(purpose, model_name) values (?, ?)
                on conflict(purpose) do update set model_name = excluded.model_name
                """,
                (assignment.purpose, assignment.model_name),
            )
            conn.commit()
        return assignment

    def delete_assignment(self, purpose: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "delete from model_assignments where purpose = ?", (purpose,)
            )
            conn.commit()

    def list_assignments(self) -> tuple[ModelAssignment, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "select purpose, model_name from model_assignments order by purpose"
            ).fetchall()
        return tuple(ModelAssignment(purpose=row["purpose"], model_name=row["model_name"]) for row in rows)

    def get_assignment(self, purpose: str) -> ModelAssignment | None:
        with self._connect() as conn:
            row = conn.execute(
                "select purpose, model_name from model_assignments where purpose = ?",
                (purpose,),
            ).fetchone()
        if row is None:
            return None
        return ModelAssignment(purpose=row["purpose"], model_name=row["model_name"])

    # --- Лимиты контекста на модель -----------------------------------------

    def set_context_limit(self, model_name: str, context_limit_tokens: int) -> ModelContextLimit:
        """UPSERT лимита контекста для модели. ``model_name`` — первичный ключ."""
        if not model_name.strip():
            raise ValidationError("Пустое имя модели.")
        if context_limit_tokens < MIN_MODEL_CONTEXT_LIMIT:
            raise ValidationError(
                f"Лимит контекста должен быть ≥ {MIN_MODEL_CONTEXT_LIMIT} токенов."
            )
        with self._connect() as conn:
            conn.execute(
                """
                insert into model_context_limits(model_name, context_limit_tokens) values (?, ?)
                on conflict(model_name) do update set context_limit_tokens = excluded.context_limit_tokens
                """,
                (model_name, context_limit_tokens),
            )
            conn.commit()
        return ModelContextLimit(model_name=model_name, context_limit_tokens=context_limit_tokens)

    def delete_context_limit(self, model_name: str) -> None:
        """Сбросить лимит модели к дефолту (удалить запись)."""
        with self._connect() as conn:
            conn.execute(
                "delete from model_context_limits where model_name = ?", (model_name,)
            )
            conn.commit()

    def get_context_limit(self, model_name: str) -> int | None:
        """Сохранённый лимит модели или ``None`` (тогда действует дефолт)."""
        with self._connect() as conn:
            row = conn.execute(
                "select context_limit_tokens from model_context_limits where model_name = ?",
                (model_name,),
            ).fetchone()
        return int(row["context_limit_tokens"]) if row is not None else None

    def list_context_limits(self) -> tuple[ModelContextLimit, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "select model_name, context_limit_tokens from model_context_limits order by model_name"
            ).fetchall()
        return tuple(
            ModelContextLimit(model_name=row["model_name"], context_limit_tokens=int(row["context_limit_tokens"]))
            for row in rows
        )

    # --- Общие настройки приложения (key-value) -----------------------------

    def get_app_setting(self, key: str) -> str | None:
        """Значение общей настройки приложения или ``None`` (тогда дефолт UI)."""
        with self._connect() as conn:
            row = conn.execute(
                "select value from app_settings where key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_app_setting(self, key: str, value: str) -> None:
        """UPSERT общей настройки приложения. ``key`` — первичный ключ."""
        if not key.strip():
            raise ValidationError("Пустой ключ настройки.")
        with self._connect() as conn:
            conn.execute(
                """
                insert into app_settings(key, value) values (?, ?)
                on conflict(key) do update set value = excluded.value
                """,
                (key, value),
            )
            conn.commit()

    # --- Internals -----------------------------------------------------------

    def _connection_from_row(self, row: sqlite3.Row) -> ProviderConnection:
        decrypted_api_key = self._secret_box.decrypt(row["credentials_api_key_encrypted"] or "")
        extras_raw = row["extras_json"] or "{}"
        try:
            extras = json.loads(extras_raw)
        except json.JSONDecodeError:
            extras = {}
        return ProviderConnection(
            connection_id=row["connection_id"],
            provider_type=row["provider_type"],
            display_name=row["display_name"],
            credentials=ProviderCredentials(api_key=decrypted_api_key or None),
            extras={str(k): str(v) for k, v in extras.items()} if isinstance(extras, dict) else {},
            source=row["source"] or "user",
            created_at=row["created_at"] or "",
            last_tested_at=row["last_tested_at"],
            last_test_status=row["last_test_status"] or "untested",
            last_test_message=row["last_test_message"] or "",
        )

    @contextmanager
    def _connect(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        # foreign_keys включён для CASCADE-семантики (хотя сейчас и не
        # используется — пока удаляем routings вручную перед deletion).
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if not self._schema_ensured:
                self._ensure_schema(connection)
                self._schema_ensured = True
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            create table if not exists provider_connections (
                connection_id text primary key,
                provider_type text not null,
                display_name text not null,
                credentials_api_key_encrypted text not null default '',
                extras_json text not null default '{}',
                source text not null default 'user',
                created_at text not null default '',
                last_tested_at text,
                last_test_status text not null default 'untested',
                last_test_message text not null default ''
            );

            create table if not exists model_routings (
                routing_id text primary key,
                connection_id text not null,
                model_name text not null,
                priority integer not null default 100,
                enabled integer not null default 1
            );

            create index if not exists idx_routings_by_model
              on model_routings (model_name, priority desc);

            create index if not exists idx_routings_by_connection
              on model_routings (connection_id);

            create table if not exists model_assignments (
                purpose text primary key,
                model_name text not null
            );

            create table if not exists model_context_limits (
                model_name text primary key,
                context_limit_tokens integer not null
            );

            create table if not exists app_settings (
                key text primary key,
                value text not null
            );
            """
        )
        connection.commit()
