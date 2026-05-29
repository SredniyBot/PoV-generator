"""Доменные типы для настроек LLM-провайдеров.

Модель данных:

* :class:`ProviderConnection` — то, что подключил админ. Тип провайдера
  (``openrouter`` / ``anthropic`` / ``claude_cli``) + credentials + extras.
  Один админ может завести несколько connections одного типа (например,
  два OpenRouter-ключа на разные тарифы) — у каждого свой ``connection_id``.

* :class:`ModelRouting` — как достать конкретную модель. Пара
  ``(connection_id, model_name)`` с приоритетом. Одна модель может иметь
  несколько routings (например, ``claude-sonnet-4-5`` через Anthropic API
  И через CLI-подписку); registry выбирает routing с наивысшим priority
  при условии ``enabled=True`` и рабочего connection.

* :class:`ModelAssignment` — какая модель используется для какого сценария
  (``execution.standard``, ``clarification_ce11`` и т.п.). Сервис говорит
  «дай модель для purpose X», resolver находит assignment → routing →
  connection → готовый ``LLMProvider``.

Все три типа immutable (frozen=True) — изменения идут через ProviderSettings-
Service (новый объект → запись в БД).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# --- Типы и vocabulary -------------------------------------------------------


ProviderType = Literal["openrouter", "anthropic", "claude_cli"]
"""Тип провайдера. Расширяется при добавлении нового адаптера в
``infrastructure/llm/providers/``."""


ConnectionSource = Literal["user", "env_bootstrap"]
"""Происхождение connection: добавлен админом вручную или импортирован из
.env при первом запуске (см. ``ensure_default_settings`` в Stage 6).
Используется UI для значка «из .env» и для понимания, что миграция уже
произошла (повторно env не импортируется)."""


TestStatus = Literal["untested", "ok", "error"]
"""Статус последнего test-вызова. ``untested`` — никогда не тестировался."""


# Канонические purpose'ы для ModelAssignment. Эти строки используются как
# первичные ключи в БД и в API. Расширяются осторожно — изменение значения
# = миграция данных.
PURPOSE_EXECUTION_TRIVIAL = "execution.trivial"
PURPOSE_EXECUTION_STANDARD = "execution.standard"
PURPOSE_EXECUTION_COMPLEX = "execution.complex"
PURPOSE_DOMAIN_PACK_SELECTOR = "domain_pack_selector"
PURPOSE_CLARIFICATION_CE11 = "clarification_ce11"
PURPOSE_COMPLEXITY_SELECTOR = "complexity_selector"
# v3.0: отдельный purpose для pre-flight планирования решений перед
# генерацией артефакта. Обычно дешёвая/быстрая модель — задача
# структурного перечисления, не глубокого анализа.
PURPOSE_DECISION_PLANNING = "decision_planning"


ALL_PURPOSES: tuple[str, ...] = (
    PURPOSE_EXECUTION_TRIVIAL,
    PURPOSE_EXECUTION_STANDARD,
    PURPOSE_EXECUTION_COMPLEX,
    PURPOSE_DOMAIN_PACK_SELECTOR,
    PURPOSE_CLARIFICATION_CE11,
    PURPOSE_COMPLEXITY_SELECTOR,
    PURPOSE_DECISION_PLANNING,
)


PURPOSE_LABELS: dict[str, str] = {
    PURPOSE_EXECUTION_TRIVIAL: "Основной workflow — простые задачи",
    PURPOSE_EXECUTION_STANDARD: "Основной workflow — стандартные задачи",
    PURPOSE_EXECUTION_COMPLEX: "Основной workflow — сложные задачи",
    PURPOSE_DOMAIN_PACK_SELECTOR: "Выбор доменных пакетов",
    PURPOSE_CLARIFICATION_CE11: "Подготовка вопросов пользователю (CE11)",
    PURPOSE_COMPLEXITY_SELECTOR: "Pre-selector сложности задачи",
    PURPOSE_DECISION_PLANNING: "Pre-flight планирование решений (v3.0)",
}


# --- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCredentials:
    """Учётные данные провайдера. Поля опциональны — для разных типов
    провайдеров нужны разные секреты (``claude_cli`` не требует api_key).

    Хранятся в БД в зашифрованном виде через :class:`SecretBox`. Этот
    dataclass представляет уже распакованное состояние.
    """

    api_key: str | None = None


@dataclass(frozen=True)
class ProviderConnection:
    """Подключённый источник моделей.

    ``extras`` — provider-specific параметры, не считающиеся секретами:
    base_url для OpenRouter, max_tokens для Anthropic, путь до CLI и т.п.
    Ключи и значения — строки (для простой сериализации).
    """

    connection_id: str
    provider_type: ProviderType
    display_name: str
    credentials: ProviderCredentials
    extras: dict[str, str] = field(default_factory=dict)
    source: ConnectionSource = "user"
    created_at: str = ""
    last_tested_at: str | None = None
    last_test_status: TestStatus = "untested"
    last_test_message: str = ""

    def with_test_result(
        self,
        *,
        status: TestStatus,
        message: str,
        tested_at: str,
    ) -> "ProviderConnection":
        """Вернуть копию с обновлёнными полями последнего test-вызова."""
        return ProviderConnection(
            connection_id=self.connection_id,
            provider_type=self.provider_type,
            display_name=self.display_name,
            credentials=self.credentials,
            extras=dict(self.extras),
            source=self.source,
            created_at=self.created_at,
            last_tested_at=tested_at,
            last_test_status=status,
            last_test_message=message,
        )


@dataclass(frozen=True)
class ModelRouting:
    """Маршрут к конкретной модели через конкретный connection.

    ``priority`` — больше = выше приоритет. Convention:
        100 — primary
         50 — backup
          0 — fallback / experimental

    Если у модели несколько enabled-routings, resolver берёт максимальный
    priority. Тестирование connection / model переводит routing в "ok",
    failure — в "error" (хранится в connection.last_test_status, не в
    routing — routing технический объект «как достать»).
    """

    routing_id: str
    connection_id: str
    model_name: str
    priority: int = 100
    enabled: bool = True


@dataclass(frozen=True)
class ModelAssignment:
    """Назначение модели на сценарий (purpose).

    ``purpose`` — одно из ``ALL_PURPOSES``. ``model_name`` должен быть таким,
    что в системе есть как минимум один ``ModelRouting`` с этим именем
    (resolver вернёт ошибку, если назначенная модель потеряла routing —
    например, после удаления connection).
    """

    purpose: str
    model_name: str
