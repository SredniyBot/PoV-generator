"""Слой harness-исполнителей — второй бэкенд исполнения узлов задач.

Симметрично ``infrastructure/llm``: контракт (``protocol``), резолвер
(``registry``) и адаптеры (``providers``). Ф1 — без Docker (stub-провайдер).
"""

from .budget import BudgetExceeded, BudgetTotals, BudgetTracker
from .capacity import HarnessCapacity, detect_host_capacity, recommend_capacity
from .gates import run_gates
from .protocol import (
    ExpectedArtifact,
    GateResult,
    HarnessGate,
    HarnessProvider,
    HarnessRunResult,
    HarnessRunSpec,
    HarvestedArtifact,
    RunLimits,
)
from .providers.aider import AiderHarnessProvider
from .providers.base import SandboxHarnessProvider
from .providers.claude_code import ClaudeCodeHarnessProvider
from .providers.command import CommandHarnessProvider
from .registry import HarnessProviderRegistry
from .sandbox import (
    DockerSandboxRuntime,
    ExecResult,
    ResourceLimits,
    SandboxHandle,
    SandboxRuntime,
    SandboxSpec,
    StubSandboxRuntime,
)
from .slots import HarnessSlotPool, SlotStatus

__all__ = [
    "AiderHarnessProvider",
    "BudgetExceeded",
    "BudgetTotals",
    "BudgetTracker",
    "ClaudeCodeHarnessProvider",
    "CommandHarnessProvider",
    "DockerSandboxRuntime",
    "ExecResult",
    "ExpectedArtifact",
    "GateResult",
    "HarnessCapacity",
    "HarnessGate",
    "HarnessProvider",
    "HarnessProviderRegistry",
    "HarnessRunResult",
    "HarnessRunSpec",
    "HarnessSlotPool",
    "HarvestedArtifact",
    "ResourceLimits",
    "RunLimits",
    "SandboxHandle",
    "SandboxHarnessProvider",
    "SandboxRuntime",
    "SandboxSpec",
    "SlotStatus",
    "StubSandboxRuntime",
    "detect_host_capacity",
    "recommend_capacity",
    "run_gates",
]
