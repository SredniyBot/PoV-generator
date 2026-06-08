"""Ф7e: host-движок harness (исполнение на хосте, переиспользование сессии).

Покрывает: настройки engine/host_security (store-миграция + валидация сервиса),
HostSandboxRuntime (маппинг /work, sh-трансляция, прямой argv, очистка),
резолв в реестре (host только для claude_code) и флаги адаптера claude
(restricted без хостового shell / full со skip-permissions).

Без реального claude/Docker: HostSandboxRuntime получает инъектированный runner,
который эмулирует поведение агента в workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pov_generator.application.harness_settings_service import HarnessSettingsService
from pov_generator.common.errors import ConflictError, ValidationError
from pov_generator.infrastructure.harness.protocol import (
    ExpectedArtifact,
    HarnessRunSpec,
)
from pov_generator.infrastructure.harness.providers.claude_code import (
    ClaudeCodeHarnessProvider,
)
from pov_generator.infrastructure.harness.registry import (
    ADAPTER_CAPABILITIES,
    HarnessConnection,
    HarnessProviderRegistry,
)
from pov_generator.infrastructure.harness.sandbox import (
    HostSandboxRuntime,
    SandboxSpec,
    shell_argv,
)
from pov_generator.infrastructure.harness_settings_store import HarnessSettingsStore

# --- настройки: store-миграция + валидация -----------------------------------


def test_store_round_trips_engine_and_security(tmp_path: Path) -> None:
    store = HarnessSettingsStore(tmp_path)
    saved = HarnessSettingsService(store).set_connection(
        provider="claude_code", engine="host", host_security="full"
    )
    assert saved.engine == "host"
    assert saved.host_security == "full"
    again = store.get_connection()
    assert again is not None
    assert again.engine == "host"
    assert again.host_security == "full"


def test_store_migrates_legacy_table_without_engine_columns(tmp_path: Path) -> None:
    # БД, созданная до Ф7e (таблица без engine/host_security): идемпотентная
    # миграция добавляет колонки, строка читается с дефолтами docker/restricted.
    import sqlite3

    db_path = tmp_path / "settings.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        create table harness_connection (
            id integer primary key check (id = 1),
            provider text not null default 'stub',
            image text, model text, command text,
            default_timeout_s integer,
            source text not null default 'user',
            updated_at text
        );
        insert into harness_connection(id, provider, source)
        values (1, 'aider', 'user');
        """
    )
    legacy.commit()
    legacy.close()

    conn = HarnessSettingsStore(tmp_path).get_connection()
    assert conn is not None
    assert conn.provider == "aider"
    assert conn.engine == "docker"
    assert conn.host_security == "restricted"


def test_service_rejects_host_for_non_claude(tmp_path: Path) -> None:
    service = HarnessSettingsService(HarnessSettingsStore(tmp_path))
    with pytest.raises(ValidationError):
        service.set_connection(provider="aider", engine="host")


def test_service_rejects_unknown_engine_and_security(tmp_path: Path) -> None:
    service = HarnessSettingsService(HarnessSettingsStore(tmp_path))
    with pytest.raises(ValidationError):
        service.set_connection(provider="claude_code", engine="podman")
    with pytest.raises(ValidationError):
        service.set_connection(
            provider="claude_code", engine="host", host_security="yolo"
        )


def test_claude_code_advertises_host_support() -> None:
    assert ADAPTER_CAPABILITIES["claude_code"]["supports_host"] is True
    assert ADAPTER_CAPABILITIES["aider"]["supports_host"] is False


# --- HostSandboxRuntime ------------------------------------------------------


class _RecordingRunner:
    """Инъектируемый раннер: пишет ожидаемый артефакт и фиксирует вызовы."""

    def __init__(self, *, write_output: bool = False) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._write_output = write_output

    def __call__(self, argv, cwd, env, timeout_s):  # noqa: ANN001 — тестовый раннер
        self.calls.append(([str(a) for a in argv], cwd))
        if self._write_output:
            out = Path(cwd) / ".povgen" / "out" / "requirements.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return 0, "ok", False


