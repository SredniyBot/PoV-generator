"""Compositional structured output — сборка сложных структур по частям.

Сложную вложенную strict-схему ненадёжно получать одним проходом (модель не
укладывается → внутренние retry CLI, штормы, «съезд» на сырой ответ). Здесь
схема декомпозируется на простые фрагменты, каждый генерируется отдельным
надёжным запросом и детерминированно склеивается — контракт сохраняется.

Публичный вход — :class:`CompositionalLLMProvider` (декоратор ``LLMProvider``):
прозрачен для вызывающих, простые схемы пропускает одним проходом, сложные
собирает по частям (проактивно по сложности / реактивно при несоответствии).

Слой инфраструктурный и самодостаточный: зависит только от ``infrastructure/llm``
(protocol, structured_output) — без обращения к application.
"""

from __future__ import annotations

from .complexity import schema_complexity, should_decompose
from .decomposer import DecompositionStrategy, SchemaTreeDecomposer
from .provider import CompositionalLLMProvider

__all__ = [
    "CompositionalLLMProvider",
    "DecompositionStrategy",
    "SchemaTreeDecomposer",
    "schema_complexity",
    "should_decompose",
]
