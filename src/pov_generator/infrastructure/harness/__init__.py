"""Слой harness-исполнителей — второй бэкенд исполнения узлов задач.

Симметрично ``infrastructure/llm``: контракт (``protocol``), резолвер
(``registry``) и адаптеры (``providers``). Ф1 — без Docker (stub-провайдер).
"""

from .protocol import (
    ExpectedArtifact,
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

__all__ = [
    "DockerSandboxRuntime",
    "ExecResult",
    "ExpectedArtifact",
    "HarnessProvider",
    "HarnessProviderRegistry",
    "HarnessRunResult",
    "HarnessRunSpec",
    "HarvestedArtifact",
    "ResourceLimits",
    "RunLimits",
    "SandboxHandle",
    "SandboxRuntime",
    "SandboxSpec",
    "StubSandboxRuntime",
]
