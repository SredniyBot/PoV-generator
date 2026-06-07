"""Generic command-harness: запуск произвольного агент-CLI в песочнице (Ф2).

Это и escape hatch (интеграция нестандартного агента), и первая проверка связки
песочница↔контракт harness: провайдер сеет brief+входы, исполняет настроенную
команду в `SandboxRuntime`, собирает результат по соглашению
(`/work/.povgen/out/<role>.<fmt>`) и сносит песочницу.

Движок-агностичен: c `StubSandboxRuntime` тестируется в CI без Docker, c
`DockerSandboxRuntime` — реальный прогон. Конкретные адаптеры (Claude Code,
Aider) — частные случаи этого паттерна (Ф7), отличаются лишь командой и образом.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..protocol import (
    HarnessRunResult,
    HarnessRunSpec,
    HarvestedArtifact,
)
from ..sandbox import (
    ResourceLimits,
    SandboxRuntime,
    SandboxSpec,
    shell_argv,
)

_BRIEF_PATH = "/work/.povgen/brief.txt"
_OUT_DIR = "/work/.povgen/out"


class CommandHarnessProvider:
    """Запускает команду агента в песочнице и собирает артефакты по соглашению."""

    def __init__(
        self,
        *,
        sandbox: SandboxRuntime,
        image: str,
        command: str | Sequence[str],
        name: str = "command",
        model: str | None = None,
        resource_limits: ResourceLimits | None = None,
        default_timeout_s: int | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._image = image
        self._command = command
        self.name = name
        self.model = model
        self._resource_limits = resource_limits or ResourceLimits()
        self._default_timeout_s = default_timeout_s

    def run(self, spec: HarnessRunSpec) -> HarnessRunResult:
        handle = self._sandbox.provision(
            SandboxSpec(image=self._image, limits=self._resource_limits, workdir="/work")
        )
        logs: list[str] = []
        try:
            # 1. Посев: brief + входные артефакты как файлы.
            seed: dict[str, bytes] = {_BRIEF_PATH: spec.brief.encode("utf-8")}
            for filename, content in spec.inputs.items():
                seed[f"/work/{filename}"] = content.encode("utf-8")
            self._sandbox.put_files(handle, seed)

            # 2. Исполнение команды агента.
            argv = (
                list(self._command)
                if not isinstance(self._command, str)
                else shell_argv(self._command)
            )
            timeout_s = (
                spec.limits.wall_clock_s
                if spec.limits and spec.limits.wall_clock_s
                else self._default_timeout_s
            )
            result = self._sandbox.exec(
                handle, argv, timeout_s=timeout_s, on_log=logs.append
            )
            transcript = "".join(logs)
            if result.timed_out:
                return HarnessRunResult(
                    status="partial",
                    transcript=transcript,
                    error=f"Прогон прерван по таймауту ({timeout_s} c).",
                )
            if result.exit_code != 0:
                return HarnessRunResult(
                    status="failed",
                    transcript=transcript,
                    error=f"Команда агента вернула код {result.exit_code}.",
                )

            # 3. Сбор результата по соглашению.
            harvested: list[HarvestedArtifact] = []
            for expected in spec.expected_artifacts:
                file_path = f"{_OUT_DIR}/{expected.role}.{expected.fmt}"
                files = self._sandbox.get_files(handle, file_path)
                content = files.get(file_path)
                if content is None:
                    return HarnessRunResult(
                        status="failed",
                        transcript=transcript,
                        error=(
                            f"Агент не положил артефакт роли '{expected.role}' "
                            f"в {file_path}."
                        ),
                    )
                harvested.append(self._harvest_one(expected.role, expected.fmt, content))
            return HarnessRunResult(
                status="completed",
                artifacts=tuple(harvested),
                transcript=transcript,
            )
        finally:
            # Контейнер всегда ephemeral — сносим в любом случае.
            self._sandbox.destroy(handle)

    @staticmethod
    def _harvest_one(role: str, fmt: str, content: bytes) -> HarvestedArtifact:
        if fmt == "json":
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload, dict):
                payload = {"value": payload}
            return HarvestedArtifact(role=role, payload=payload, fmt="json")
        # markdown/files и прочее — как файловый бандл (Ф5 разовьёт хранение).
        return HarvestedArtifact(role=role, files={f"{role}.{fmt}": content}, fmt=fmt)
