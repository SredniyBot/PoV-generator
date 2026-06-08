"""Эфемерные креды агента из LLM-подключения проекта (Ф7e+).

Агент в песочнице (aider/claude/command) вызывает LLM — значит ему нужны ключ и
адрес провайдера. Единый источник истины: настроенные LLM-подключения проекта
(«Настройки → LLM»). Мы НЕ заводим отдельный секрет для harness и НЕ персистим
ключ: берём его из LLM-подключения в момент прогона, отображаем в env песочницы
только на время exec, и нигде больше (правило проекта «секреты не хранятся»).

Здесь — чистый маппинг ``ProviderConnection`` → переменные окружения + имя модели
для конкретного адаптера. Без I/O, тестируется в изоляции.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.llm_settings import ProviderConnection


@dataclass(frozen=True)
class HarnessCredentials:
    """Эфемерные креды прогона: env для песочницы + (опц.) имя модели."""

    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None


def _prefixed_model(model: str | None, provider: str, adapter: str) -> str | None:
    """litellm-адаптеры (aider) требуют префикс провайдера в имени модели.
    Если имя уже с префиксом ('/') — не трогаем. Не-litellm адаптеры
    (claude_code/CLI) принимают чистое имя."""
    if not model or adapter != "aider" or "/" in model:
        return model
    return f"{provider}/{model}"


def credentials_from_connection(
    connection: ProviderConnection | None,
    *,
    model: str | None,
    adapter: str,
) -> HarnessCredentials:
    """Собрать эфемерные креды агента из LLM-подключения проекта.

    Маппинг по типу провайдера подключения; имя модели — с префиксом провайдера
    для litellm-адаптеров (aider). ``claude_cli`` (локальная сессия) ключа не
    несёт — он применим только в host-режиме, где сессия уже залогинена.
    """
    if connection is None:
        return HarnessCredentials()
    key = (connection.credentials.api_key or "").strip()
    base_url = (connection.extras.get("base_url") or "").strip()
    provider_type = connection.provider_type
    env: dict[str, str] = {}

    if provider_type == "openrouter":
        if key:
            # OpenRouter — OpenAI-совместимый: даём оба имени ключа, чтобы любой
            # агент/SDK нашёл свой.
            env["OPENROUTER_API_KEY"] = key
            env["OPENAI_API_KEY"] = key
        url = base_url or "https://openrouter.ai/api/v1"
        env["OPENAI_API_BASE"] = url
        env["OPENAI_BASE_URL"] = url
        return HarnessCredentials(env=env, model=_prefixed_model(model, "openrouter", adapter))

    if provider_type == "anthropic":
        if key:
            env["ANTHROPIC_API_KEY"] = key
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        return HarnessCredentials(env=env, model=_prefixed_model(model, "anthropic", adapter))

    # claude_cli: ключа нет (локальная сессия). Применимо только в host-режиме —
    # креды берёт сам claude из ~/.claude. В docker инъецировать нечего.
    return HarnessCredentials(env={}, model=model)
