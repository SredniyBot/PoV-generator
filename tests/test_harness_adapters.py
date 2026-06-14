"""Ф7 — реальные адаптеры за HarnessProvider (общая обвязка + специализации).

Aider — git-нативный «редактор»: правит файлы и коммитит, поэтому сбор —
diff-harvest (что изменилось относительно базовой ревизии). Проверяется на
``StubSandboxRuntime`` (эмуляция git+aider через exec_handler) — без Docker.
Реальный Docker-прогон — за явным opt-in (POV_HARNESS_DOCKER_TEST), как и у
песочницы.
"""

from __future__ import annotations

from pov_generator.infrastructure.harness import (
    AiderHarnessProvider,
    ClaudeCodeHarnessProvider,
    ExpectedArtifact,
    HarnessRunSpec,
    StubSandboxRuntime,
)
from pov_generator.infrastructure.harness.protocol import HarnessGate
from pov_generator.infrastructure.harness.sandbox import ExecResult, SandboxHandle


def _aider_handler(*, edits: dict[str, bytes], gate_exit: int = 0):
    """exec_handler: prepare (git) → aider (пишет файлы) → гейты.

    Сбор — общий subtree-harvest (RG-C): провайдер сам читает /work через
    get_files, отдельной diff-команды нет."""

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        if "git init" in cmd:  # инициализация репозитория для работы aider
            return ExecResult(exit_code=0, stdout="", stderr="")
        if "aider" in cmd:  # запуск агента — эмулируем правки в /work
            rt.put_files(handle, {f"/work/{p}": c for p, c in edits.items()})
            return ExecResult(exit_code=0, stdout="aider: applied edits", stderr="")
        # любая иная команда трактуется как гейт
        return ExecResult(exit_code=gate_exit, stdout="gate", stderr="")

    return handler


def _spec(role: str = "component_bundle", gates: tuple[HarnessGate, ...] = ()) -> HarnessRunSpec:
    return HarnessRunSpec(
        brief="добавь модуль X",
        expected_artifacts=(ExpectedArtifact(role=role, fmt="files"),),
        gates=gates,
    )


def test_aider_subtree_harvest_collects_files() -> None:
    handler = _aider_handler(
        edits={"src/app.py": b"print('hi')\n", "README.md": b"# proj\n"},
    )
    provider = AiderHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler),
        image="povgen/aider:latest",
        model="gpt-4o-mini",
    )
    result = provider.run(_spec())

    assert result.status == "completed"
    art = result.artifacts[0]
    assert art.role == "component_bundle"
    assert art.fmt == "files"
    # Subtree-harvest: дерево кода из /work (служебный .povgen/brief.txt отсеян).
    assert set(art.files or {}) == {"src/app.py", "README.md"}
    assert art.files["src/app.py"] == b"print('hi')\n"
    assert "aider: applied edits" in result.transcript


def test_aider_command_passes_brief_and_model() -> None:
    captured: list[str] = []

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        captured.append(cmd)
        if "aider" in cmd:
            rt.put_files(handle, {"/work/a.py": b"x\n"})
            return ExecResult(0, "", "")
        return ExecResult(0, "", "")

    provider = AiderHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler),
        image="x",
        model="claude-sonnet-4-5",
    )
    provider.run(_spec())

    aider_cmd = next(c for c in captured if "aider" in c)
    assert "--yes" in aider_cmd
    assert "--message-file" in aider_cmd
    assert "/work/.povgen/brief.txt" in aider_cmd
    assert "--model" in aider_cmd and "claude-sonnet-4-5" in aider_cmd


def test_aider_no_output_fails() -> None:
    # Пустой workspace (агент ничего не произвёл) → subtree-harvest даёт пусто →
    # узел падает с понятной причиной (а не пустой diff).
    handler = _aider_handler(edits={})
    provider = AiderHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler), image="x"
    )
    result = provider.run(_spec())
    assert result.status == "failed"
    assert "бандл" in (result.error or "")


def test_aider_uses_project_model_not_fabricated_default() -> None:
    # Нет override модели у подключения → берём настроенную модель проекта
    # (model_hint), а НЕ выдуманный gpt-4o-mini.
    captured: list[str] = []

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        captured.append(cmd)
        if "aider" in cmd:
            rt.put_files(handle, {"/work/a.py": b"x\n"})
        return ExecResult(0, "", "")

    provider = AiderHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler), image="x", model=None
    )
    spec = HarnessRunSpec(
        brief="b",
        expected_artifacts=(ExpectedArtifact(role="component_bundle", fmt="files"),),
        model_hint="anthropic/claude-sonnet-4-5",
    )
    provider.run(spec)
    aider_cmd = next(c for c in captured if "aider" in c)
    assert "claude-sonnet-4-5" in aider_cmd
    assert "gpt-4o-mini" not in aider_cmd


