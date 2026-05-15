"""LLM-провайдеры — единый интерфейс над OpenRouter / Anthropic / Claude CLI.

Сюда обращаются все application-сервисы, которым нужен LLM-вызов
(execution, domain-pack selection, clarification CE11, complexity selector).
Каждый сервис принимает ``LLMProviderRegistry`` через конструктор и
вызывает ``registry.from_env(...).chat_json(...)`` — switch по
имени провайдера живёт ровно в одном месте.

Добавление нового провайдера = новый класс-адаптер + одна строка
регистрации в ``LLMProviderRegistry._builders``.
"""

from __future__ import annotations

from .protocol import LLMProvider
from .registry import LLMProviderRegistry, LLMResolution

__all__ = ["LLMProvider", "LLMProviderRegistry", "LLMResolution"]
