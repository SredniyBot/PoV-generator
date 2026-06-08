"""Persistence активного harness-подключения (Ф7d).

System-wide, как и LLM-настройки: один файл ``<runtime_root>/settings.db``
(делит файл с :class:`SqliteSettingsStore`, но своя таблица). Подключение —
синглтон (одна активная строка ``id=1``): на первом этапе один дефолтный
harness-connection (см. дизайн §11).

Секреты НЕ хранятся (правило проекта) — таблица содержит только нечувствительный
выбор: тип/образ/модель/команда/таймаут. Поэтому :class:`SecretBox` здесь не
нужен.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..domain.harness_settings import HarnessConnectionSettings

_DB_FILENAME = "settings.db"


class HarnessSettingsStore:
    """CRUD синглтон-репозиторий harness-подключения (без секретов)."""

    def __init__(self, runtime_root: Path) -> None:
        self._db_path = runtime_root / _DB_FILENAME
        self._schema_ensured = False

    def get_connection(self) -> HarnessConnectionSettings | None:
        """Активное подключение или None, если ещё не задано."""
        with self._connect() as conn:
            row = conn.execute(
                """
                select provider, image, model, command, default_timeout_s,
                       engine, host_security, network, source, updated_at
                from harness_connection where id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return HarnessConnectionSettings(
            provider=row["provider"],
            image=row["image"],
            model=row["model"],
            command=row["command"],
            default_timeout_s=row["default_timeout_s"],
            engine=row["engine"] or "docker",
            host_security=row["host_security"] or "restricted",
            network=row["network"] or "none",
            source=row["source"],
            updated_at=row["updated_at"],
        )

    def set_connection(
        self, settings: HarnessConnectionSettings
    ) -> HarnessConnectionSettings:
        """UPSERT активного подключения (синглтон ``id=1``)."""
        with self._connect() as conn:
            conn.execute(
                """
                insert into harness_connection(
                    id, provider, image, model, command, default_timeout_s,
                    engine, host_security, network, source, updated_at
                ) values (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    provider = excluded.provider,
                    image = excluded.image,
                    model = excluded.model,
                    command = excluded.command,
                    default_timeout_s = excluded.default_timeout_s,
                    engine = excluded.engine,
                    host_security = excluded.host_security,
                    network = excluded.network,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    settings.provider,
                    settings.image,
                    settings.model,
                    settings.command,
                    settings.default_timeout_s,
                    settings.engine,
                    settings.host_security,
                    settings.network,
                    settings.source,
                    settings.updated_at,
                ),
            )
            conn.commit()
        return settings

    @contextmanager
    def _connect(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
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
            create table if not exists harness_connection (
                id integer primary key check (id = 1),
                provider text not null default 'stub',
                image text,
                model text,
                command text,
                default_timeout_s integer,
                engine text not null default 'docker',
                host_security text not null default 'restricted',
                network text not null default 'none',
                source text not null default 'user',
                updated_at text
            );
            """
        )
        # Идемпотентная миграция для БД, созданных до Ф7e (без engine/host_security).
        self._ensure_column(connection, "engine", "text not null default 'docker'")
        self._ensure_column(connection, "network", "text not null default 'none'")
        self._ensure_column(
            connection, "host_security", "text not null default 'restricted'"
        )
        connection.commit()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, name: str, decl: str
    ) -> None:
        cols = {
            row["name"]
            for row in connection.execute("pragma table_info(harness_connection)")
        }
        if name not in cols:
            connection.execute(
                f"alter table harness_connection add column {name} {decl}"
            )