def test_aider_failed_gate_blocks_harvest() -> None:
    handler = _aider_handler(edits={"a.py": b"x\n"}, gate_exit=1)
    provider = AiderHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler), image="x"
    )
    result = provider.run(_spec(gates=(HarnessGate(name="tests", command="pytest"),)))
    assert result.status == "failed"
    assert "tests" in (result.error or "")
    assert result.artifacts == ()


# --- Claude Code: сбор по соглашению ----------------------------------------


def test_claude_code_convention_harvest() -> None:
    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        if "claude -p" in cmd:  # запуск агента — пишет по соглашению
            rt.put_files(
                handle, {"/work/.povgen/out/component_bundle.json": b'{"ok": true}'}
            )
            return ExecResult(exit_code=0, stdout="claude: done", stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")

    provider = ClaudeCodeHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler),
        image="povgen/claude-code:latest",
    )
    spec = HarnessRunSpec(
        brief="собери компонент",
        expected_artifacts=(ExpectedArtifact(role="component_bundle", fmt="json"),),
    )
    result = provider.run(spec)

    assert result.status == "completed"
    assert result.artifacts[0].payload == {"ok": True}
    assert "claude: done" in result.transcript


def test_claude_code_command_shape() -> None:
    captured: list[str] = []

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        captured.append(cmd)
        if "claude -p" in cmd:
            rt.put_files(handle, {"/work/.povgen/out/r.json": b"{}"})
            return ExecResult(0, "", "")
        return ExecResult(0, "", "")

    provider = ClaudeCodeHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler),
        image="x",
        model="claude-opus-4-8",
    )
    provider.run(
        HarnessRunSpec(
            brief="b", expected_artifacts=(ExpectedArtifact(role="r", fmt="json"),)
        )
    )

    claude_cmd = next(c for c in captured if "claude -p" in c)
    # Бриф подаётся через STDIN (cat | claude -p), а не аргументом (иначе большой
    # бриф упирается в лимит длины argv → «Argument list too long», код 126).
    assert "cat /work/.povgen/brief.txt | claude -p" in claude_cmd
    assert "$(cat" not in claude_cmd
    assert "--dangerously-skip-permissions" in claude_cmd
    assert "--model claude-opus-4-8" in claude_cmd


def _claude_code_cmd(host_security: str | None) -> str:
    captured: list[str] = []

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        captured.append(cmd)
        if "claude -p" in cmd:
            rt.put_files(handle, {"/work/.povgen/out/r.json": b"{}"})
        return ExecResult(0, "", "")

    provider = ClaudeCodeHarnessProvider(
        sandbox=StubSandboxRuntime(exec_handler=handler),
        image="x",
        host_security=host_security,
    )
    provider.run(
        HarnessRunSpec(brief="b", expected_artifacts=(ExpectedArtifact(role="r", fmt="json"),))
    )
    return next(c for c in captured if "claude -p" in c)


def test_claude_code_host_modes_never_use_dangerously_skip() -> None:
    """Регрессия (код 126): на HOST (без ОС-изоляции) НЕЛЬЗЯ
    ``--dangerously-skip-permissions`` — claude отказывает его исполнять в
    headless и выходит мгновенно. Только docker (host_security=None) безопасно
    использует skip-permissions."""
    # Docker (None) — полный автономный доступ (изоляция контейнером).
    assert "--dangerously-skip-permissions" in _claude_code_cmd(None)
    # Host «restricted» — acceptEdits + запрет shell/сети, без skip-permissions.
    restricted = _claude_code_cmd("restricted")
    assert "--dangerously-skip-permissions" not in restricted
    assert "--permission-mode acceptEdits" in restricted
    assert "--disallowedTools" in restricted
    # Host «full» — рабочий максимум: acceptEdits со всеми инструментами, но
    # ВСЁ РАВНО без skip-permissions (иначе тот самый код 126).
    full = _claude_code_cmd("full")
    assert "--dangerously-skip-permissions" not in full
    assert "--permission-mode acceptEdits" in full
    assert "--disallowedTools" not in full
