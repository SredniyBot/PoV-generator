"""Ф8a — общий том сборочной группы (B-lite) в песочнице.

Несколько песочниц с одним ``volume`` делят рабочий каталог: компонент,
собранный в одной, виден следующей в той же группе. Разные тома и отсутствие
тома — изолированы. Проверяется на ``StubSandboxRuntime`` (без Docker).
"""

from __future__ import annotations

from pov_generator.infrastructure.harness import SandboxSpec, StubSandboxRuntime


def test_same_volume_shares_files() -> None:
    rt = StubSandboxRuntime()
    a = rt.provision(SandboxSpec(image="x", volume="grp-1"))
    rt.put_files(a, {"/work/shared.py": b"x = 1\n"})
    rt.destroy(a)  # контейнер ephemeral, но том группы переживает снос

    # Вторая песочница той же группы видит файл первой.
    b = rt.provision(SandboxSpec(image="x", volume="grp-1"))
    got = rt.get_files(b, "/work/shared.py")
    assert got == {"/work/shared.py": b"x = 1\n"}


def test_different_volumes_are_isolated() -> None:
    rt = StubSandboxRuntime()
    a = rt.provision(SandboxSpec(image="x", volume="grp-1"))
    rt.put_files(a, {"/work/a.py": b"1"})
    b = rt.provision(SandboxSpec(image="x", volume="grp-2"))
    assert rt.get_files(b, "/work/a.py") == {}


def test_no_volume_is_private() -> None:
    rt = StubSandboxRuntime()
    a = rt.provision(SandboxSpec(image="x"))
    rt.put_files(a, {"/work/a.py": b"1"})
    b = rt.provision(SandboxSpec(image="x"))
    assert rt.get_files(b, "/work/a.py") == {}


def test_concurrent_handles_same_volume_see_each_other() -> None:
    rt = StubSandboxRuntime()
    a = rt.provision(SandboxSpec(image="x", volume="grp"))
    b = rt.provision(SandboxSpec(image="x", volume="grp"))
    rt.put_files(a, {"/work/from_a.py": b"a"})
    rt.put_files(b, {"/work/from_b.py": b"b"})
    # Обе песочницы группы видят файлы друг друга в реальном времени.
    assert set(rt.get_files(a, "/work")) == {"/work/from_a.py", "/work/from_b.py"}
    assert set(rt.get_files(b, "/work")) == {"/work/from_a.py", "/work/from_b.py"}


def test_build_group_flows_into_adapter_sandbox() -> None:
    """produce_artifact(build_group=...) → SandboxSpec.volume у адаптера."""
    from pov_generator.application.harness_execution_service import HarnessExecutionService
    from pov_generator.infrastructure.harness import (
        HarnessConnection,
        HarnessProviderRegistry,
    )
    from pov_generator.infrastructure.harness.sandbox import ExecResult

    seen_volumes: list[str | None] = []

    def handler(rt, handle, argv):
        # Песочница ещё жива во время exec — фиксируем её том группы.
        seen_volumes.append(rt.spec_for(handle).volume)
        rt.put_files(handle, {"/work/.povgen/out/component_implementation.files": b"x"})
        return ExecResult(exit_code=0, stdout="", stderr="")

    registry = HarnessProviderRegistry(
        connection=HarnessConnection(provider="command", command="build", image="x"),
        sandbox=StubSandboxRuntime(exec_handler=handler),
    )
    service = HarnessExecutionService(registry)
    service.produce_artifact(
        artifact_role="component_implementation",
        system_prompt="SYS",
        user_prompt="USR",
        output_kind="bundle",
        build_group="grp-42",
    )
    assert "grp-42" in seen_volumes
