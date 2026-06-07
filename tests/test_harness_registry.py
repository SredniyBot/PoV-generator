"""Ф7c — резолв harness-адаптера из подключения/env + матрица возможностей.

Реестр собирает нужный адаптер по :class:`HarnessConnection` (тип/образ/модель)
с инъекцией песочницы. Дефолт (ничего не настроено) — stub, поэтому CI зелёный
без Docker. Реальные адаптеры строятся с переданной песочницей (в тестах —
``StubSandboxRuntime``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pov_generator.common.errors import ConflictError
from pov_generator.infrastructure.harness import (
    AiderHarnessProvider,
    ClaudeCodeHarnessProvider,
    CommandHarnessProvider,
    HarnessConnection,
    HarnessProviderRegistry,
    StubSandboxRuntime,
    connection_from_env,
)
from pov_generator.interfaces.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_registry_is_stub() -> None:
    registry = HarnessProviderRegistry(connection=HarnessConnection())
    assert registry.default_provider_name() == "stub"
    assert registry.resolve_default().name == "stub"
    assert set(registry.supported_providers) == {"stub", "aider", "claude_code", "command"}


def test_registry_builds_aider_with_config() -> None:
    sandbox = StubSandboxRuntime()
    registry = HarnessProviderRegistry(
        connection=HarnessConnection(
            provider="aider", image="my/aider:1", model="gpt-4o-mini"
        ),
        sandbox=sandbox,
    )
    provider = registry.resolve_default()
    assert isinstance(provider, AiderHarnessProvider)
    assert provider.name == "aider"
    assert provider.model == "gpt-4o-mini"


def test_registry_builds_claude_code_and_command() -> None:
    sandbox = StubSandboxRuntime()
    cc = HarnessProviderRegistry(
        connection=HarnessConnection(provider="claude_code"), sandbox=sandbox
    ).resolve_default()
    assert isinstance(cc, ClaudeCodeHarnessProvider)

    cmd = HarnessProviderRegistry(
        connection=HarnessConnection(provider="command", command="run"), sandbox=sandbox
    ).resolve_default()
    assert isinstance(cmd, CommandHarnessProvider)


def test_registry_rejects_unknown_provider() -> None:
    registry = HarnessProviderRegistry(
        connection=HarnessConnection(provider="nope"), sandbox=StubSandboxRuntime()
    )
    with pytest.raises(ConflictError):
        registry.resolve_default()


def test_connection_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POV_HARNESS_PROVIDER", raising=False)
    assert connection_from_env().provider == "stub"

    monkeypatch.setenv("POV_HARNESS_PROVIDER", "aider")
    monkeypatch.setenv("POV_HARNESS_IMAGE", "img:1")
    monkeypatch.setenv("POV_HARNESS_MODEL", "m")
    monkeypatch.setenv("POV_HARNESS_TIMEOUT_S", "120")
    conn = connection_from_env()
    assert (conn.provider, conn.image, conn.model, conn.default_timeout_s) == (
        "aider",
        "img:1",
        "m",
        120,
    )


def test_registry_does_not_build_docker_for_stub() -> None:
    # Stub не требует песочницы: резолв без инъекции и без Docker не падает.
    registry = HarnessProviderRegistry(connection=HarnessConnection(provider="stub"))
    assert registry.resolve_default().name == "stub"


def test_api_harness_adapters_endpoint(tmp_path: Path) -> None:
    app = create_app(repo_root=REPO_ROOT, runtime_root=tmp_path / "runtime")
    client = TestClient(app)
    resp = client.get("/api/harness/adapters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] == "stub"  # CI: без env — дефолт stub
    assert "aider" in body["capabilities"]
    assert body["capabilities"]["aider"]["git_native"] is True
    assert body["capabilities"]["claude_code"]["needs_docker"] is True
