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


def test_harness_exhaustion_raises_provider_exhausted() -> None:
    """Issue 4: исчерпание лимита провайдера ВО ВРЕМЯ прогона агента →
    ProviderExhaustedError (раннер остановит пайплайн), а не обычный fail узла,
    после которого прогон добивал бы задачи в исчерпанном окне."""
    import pytest

    from pov_generator.common.errors import ProviderExhaustedError

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        if "claude -p" in argv[-1]:
            return ExecResult(1, "API error 429: rate limit exceeded — overloaded", "")
        return ExecResult(0, "", "")

    provider = ClaudeCodeHarnessProvider(sandbox=StubSandboxRuntime(exec_handler=handler), image="x")
    with pytest.raises(ProviderExhaustedError):
        provider.run(
            HarnessRunSpec(brief="b", expected_artifacts=(ExpectedArtifact(role="r", fmt="files"),))
        )


def test_harness_non_exhaustion_failure_stays_failed() -> None:
    """Обычный сбой агента (без маркеров лимита) — НЕ исчерпание: статус failed,
    исключение не бросаем (раннер пометит узел и пойдёт дальше)."""
    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        if "claude -p" in argv[-1]:
            return ExecResult(1, "TypeError: что-то пошло не так", "")
        return ExecResult(0, "", "")

    provider = ClaudeCodeHarnessProvider(sandbox=StubSandboxRuntime(exec_handler=handler), image="x")
    result = provider.run(
        HarnessRunSpec(brief="b", expected_artifacts=(ExpectedArtifact(role="r", fmt="files"),))
    )
    assert result.status == "failed"


def test_gates_run_in_harvest_zone() -> None:
    """Issue 3: гейты компонента проверяют ЗОНУ СБОРА (services/<сервис>/), куда
    агент пишет код, а не корень /work — иначе smoke ``test -f README.md`` ложно
    падает (агент создал services/svc/README.md, а гейт смотрел /work/README.md)."""
    captured: list[str] = []

    def handler(rt: StubSandboxRuntime, handle: SandboxHandle, argv: list[str]) -> ExecResult:
        cmd = argv[-1]
        captured.append(cmd)
        if "claude -p" in cmd:
            rt.put_files(handle, {"/work/services/svc/README.md": b"ok"})
        return ExecResult(0, "", "")

    provider = ClaudeCodeHarnessProvider(sandbox=StubSandboxRuntime(exec_handler=handler), image="x")
    provider.run(
        HarnessRunSpec(
            brief="b",
            expected_artifacts=(ExpectedArtifact(role="r", fmt="files"),),
            gates=(HarnessGate(name="smoke", command="test -f README.md"),),
            harvest_path="/work/services/svc",
        )
    )
    gate_cmd = next(c for c in captured if "test -f README.md" in c)
    assert "cd /work/services/svc &&" in gate_cmd


def test_claude_code_docker_mcp_wiring(monkeypatch) -> None:
    """Подход B: при заданном POV_HARNESS_DOCKER_MCP_CONFIG host-агент получает
    docker-инструменты через MCP (--mcp-config + --strict-mcp-config +
    --allowedTools mcp__*), host-shell (Bash) запрещён — исполнение через docker."""
    monkeypatch.setenv("POV_HARNESS_DOCKER_MCP_CONFIG", "/c/Users/me/.povgen/docker-mcp.json")
    cmd = _claude_code_cmd("full")
    assert '--mcp-config "/c/Users/me/.povgen/docker-mcp.json"' in cmd
    assert "--strict-mcp-config" in cmd
    assert '--allowedTools "mcp__*"' in cmd
    assert "--disallowedTools" in cmd and "Bash" in cmd


def test_claude_code_no_docker_mcp_when_env_unset(monkeypatch) -> None:
    """Без POV_HARNESS_DOCKER_MCP_CONFIG MCP не подключается — поведение как было."""
    monkeypatch.delenv("POV_HARNESS_DOCKER_MCP_CONFIG", raising=False)
    cmd = _claude_code_cmd("full")
    assert "--mcp-config" not in cmd


def test_docker_mcp_not_attached_in_docker_engine(monkeypatch) -> None:
    """В docker-движке (host_security=None) MCP НЕ подключаем (был бы docker-in-docker)."""
    monkeypatch.setenv("POV_HARNESS_DOCKER_MCP_CONFIG", "/x.json")
    cmd = _claude_code_cmd(None)
    assert "--mcp-config" not in cmd


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
