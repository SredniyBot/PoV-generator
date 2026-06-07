"""Ф5c — гейты «готово» (Definition-of-Done) для harness-узлов.

После прогона агента объявленные шаблоном гейты (build/test/lint) исполняются в
той же песочнице; провал любого = узел не достиг готовности (артефакт не
принимается, дальше — штатный ретрай). Сборка Docker-образа (kaniko) — частный
случай команды-гейта.

Без Docker: всё на ``StubSandboxRuntime`` через ``CommandHarnessProvider``.
"""

from __future__ import annotations

import json
from pathlib import Path

from pov_generator.application.harness_execution_service import HarnessExecutionService
from pov_generator.application.registry_service import RegistryService
from pov_generator.domain.registry import HarnessGateSpec
from pov_generator.infrastructure.filesystem_registry import FilesystemRegistryLoader
from pov_generator.infrastructure.harness import (
    ExpectedArtifact,
    HarnessGate,
    HarnessProviderRegistry,
    HarnessRunResult,
    HarnessRunSpec,
    HarvestedArtifact,
    SandboxSpec,
    StubSandboxRuntime,
    run_gates,
)
from pov_generator.infrastructure.harness.providers.command import CommandHarnessProvider
from pov_generator.infrastructure.harness.sandbox import ExecResult, SandboxHandle

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_TEMPLATE_REF = "harness.demo_bundle@1.0.0"


# --- run_gates напрямую -----------------------------------------------------


def test_run_gates_reports_pass_and_fail_and_runs_all() -> None:
    seen: list[str] = []

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        seen.append(cmd)
        if cmd == "ok":
            return ExecResult(exit_code=0, stdout="ok-log", stderr="")
        return ExecResult(exit_code=3, stdout="fail-log", stderr="")

    rt = StubSandboxRuntime(exec_handler=handler)
    handle = rt.provision(SandboxSpec(image="x"))
    results = run_gates(
        rt,
        handle,
        (
            HarnessGate(name="lint", command="ok"),
            HarnessGate(name="tests", command="bad"),
            HarnessGate(name="build", command="ok"),
        ),
    )

    assert [(g.name, g.passed, g.exit_code) for g in results] == [
        ("lint", True, 0),
        ("tests", False, 3),
        ("build", True, 0),
    ]
    # Все гейты исполнены, даже после провала среднего (полнота отчёта).
    assert seen == ["ok", "bad", "ok"]
    assert results[1].log == "fail-log"


def test_run_gates_timeout_is_not_passed() -> None:
    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="", timed_out=True)

    rt = StubSandboxRuntime(exec_handler=handler)
    handle = rt.provision(SandboxSpec(image="x"))
    results = run_gates(rt, handle, (HarnessGate(name="slow", command="sleep"),))
    # exit_code=0, но timed_out → НЕ пройден.
    assert results[0].passed is False


# --- CommandHarnessProvider: гейты как часть прогона ------------------------


def _bundle_spec(gates: tuple[HarnessGate, ...]) -> HarnessRunSpec:
    return HarnessRunSpec(
        brief="собери модуль",
        expected_artifacts=(ExpectedArtifact(role="demo", fmt="json"),),
        gates=gates,
    )


def _agent_then_gate(gate_exit: int):
    """exec_handler: команда сборки кладёт выход, любая иная команда = гейт."""

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        if argv[-1] == "build":
            rt.put_files(
                handle,
                {"/work/.povgen/out/demo.json": json.dumps({"ok": True}).encode("utf-8")},
            )
            return ExecResult(exit_code=0, stdout="built", stderr="")
        return ExecResult(exit_code=gate_exit, stdout=f"gate->{gate_exit}", stderr="")

    return handler


def test_command_provider_passes_gates_then_harvests() -> None:
    provider = CommandHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=_agent_then_gate(gate_exit=0)),
        image="x",
        command="build",
    )
    result = provider.run(_bundle_spec((HarnessGate(name="tests", command="pytest"),)))

    assert result.status == "completed"
    assert result.gates and all(g.passed for g in result.gates)
    assert result.gates[0].name == "tests"
    assert result.artifacts[0].payload == {"ok": True}


def test_command_provider_failed_gate_blocks_harvest() -> None:
    provider = CommandHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=_agent_then_gate(gate_exit=1)),
        image="x",
        command="build",
    )
    result = provider.run(_bundle_spec((HarnessGate(name="tests", command="pytest"),)))

    assert result.status == "failed"
    assert "tests" in (result.error or "")
    assert result.gates and result.gates[0].passed is False
    # Провал гейта = артефакт НЕ собран (DoD не достигнут).
    assert result.artifacts == ()


def test_command_provider_without_gates_completes() -> None:
    # Узлы без гейтов работают как раньше (gates по умолчанию пусты).
    provider = CommandHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=_agent_then_gate(gate_exit=99)),
        image="x",
        command="build",
    )
    result = provider.run(_bundle_spec(()))
    assert result.status == "completed"
    assert result.gates == ()
    assert result.artifacts[0].payload == {"ok": True}


# --- HarnessExecutionService прокидывает гейты в spec ------------------------


class _CapturingProvider:
    name = "capturing"
    model = None

    def __init__(self) -> None:
        self.spec: HarnessRunSpec | None = None

    def run(self, spec: HarnessRunSpec) -> HarnessRunResult:
        self.spec = spec
        role = spec.expected_artifacts[0].role
        return HarnessRunResult(
            status="completed",
            artifacts=(HarvestedArtifact(role=role, payload={"ok": True}, fmt="json"),),
            transcript="",
        )


class _CapturingRegistry(HarnessProviderRegistry):
    def __init__(self, provider: _CapturingProvider) -> None:
        self._provider = provider

    def default_provider_name(self) -> str:
        return "capturing"

    def resolve_default(self) -> _CapturingProvider:  # type: ignore[override]
        return self._provider


def test_produce_artifact_converts_and_forwards_gates() -> None:
    provider = _CapturingProvider()
    service = HarnessExecutionService(_CapturingRegistry(provider))

    service.produce_artifact(
        artifact_role="demo",
        system_prompt="SYS",
        user_prompt="USR",
        gates=(HarnessGateSpec(name="tests", command="pytest -q", timeout_s=120),),
    )

    assert provider.spec is not None
    assert len(provider.spec.gates) == 1
    gate = provider.spec.gates[0]
    assert isinstance(gate, HarnessGate)
    assert (gate.name, gate.command, gate.timeout_s) == ("tests", "pytest -q", 120)


# --- Парсинг harness_gates из YAML-шаблона ----------------------------------


def test_template_parses_harness_gates() -> None:
    snapshot, report = RegistryService(
        FilesystemRegistryLoader(REPO_ROOT / "templates")
    ).validate()
    assert report.is_valid

    template = snapshot.resolve_template(BUNDLE_TEMPLATE_REF)
    assert len(template.harness_gates) == 1
    gate = template.harness_gates[0]
    assert isinstance(gate, HarnessGateSpec)
    assert gate.name == "smoke"
    assert gate.command == "test -f src/main.py"
