"""Адаптеры harness-исполнителей.

stub — тест-дубль (фикстуры, без Docker). command — generic command-harness,
исполняет агент-CLI в песочнице (Ф2). Конкретные адаптеры (Claude Code, Aider) —
частные случаи command (Ф7).
"""

from .command import CommandHarnessProvider
from .stub import StubHarnessProvider

__all__ = ["CommandHarnessProvider", "StubHarnessProvider"]
