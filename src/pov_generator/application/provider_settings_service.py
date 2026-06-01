"""Application-сервис управления LLM-настройками.

Высокоуровневое API над :class:`SqliteSettingsStore`:

* CRUD над connections / routings / assignments с автозаполнением полей
  (UUID, created_at).
* ``add_connection`` после успешного создания **автоматически создаёт
  default routings** для известных моделей этого провайдера. Админ может
  поправить routings руками в UI.
* ``test_connection`` / ``test_model`` — реальные mini-вызовы через
  :class:`LLMProviderRegistry` для проверки работоспособности.
* Каталог известных моделей (`KNOWN_MODELS_BY_PROVIDER`) — статический
  seed; кастомные модели админ может добавить руками через
  :meth:`add_custom_model`.
* :meth:`ensure_default_settings` — bootstrap: при пустой БД создаёт
  connections из env-переменных, чтобы CLI / тесты, рассчитывающие на
  старую `.env`-конфигурацию, продолжали работать.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..common.errors import ValidationError
from ..common.serialization import utc_now_iso
from ..domain.llm_settings import (
    ALL_PURPOSES,
    PURPOSE_CLARIFICATION_CE11,
    PURPOSE_COMPLEXITY_SELECTOR,
    PURPOSE_DECISION_PLANNING,
    PURPOSE_DOMAIN_PACK_SELECTOR,
    PURPOSE_EXECUTION_COMPLEX,
    PURPOSE_EXECUTION_STANDARD,
    PURPOSE_EXECUTION_TRIVIAL,
    ModelAssignment,
    ModelRouting,
    ProviderConnection,
    ProviderCredentials,
    ProviderType,
)
from ..infrastructure.llm import LLMProviderRegistry
from ..infrastructure.llm_settings_store import SqliteSettingsStore

# Каталог известных моделей. Используется для автозаполнения routings при
# создании connection. Кастомные модели админ добавляет через
# add_custom_model.
KNOWN_MODELS_BY_PROVIDER: dict[ProviderType, tuple[str, ...]] = {
    "openrouter": (
        "openai/gpt-4.1-mini",
        "openai/gpt-4o-mini",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-chat",
        "anthropic/claude-opus-4-7",
        "anthropic/claude-sonnet-4-5",
        "anthropic/claude-haiku-4-5",
    ),
    "anthropic": (
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
        "claude-opus-4-7",  # текущий флагман
        "claude-opus-4-6",  # legacy, для совместимости с прошлыми проектами
    ),
    "claude_cli": (
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
        "claude-opus-4-7",
        "claude-opus-4-6",
    ),
}


# Рекомендованные модели по сложности (для кнопки "Сбросить к рекомендуемым"
# в Settings → Default Models). Сначала пробуем claude (если есть), потом
# openrouter-эквивалент.
RECOMMENDED_BY_PURPOSE: dict[str, tuple[str, ...]] = {
    PURPOSE_EXECUTION_TRIVIAL: ("claude-haiku-4-5", "openai/gpt-4o-mini"),
    PURPOSE_EXECUTION_STANDARD: ("claude-sonnet-4-5", "deepseek/deepseek-v4-flash"),
    # Opus 4.7 — текущий флагман на сложных задачах синтеза.
    PURPOSE_EXECUTION_COMPLEX: ("claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-5", "openai/gpt-4.1-mini"),
    PURPOSE_DOMAIN_PACK_SELECTOR: ("claude-sonnet-4-5", "claude-haiku-4-5"),
    PURPOSE_CLARIFICATION_CE11: ("claude-sonnet-4-5", "claude-haiku-4-5"),
    PURPOSE_COMPLEXITY_SELECTOR: ("claude-haiku-4-5", "openai/gpt-4o-mini"),
    # v3.0: выявление решений до сборки. Структурная задача
    # (перечисление выборов), не глубокий анализ — поэтому быстрая/дешёвая
    # модель. Sonnet как fallback, если haiku нет.
    PURPOSE_DECISION_PLANNING: ("claude-haiku-4-5", "claude-sonnet-4-5", "openai/gpt-4o-mini"),
}


@dataclass(frozen=True)
class TestResult:
    """Результат test_connection / test_model."""

    status: str  # "ok" | "error"
    message: str
    latency_ms: int
    sample_response: str | None = None
    tested_at: str = ""


class ProviderSettingsService:
    def __init__(
        self,
        store: SqliteSettingsStore,
        *,
        llm_registry: LLMProviderRegistry | None = None,
    ) -> None:
        self._store = store
        # Registry для test-вызовов; для основного резолва сервисы получают
        # свой registry, связанный с тем же store.
        self._llm = llm_registry or LLMProviderRegistry(settings_store=store)

    # --- Connections ---------------------------------------------------------

    def list_connections(self) -> tuple[ProviderConnection, ...]:
        return self._store.list_connections()

    def get_connection(self, connection_id: str) -> ProviderConnection:
        conn = self._store.get_connection(connection_id)
        if conn is None:
            raise ValidationError(f"Connection '{connection_id}' не найден.")
        return conn

    def add_connection(
        self,
        *,
        provider_type: ProviderType,
        display_name: str,
        api_key: str | None = None,
        extras: dict[str, str] | None = None,
        source: str = "user",
        seed_default_routings: bool = True,
    ) -> ProviderConnection:
        """Создать connection.

        Если ``seed_default_routings=True`` — после сохранения создаёт
        routings для всех known-моделей этого провайдера с приоритетом 100
        (или 50, если у модели уже есть routing — чтобы существующий
        primary остался primary).
        """
        connection = ProviderConnection(
            connection_id=str(uuid.uuid4()),
            provider_type=provider_type,
            display_name=display_name.strip() or _default_display_name(provider_type),
            credentials=ProviderCredentials(api_key=(api_key or "").strip() or None),
            extras={str(k): str(v) for k, v in (extras or {}).items()},
            source=source,
            created_at=utc_now_iso(),
        )
        self._store.add_connection(connection)
        if seed_default_routings:
            self._seed_default_routings(connection)
        return connection

    def update_connection(
        self,
        connection_id: str,
        *,
        display_name: str | None = None,
        api_key: str | None = None,
        extras: dict[str, str] | None = None,
    ) -> ProviderConnection:
        """Частично обновить connection. Поля, переданные ``None``, не меняются.

        ``api_key=""`` (пустая строка) — явный сброс ключа (для отзыва).
        ``api_key=None`` — не менять.
        """
        existing = self.get_connection(connection_id)
        new_display_name = display_name.strip() if display_name is not None else existing.display_name
        if api_key is None:
            new_api_key = existing.credentials.api_key
        elif api_key.strip() == "":
            new_api_key = None
        else:
            new_api_key = api_key.strip()
        new_extras = (
            {str(k): str(v) for k, v in extras.items()} if extras is not None else dict(existing.extras)
        )
        updated = ProviderConnection(
            connection_id=existing.connection_id,
            provider_type=existing.provider_type,
            display_name=new_display_name,
            credentials=ProviderCredentials(api_key=new_api_key),
            extras=new_extras,
            source=existing.source,
            created_at=existing.created_at,
            # При смене credentials сбрасываем результат теста — нужно
            # пере-протестировать.
            last_tested_at=existing.last_tested_at if api_key is None else None,
            last_test_status=existing.last_test_status if api_key is None else "untested",
            last_test_message=existing.last_test_message if api_key is None else "",
        )
        self._store.update_connection(updated)
        return updated

    def delete_connection(self, connection_id: str) -> None:
        self._store.delete_connection(connection_id)

    # --- Models / routings ---------------------------------------------------

    def list_models(self) -> tuple[dict[str, object], ...]:
        """Каталог моделей: имя → список routings (по приоритету) с расшифровкой
        connection-имени для UI.

        Возвращает tuple of dicts, чтобы api-слой мог сериализовать без
        дополнительной модели.
        """
        connections_by_id = {c.connection_id: c for c in self.list_connections()}
        routings = self._store.list_routings()
        by_model: dict[str, list[dict[str, object]]] = {}
        for routing in routings:
            connection = connections_by_id.get(routing.connection_id)
            if connection is None:
                continue
            by_model.setdefault(routing.model_name, []).append(
                {
                    "routing_id": routing.routing_id,
                    "connection_id": routing.connection_id,
                    "connection_display_name": connection.display_name,
                    "provider_type": connection.provider_type,
                    "priority": routing.priority,
                    "enabled": routing.enabled,
                }
            )
        result = []
        for model_name in sorted(by_model.keys()):
            sorted_routings = sorted(
                by_model[model_name],
                key=lambda r: (-int(r["priority"]), str(r["routing_id"])),
            )
            result.append({"model_name": model_name, "routings": sorted_routings})
        return tuple(result)

    def add_custom_model(
        self,
        *,
        connection_id: str,
        model_name: str,
        priority: int = 100,
    ) -> ModelRouting:
        """Добавить routing для модели, которой нет в KNOWN_MODELS_BY_PROVIDER.

        Сервис не проверяет, существует ли модель у провайдера — это
        обязанность ``test_model``. UI после добавления должен предложить
        протестировать.
        """
        existing = self._store.get_connection(connection_id)
        if existing is None:
            raise ValidationError(f"Connection '{connection_id}' не найден.")
        routing = ModelRouting(
            routing_id=str(uuid.uuid4()),
            connection_id=connection_id,
            model_name=model_name.strip(),
            priority=priority,
            enabled=True,
        )
        if not routing.model_name:
            raise ValidationError("Имя модели не может быть пустым.")
        self._store.add_routing(routing)
        return routing

    def update_routing(
        self,
        routing_id: str,
        *,
        priority: int | None = None,
        enabled: bool | None = None,
    ) -> ModelRouting:
        for routing in self._store.list_routings():
            if routing.routing_id == routing_id:
                updated = ModelRouting(
                    routing_id=routing.routing_id,
                    connection_id=routing.connection_id,
                    model_name=routing.model_name,
                    priority=priority if priority is not None else routing.priority,
                    enabled=enabled if enabled is not None else routing.enabled,
                )
                self._store.update_routing(updated)
                return updated
        raise ValidationError(f"Routing '{routing_id}' не найден.")

    def delete_routing(self, routing_id: str) -> None:
        self._store.delete_routing(routing_id)

    # --- Assignments ---------------------------------------------------------

    def list_assignments(self) -> tuple[ModelAssignment, ...]:
        return self._store.list_assignments()

    def set_assignment(self, *, purpose: str, model_name: str) -> ModelAssignment:
        if purpose not in ALL_PURPOSES:
            raise ValidationError(
                f"Неизвестный сценарий '{purpose}'. Допустимые: {', '.join(ALL_PURPOSES)}."
            )
        if not model_name.strip():
            raise ValidationError("Имя модели не может быть пустым.")
        return self._store.set_assignment(ModelAssignment(purpose=purpose, model_name=model_name.strip()))

    def reset_assignments_to_recommended(self) -> tuple[ModelAssignment, ...]:
        """Назначить дефолты на основе RECOMMENDED_BY_PURPOSE.

        Для каждого purpose берётся первая модель из рекомендуемых, у
        которой есть хотя бы один routing. Если ни одна модель из рекомендаций
        не доступна — purpose пропускается (UI покажет «не назначено»).
        """
        available_models = {entry["model_name"] for entry in self.list_models()}
        applied: list[ModelAssignment] = []
        for purpose, recommended in RECOMMENDED_BY_PURPOSE.items():
            for model_name in recommended:
                if model_name in available_models:
                    applied.append(self.set_assignment(purpose=purpose, model_name=model_name))
                    break
        return tuple(applied)

    # --- Testing -------------------------------------------------------------

    def test_connection(self, connection_id: str, *, test_model: str | None = None) -> TestResult:
        """Реальный mini-вызов через connection.

        Стратегия:
        1. Берём connection из store.
        2. Если ``test_model`` задан — тестируем именно его. Иначе — первый
           routing с наивысшим приоритетом для этого connection. Иначе —
           первый known-model для типа провайдера.
        3. Делаем минимальный chat_json: system='Reply with: OK', user='ping',
           schema просит ответ в виде {"ok": true}.
        4. Сохраняем результат в connection.last_test_*.

        Не бросает исключений — все ошибки возвращает в TestResult.status='error'.
        """
        import time

        try:
            connection = self.get_connection(connection_id)
        except ValidationError as exc:
            return TestResult(
                status="error",
                message=str(exc),
                latency_ms=0,
                tested_at=utc_now_iso(),
            )

        model_to_test = test_model or self._pick_test_model_for(connection)
        if not model_to_test:
            return self._save_test_result(
                connection,
                TestResult(
                    status="error",
                    message=(
                        f"Не могу протестировать: для типа '{connection.provider_type}' "
                        "нет известных моделей. Добавьте custom-модель в каталог."
                    ),
                    latency_ms=0,
                    tested_at=utc_now_iso(),
                ),
            )

        started = time.perf_counter()
        try:
            llm = self._llm._build_from_connection(  # type: ignore[attr-defined]
                connection,
                model=model_to_test,
                complexity=None,
            )
        except Exception as exc:  # noqa: BLE001
            return self._save_test_result(
                connection,
                TestResult(
                    status="error",
                    message=f"Не удалось собрать провайдер: {exc}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    tested_at=utc_now_iso(),
                ),
            )

        try:
            payload = llm.chat_json(
                system_prompt="You are a connectivity probe. Reply briefly.",
                user_prompt="Reply with exactly: OK",
                schema={
                    "type": "object",
                    "required": ["reply"],
                    "additionalProperties": False,
                    "properties": {"reply": {"type": "string"}},
                },
            ).payload
        except Exception as exc:  # noqa: BLE001
            return self._save_test_result(
                connection,
                TestResult(
                    status="error",
                    message=f"Вызов модели завершился ошибкой: {exc}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    tested_at=utc_now_iso(),
                ),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        sample = (payload.get("reply") or "").strip() if isinstance(payload, dict) else ""
        return self._save_test_result(
            connection,
            TestResult(
                status="ok",
                message=f"Подключение работает; модель: {model_to_test}.",
                latency_ms=latency_ms,
                sample_response=sample or None,
                tested_at=utc_now_iso(),
            ),
        )

    def test_model(self, *, model_name: str) -> TestResult:
        """Протестировать первый рабочий routing для модели."""
        routings = self._store.list_routings_for_model(model_name)
        if not routings:
            return TestResult(
                status="error",
                message=f"У модели '{model_name}' нет enabled routings.",
                latency_ms=0,
                tested_at=utc_now_iso(),
            )
        return self.test_connection(routings[0].connection_id, test_model=model_name)

    # --- Bootstrap from env --------------------------------------------------

    def ensure_default_settings(self) -> tuple[ProviderConnection, ...]:
        """При пустой БД импортирует connections из env-переменных.

        Авто-импорт срабатывает только если БД пуста — повторно не делается.
        Если хочется чистого env-режима для CI, переключатель —
        ``POV_CONFIG_FROM_ENV_ONLY=true`` (TODO в Stage 6).

        Возвращает: список созданных connections (пустой если БД уже не пуста).
        """
        import os

        if self._store.list_connections():
            return ()

        created: list[ProviderConnection] = []
        # OpenRouter — ключ.
        if os.environ.get("POV_OPENROUTER_API_KEY"):
            created.append(
                self.add_connection(
                    provider_type="openrouter",
                    display_name="OpenRouter (из .env)",
                    api_key=os.environ["POV_OPENROUTER_API_KEY"],
                    extras={
                        "base_url": os.environ.get(
                            "POV_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
                        ),
                    },
                    source="env_bootstrap",
                )
            )
        # Anthropic API.
        anthropic_key = os.environ.get("POV_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            created.append(
                self.add_connection(
                    provider_type="anthropic",
                    display_name="Anthropic API (из .env)",
                    api_key=anthropic_key,
                    extras={"max_tokens": os.environ.get("POV_CLAUDE_MAX_TOKENS", "32768")},
                    source="env_bootstrap",
                )
            )
        # Claude CLI (если POV_EXECUTION_PROVIDER указывает на subscription).
        # Поскольку нет однозначного env-маркера «CLI установлен и залогинен»,
        # ориентируемся на явное `POV_EXECUTION_PROVIDER=claude_subscription`.
        if os.environ.get("POV_EXECUTION_PROVIDER") == "claude_subscription":
            created.append(
                self.add_connection(
                    provider_type="claude_cli",
                    display_name="Claude CLI (подписка)",
                    api_key=None,
                    extras={},
                    source="env_bootstrap",
                )
            )

        # Если что-то создалось и нет assignments — раскладываем рекомендуемые.
        if created and not self._store.list_assignments():
            self.reset_assignments_to_recommended()

        return tuple(created)

    def sync_missing_purpose_assignments(self) -> tuple[ModelAssignment, ...]:
        """Достроить assignments для purposes, появившихся после установки.

        Для существующих пользователей (у которых БД уже не пустая, поэтому
        ``ensure_default_settings`` ничего не делает) — этот метод ловит
        случай, когда мы добавили новый purpose (например, decision_planning
        в v3.0), но в их settings.db для него нет назначения.

        Логика:
        - Идём по RECOMMENDED_BY_PURPOSE.
        - Для каждого purpose без assignment пробуем назначить первую
          рекомендуемую модель из доступных. Если ни одна не доступна —
          purpose остаётся без назначения (как и было).

        Существующие assignments НЕ переписываются — пользователь мог
        выбрать другую модель осознанно.

        Возвращает список созданных назначений (пустой, если все уже на месте).
        """
        existing_purposes = {a.purpose for a in self._store.list_assignments()}
        available_models = {entry["model_name"] for entry in self.list_models()}
        applied: list[ModelAssignment] = []
        for purpose, recommended in RECOMMENDED_BY_PURPOSE.items():
            if purpose in existing_purposes:
                continue
            for model_name in recommended:
                if model_name in available_models:
                    applied.append(self.set_assignment(purpose=purpose, model_name=model_name))
                    break
        return tuple(applied)

    # --- Internals -----------------------------------------------------------

    def sync_known_routings(self, connection_id: str) -> tuple[ModelRouting, ...]:
        """Добавить routings для known-моделей провайдера, отсутствующих
        у этого connection.

        Используется когда:
        * KNOWN_MODELS_BY_PROVIDER пополнили в новом релизе (например, opus 4.7),
          а старые connections были созданы до этого.
        * Админ хочет «обновить каталог» одной кнопкой в UI.

        Возвращает: tuple новых routings (пустой, если всё уже в каталоге).
        Существующие routings не трогаются — priority остаётся.
        """
        connection = self.get_connection(connection_id)
        known = set(KNOWN_MODELS_BY_PROVIDER.get(connection.provider_type, ()))
        existing_for_conn = {
            r.model_name
            for r in self._store.list_routings()
            if r.connection_id == connection_id
        }
        missing = sorted(known - existing_for_conn)
        # Для отсутствующих моделей: priority = 100 если у модели нет routings
        # ни у кого, иначе 50 (backup для существующих primary).
        all_routed_models = {r.model_name for r in self._store.list_routings()}
        added: list[ModelRouting] = []
        for model_name in missing:
            priority = 50 if model_name in all_routed_models else 100
            routing = self._store.add_routing(
                ModelRouting(
                    routing_id=str(uuid.uuid4()),
                    connection_id=connection_id,
                    model_name=model_name,
                    priority=priority,
                    enabled=True,
                )
            )
            added.append(routing)
        return tuple(added)

    def diagnose_resolution(self) -> tuple[dict[str, object], ...]:
        """Для каждого purpose посчитать, что реально пойдёт в LLM-вызов.

        Используется UI-панелью «Куда пойдёт» в Settings → Assignments,
        чтобы пользователь видел: «при запуске пайплайна execution.standard
        отправится в claude_subscription / Claude CLI / claude-opus-4-7».
        Без подобного diagnostic'а пользователь не уверен, что его
        настройки применяются — отсюда вопросы вида «а ты точно
        переключаешь модели?».

        Каждая запись — словарь с полями: ``purpose``, ``label``,
        ``model_name`` (из assignment), ``resolved`` — что фактически
        выбрано (или ``None`` если резолв упал) с пояснением ошибки.
        """

        out: list[dict[str, object]] = []
        for purpose in ALL_PURPOSES:
            label = PURPOSE_LABELS_FOR_UI.get(purpose, purpose)
            assignment = self._store.get_assignment(purpose)
            entry: dict[str, object] = {
                "purpose": purpose,
                "label": label,
                "model_name": assignment.model_name if assignment else None,
                "resolved": None,
                "error": None,
            }
            if assignment is None:
                entry["error"] = "Не назначено"
                out.append(entry)
                continue

            # Реплицируем логику resolve_for_purpose без построения провайдера
            # (чтобы не делать сетевых вызовов и не требовать рабочих кредитов).
            routings = self._store.list_routings_for_model(assignment.model_name)
            if not routings:
                entry["error"] = "У выбранной модели нет рабочих маршрутов"
                out.append(entry)
                continue
            primary = routings[0]
            connection = self._store.get_connection(primary.connection_id)
            if connection is None:
                entry["error"] = "Routing ссылается на удалённый connection"
                out.append(entry)
                continue
            entry["resolved"] = {
                "provider_type": connection.provider_type,
                "connection_id": connection.connection_id,
                "connection_display_name": connection.display_name,
                "model_name": assignment.model_name,
                "fallback_routings": [
                    {
                        "connection_display_name": (
                            self._store.get_connection(r.connection_id).display_name
                            if self._store.get_connection(r.connection_id)
                            else "(удалён)"
                        ),
                        "provider_type": (
                            self._store.get_connection(r.connection_id).provider_type
                            if self._store.get_connection(r.connection_id)
                            else "unknown"
                        ),
                    }
                    for r in routings[1:]
                ],
            }
            out.append(entry)
        return tuple(out)

    def sync_all_connections(self) -> dict[str, int]:
        """Прогон sync_known_routings по всем connections. Используется при
        запуске API — чтобы старые connections автоматически подцепляли
        новые модели из KNOWN_MODELS_BY_PROVIDER (например, opus 4.7).

        Возвращает: ``{connection_id: count_added}``.
        """
        summary: dict[str, int] = {}
        for connection in self.list_connections():
            added = self.sync_known_routings(connection.connection_id)
            if added:
                summary[connection.connection_id] = len(added)
        return summary

    def _seed_default_routings(self, connection: ProviderConnection) -> None:
        """Создать routings для всех known-моделей провайдера.

        Если у модели уже есть routings — добавляем backup с priority=50
        (чтобы существующий primary остался primary).
        """
        known = KNOWN_MODELS_BY_PROVIDER.get(connection.provider_type, ())
        existing_routings = {r.model_name for r in self._store.list_routings()}
        for model_name in known:
            priority = 50 if model_name in existing_routings else 100
            self._store.add_routing(
                ModelRouting(
                    routing_id=str(uuid.uuid4()),
                    connection_id=connection.connection_id,
                    model_name=model_name,
                    priority=priority,
                    enabled=True,
                )
            )

    def _pick_test_model_for(self, connection: ProviderConnection) -> str | None:
        """Какую модель использовать для test_connection.

        Приоритет: первый enabled routing у этого connection → первая
        known-модель для типа провайдера → None.
        """
        routings_for_conn: list[ModelRouting] = [
            r
            for r in self._store.list_routings()
            if r.connection_id == connection.connection_id and r.enabled
        ]
        if routings_for_conn:
            routings_for_conn.sort(key=lambda r: -r.priority)
            return routings_for_conn[0].model_name
        known = KNOWN_MODELS_BY_PROVIDER.get(connection.provider_type, ())
        return known[0] if known else None

    def _save_test_result(self, connection: ProviderConnection, result: TestResult) -> TestResult:
        updated = connection.with_test_result(
            status=result.status,  # type: ignore[arg-type]
            message=result.message,
            tested_at=result.tested_at,
        )
        try:
            self._store.update_connection(updated)
        except ValidationError:
            # Connection удалили между шагами — не критично.
            pass
        return result


def _default_display_name(provider_type: str) -> str:
    return {
        "openrouter": "OpenRouter",
        "anthropic": "Anthropic API",
        "claude_cli": "Claude CLI",
    }.get(provider_type, provider_type)


__all__ = [
    "KNOWN_MODELS_BY_PROVIDER",
    "PURPOSE_LABELS_FOR_UI",
    "ProviderSettingsService",
    "RECOMMENDED_BY_PURPOSE",
    "TestResult",
]


# Доступно для UI: метки purposes, чтобы фронт не дублировал словарь.
from ..domain.llm_settings import PURPOSE_LABELS as PURPOSE_LABELS_FOR_UI  # noqa: E402
