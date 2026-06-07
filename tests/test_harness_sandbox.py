"""Ф2 harness-песочницы: SandboxRuntime (stub-движок в CI) + command-провайдер.

Docker-путь проверяется только при явном opt-in (POV_HARNESS_DOCKER_TEST=1 и
доступном демоне) — CI кросс-платформенный остаётся зелёным без Docker.
"""

from __future__ import annotations

import json
import os

import pytest

from pov_generator.common.errors import ConflictError
from pov_generator.infrastructure.harness import (
    ExpectedArtifact,
    HarnessRunSpec,
    ResourceLimits,
    SandboxSpec,
    StubSandboxRuntime,
)
from pov_generator.infrastructure.harness.providers.command import CommandHarnessProvider
from pov_generator.infrastructure.harness.sandbox import ExecResult, SandboxHandle

# --- StubSandboxRuntime: жизненный цикл -------------------------------------


def test_stub_sandbox_lifecycle_put_get_destroy() -> None:
    rt = StubSandboxRuntime()
    handle = rt.provision(SandboxSpec(image="x", limits=ResourceLimits(cpus=1.0, memory_mb=512)))
    rt.put_files(handle, {"/work/in.txt": b"hello", "/work/.povgen/out/a.json": b"{}"})

    one = rt.get_files(handle, "/work/in.txt")
    assert one == {"/work/in.txt": b"hello"}
    under = rt.get_files(handle, "/work/.povgen/out")
    assert "/work/.povgen/out/a.json" in under

    # Лимиты доехали до движка (для будущего enforce'а).
    assert rt.spec_for(handle).limits.memory_mb == 512

    rt.destroy(handle)
    with pytest.raises(ConflictError):
        rt.put_files(handle, {"/work/x": b"1"})


def test_stub_sandbox_exec_default_and_handler() -> None:
    rt_default = StubSandboxRuntime()
    h = rt_default.provision(SandboxSpec(image="x"))
    res = rt_default.exec(h, ["echo", "hi"])
    assert res.exit_code == 0
    assert rt_default.exec_calls == [(h.id, ["echo", "hi"])]

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        rt.put_files(handle, {"/work/result.txt": b"done"})
        return ExecResult(exit_code=0, stdout="agent log line", stderr="")

    rt_h = StubSandboxRuntime(exec_handler=handler)
    h2 = rt_h.provision(SandboxSpec(image="x"))
    logs: list[str] = []
    res2 = rt_h.exec(h2, ["agent"], on_log=logs.append)
    assert res2.exit_code == 0
    assert logs == ["agent log line"]
    assert rt_h.get_files(h2, "/work/result.txt") == {"/work/result.txt": b"done"}


# --- CommandHarnessProvider поверх stub-движка ------------------------------


def _spec(role: str = "demo_output") -> HarnessRunSpec:
    return HarnessRunSpec(
        brief="сделай X",
        expected_artifacts=(ExpectedArtifact(role=role, fmt="json"),),
        inputs={"context.md": "входные данные"},
    )


def test_command_provider_harvests_convention_output() -> None:
    captured: dict[str, object] = {}

    def agent(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        # Агент видит brief и входы, кладёт результат по соглашению.
        seeded = rt.get_files(handle, "/work/.povgen/brief.txt")
        captured["brief_present"] = "/work/.povgen/brief.txt" in seeded
        captured["input_present"] = bool(rt.get_files(handle, "/work/context.md"))
        rt.put_files(
            handle,
            {"/work/.povgen/out/demo_output.json": json.dumps({"ok": True}).encode("utf-8")},
        )
        return ExecResult(exit_code=0, stdout="built", stderr="")

    provider = CommandHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=agent),
        image="povgen/demo:latest",
        command="run-agent",
    )
    result = provider.run(_spec())
    assert result.status == "completed"
    assert captured == {"brief_present": True, "input_present": True}
    assert len(result.artifacts) == 1
    assert result.artifacts[0].payload == {"ok": True}
    assert "built" in result.transcript


def test_command_provider_fails_when_no_output() -> None:
    provider = CommandHarnessProvider(
        sandbox=StubSandboxRuntime(),  # дефолтный exec ничего не пишет
        image="x",
        command="noop",
    )
    result = provider.run(_spec())
    assert result.status == "failed"
    assert "demo_output" in (result.error or "")


def test_command_provider_failed_exit_code() -> None:
    def failing(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        return ExecResult(exit_code=2, stdout="", stderr="boom")

    provider = CommandHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=failing), image="x", command="run"
    )
    result = provider.run(_spec())
    assert result.status == "failed"
    assert "код 2" in (result.error or "")


def test_command_provider_timeout_is_partial() -> None:
    def slow(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        return ExecResult(exit_code=124, stdout="", stderr="", timed_out=True)

    provider = CommandHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=slow), image="x", command="run"
    )
    result = provider.run(_spec())
    assert result.status == "partial"


def test_command_provider_destroys_sandbox_on_success() -> None:
    rt = StubSandboxRuntime(
        exec_handler=lambda r, h, a: (
            r.put_files(h, {"/work/.povgen/out/demo_output.json": b"{}"})
            or ExecResult(exit_code=0, stdout="", stderr="")
        )
    )
    provider = CommandHarnessProvider(sandbox=rt, image="x", command="run")
    provider.run(_spec())
    # После прогона песочница снесена (ephemeral): любой доступ к ней падает.
    # Берём последний выданный handle через внутренний счётчик.
    leftover = SandboxHandle(id="stub-1", workdir="/work")
    with pytest.raises(ConflictError):
        rt.get_files(leftover, "/work")


# --- Docker-путь: только по явному opt-in -----------------------------------


@pytest.mark.skipif(
    os.environ.get("POV_HARNESS_DOCKER_TEST") != "1",
    reason="Docker-тест включается POV_HARNESS_DOCKER_TEST=1 (нужен демон + образ).",
)
def test_docker_sandbox_smoke() -> None:  # pragma: no cover - локальный smoke
    docker = pytest.importorskip("docker")
    from pov_generator.infrastructure.harness import DockerSandboxRuntime

    try:
        docker.from_env().ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker-демон недоступен")

    rt = DockerSandboxRuntime()
    handle = rt.provision(SandboxSpec(image="busybox:latest", limits=ResourceLimits(memory_mb=128)))
    try:
        rt.put_files(handle, {"/work/.povgen/out/demo_output.json": b'{"ok": true}'})
        files = rt.get_files(handle, "/work/.povgen/out/demo_output.json")
        assert files["/work/.povgen/out/demo_output.json"] == b'{"ok": true}'
        res = rt.exec(handle, ["echo", "hello"])
        assert res.exit_code == 0
        assert "hello" in res.stdout
    finally:
        rt.destroy(handle)
