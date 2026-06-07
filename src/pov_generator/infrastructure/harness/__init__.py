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
    "BudgetExceeded",
    "BudgetTotals",
    "BudgetTracker",
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
    "SandboxRuntime",
    "SandboxSpec",
    "SlotStatus",
    "StubSandboxRuntime",
    "detect_host_capacity",
    "recommend_capacity",
    "run_gates",
]