def test_host_runtime_seeds_and_collects_files(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    rt = HostSandboxRuntime(root=tmp_path, runner=runner)
    handle = rt.provision(SandboxSpec(image="unused"))
    rt.put_files(handle, {"/work/.povgen/brief.txt": b"hello"})

    real = Path(handle.native)
    assert (real / ".povgen" / "brief.txt").read_bytes() == b"hello"

    files = rt.get_files(handle, "/work/.povgen/brief.txt")
    assert files == {"/work/.povgen/brief.txt": b"hello"}

    rt.destroy(handle)
    assert not real.exists()  # эфемерный каталог снесён


def test_host_runtime_rewrites_direct_argv_paths(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    rt = HostSandboxRuntime(root=tmp_path, runner=runner)
    handle = rt.provision(SandboxSpec(image="unused"))
    rt.exec(handle, ["mytool", "/work/sub/file", "plain"])
    argv, cwd = runner.calls[-1]
    real = str(Path(handle.native))
    assert argv[0] == "mytool"
    assert argv[1] == str(Path(real) / "sub" / "file")
    assert argv[2] == "plain"
    assert cwd == real


def test_host_runtime_translates_shell_to_relative(tmp_path: Path, monkeypatch) -> None:
    # POSIX-шелл резолвим детерминированно (не зависим от наличия bash в CI).
    import pov_generator.infrastructure.harness.sandbox as sb

    monkeypatch.setattr(sb, "_resolve_posix_shell", lambda: "sh")
    runner = _RecordingRunner()
    rt = HostSandboxRuntime(root=tmp_path, runner=runner)
    handle = rt.provision(SandboxSpec(image="unused"))
    rt.exec(handle, shell_argv("cd /work && cat /work/.povgen/brief.txt"))
    argv, _cwd = runner.calls[-1]
    assert argv[0] == "sh"
    assert argv[1] == "-lc"
    # cd /work убран (cwd уже = workspace), /work/ переписан на относительный.
    assert argv[2] == "cat .povgen/brief.txt"


def test_host_runtime_shared_volume_survives_destroy(tmp_path: Path) -> None:
    rt = HostSandboxRuntime(root=tmp_path, runner=_RecordingRunner())
    a = rt.provision(SandboxSpec(image="unused", volume="grp-1"))
    b = rt.provision(SandboxSpec(image="unused", volume="grp-1"))
    assert a.native == b.native  # общий том группы — один каталог
    rt.destroy(a)
    assert Path(b.native).exists()  # снос одной песочницы не сносит общий том


# --- реестр: host только для claude_code -------------------------------------


def test_registry_host_builds_claude_with_security(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    sandbox = HostSandboxRuntime(root=tmp_path, runner=runner)
    registry = HarnessProviderRegistry(
        connection=HarnessConnection(
            provider="claude_code", engine="host", host_security="full"
        ),
        sandbox=sandbox,
    )
    provider = registry.resolve_default()
    assert isinstance(provider, ClaudeCodeHarnessProvider)
    assert provider._host_security == "full"


def test_registry_host_rejects_non_claude(tmp_path: Path) -> None:
    registry = HarnessProviderRegistry(
        connection=HarnessConnection(provider="aider", engine="host"),
        sandbox=HostSandboxRuntime(root=tmp_path, runner=_RecordingRunner()),
    )
    with pytest.raises(ConflictError):
        registry.resolve_default()


# --- адаптер claude: флаги restricted/full -----------------------------------


def _command_str(provider: ClaudeCodeHarnessProvider) -> str:
    spec = HarnessRunSpec(brief="b", expected_artifacts=())
    return " ".join(provider._build_command(spec))


def test_claude_restricted_has_no_host_shell(tmp_path: Path) -> None:
    provider = ClaudeCodeHarnessProvider(
        sandbox=HostSandboxRuntime(root=tmp_path, runner=_RecordingRunner()),
        image="unused",
        host_security="restricted",
    )
    cmd = _command_str(provider)
    assert "--disallowedTools" in cmd
    assert "Bash" in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_claude_full_uses_skip_permissions(tmp_path: Path) -> None:
    provider = ClaudeCodeHarnessProvider(
        sandbox=HostSandboxRuntime(root=tmp_path, runner=_RecordingRunner()),
        image="unused",
        host_security="full",
    )
    assert "--dangerously-skip-permissions" in _command_str(provider)


# --- интеграция: прогон через host-песочницу ---------------------------------


def test_host_provider_run_harvests_output(tmp_path: Path, monkeypatch) -> None:
    import pov_generator.infrastructure.harness.sandbox as sb

    monkeypatch.setattr(sb, "_resolve_posix_shell", lambda: "sh")
    runner = _RecordingRunner(write_output=True)
    provider = ClaudeCodeHarnessProvider(
        sandbox=HostSandboxRuntime(root=tmp_path, runner=runner),
        image="unused",
        host_security="restricted",
    )
    result = provider.run(
        HarnessRunSpec(
            brief="собери требования",
            expected_artifacts=(ExpectedArtifact(role="requirements", fmt="json"),),
        )
    )
    assert result.status == "completed"
    assert len(result.artifacts) == 1
    assert result.artifacts[0].payload == {"ok": True}
